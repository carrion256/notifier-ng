#!/usr/bin/env python3
"""Codex adapter for notifier-ng.

Accepts one of:
  * a codex Stop / SubagentStop hook payload (JSON object on stdin)
  * the legacy notify payload (JSON object appended as the final argv
    argument; codex's notify hook passes it that way with stdin nulled)

and emits a single normalized NDJSON object: state idle, mode event.
The event is keyed by the turn id and the subject is the stable
session/thread id, plus the subagent id for SubagentStop payloads.
When message text is available the event may also carry a `context`
object with chronological `items` (role/text pairs from the payload
only, capped at the last 20 items and 20000 aggregate characters).
Non-secret session context is attached as metadata from a fixed
environment allowlist shared by contract with the other adapters
(ZELLIJ_SESSION_NAME, ZELLIJ_PANE_ID, NZM_SESSION_NAME,
NZM_FLEET_PANE, NZM_FLEET_ROLE); empty values are dropped.

Exit status: 0 = processed, or a well-formed payload that is not an
idle/stopped signal; 1 = malformed payload.
"""

import json
import os
import sys

SOURCE = "codex"

CONTEXT_ITEM_LIMIT = 20
CONTEXT_CHAR_LIMIT = 20000


def die(message):
    print(f"{SOURCE}: {message}", file=sys.stderr)
    sys.exit(1)


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


def emit(subject, state, mode, event_id=None, title=None, message=None, context=None, metadata=None):
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
    if context:
        record["context"] = {"items": context}
    if metadata:
        record["metadata"] = metadata
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def str_or_none(value):
    if isinstance(value, str) and value != "":
        return value
    return None

def concise(value, limit=280):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def task_result(task, result):
    task = concise(task)
    result = concise(result)
    parts = []
    if task:
        parts.append(f"Task: {task}")
    if result:
        parts.append(f"Result: {result}")
    return "\n".join(parts) or None


def last_string(values):
    if not isinstance(values, list):
        return None
    return next((value for value in reversed(values) if isinstance(value, str) and value.strip()), None)


def normalized_text(value):
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def bounded_context(items):
    """Apply the hard context ceiling to a chronological (role, text) list.

    Keeps the most recent items while honoring at most CONTEXT_ITEM_LIMIT
    items and CONTEXT_CHAR_LIMIT aggregate characters; a single newest item
    longer than the aggregate ceiling is trimmed to fit it.
    """
    kept = []
    total = 0
    for role, text in reversed(items):
        if len(kept) >= CONTEXT_ITEM_LIMIT:
            break
        if total + len(text) <= CONTEXT_CHAR_LIMIT:
            kept.append((role, text))
            total += len(text)
        elif not kept:
            kept.append((role, text[:CONTEXT_CHAR_LIMIT]))
            total = CONTEXT_CHAR_LIMIT
    kept.reverse()
    return kept


def context_items(input_messages, last_assistant_message):
    """Chronological context items from payload message fields only."""
    items = []
    if isinstance(input_messages, list):
        for value in input_messages:
            text = normalized_text(value)
            if text is not None:
                items.append(("user", text))
    text = normalized_text(last_assistant_message)
    if text is not None:
        items.append(("assistant", text))
    return [
        {"role": role, "text": text}
        for role, text in bounded_context(items)
    ]


def handle_legacy(payload):
    thread_id = str_or_none(payload.get("thread-id"))
    turn_id = str_or_none(payload.get("turn-id"))
    if thread_id is None:
        die("legacy notify payload missing non-empty 'thread-id'")
    if turn_id is None:
        die("legacy notify payload missing non-empty 'turn-id'")
    metadata = env_metadata()
    cwd = str_or_none(payload.get("cwd"))
    if cwd is not None:
        metadata["cwd"] = cwd
    client = str_or_none(payload.get("client"))
    if client is not None:
        metadata["client"] = client
    emit(
        subject=thread_id,
        state="idle",
        mode="event",
        event_id=f"{thread_id}:{turn_id}",
        title="Codex turn complete",
        message=task_result(
            last_string(payload.get("input-messages")),
            payload.get("last-assistant-message"),
        ),
        context=context_items(
            payload.get("input-messages"),
            payload.get("last-assistant-message"),
        ),
        metadata=metadata or None,
    )


def handle_stop(payload, subagent):
    label = "SubagentStop" if subagent else "Stop"
    session_id = str_or_none(payload.get("session_id"))
    turn_id = str_or_none(payload.get("turn_id"))
    if session_id is None:
        die(f"{label} hook payload missing non-empty 'session_id'")
    if turn_id is None:
        die(f"{label} hook payload missing non-empty 'turn_id'")
    subject = session_id
    event_id = f"{session_id}:{turn_id}"
    metadata = env_metadata()
    for key in ("cwd", "model", "transcript_path"):
        value = str_or_none(payload.get(key))
        if value is not None:
            metadata[key] = value
    if subagent:
        agent_id = str_or_none(payload.get("agent_id"))
        if agent_id is None:
            die("SubagentStop hook payload missing non-empty 'agent_id'")
        subject = f"{session_id}:{agent_id}"
        event_id = f"{session_id}:{agent_id}:{turn_id}"
        metadata["agent_id"] = agent_id
        agent_type = str_or_none(payload.get("agent_type"))
        if agent_type is not None:
            metadata["agent_type"] = agent_type
    emit(
        subject=subject,
        state="idle",
        mode="event",
        event_id=event_id,
        title="Codex subagent turn complete" if subagent else "Codex turn complete",
        message=task_result(None, payload.get("last_assistant_message")),
        context=context_items(None, payload.get("last_assistant_message")),
        metadata=metadata or None,
    )


def main():
    if len(sys.argv) > 1:
        # codex's legacy notify hook appends the payload JSON as the final argv
        # argument (stdin is nulled); a supplied argument must be a JSON object.
        try:
            payload = json.loads(sys.argv[-1])
        except (TypeError, ValueError) as exc:
            die(f"final argv argument is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            die("final argv argument is not a JSON object")
    else:
        raw = sys.stdin.read()
        if raw.strip() == "":
            sys.exit(0)  # no payload -> nothing to report
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            die(f"stdin is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            die("stdin JSON is not an object")

    if payload.get("type") == "agent-turn-complete":
        handle_legacy(payload)
        return
    hook = payload.get("hook_event_name")
    if not isinstance(hook, str) or hook == "":
        die("payload is neither a legacy notify payload nor a codex hook payload")
    if hook == "Stop":
        handle_stop(payload, subagent=False)
    elif hook == "SubagentStop":
        handle_stop(payload, subagent=True)
    # other codex hook events are not idle/stopped signals -> no output


if __name__ == "__main__":
    main()
