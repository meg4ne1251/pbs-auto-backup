import contextlib
import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pbs_agent
import pve_controller


class AgentSafetyTests(unittest.TestCase):
    def test_shutdown_does_not_acknowledge_failed_command(self):
        output = io.StringIO()
        with (patch.object(sys, "argv", ["pbs_agent.py", "--config", "/unused", "shutdown"]),
              patch.object(pbs_agent, "config", return_value={}),
              patch.object(pbs_agent, "readiness"),
              patch.object(pbs_agent, "activity", return_value={"idle": True}),
              patch.object(pbs_agent, "run", side_effect=pbs_agent.StateError("shutdown failed")),
              contextlib.redirect_stdout(output)):
            with self.assertRaises(pbs_agent.StateError):
                pbs_agent.main()
        self.assertEqual(output.getvalue(), "")

    def test_scan_states(self):
        prefix = "  pool: backup-pool\n state: ONLINE\n  scan: "
        self.assertTrue(pbs_agent.scan_busy(prefix + "scrub in progress since Monday", "backup-pool"))
        self.assertTrue(pbs_agent.scan_busy(prefix + "scrub paused since Monday", "backup-pool"))
        self.assertFalse(pbs_agent.scan_busy(prefix + "scrub repaired 0B in 00:01:00", "backup-pool"))
        with self.assertRaises(pbs_agent.StateError):
            pbs_agent.scan_busy(prefix + "new scan format", "backup-pool")
        with self.assertRaises(pbs_agent.StateError):
            pbs_agent.scan_busy("pool: another\nscan: none requested", "backup-pool")

    def test_task_list_invalid_or_truncated_fails_closed(self):
        cfg = {"pool": "backup-pool"}
        with patch.object(pbs_agent, "run", return_value="not-json"):
            with self.assertRaises(pbs_agent.StateError):
                pbs_agent.activity(cfg)
        with patch.object(pbs_agent, "run", return_value=json.dumps([{"upid": "u"}] * 1000)):
            with self.assertRaises(pbs_agent.StateError):
                pbs_agent.activity(cfg)

    def test_running_task_blocks_shutdown(self):
        cfg = {"pool": "backup-pool"}
        status = "pool: backup-pool\nscan: none requested\n"
        with patch.object(pbs_agent, "run", side_effect=[json.dumps([{"upid": "u"}]), status]):
            self.assertFalse(pbs_agent.activity(cfg)["idle"])


