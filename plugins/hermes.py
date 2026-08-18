#!/usr/bin/env python3
"""Hermes adapter for notifier-ng.

Accepts a hermes shell-hook payload (a single JSON object on stdin,
per hermes agent/shell_hooks.py wire shape) and emits normalized NDJSON:

  * on_session_end   -> state idle, mode event, but ONLY when the turn
    completed and was not interrupted; keyed by turn id
  * on_session_finalize with reason "session_expired" -> state stopped,
    mode event
  * any other hook event or any other finalization reason -> no output

Exit status: 0 = processed or not applicable; 1 = malformed payload.
"""

import json
import os
import sys

SOURCE = "hermes"
SESSION_EXPIRED = "session_expired"


def die(message):
    print(f"{SOURCE}: {message}", file=sys.stderr)
    sys.exit(1)


def emit(subject, state, mode, event_id=None, title=None, message=None, metadata=None):
    record = {
        "version": 1,
        "source": SOURCE,
        "subject": subject,
        "state": state,
        "mode": mode,
    }
    if event_id:
        record["event_id"] = event_id
    if title:
        record["title"] = title
    if message:
        record["message"] = message
    if metadata:
        record["metadata"] = metadata
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def str_or_none(value):
    if isinstance(value, str) and value != "":
        return value
    return None


def require_extra(payload, event):
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        die(f"{event} payload missing object 'extra'")
    return extra

def env_metadata():
    """Fixed non-secret environment allowlist (shared by contract; never a
    wildcard prefix export, so token-like NZM_* variables stay out)."""
    meta = {}
    for name in (
        "ZELLIJ_SESSION_NAME",
        "ZELLIJ_PANE_ID",
        "NZM_SESSION_NAME",
        "NZM_FLEET_PANE",
        "NZM_FLEET_ROLE",
    ):
        value = os.environ.get(name)
        if value is not None and value != "":
            meta[name] = value
    return meta


def handle_session_end(payload):
    extra = require_extra(payload, "on_session_end")
    completed = extra.get("completed", payload.get("completed"))
    interrupted = extra.get("interrupted", payload.get("interrupted"))
    if completed is not True or interrupted:
        return  # interrupted or incomplete turn -> no idle signal
    turn_id = extra.get("turn_id", payload.get("turn_id"))
    if not isinstance(turn_id, (str, int)) or isinstance(turn_id, bool) or str(turn_id) == "":
        die("on_session_end payload missing non-empty 'turn_id'")
    turn_id = str(turn_id)
    session_id = str_or_none(payload.get("session_id"))
    task_id = str_or_none(extra.get("task_id"))
    subject = session_id or task_id or "hermes"
    metadata = env_metadata()
    if task_id is not None:
        metadata["task_id"] = task_id
    for key in ("model", "platform"):
        value = str_or_none(extra.get(key)) or str_or_none(payload.get(key))
        if value is not None:
            metadata[key] = value
    cwd = str_or_none(payload.get("cwd"))
    if cwd is not None:
        metadata["cwd"] = cwd
    emit(
        subject=subject,
        state="idle",
        mode="event",
        event_id=f"{subject}:turn:{turn_id}",
        title="Hermes turn complete",
        metadata=metadata or None,
    )


def handle_session_finalize(payload):
    extra = require_extra(payload, "on_session_finalize")
    reason = extra.get("reason", payload.get("reason"))
    if reason != SESSION_EXPIRED:
        return  # new_session, shutdown, ... -> no output
    session_id = str_or_none(payload.get("session_id"))
    subject = session_id or "hermes"
    metadata = env_metadata()
    platform = str_or_none(extra.get("platform")) or str_or_none(payload.get("platform"))
    if platform is not None:
        metadata["platform"] = platform
    emit(
        subject=subject,
        state="stopped",
        mode="event",
        event_id=f"{subject}:{SESSION_EXPIRED}",
        title="Hermes session expired",
        message=f"Session {session_id} expired" if session_id is not None else None,
        metadata=metadata or None,
    )


def main():
    raw = sys.stdin.read()
    if raw.strip() == "":
        die("empty stdin: expected a shell-hook JSON payload")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        die(f"stdin is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        die("stdin JSON is not an object")
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or event == "":
        die("payload missing non-empty 'hook_event_name'")
    if event == "on_session_end":
        handle_session_end(payload)
    elif event == "on_session_finalize":
        handle_session_finalize(payload)
    # other hook events (on_session_start, on_tool_use, ...) -> no output


if __name__ == "__main__":
    main()
