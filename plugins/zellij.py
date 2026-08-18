#!/usr/bin/env python3
"""Zellij adapter for notifier-ng.

Queries `zellij -s SESSION action list-panes --json` (session from
--session or $ZELLIJ_SESSION_NAME) and emits one normalized NDJSON
snapshot per listed pane:

  * pane running  -> state active
  * pane exited   -> state stopped (with exit_status / is_held metadata)

Zellij exposes no prompt-idle signal (only pane lifecycle), so idle is
never claimed; interactive panes stay active.

Exit status: 0 = query succeeded (record list may be empty);
1 = no session, zellij missing, query failure, or malformed query output.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

SOURCE = "zellij"
QUERY_TIMEOUT = 30


def die(message):
    print(f"{SOURCE}: {message}", file=sys.stderr)
    sys.exit(1)


def emit(subject, state, metadata=None):
    record = {
        "version": 1,
        "source": SOURCE,
        "subject": subject,
        "state": state,
        "mode": "snapshot",
    }
    if metadata:
        record["metadata"] = metadata
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def pane_metadata(pane):
    metadata = {}
    for key in ("pane_command", "pane_cwd", "tab_name"):
        value = pane.get(key)
        if isinstance(value, str) and value != "":
            metadata[key] = value
    if pane.get("is_plugin") is True:
        metadata["is_plugin"] = True
    return metadata


def list_sessions(zellij):
    try:
        proc = subprocess.run(
            [zellij, "list-sessions", "--short"],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"zellij session query timed out after {QUERY_TIMEOUT}s")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        die(f"zellij session query failed (exit {proc.returncode}): {detail}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def scan_session(zellij, session):
    try:
        proc = subprocess.run(
            [zellij, "-s", session, "action", "list-panes", "--json"],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        die(f"zellij query timed out after {QUERY_TIMEOUT}s for session {session!r}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        die(f"zellij query failed for session {session!r} (exit {proc.returncode}): {detail}")
    try:
        panes = json.loads(proc.stdout)
    except ValueError as exc:
        die(f"zellij output for session {session!r} is not valid JSON: {exc}")
    if not isinstance(panes, list):
        die(f"zellij output for session {session!r} is not a JSON array of panes")

    for pane in panes:
        if not isinstance(pane, dict):
            die(f"zellij output for session {session!r} contains a non-object pane entry")
        pane_id = pane.get("id")
        if not isinstance(pane_id, int) or isinstance(pane_id, bool):
            die(f"pane entry missing integer 'id': {pane!r}")
        exited = pane.get("exited")
        if not isinstance(exited, bool):
            die(f"pane {pane_id} missing boolean 'exited'")
        is_plugin = pane.get("is_plugin") is True
        kind = "plugin" if is_plugin else "pane"
        subject = f"{session}:{kind}:{pane_id}"
        if exited:
            metadata = pane_metadata(pane)
            exit_status = pane.get("exit_status")
            if exit_status is not None:
                metadata["exit_status"] = exit_status
            if pane.get("is_held") is True:
                metadata["is_held"] = True
            emit(subject=subject, state="stopped", metadata=metadata or None)
        else:
            emit(subject=subject, state="active", metadata=pane_metadata(pane) or None)


def main():
    parser = argparse.ArgumentParser(description="Emit notifier-ng NDJSON from zellij panes.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--session",
        help="zellij session name (default: $ZELLIJ_SESSION_NAME)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="scan every currently running zellij session",
    )
    args = parser.parse_args()

    zellij = shutil.which("zellij")
    if zellij is None:
        die("zellij executable not found on PATH")
    if args.all:
        sessions = list_sessions(zellij)
    else:
        session = args.session or os.environ.get("ZELLIJ_SESSION_NAME")
        if not session:
            die("no session: pass --session/--all or run inside a zellij session ($ZELLIJ_SESSION_NAME)")
        sessions = [session]
    for session in sessions:
        scan_session(zellij, session)


if __name__ == "__main__":
    main()