class ControllerSafetyTests(unittest.TestCase):
    def test_shutdown_guard_starts_before_wake_time(self):
        self.assertFalse(pve_controller.in_shutdown_guard_window(dt.datetime(2026, 10, 5, 5, 39)))
        self.assertTrue(pve_controller.in_shutdown_guard_window(dt.datetime(2026, 10, 5, 5, 40)))
        self.assertTrue(pve_controller.in_shutdown_guard_window(dt.datetime(2026, 10, 5, 6, 30)))
        self.assertFalse(pve_controller.in_shutdown_guard_window(dt.datetime(2026, 10, 5, 6, 31)))
        self.assertTrue(pve_controller.in_shutdown_guard_window(dt.datetime(2026, 10, 5, 5, 35), 600))

    def test_ssh_separates_remote_rejection_from_transport_failure(self):
        cfg = {"ssh_key": "/key", "known_hosts": "/hosts", "ssh_user": "root", "pbs_host": "pbs"}
        for code, expected in ((1, pve_controller.RemoteCommandRejected),
                               (255, pve_controller.ControlError)):
            with self.subTest(code=code):
                result = subprocess.CompletedProcess(["ssh"], code, "", "failed")
                with patch.object(pve_controller.subprocess, "run", return_value=result):
                    with self.assertRaises(expected) as caught:
                        pve_controller.ssh(cfg, "shutdown")
                    self.assertIs(type(caught.exception), expected)

    def test_unknown_remote_state_fails_closed(self):
        with patch.object(pve_controller, "ssh", return_value={"idle": "true", "running_tasks": 0, "zfs_scan_busy": False}):
            with self.assertRaises(pve_controller.ControlError):
                pve_controller.remote_idle({})
        with patch.object(pve_controller, "ssh", return_value={"idle": True, "running_tasks": 1, "zfs_scan_busy": False}):
            with self.assertRaises(pve_controller.ControlError):
                pve_controller.remote_idle({})

    def test_maintenance_state_persists_until_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_dir": directory}
            pve_controller.set_maintenance(cfg, True)
            self.assertTrue(pve_controller.maintenance_active(cfg))
            self.assertTrue(Path(directory, "maintenance.json").exists())
            pve_controller.set_maintenance(cfg, False)
            self.assertFalse(pve_controller.maintenance_active(cfg))

    def test_busy_sample_resets_idle_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_dir": directory, "idle_interval_seconds": 1,
                   "idle_checks": 3, "normal_monitor_seconds": 10}
            clock = [0]

            def sleep(seconds):
                clock[0] += seconds

            with (patch.object(pve_controller.time, "monotonic", side_effect=lambda: clock[0]),
                  patch.object(pve_controller.time, "sleep", side_effect=sleep),
                  patch.object(pve_controller, "in_shutdown_guard_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle",
                               side_effect=[True, False, True, True, True, True]) as idle,
                  patch.object(pve_controller, "ssh", return_value={"shutdown_requested": True}) as ssh,
                  patch.object(pve_controller, "verify_poweroff", return_value=True)):
                pve_controller.stop(cfg)
            self.assertEqual(idle.call_count, 6)
            ssh.assert_called_once_with(cfg, "shutdown")

    def test_shutdown_verification_holds_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_dir": directory, "idle_checks": 1}

            def verify(_cfg, _deadline):
                with self.assertRaises(pve_controller.ControlError):
                    with pve_controller.lock(Path(directory, "operation.lock"), 0):
                        pass
                return True

            with (patch.object(pve_controller, "in_shutdown_guard_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle", return_value=True),
                  patch.object(pve_controller, "ssh", return_value={"shutdown_requested": True}),
                  patch.object(pve_controller, "verify_poweroff", side_effect=verify) as checked):
                pve_controller.stop(cfg)
            checked.assert_called_once()

    def test_guard_rechecked_after_final_idle_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_dir": directory, "idle_checks": 1,
                   "normal_monitor_seconds": 1, "idle_interval_seconds": 1}
            clock = [0]

            def sleep(seconds):
                clock[0] += seconds

            with (patch.object(pve_controller.time, "monotonic", side_effect=lambda: clock[0]),
                  patch.object(pve_controller.time, "sleep", side_effect=sleep),
                  patch.object(pve_controller, "in_shutdown_guard_window",
                               side_effect=[False, False, True, True]),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle", return_value=True),
                  patch.object(pve_controller, "ssh") as ssh):
                pve_controller.stop(cfg)
            ssh.assert_not_called()

    def test_failed_or_uncertain_shutdown_retains_maintenance_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"state_dir": directory, "idle_checks": 1, "idle_interval_seconds": 1,
                   "maintenance_monitor_seconds": 2}
            pve_controller.set_maintenance(cfg, True)
            clock = [0]

            def sleep(seconds):
                clock[0] += seconds

            with (patch.object(pve_controller.time, "monotonic", side_effect=lambda: clock[0]),
                  patch.object(pve_controller.time, "sleep", side_effect=sleep),
                  patch.object(pve_controller, "in_shutdown_guard_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle", return_value=True),
                  patch.object(pve_controller, "ssh",
                               side_effect=pve_controller.RemoteCommandRejected("PBS is busy")),
                  patch.object(pve_controller, "verify_poweroff") as verify):
                pve_controller.stop(cfg)
            verify.assert_not_called()
            self.assertTrue(pve_controller.maintenance_active(cfg))

            with (patch.object(pve_controller, "in_shutdown_guard_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle", return_value=True),
                  patch.object(pve_controller, "ssh", side_effect=pve_controller.ControlError("SSH disconnected")),
                  patch.object(pve_controller, "verify_poweroff", return_value=True)):
                pve_controller.stop(cfg)
            self.assertTrue(pve_controller.maintenance_active(cfg))

            with (patch.object(pve_controller, "in_shutdown_guard_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle", return_value=True),
                  patch.object(pve_controller, "ssh", return_value={"shutdown_requested": True}),
                  patch.object(pve_controller, "verify_poweroff", return_value=True)):
                pve_controller.stop(cfg)
            self.assertFalse(pve_controller.maintenance_active(cfg))


if __name__ == "__main__":
    unittest.main()
