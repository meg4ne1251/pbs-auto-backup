#!/usr/bin/env python3
"""Restricted PBS-side SSH command for readiness, activity and poweroff."""

import argparse
import json
import os
import re
import subprocess
import sys


class StateError(Exception):
    pass


def run(*args, timeout=20):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise StateError(f"command failed: {args[0]} {args[1] if len(args) > 1 else ''}: {exc}") from exc
    return result.stdout.strip()


def config(path):
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise StateError("configuration must be an object")
    for key in ("pool", "dataset", "datastore", "datastore_path"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise StateError(f"missing {key}")
    if not value["dataset"].startswith(value["pool"] + "/"):
        raise StateError("dataset must belong to pool")
    if not os.path.isabs(value["datastore_path"]):
        raise StateError("datastore_path must be absolute")
    for key in ("pool", "dataset", "datastore"):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", value[key]):
            raise StateError(f"invalid {key}")
    return value


def readiness(cfg):
    for service in ("proxmox-backup", "proxmox-backup-proxy"):
        if run("systemctl", "is-active", service) != "active":
            raise StateError(f"{service} is not active")
    if run("zpool", "list", "-H", "-o", "name", cfg["pool"]) != cfg["pool"]:
        raise StateError("pool missing")
    fields = run("zfs", "list", "-H", "-o", "mounted,mountpoint", cfg["dataset"]).split("\t")
    if len(fields) != 2 or fields[0] != "yes":
        raise StateError("dataset is not mounted")
    mountpoint = os.path.realpath(fields[1])
    path = os.path.realpath(cfg["datastore_path"])
    if path != mountpoint and not path.startswith(mountpoint.rstrip("/") + "/"):
        raise StateError("datastore path is outside mounted dataset")
    if run("findmnt", "-n", "-o", "TARGET", "--target", path) != mountpoint:
        raise StateError("datastore dataset is not the active mount")
    if run("zfs", "get", "-H", "-o", "value", "readonly", cfg["dataset"]) != "off":
        raise StateError("datastore dataset is read-only")
    try:
        store = json.loads(run("proxmox-backup-manager", "datastore", "show", cfg["datastore"], "--output-format", "json"))
    except json.JSONDecodeError as exc:
        raise StateError("invalid datastore JSON") from exc
    if not isinstance(store, dict) or os.path.realpath(store.get("path", "")) != path:
        raise StateError("datastore configuration does not match path")
    if (not os.path.isdir(path) or os.statvfs(path).f_flag & os.ST_RDONLY
            or not os.access(path, os.R_OK | os.W_OK | os.X_OK)):
        raise StateError("datastore path is unavailable")


def scan_busy(status, pool):
    if not re.search(rf"^\s*pool:\s*{re.escape(pool)}\s*$", status, re.MULTILINE):
        raise StateError("unexpected zpool status output")
    match = re.search(r"^\s*scan:\s*(.+)$", status, re.MULTILINE)
    if not match:
        raise StateError("missing ZFS scan state")
    scan = match.group(1).lower()
    if "in progress" in scan or "paused" in scan or "suspended" in scan:
        return True
    if (scan.startswith("none requested") or scan.startswith("scrub repaired")
            or scan.startswith("resilvered") or scan.startswith("scrub canceled")
            or scan.startswith("scrub cancelled")):
        return False
    raise StateError(f"unknown ZFS scan state: {scan}")


def activity(cfg):
    try:
        tasks = json.loads(run("proxmox-backup-manager", "task", "list", "--limit", "1000", "--output-format", "json"))
    except json.JSONDecodeError as exc:
        raise StateError("invalid task JSON") from exc
    if not isinstance(tasks, list) or len(tasks) >= 1000:
        raise StateError("task list invalid or truncated")
    if any(not isinstance(task, dict) or not isinstance(task.get("upid"), str) for task in tasks):
        raise StateError("unexpected task format")
    # The CLI lists running tasks by default. Any entry is therefore busy.
    scan = scan_busy(run("zpool", "status", cfg["pool"]), cfg["pool"])
    return {"idle": not tasks and not scan, "running_tasks": len(tasks), "zfs_scan_busy": scan}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("command", choices=("ready", "idle", "shutdown"), nargs="?")
    args = parser.parse_args()
    # With a forced command, reject extra or altered SSH_ORIGINAL_COMMAND values.
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    if original is not None:
        if original not in ("ready", "idle", "shutdown") or (args.command and args.command != original):
            raise StateError("SSH command is not allowed")
        command = original
    else:
        command = args.command
    if command is None:
        parser.error("command required")
    cfg = config(args.config)
    if command == "ready":
        readiness(cfg)
        print(json.dumps({"ready": True}))
    elif command == "idle":
        print(json.dumps(activity(cfg)))
    else:
        readiness(cfg)
        if not activity(cfg)["idle"]:
            raise StateError("PBS is busy")
        run("shutdown", "-h", "now", timeout=10)
        print(json.dumps({"shutdown_requested": True}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (StateError, OSError, ValueError, KeyError) as exc:
        print(f"pbs-agent: {exc}", file=sys.stderr)
        sys.exit(1)
