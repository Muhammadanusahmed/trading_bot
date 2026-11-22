"""
Shared run records for the dashboard (Phase 1 data recording).

Every layer calls heartbeat() once per loop, so data/heartbeat/<agent>.json
always says when that agent last ran and whether that run worked. A stale
last_run means the agent has stopped; status "error" means it is running but
failing. append_jsonl() is the one helper every history log uses.

These records are for reporting only. Nothing here may stop a layer from
trading or alerting, so every write swallows its own errors.
"""

import json
import os
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
HEARTBEAT_DIR = DATA_DIR / "heartbeat"


def utc_now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def append_jsonl(path, record):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"Could not write {path}: {e}")


def heartbeat(agent, status="ok", error=None, **extra):
    """Record one loop of `agent`. status is "ok" or "error"."""
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        path = HEARTBEAT_DIR / f"{agent}.json"
        record = json.loads(path.read_text()) if path.exists() else {}
        now = utc_now()
        if record.get("pid") != os.getpid():
            # first loop of a new process: restart count shows crash loops
            record["started_at"] = now
            record["pid"] = os.getpid()
            record["restarts"] = record.get("restarts", -1) + 1
            record["runs"] = 0
            record["errors"] = 0
        record["agent"] = agent
        record["last_run"] = now
        record["last_run_epoch"] = time.time()
        record["status"] = status
        record["runs"] = record.get("runs", 0) + 1
        if status == "ok":
            record["last_ok"] = now
        else:
            record["errors"] = record.get("errors", 0) + 1
            record["last_error"] = str(error)[:500]
            record["last_error_time"] = now
        record.update(extra)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(path)  # atomic, so the dashboard never reads half a file
    except Exception as e:
        print(f"Could not write heartbeat for {agent}: {e}")
