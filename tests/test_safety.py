import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pbs_agent
import pve_controller


class AgentSafetyTests(unittest.TestCase):
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
                  patch.object(pve_controller, "in_backup_window", return_value=False),
                  patch.object(pve_controller, "ping", return_value=True),
                  patch.object(pve_controller, "remote_idle",
                               side_effect=[True, False, True, True, True, True]) as idle,
                  patch.object(pve_controller, "ssh", return_value={"shutdown_requested": True}) as ssh,
                  patch.object(pve_controller, "verify_poweroff", return_value=True)):
                pve_controller.stop(cfg)
            self.assertEqual(idle.call_count, 6)
            ssh.assert_called_once_with(cfg, "shutdown")


if __name__ == "__main__":
    unittest.main()
