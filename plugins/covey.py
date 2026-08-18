#!/usr/bin/env python3
"""Covey adapter for notifier-ng.

Read-only scan of a Covey SQLite database (--db PATH; default ./covey.db)
and emission of one normalized NDJSON snapshot per session row:

  * state 'active'  -> state active, mode snapshot
  * state 'stale' / 'exited' -> state stopped, mode snapshot, event keyed
    by updated_at (epoch ms)
  * anything else   -> fail loudly

Principal, role, and subtask title are included as metadata. No writes
are made to the database (opened with mode=ro).

Exit status: 0 = scan succeeded (possibly zero records);
1 = missing/unreadable database, query failure, or unexpected row values.
"""

import argparse
import datetime
import json
import sqlite3
import sys

SOURCE = "covey"
STOPPED_STATES = ("stale", "exited")


def die(message):
    print(f"{SOURCE}: {message}", file=sys.stderr)
    sys.exit(1)


def iso_timestamp(epoch_ms):
    return datetime.datetime.fromtimestamp(
        epoch_ms / 1000.0, datetime.timezone.utc
    ).isoformat(timespec="milliseconds")


def emit(subject, state, mode, event_id=None, timestamp=None, metadata=None):
    record = {
        "version": 1,
        "source": SOURCE,
        "subject": subject,
        "state": state,
        "mode": mode,
    }
    if event_id:
        record["event_id"] = event_id
    if timestamp:
        record["timestamp"] = timestamp
    if metadata:
        record["metadata"] = metadata
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def main():
    parser = argparse.ArgumentParser(description="Emit notifier-ng NDJSON from a Covey database.")
    parser.add_argument(
        "--db",
        default="./covey.db",
        help="path to the Covey SQLite database (default: ./covey.db)",
    )
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        die(f"cannot open database {args.db}: {exc}")
    try:
        rows = conn.execute(
            """
            SELECT s.session_token, s.agent_principal_id, s.role, s.state,
                   s.active_subtask_id, s.updated_at, st.title
            FROM sessions AS s
            LEFT JOIN subtasks AS st ON st.subtask_id = s.active_subtask_id
            ORDER BY s.session_token
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        die(f"database query failed ({args.db}): {exc}")

    for session_token, principal, role, state, _subtask_id, updated_at, subtask_title in rows:
        metadata = {"principal": principal, "role": role}
        if isinstance(subtask_title, str) and subtask_title != "":
            metadata["subtask"] = subtask_title
        if state == "active":
            emit(
                subject=session_token,
                state="active",
                mode="snapshot",
                timestamp=iso_timestamp(updated_at),
                metadata=metadata,
            )
        elif state in STOPPED_STATES:
            emit(
                subject=session_token,
                state="stopped",
                mode="snapshot",
                event_id=f"stopped:{updated_at}",
                timestamp=iso_timestamp(updated_at),
                metadata=metadata,
            )
        else:
            die(f"unexpected session state {state!r} for {session_token}")


if __name__ == "__main__":
    main()
