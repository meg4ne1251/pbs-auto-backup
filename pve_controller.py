#!/usr/bin/env python3
"""PVE-side WOL and fail-closed PBS shutdown monitor."""

import argparse
import contextlib
import datetime as dt
import fcntl
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


LOG = logging.getLogger("pbs-auto-backup")


class ControlError(Exception):
    pass


class RemoteCommandRejected(ControlError):
    """The remote command ran and reported a failure."""


def positive(cfg, key, default):
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ControlError(f"{key} must be a positive integer")
    return value


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg, dict):
        raise ControlError("configuration must be an object")
    for key in ("pbs_host", "ssh_user", "ssh_key", "known_hosts", "mac", "wol_broadcast", "state_dir"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise ControlError(f"missing {key}")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", cfg["pbs_host"]):
        raise ControlError("invalid pbs_host")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", cfg["ssh_user"]):
        raise ControlError("invalid ssh_user")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", cfg["mac"]):
        raise ControlError("invalid mac")
    ipaddress.IPv4Address(cfg["wol_broadcast"])
    for key in ("ssh_key", "known_hosts", "state_dir"):
        if not os.path.isabs(cfg[key]):
            raise ControlError(f"{key} must be absolute")
    for key, default in (("startup_timeout_seconds", 600), ("wol_retry_seconds", 120),
                         ("idle_interval_seconds", 60), ("idle_checks", 3),
                         ("normal_monitor_seconds", 21600), ("maintenance_monitor_seconds", 86400),
                         ("handoff_wait_seconds", 600), ("shutdown_retry_seconds", 300),
                         ("shutdown_attempts", 2), ("ping_interval_seconds", 10)):
        positive(cfg, key, default)
    return cfg


def seconds(cfg, key, default):
    return cfg.get(key, default)


def ping(cfg):
    try:
        return subprocess.run(["ping", "-n", "-c", "1", "-W", "2", cfg["pbs_host"]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ssh(cfg, command):
    argv = ["ssh", "-i", cfg["ssh_key"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={cfg['known_hosts']}",
            "-o", "ConnectTimeout=8", f"{cfg['ssh_user']}@{cfg['pbs_host']}", command]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError(f"SSH {command} failed: {exc}") from exc
    if result.returncode:
        error = f"SSH {command} failed: {result.stderr.strip()[:300]}"
        if result.returncode != 255:
            raise RemoteCommandRejected(error)
        raise ControlError(error)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ControlError(f"SSH {command} returned invalid JSON") from exc


def remote_ready(cfg):
    result = ssh(cfg, "ready")
    if result != {"ready": True}:
        raise ControlError("unexpected readiness result")


def remote_idle(cfg):
    result = ssh(cfg, "idle")
    if (not isinstance(result, dict) or type(result.get("idle")) is not bool
            or type(result.get("running_tasks")) is not int
            or type(result.get("zfs_scan_busy")) is not bool
            or result["running_tasks"] < 0
            or result["idle"] != (result["running_tasks"] == 0 and not result["zfs_scan_busy"])):
        raise ControlError("unexpected activity result")
    return result["idle"]


@contextlib.contextmanager
def lock(path, wait_seconds):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ControlError(f"lock wait exceeded: {path}")
                time.sleep(1)
        yield
    finally:
        os.close(fd)


def state_path(cfg):
    return Path(cfg["state_dir"]) / "maintenance.json"


def maintenance_active(cfg):
    path = state_path(cfg)
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if value != {"active": True}:
        raise ControlError("invalid maintenance state")
    return True


def set_maintenance(cfg, active):
    path = state_path(cfg)
    if active:
        temp = path.with_suffix(".tmp")
        temp.write_text('{"active": true}\n', encoding="utf-8")
        os.replace(temp, path)
    else:
        path.unlink(missing_ok=True)


def wol(cfg):
    mac = bytes.fromhex(cfg["mac"].replace(":", ""))
    packet = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (cfg["wol_broadcast"], 9))
    LOG.info("WOL packet sent")


def start(cfg):
    path = Path(cfg["state_dir"])
    # A stop holds this lock until its shutdown verification has finished.
    with lock(path / "operation.lock", seconds(cfg, "shutdown_retry_seconds", 300) + 120):
        deadline = time.monotonic() + seconds(cfg, "startup_timeout_seconds", 600)
        next_wol = 0
        while time.monotonic() < deadline:
            if ping(cfg):
                try:
                    remote_ready(cfg)
                    LOG.info("PBS ready")
                    return
                except ControlError as exc:
                    LOG.warning("PBS not ready: %s", exc)
            elif time.monotonic() >= next_wol:
                wol(cfg)
                next_wol = time.monotonic() + seconds(cfg, "wol_retry_seconds", 120)
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        raise ControlError("startup timeout; backup job is not retried")


def in_shutdown_guard_window(now, shutdown_retry_seconds=300):
    current = now.hour * 3600 + now.minute * 60 + now.second
    # Leave time for SSH, the complete shutdown check, and the 05:50 start.
    guard_seconds = shutdown_retry_seconds + 300
    if guard_seconds >= 86400:
        return True
    begin = (5 * 3600 + 50 * 60 - guard_seconds) % 86400
    end = 6 * 3600 + 31 * 60
    if begin < end:
        return begin <= current < end
    return current >= begin or current < end


def verify_poweroff(cfg, deadline):
    misses = 0
    while time.monotonic() < deadline:
        if ping(cfg):
            misses = 0
        else:
            misses += 1
            if misses >= 3:
                LOG.info("PBS unreachable after shutdown request (power state unverified)")
                return True
        time.sleep(min(seconds(cfg, "ping_interval_seconds", 10), max(0, deadline - time.monotonic())))
    return False


def stop(cfg):
    path = Path(cfg["state_dir"])
    with lock(path / "monitor.lock", seconds(cfg, "handoff_wait_seconds", 600)):
        maintenance = maintenance_active(cfg) or dt.datetime.now().day == 1
        if maintenance:
            set_maintenance(cfg, True)
        duration = seconds(cfg, "maintenance_monitor_seconds", 86400) if maintenance else seconds(cfg, "normal_monitor_seconds", 21600)
        deadline = time.monotonic() + duration
        streak = 0
        attempts = 0
        LOG.info("monitor started; maintenance=%s", maintenance)
        while time.monotonic() < deadline:
            if in_shutdown_guard_window(dt.datetime.now(), seconds(cfg, "shutdown_retry_seconds", 300)):
                if streak:
                    LOG.info("shutdown guard window; idle streak reset")
                streak = 0
            else:
                try:
                    if not ping(cfg):
                        raise ControlError("PBS not reachable")
                    idle = remote_idle(cfg)
                    if idle:
                        streak += 1
                        LOG.info("idle check %s/%s", streak, seconds(cfg, "idle_checks", 3))
                    else:
                        streak = 0
                        LOG.info("PBS busy; shutdown deferred")
                except ControlError as exc:
                    streak = 0
                    LOG.warning("state unknown; shutdown deferred: %s", exc)
                if streak >= seconds(cfg, "idle_checks", 3):
                    with lock(path / "operation.lock", seconds(cfg, "startup_timeout_seconds", 600)):
                        if in_shutdown_guard_window(dt.datetime.now(), seconds(cfg, "shutdown_retry_seconds", 300)):
                            streak = 0
                            continue
                        try:
                            if not remote_idle(cfg):
                                LOG.info("activity appeared before shutdown")
                                streak = 0
                                continue
                            if in_shutdown_guard_window(dt.datetime.now(), seconds(cfg, "shutdown_retry_seconds", 300)):
                                streak = 0
                                continue
                            LOG.info("requesting normal shutdown")
                            try:
                                response = ssh(cfg, "shutdown")
                                if (response != {"shutdown_requested": True}
                                        or response["shutdown_requested"] is not True):
                                    raise ControlError("unexpected shutdown result")
                                outcome = "accepted"
                            except RemoteCommandRejected as exc:
                                LOG.warning("shutdown rejected; monitoring continues: %s", exc)
                                streak = 0
                                outcome = "rejected"
                            except ControlError as exc:
                                LOG.warning("shutdown SSH result uncertain: %s", exc)
                                outcome = "unknown"
                        except ControlError as exc:
                            LOG.warning("final state unknown; shutdown deferred: %s", exc)
                            streak = 0
                            continue
                        if outcome != "rejected":
                            attempts += 1
                            check_deadline = min(deadline, time.monotonic() + seconds(cfg, "shutdown_retry_seconds", 300))
                            if verify_poweroff(cfg, check_deadline):
                                if outcome == "accepted":
                                    set_maintenance(cfg, False)
                                else:
                                    LOG.warning("PBS unreachable, but shutdown was unconfirmed; maintenance state retained")
                                return
                    if outcome != "rejected":
                        LOG.warning("PBS still responds after shutdown request %s", attempts)
                        streak = 0
                        if attempts >= seconds(cfg, "shutdown_attempts", 2):
                            raise ControlError("shutdown retry limit reached; PBS left running")
            time.sleep(min(seconds(cfg, "idle_interval_seconds", 60), max(0, deadline - time.monotonic())))
        LOG.warning("monitor deadline reached; PBS left running; maintenance=%s", maintenance)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    Path(cfg["state_dir"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.command == "start":
        start(cfg)
    else:
        stop(cfg)


if __name__ == "__main__":
    try:
        main()
    except (ControlError, OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
