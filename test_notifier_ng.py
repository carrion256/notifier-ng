#!/usr/bin/env python3
"""Behavioral test suite for notifier-ng (core CLI and source plugins).

Everything is exercised through real processes and loopback HTTP servers
(strictly standard library): each plugin executable is run as a subprocess
with real argv/stdin/stdout/stderr, and the core CLI is driven with its
documented argv/config/stdin contract against local capture servers.

No public network is used, no external user state is touched (all
databases, configs, and state files live in temporary directories), and
the suite is fully deterministic.

Run as:  python3 test_notifier_ng.py
"""

import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "notifier_ng.py"
PLUGINS = ROOT / "plugins"

SUBPROCESS_TIMEOUT = 60


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_ndjson(text):
    """Parse newline-delimited JSON; returns a list of parsed records."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def run_plugin(name, *args, stdin=None, env=None, cwd=None, timeout=SUBPROCESS_TIMEOUT):
    """Run one plugin executable exactly as a CLI, capturing output.

    Invoked through the running interpreter so shebang resolution never
    depends on the ambient PATH; argv/stdin/stdout/stderr/exit status are
    all real process behavior.
    """
    cmd = [sys.executable, str(PLUGINS / name), *args]
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
    )


def env_with(**overrides):
    """A copy of the current environment with the named variables set."""
    env = os.environ.copy()
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def write_script(path, code):
    """Write an executable script whose shebang points at the current interpreter.

    The shebang uses sys.executable directly so the script keeps working
    even when the test restricts PATH (e.g. the fake-zellij scenarios).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
    path.chmod(0o755)
    return path


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records every request verbatim; serves a scripted status/body."""

    def log_message(self, *args):  # keep the test output quiet
        pass

    def _handle(self):
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        with server.lock:
            server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": body,
                }
            )
        status = server.status  # int, or callable(request) -> int
        if callable(status):
            status = status(server.requests[-1])
        payload = server.body if isinstance(server.body, bytes) else server.body.encode("utf-8")
        self.send_response(status)
        if server.location:
            self.send_header("Location", str(server.location))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


class CaptureServer:
    def __init__(self, status=200, body=b"", host="127.0.0.1", location=None):
        self.status = status
        self.body = body
        self.location = location
        self._host = host
        self._httpd = ThreadingHTTPServer((host, 0), _CaptureHandler)
        self._httpd.requests = []
        self._httpd.lock = threading.Lock()
        self._httpd.status = self.status
        self._httpd.body = self.body
        self._httpd.location = self.location
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        netloc = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{netloc}:{self._httpd.server_port}"

    @property
    def port(self):
        return self._httpd.server_port

    @property
    def requests(self):
        with self._httpd.lock:
            return list(self._httpd.requests)

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TempState:
    """A temporary working directory with convenience paths."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="notifier-ng-test-")
        self.root = Path(self._tmp.name)

    def path(self, *parts):
        return self.root.joinpath(*parts)

    def cleanup(self):
        self._tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class NzBaseTestCase(unittest.TestCase):
    """Shared assertions for normalized NDJSON records."""

    def assertRecord(self, rec, *, source, subject, state, mode, **fields):
        self.assertEqual(rec.get("version"), 1, rec)
        self.assertEqual(rec.get("source"), source, rec)
        self.assertEqual(rec.get("subject"), subject, rec)
        self.assertEqual(rec.get("state"), state, rec)
        self.assertEqual(rec.get("mode"), mode, rec)
        for key, expected in fields.items():
            self.assertEqual(rec.get(key), expected, rec)


# ---------------------------------------------------------------------------
# codex plugin
# ---------------------------------------------------------------------------

class CodexPluginTests(NzBaseTestCase):
    PLUGIN = "codex.py"

    def test_legacy_notify_argv_emits_normalized_idle_event(self):
        env = env_with(
            ZELLIJ_SESSION_NAME="office",
            ZELLIJ_PANE_ID="pane-7",
            NZM_SESSION_NAME="fleet-a",
            NZM_FLEET_PANE="pane-9",
            NZM_FLEET_ROLE="worker",
            NZM_TASK_TOKEN="tok-123",
            NZM_API_KEY="sk-456",
            NZM_DECOY="surprise",
            NZM_EMPTY="",
        )
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "thread-42",
            "turn-id": "turn-9",
            "cwd": "/srv/work",
            "client": "oauth-cli",
            "input-messages": ["Investigate the idle notification path"],
            "last-assistant-message": "wrapped up",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 1, proc.stdout)
        rec = records[0]
        self.assertRecord(
            rec,
            source="codex",
            subject="thread-42",
            state="idle",
            mode="event",
            event_id="thread-42:turn-9",
            title="Codex turn complete",
            message="Task: Investigate the idle notification path\nResult: wrapped up",
        )
        meta = rec["metadata"]
        self.assertEqual(meta["cwd"], "/srv/work")
        self.assertEqual(meta["client"], "oauth-cli")
        self.assertEqual(meta["ZELLIJ_SESSION_NAME"], "office")
        self.assertEqual(meta["ZELLIJ_PANE_ID"], "pane-7")
        self.assertEqual(meta["NZM_SESSION_NAME"], "fleet-a")
        self.assertEqual(meta["NZM_FLEET_PANE"], "pane-9")
        self.assertEqual(meta["NZM_FLEET_ROLE"], "worker")
        self.assertNotIn("NZM_TASK_TOKEN", meta, "token-like env vars must not leak into metadata")
        self.assertNotIn("NZM_API_KEY", meta, "token-like env vars must not leak into metadata")
        self.assertNotIn("NZM_DECOY", meta, "non-allowlisted NZM_* variables must not be enumerated")
        self.assertNotIn("NZM_EMPTY", meta, "non-allowlisted (even empty) variables must not be enumerated")

    def test_env_metadata_drops_empty_allowlisted_values(self):
        env = env_with(
            ZELLIJ_PANE_ID="",
            ZELLIJ_SESSION_NAME="office",
            NZM_SESSION_NAME="fleet-a",
        )
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "thread-42",
            "turn-id": "turn-9",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta = parse_ndjson(proc.stdout)[0]["metadata"]
        self.assertNotIn("ZELLIJ_PANE_ID", meta, "empty allowlisted values must be dropped")
        self.assertEqual(meta["ZELLIJ_SESSION_NAME"], "office")
        self.assertEqual(meta["NZM_SESSION_NAME"], "fleet-a")

    def test_legacy_payload_missing_thread_id_fails(self):
        payload = {"type": "agent-turn-complete", "turn-id": "turn-9"}
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("thread-id", proc.stderr)

    def test_legacy_payload_missing_turn_id_fails(self):
        payload = {"type": "agent-turn-complete", "thread-id": "thread-42"}
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("turn-id", proc.stderr)

    def test_legacy_payload_empty_strings_fail(self):
        payload = {"type": "agent-turn-complete", "thread-id": "", "turn-id": "t"}
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("thread-id", proc.stderr)

    def test_argv_payload_must_be_json_object(self):
        proc = run_plugin(self.PLUGIN, "not-json{")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSON", proc.stderr)
        proc = run_plugin(self.PLUGIN, json.dumps(["a", "list"]))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a JSON object", proc.stderr)

    def test_stdin_stop_hook_emits_idle_event(self):
        payload = {
            "hook_event_name": "Stop",
            "session_id": "session-5",
            "turn_id": "turn-2",
            "model": "gpt-5",
            "cwd": "/repo",
            "last_assistant_message": "done",
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 1, proc.stdout)
        rec = records[0]
        self.assertRecord(
            rec,
            source="codex",
            subject="session-5",
            state="idle",
            mode="event",
            event_id="session-5:turn-2",
            title="Codex turn complete",
            message="Result: done",
        )
        self.assertEqual(rec["metadata"]["model"], "gpt-5")
        self.assertEqual(rec["metadata"]["cwd"], "/repo")

    def test_legacy_message_is_whitespace_normalized_and_truncated(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "thread-42",
            "turn-id": "turn-9",
            "input-messages": ["first", "  Last\n task  "],
            "last-assistant-message": "result " + "x" * 400,
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        message = parse_ndjson(proc.stdout)[0]["message"]
        self.assertTrue(message.startswith("Task: Last task\nResult: result "))
        self.assertTrue(message.endswith("…"))
        self.assertLessEqual(len(message.split("\n", 1)[1]) - len("Result: "), 280)

    def test_stdin_stop_hook_missing_ids_fail(self):
        proc = run_plugin(self.PLUGIN, stdin=json.dumps({"hook_event_name": "Stop", "turn_id": "t"}))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("session_id", proc.stderr)
        proc = run_plugin(self.PLUGIN, stdin=json.dumps({"hook_event_name": "Stop", "session_id": "s"}))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("turn_id", proc.stderr)

    def test_stdin_subagent_stop_requires_agent_id(self):
        payload = {"hook_event_name": "SubagentStop", "session_id": "s", "turn_id": "t"}
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("agent_id", proc.stderr)

    def test_stdin_subagent_stop_emits_compound_subject(self):
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "session-5",
            "turn_id": "turn-2",
            "agent_id": "agent-11",
            "agent_type": "scout",
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 1, proc.stdout)
        rec = records[0]
        self.assertRecord(
            rec,
            source="codex",
            subject="session-5:agent-11",
            state="idle",
            mode="event",
            event_id="session-5:agent-11:turn-2",
            title="Codex subagent turn complete",
        )
        self.assertEqual(rec["metadata"]["agent_id"], "agent-11")
        self.assertEqual(rec["metadata"]["agent_type"], "scout")

    def test_other_hook_events_emit_nothing(self):
        payload = {"hook_event_name": "AgentMessage", "session_id": "s", "turn_id": "t"}
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_empty_stdin_emits_nothing(self):
        proc = run_plugin(self.PLUGIN, stdin="")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_malformed_stdin_fails(self):
        proc = run_plugin(self.PLUGIN, stdin="{'broken': ")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stdin is not valid JSON", proc.stderr)
        proc = run_plugin(self.PLUGIN, stdin='"a string"')
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not an object", proc.stderr)

    def test_payload_without_recognized_shape_fails(self):
        proc = run_plugin(self.PLUGIN, stdin=json.dumps({"type": "agent-message"}))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("neither a legacy notify payload nor a codex hook payload", proc.stderr)

    def test_legacy_context_is_chronological_with_final_assistant(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "thread-42",
            "turn-id": "turn-9",
            "input-messages": ["first", "  Second \n message ", "third"],
            "last-assistant-message": "final answer",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = parse_ndjson(proc.stdout)[0]["context"]["items"]
        self.assertEqual(
            items,
            [
                {"role": "user", "text": "first"},
                {"role": "user", "text": "Second message"},
                {"role": "user", "text": "third"},
                {"role": "assistant", "text": "final answer"},
            ],
        )

    def test_legacy_context_ceiling_keeps_last_20_items(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "t",
            "turn-id": "u",
            "input-messages": [f"message-{i:02d}" for i in range(25)],
            "last-assistant-message": "done",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = parse_ndjson(proc.stdout)[0]["context"]["items"]
        self.assertEqual(len(items), 20)
        self.assertEqual(
            [item["text"] for item in items],
            [f"message-{i:02d}" for i in range(6, 25)] + ["done"],
        )
        self.assertEqual(items[-1], {"role": "assistant", "text": "done"})

    def test_legacy_context_ceiling_caps_aggregate_characters(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "t",
            "turn-id": "u",
            "input-messages": ["x" * 2000 for _ in range(12)],
            "last-assistant-message": "done",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = parse_ndjson(proc.stdout)[0]["context"]["items"]
        self.assertLessEqual(len(items), 20)
        self.assertLessEqual(sum(len(item["text"]) for item in items), 20000)
        # newest content is retained: the final assistant message plus as
        # many of the most recent user messages as fit within the ceiling
        self.assertEqual(items[-1], {"role": "assistant", "text": "done"})
        self.assertEqual(len(items), 10)
        for item in items[:-1]:
            self.assertEqual(item["role"], "user")
            self.assertEqual(len(item["text"]), 2000)

    def test_legacy_context_skips_malformed_non_string_messages(self):
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "t",
            "turn-id": "u",
            "input-messages": [
                "real first",
                None,
                42,
                ["nested", "list"],
                {"role": "user"},
                "",
                "   ",
                "real second",
            ],
            "last-assistant-message": "  wrapped\n up  ",
        }
        proc = run_plugin(self.PLUGIN, json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = parse_ndjson(proc.stdout)[0]
        self.assertEqual(
            rec["context"],
            {
                "items": [
                    {"role": "user", "text": "real first"},
                    {"role": "user", "text": "real second"},
                    {"role": "assistant", "text": "wrapped up"},
                ]
            },
        )
        # fallback message still derives from the last valid input string
        self.assertEqual(rec["message"], "Task: real second\nResult: wrapped up")

    def test_stop_hook_context_is_payload_only_not_transcript(self):
        payload = {
            "hook_event_name": "Stop",
            "session_id": "session-5",
            "turn_id": "turn-2",
            "model": "gpt-5",
            "cwd": "/repo",
            "transcript_path": "/nonexistent/transcript.jsonl",
            "last_assistant_message": "done",
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = parse_ndjson(proc.stdout)[0]
        self.assertEqual(rec["context"], {"items": [{"role": "assistant", "text": "done"}]})
        self.assertEqual(rec["metadata"]["transcript_path"], "/nonexistent/transcript.jsonl")
        self.assertEqual(rec["message"], "Result: done")

    def test_stop_hook_context_requires_valid_assistant_message(self):
        for value in (None, 42, "", "   "):
            payload = {
                "hook_event_name": "Stop",
                "session_id": "s",
                "turn_id": "t",
                "last_assistant_message": value,
            }
            proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("context", parse_ndjson(proc.stdout)[0], repr(value))

    def test_stdin_subagent_stop_context_is_final_assistant_only(self):
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "session-5",
            "turn_id": "turn-2",
            "agent_id": "agent-11",
            "last_assistant_message": "wrapped up",
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = parse_ndjson(proc.stdout)[0]
        self.assertEqual(
            rec["context"],
            {"items": [{"role": "assistant", "text": "wrapped up"}]},
        )


# ---------------------------------------------------------------------------
# hermes plugin
# ---------------------------------------------------------------------------

class HermesPluginTests(NzBaseTestCase):
    PLUGIN = "hermes.py"

    def test_session_end_completed_emits_idle_event(self):
        env = env_with(
            ZELLIJ_SESSION_NAME="matrix",
            NZM_SESSION_NAME="fleet-b",
            NZM_FLEET_PANE="pane-1",
            NZM_FLEET_ROLE="leader",
            NZM_TASK_TOKEN="tok-x",
            NZM_API_KEY="sk-y",
            NZM_RUN="alpha",
        )
        payload = {
            "hook_event_name": "on_session_end",
            "session_id": "session-31",
            "cwd": "/srv/app",
            "extra": {
                "completed": True,
                "interrupted": False,
                "turn_id": 88,
                "task_id": "task-4",
                "model": "hermes-x",
                "platform": "linux",
            },
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 1, proc.stdout)
        rec = records[0]
        self.assertRecord(
            rec,
            source="hermes",
            subject="session-31",
            state="idle",
            mode="event",
            event_id="session-31:turn:88",
            title="Hermes turn complete",
        )
        meta = rec["metadata"]
        self.assertEqual(meta["task_id"], "task-4")
        self.assertEqual(meta["model"], "hermes-x")
        self.assertEqual(meta["platform"], "linux")
        self.assertEqual(meta["cwd"], "/srv/app")
        self.assertEqual(meta["ZELLIJ_SESSION_NAME"], "matrix")
        self.assertEqual(meta["NZM_SESSION_NAME"], "fleet-b")
        self.assertEqual(meta["NZM_FLEET_PANE"], "pane-1")
        self.assertEqual(meta["NZM_FLEET_ROLE"], "leader")
        self.assertNotIn("NZM_TASK_TOKEN", meta, "token-like env vars must not leak into metadata")
        self.assertNotIn("NZM_API_KEY", meta, "token-like env vars must not leak into metadata")
        self.assertNotIn("NZM_RUN", meta, "non-allowlisted NZM_* variables must not be enumerated")

    def test_session_end_interrupted_emits_nothing(self):
        payload = {
            "hook_event_name": "on_session_end",
            "session_id": "s",
            "extra": {"completed": True, "interrupted": True, "turn_id": 1},
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_session_end_not_completed_emits_nothing(self):
        payload = {
            "hook_event_name": "on_session_end",
            "session_id": "s",
            "extra": {"completed": False, "interrupted": False, "turn_id": 1},
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_session_end_missing_turn_id_fails(self):
        payload = {"hook_event_name": "on_session_end", "extra": {"completed": True}}
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("turn_id", proc.stderr)

    def test_session_end_missing_extra_fails(self):
        payload = {"hook_event_name": "on_session_end", "session_id": "s", "completed": True}
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("extra", proc.stderr)

    def test_session_finalize_expired_emits_stopped_event(self):
        env = env_with(ZELLIJ_PANE_ID="pane-0")
        payload = {
            "hook_event_name": "on_session_finalize",
            "session_id": "session-31",
            "extra": {"reason": "session_expired", "platform": "darwin"},
        }
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 1, proc.stdout)
        rec = records[0]
        self.assertRecord(
            rec,
            source="hermes",
            subject="session-31",
            state="stopped",
            mode="event",
            event_id="session-31:session_expired",
            title="Hermes session expired",
            message="Session session-31 expired",
        )
        self.assertEqual(rec["metadata"]["platform"], "darwin")
        self.assertEqual(rec["metadata"]["ZELLIJ_PANE_ID"], "pane-0")

    def test_session_finalize_other_reason_emits_nothing(self):
        for reason in ("new_session", "shutdown", "manual"):
            payload = {
                "hook_event_name": "on_session_finalize",
                "session_id": "s",
                "extra": {"reason": reason},
            }
            proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
            self.assertEqual(proc.returncode, 0, reason)
            self.assertEqual(proc.stdout, "", reason)

    def test_session_finalize_missing_extra_fails(self):
        payload = {"hook_event_name": "on_session_finalize", "reason": "session_expired"}
        proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("extra", proc.stderr)

    def test_other_hook_events_emit_nothing(self):
        for event in ("on_session_start", "on_tool_use"):
            payload = {"hook_event_name": event, "session_id": "s"}
            proc = run_plugin(self.PLUGIN, stdin=json.dumps(payload))
            self.assertEqual(proc.returncode, 0, event)
            self.assertEqual(proc.stdout, "", event)

    def test_empty_stdin_fails(self):
        proc = run_plugin(self.PLUGIN, stdin="")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("empty stdin", proc.stderr)

    def test_malformed_stdin_fails(self):
        proc = run_plugin(self.PLUGIN, stdin="<<<")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stdin is not valid JSON", proc.stderr)
        proc = run_plugin(self.PLUGIN, stdin="42")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not an object", proc.stderr)

    def test_missing_hook_event_name_fails(self):
        proc = run_plugin(self.PLUGIN, stdin=json.dumps({"session_id": "s"}))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hook_event_name", proc.stderr)


# ---------------------------------------------------------------------------
# covey plugin (read-only database scan)
# ---------------------------------------------------------------------------

def make_covey_db(path):
    """Create the Covey schema with deterministic rows; returns a dict of tokens."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_token TEXT,
            agent_principal_id TEXT,
            role TEXT,
            state TEXT,
            active_subtask_id TEXT,
            updated_at INTEGER
        );
        CREATE TABLE subtasks (subtask_id TEXT, title TEXT);
        INSERT INTO sessions VALUES
            ('active-1', 'principal-a', 'codex', 'active', 'st-1', 1723980000000),
            ('exited-1', 'principal-b', 'hermes', 'exited', NULL, 1723960000000),
            ('old-1',   'principal-c', 'covey', 'stale',  NULL, 1723970000000);
        INSERT INTO subtasks VALUES ('st-1', 'Write parser');
        """
    )
    conn.commit()
    conn.close()
    return {
        "active": ("active-1", "principal-a", "codex", 1723980000000, "Write parser"),
        "exited": ("exited-1", "principal-b", "hermes", 1723960000000, None),
        "stale": ("old-1", "principal-c", "covey", 1723970000000, None),
    }


def iso_utc_ms(epoch_ms):
    return datetime.datetime.fromtimestamp(
        epoch_ms / 1000.0, datetime.timezone.utc
    ).isoformat(timespec="milliseconds")


class CoveyPluginTests(NzBaseTestCase):
    PLUGIN = "covey.py"

    def scan(self, db_path, *extra):
        return run_plugin(self.PLUGIN, "--db", str(db_path), *extra)

    def test_scan_emits_normalized_snapshots_in_token_order(self):
        with TempState() as ts:
            db = ts.path("covey.db")
            rows = make_covey_db(db)
            proc = self.scan(db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = parse_ndjson(proc.stdout)
        self.assertEqual(len(records), 3, proc.stdout)
        # ORDER BY session_token: 'active-1' < 'exited-1' < 'old-1'
        active_rec, exited_rec, stale_rec = records

        token, principal, role, ts_ms, subtask = rows["active"]
        self.assertRecord(
            active_rec,
            source="covey",
            subject=token,
            state="active",
            mode="snapshot",
            timestamp=iso_utc_ms(ts_ms),
        )
        self.assertEqual(
            active_rec["metadata"],
            {"principal": principal, "role": role, "subtask": subtask},
            active_rec,
        )

        token, principal, role, ts_ms, _ = rows["exited"]
        self.assertRecord(
            exited_rec,
            source="covey",
            subject=token,
            state="stopped",
            mode="snapshot",
            event_id=f"stopped:{ts_ms}",
            timestamp=iso_utc_ms(ts_ms),
        )
        self.assertEqual(exited_rec["metadata"], {"principal": principal, "role": role})

        token, principal, role, ts_ms, _ = rows["stale"]
        self.assertRecord(
            stale_rec,
            source="covey",
            subject=token,
            state="stopped",
            mode="snapshot",
            event_id=f"stopped:{ts_ms}",
            timestamp=iso_utc_ms(ts_ms),
        )
        self.assertEqual(stale_rec["metadata"], {"principal": principal, "role": role})

    def test_scan_never_writes_to_database(self):
        with TempState() as ts:
            db = ts.path("covey.db")
            make_covey_db(db)
            before = db.read_bytes()
            proc = self.scan(db)
            after = db.read_bytes()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after, before, "scan must leave the database byte-identical")

    def test_missing_database_fails(self):
        with TempState() as ts:
            proc = self.scan(ts.path("no-such.db"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot open database", proc.stderr)

    def test_unexpected_state_fails(self):
        with TempState() as ts:
            db = ts.path("covey.db")
            make_covey_db(db)
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO sessions VALUES ('weird-1','p','r','paused',NULL,1)"
            )
            conn.commit()
            conn.close()
            proc = self.scan(db)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unexpected session state", proc.stderr)

    def test_default_db_path_is_relative_covey_db(self):
        with TempState() as ts:
            proc = run_plugin(self.PLUGIN, cwd=str(ts.root))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot open database", proc.stderr)


# ---------------------------------------------------------------------------
# zellij plugin (queries a real `zellij` executable)
# ---------------------------------------------------------------------------

FAKE_ZELLIJ = """\
import json, os, sys
with open(os.environ["FAKE_ZELLIJ_ARGS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("FAKE_ZELLIJ_OUT", "[]"))
sys.stderr.write(os.environ.get("FAKE_ZELLIJ_ERR", ""))
sys.exit(int(os.environ.get("FAKE_ZELLIJ_RC", "0")))
"""


class ZellijPluginTests(NzBaseTestCase):
    PLUGIN = "zellij.py"

    def run_with_fake(self, panes, *, output=None, session=None, rc=0, stderr=""):
        with TempState() as ts:
            bin_dir = ts.path("bin")
            fake = write_script(bin_dir / "zellij", FAKE_ZELLIJ)
            args_file = ts.path("fake-args.ndjson")
            env = env_with(
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                FAKE_ZELLIJ_ARGS=str(args_file),
                FAKE_ZELLIJ_OUT=output if output is not None else json.dumps(panes),
                FAKE_ZELLIJ_RC=str(rc),
                FAKE_ZELLIJ_ERR=stderr,
            )
            cmd = [sys.executable, str(PLUGINS / self.PLUGIN)]
            if session is not None:
                cmd += ["--session", session]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT
            )
            recorded = (
                [json.loads(line) for line in args_file.read_text().splitlines() if line.strip()]
                if args_file.exists()
                else []
            )
            body = proc.stdout
            err = proc.stderr
        return proc, recorded, body, err

    def test_query_uses_expected_argv_and_emits_snapshots(self):
        panes = [
            {"id": 1, "exited": False, "pane_command": "bash", "pane_cwd": "/srv", "tab_name": "main"},
            {"id": 2, "exited": True, "exit_status": 0, "is_held": True, "pane_command": "vim"},
            {"id": 3, "exited": True, "exit_status": 130, "is_held": False, "pane_command": ""},
        ]
        proc, recorded, body, _ = self.run_with_fake(panes, session="sess-one")
        self.assertEqual(proc.returncode, 0, body + proc.stderr)
        self.assertEqual(
            recorded,
            [["-s", "sess-one", "action", "list-panes", "--json"]],
            "zellij must be called with the documented argv shape",
        )
        records = parse_ndjson(body)
        self.assertEqual(len(records), 3, body)
        rec1, rec2, rec3 = records
        self.assertRecord(
            rec1,
            source="zellij",
            subject="sess-one:pane:1",
            state="active",
            mode="snapshot",
        )
        self.assertEqual(
            rec1["metadata"],
            {"pane_command": "bash", "pane_cwd": "/srv", "tab_name": "main"},
        )
        self.assertRecord(
            rec2,
            source="zellij",
            subject="sess-one:pane:2",
            state="stopped",
            mode="snapshot",
        )
        self.assertEqual(
            rec2["metadata"],
            {"pane_command": "vim", "exit_status": 0, "is_held": True},
        )
        self.assertRecord(
            rec3,
            source="zellij",
            subject="sess-one:pane:3",
            state="stopped",
            mode="snapshot",
        )
        self.assertEqual(
            rec3["metadata"],
            {"exit_status": 130},
            "empty strings and false flags must be dropped from metadata",
        )

    def test_session_from_environment_when_flag_absent(self):
        panes = []
        with TempState() as ts:
            bin_dir = ts.path("bin")
            write_script(bin_dir / "zellij", FAKE_ZELLIJ)
            args_file = ts.path("fake-args.ndjson")
            env = env_with(
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                FAKE_ZELLIJ_ARGS=str(args_file),
                FAKE_ZELLIJ_OUT="[]",
                ZELLIJ_SESSION_NAME="env-sess",
            )
            proc = subprocess.run(
                [sys.executable, str(PLUGINS / self.PLUGIN)],
                capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT,
            )
            recorded = [json.loads(line) for line in args_file.read_text().splitlines() if line.strip()]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(recorded, [["-s", "env-sess", "action", "list-panes", "--json"]])

    def test_all_scans_each_listed_session(self):
        with TempState() as ts:
            bin_dir = ts.path("bin")
            args_file = ts.path("fake-args.ndjson")
            fake = write_script(
                bin_dir / "zellij",
                """
import json, os, sys
with open(os.environ["FAKE_ZELLIJ_ARGS"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["list-sessions", "--short"]:
    print("alpha\\nbeta")
else:
    session = sys.argv[2]
    print(json.dumps([{"id": 1, "exited": session == "beta"}]))
""",
            )
            env = env_with(
                PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                FAKE_ZELLIJ_ARGS=str(args_file),
            )
            proc = subprocess.run(
                [sys.executable, str(PLUGINS / self.PLUGIN), "--all"],
                capture_output=True,
                text=True,
                env=env,
                timeout=SUBPROCESS_TIMEOUT,
            )
            recorded = [json.loads(line) for line in args_file.read_text().splitlines() if line.strip()]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            recorded,
            [
                ["list-sessions", "--short"],
                ["-s", "alpha", "action", "list-panes", "--json"],
                ["-s", "beta", "action", "list-panes", "--json"],
            ],
        )
        records = parse_ndjson(proc.stdout)
        self.assertRecord(records[0], source="zellij", subject="alpha:pane:1", state="active", mode="snapshot")
        self.assertRecord(records[1], source="zellij", subject="beta:pane:1", state="stopped", mode="snapshot")

    def test_no_session_fails(self):
        env = env_with()
        env.pop("ZELLIJ_SESSION_NAME", None)
        proc = subprocess.run(
            [sys.executable, str(PLUGINS / self.PLUGIN)],
            capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no session", proc.stderr)

    def test_missing_zellij_executable_fails(self):
        with TempState() as ts:
            env = env_with(
                PATH=str(ts.path("empty-bin")),
                ZELLIJ_SESSION_NAME="sess",
            )
            proc = subprocess.run(
                [sys.executable, str(PLUGINS / self.PLUGIN), "--session", "sess"],
                capture_output=True, text=True, env=env, timeout=SUBPROCESS_TIMEOUT,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("zellij executable not found", proc.stderr)

    def test_query_failure_fails(self):
        proc, recorded, _, err = self.run_with_fake([], session="s", rc=1, stderr="boom")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("zellij query failed", err)

    def test_non_json_output_fails(self):
        proc, _, _, err = self.run_with_fake([], output="{oops", session="s")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSON", err)

    def test_non_list_output_fails(self):
        proc, _, _, err = self.run_with_fake({"id": 1}, session="s")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a JSON array", err)

    def test_pane_without_integer_id_fails(self):
        proc, _, _, err = self.run_with_fake([{"id": "1", "exited": False}], session="s")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing integer 'id'", err)

    def test_pane_without_boolean_exited_fails(self):
        proc, _, _, err = self.run_with_fake([{"id": 1, "exited": "no"}], session="s")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing boolean 'exited'", err)

    def test_non_object_pane_fails(self):
        proc, _, _, err = self.run_with_fake([17], session="s")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("non-object pane entry", err)


# ---------------------------------------------------------------------------
# core CLI (notifier_ng.py)
# ---------------------------------------------------------------------------

class CoreCliTests(unittest.TestCase):
    """Contract tests for the ingest/transport/state CLI.

    Every scenario drives `notifier_ng.py` as a real subprocess: a config
    file on disk, NDJSON on stdin, a state file in a temp directory, and
    loopback capture servers standing in for the transports. Nothing here
    imports the core module or inspects repository text.
    """

    NTFY = {"type": "ntfy", "id": "ntfy-a"}
    HASS = {
        "type": "home_assistant",
        "id": "ha",
        "url_env": "HASS_URL",
        "token_env": "HASS_TOKEN",
        "service": "mobile_app_test_device",
    }

    @classmethod
    def setUpClass(cls):
        if not CORE.exists():
            raise AssertionError(
                f"{CORE} is missing; core behavior cannot be exercised. "
                "Land notifier_ng.py before running this suite."
            )
        if not (PLUGINS / "codex.py").exists():
            raise AssertionError("plugins/codex.py is missing; source tests cannot run")

    def _core_env(self, home, env_extra=None):
        """Isolated environment: temp HOME, python first on PATH, no ambient secrets."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("HASS_TOKEN", "HASS_URL", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
        }
        env["HOME"] = str(home)
        env["PATH"] = f"{Path(sys.executable).resolve().parent}:{env.get('PATH', '')}"
        for key, value in (env_extra or {}).items():
            env[key] = str(value)
        return env

    def _write_config(self, ts, config):
        cfg = ts.path("config.json")
        cfg.write_text(json.dumps(config), encoding="utf-8")
        state = ts.path("state.json")
        return cfg, state

    def _invoke(self, cfg, state, argv, stdin=b"", env=None, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(CORE), "--config", str(cfg), "--state", str(state), *argv],
            input=stdin,
            capture_output=True,
            env=env,
            cwd=cwd,
            timeout=SUBPROCESS_TIMEOUT,
        )
        return SimpleNamespace(
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
        )

    def run_ingest(self, ts, config, events, *, env_extra=None, home=None):
        """Run `ingest` with the given events on stdin; returns (proc, state_path)."""
        cfg, state = self._write_config(ts, config)
        stdin = b"".join(
            json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n" for event in events
        )
        proc = self._invoke(
            cfg,
            state,
            ["ingest"],
            stdin=stdin,
            env=self._core_env(home if home is not None else ts.root, env_extra),
            cwd=str(ts.root),
        )
        return proc, state

    def run_source(self, ts, config, plugin, *plugin_args, stdin=b"", env_extra=None, home=None):
        """Run `source <plugin> [args...]`; returns (proc, state_path)."""
        cfg, state = self._write_config(ts, config)
        proc = self._invoke(
            cfg,
            state,
            ["source", str(plugin), *plugin_args],
            stdin=stdin,
            env=self._core_env(home if home is not None else ts.root, env_extra),
            cwd=str(ts.root),
        )
        return proc, state

    @staticmethod
    def header(request, name):
        """Case-insensitive header lookup on a captured request."""
        lowered = name.lower()
        for key, value in request["headers"].items():
            if key.lower() == lowered:
                return value
        return None

    def assertSinglePost(self, server, *, title=None, body, tags=None, path="/"):
        """Assert the server captured exactly one POST with the given shape.

        Title/Tags are ntfy-specific wire headers; they are asserted only
        when the caller supplies them (Home Assistant carries a JSON body
        and no Title/Tags headers).
        """
        requests = server.requests
        self.assertEqual(len(requests), 1, requests)
        request = requests[0]
        self.assertEqual(request["method"], "POST", request)
        self.assertEqual(request["path"], path, request)
        if title is not None:
            self.assertEqual(self.header(request, "Title"), title, request)
        if tags is not None:
            self.assertEqual(self.header(request, "Tags"), tags, request)
        self.assertEqual(request["body"], body, request)
        return request

    # -- dedup / state machine ------------------------------------------------

    def test_first_idle_event_sends_once_and_duplicate_is_suppressed(self):
        event = {
            "version": 1, "source": "src", "subject": "subj-1", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "first idle",
        }
        with CaptureServer() as server, TempState() as ts:
            config = {"include_message_text": True, "transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertSinglePost(server, title="src: idle", body="first idle", tags="idle")

            # identical event again: durable dedup across processes
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, "duplicate must not re-notify")

    def test_active_event_rearms_next_idle(self):
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            idle1 = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                     "mode": "event", "event_id": "i1", "message": "first"}
            proc, _ = self.run_ingest(ts, config, [idle1])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1)

            proc, _ = self.run_ingest(ts, config, [idle1])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, "duplicate must be suppressed")

            active = {"version": 1, "source": "s", "subject": "k", "state": "active",
                      "mode": "event", "event_id": "a1"}
            proc, _ = self.run_ingest(ts, config, [active])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, "active in itself must not notify")

            idle2 = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                     "mode": "event", "event_id": "i2", "message": "re-armed"}
            proc, _ = self.run_ingest(ts, config, [idle2])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 2, "idle after active must notify")

    def test_snapshot_baseline_suppresses_then_active_to_idle_sends(self):
        baseline = {"version": 1, "source": "s", "subject": "k", "state": "active",
                    "mode": "snapshot", "timestamp": "2026-01-01T00:00:00.000+00:00"}
        with CaptureServer() as server, TempState() as ts:
            config = {"include_message_text": True, "transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, config, [baseline])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(server.requests, [], "first snapshot is a silent baseline")

            proc, _ = self.run_ingest(ts, config, [baseline])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(server.requests, [], "unchanged snapshot stays silent")

            idle = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                    "mode": "event", "event_id": "i9", "message": "now idle"}
            proc, _ = self.run_ingest(ts, config, [idle])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="now idle", tags="idle")

            proc, _ = self.run_ingest(ts, config, [idle])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, "duplicate idle must stay suppressed")

    def test_concurrent_identical_ingest_yields_single_request(self):
        event = (json.dumps(
            {"version": 1, "source": "c", "subject": "race-1", "state": "idle",
             "mode": "event", "event_id": "r1", "message": "race"}
        ) + "\n").encode("utf-8")
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            cfg, state = self._write_config(ts, config)
            cmd = [sys.executable, str(CORE), "--config", str(cfg),
                   "--state", str(state), "ingest"]
            env = self._core_env(ts.root)
            procs = [
                subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=env, cwd=str(ts.root),
                )
                for _ in range(2)
            ]
            results = [proc.communicate(input=event, timeout=SUBPROCESS_TIMEOUT) for proc in procs]
        for proc, (stdout, stderr) in zip(procs, results):
            self.assertEqual(proc.returncode, 0, stderr.decode())
            self.assertEqual(stdout.decode(), "")
        self.assertEqual(len(server.requests), 1, "concurrent duplicates must notify once")

    def test_partial_transport_failure_retries_only_failed_transport(self):
        failures = {"count": 0}

        def flaky_status(_request):
            failures["count"] += 1
            return 500 if failures["count"] <= 1 else 200

        event = {"version": 1, "source": "s", "subject": "retry-1", "state": "idle",
                 "mode": "event", "event_id": "rt1", "message": "deliver me"}
        with CaptureServer() as ok, CaptureServer(status=flaky_status) as broken, TempState() as ts:
            config = {
                "transports": [
                    {**self.NTFY, "id": "ntfy-ok", "url": ok.url},
                    {**self.NTFY, "id": "ntfy-broken", "url": broken.url},
                ]
            }
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 1, "transport failure must surface as exit 1")
            self.assertIn("ntfy-broken", proc.stderr)
            self.assertIn("500", proc.stderr)
            self.assertEqual(len(ok.requests), 1)
            self.assertEqual(len(broken.requests), 1)

            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
        self.assertEqual(len(ok.requests), 1, "healthy transport must not be re-contacted")
        self.assertEqual(len(broken.requests), 2, "failed transport must be retried")
        self.assertEqual(
            broken.requests[0]["body"], broken.requests[1]["body"],
            "retry must carry the identical payload",
        )

    # -- optional summarizer ---------------------------------------------------

    @staticmethod
    def summarizer_config(command, **overrides):
        value = {
            "command": [str(command)],
            "timeout_seconds": 2,
            "last_items": 6,
            "max_item_chars": 1200,
            "max_context_chars": 5000,
            "max_summary_chars": 450,
            "max_summary_output_bytes": 4096,
            "states": ["idle"],
        }
        value.update(overrides)
        return value

    def test_summarizer_success_replaces_body_and_never_persists_context(self):
        fake = """\
import json, os, sys
request = json.load(sys.stdin)
open(os.environ["SUMMARY_REQUEST"], "w").write(json.dumps(request))
print(json.dumps({"version": 1, "summary": "Implemented the notifier summary and verified it."}))
"""
        event = {
            "version": 1, "source": "omp", "subject": "s", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "fallback secret-body",
            "context": {"items": [
                {"role": "user", "text": "Build the summarizer"},
                {"role": "assistant", "text": "Implemented and tested it"},
            ]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            request_path = ts.path("request.json")
            config = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, state = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_REQUEST": request_path})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="omp: idle", body="Summary: Implemented the notifier summary and verified it.", tags="idle")
            request = json.loads(request_path.read_text())
            self.assertEqual(set(request), {"version", "source", "state", "context", "max_summary_chars"})
            state_text = state.read_text()
            self.assertNotIn("Build the summarizer", state_text)
            self.assertNotIn("Implemented and tested it", state_text)
            self.assertIn('"summary_status":"success"', state_text)

    def test_summarizer_failure_is_cached_and_fallback_reused(self):
        fake = """\
import os, sys
with open(os.environ["SUMMARY_COUNT"], "a") as f: f.write("1\\n")
sys.exit(1)
"""
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "plain fallback",
            "context": {"items": [{"role": "assistant", "text": "done"}]},
        }
        with CaptureServer() as good, CaptureServer(status=500) as bad, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            count = ts.path("count")
            config = {
                "allow_remote_context": True,
                "include_message_text": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [
                    {**self.NTFY, "id": "good", "url": good.url},
                    {**self.NTFY, "id": "bad", "url": bad.url},
                ],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(count.read_text().splitlines(), ["1"])
            self.assertEqual(good.requests[0]["body"], "plain fallback")
            self.assertEqual(bad.requests[0]["body"], "plain fallback")

    def test_summarizer_timeout_kills_process_group_and_falls_back(self):
        fake = """\
import os, pathlib, sys, time
path = pathlib.Path(os.environ["SUMMARY_SENTINEL"])
pid = os.fork()
if pid == 0:
    time.sleep(2)
    path.write_text("orphan")
    os._exit(0)
sys.stdin.buffer.read()
time.sleep(10)
"""
        event = {
            "version": 1, "source": "s", "subject": "timeout", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "fast fallback",
            "context": {"items": [{"role": "user", "text": "work"}]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            sentinel = ts.path("sentinel")
            config = {
                "allow_remote_context": True,
                "include_message_text": True,
                "summarizer": self.summarizer_config(summarizer, timeout_seconds=1),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            started = time.monotonic()
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_SENTINEL": sentinel})
            elapsed = time.monotonic() - started
            time.sleep(2.2)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLess(elapsed, 2.5)
            self.assertFalse(sentinel.exists())
            self.assertSinglePost(server, title="s: idle", body="fast fallback", tags="idle")

    def test_summarizer_config_and_context_are_strict(self):
        valid_event = {
            "version": 1, "source": "s", "subject": "x", "state": "idle", "mode": "event",
            "context": {"items": [{"role": "assistant", "text": "done"}]},
        }
        ntfy = {**self.NTFY, "url": "http://127.0.0.1:9"}
        cases = [
            ({"command": "bad"}, "command must be a non-empty array"),
            ({"command": ["x"], "timeout_seconds": 0}, "timeout_seconds"),
            ({"command": ["x"], "states": ["active"]}, "states must contain only"),
            ({"command": ["x"], "max_summary_chars": 1000, "max_summary_output_bytes": 1024}, "at least 4 *"),
        ]
        for summarizer, marker in cases:
            with self.subTest(summarizer=summarizer):
                with TempState() as ts:
                    proc, _ = self.run_ingest(ts, {"summarizer": summarizer, "transports": [ntfy]}, [valid_event])
                self.assertEqual(proc.returncode, 2)
                self.assertIn(marker, proc.stderr)
        invalid_events = [
            ({**valid_event, "context": []}, "context must be an object"),
            ({**valid_event, "context": {"items": [{"role": "tool", "text": "x"}]}}, "role must be user or assistant"),
            ({**valid_event, "context": {"items": [{"role": "user", "text": ""}]}}, "non-empty string"),
        ]
        for event, marker in invalid_events:
            with self.subTest(event=event):
                with TempState() as ts:
                    proc, _ = self.run_ingest(ts, {"transports": [ntfy]}, [event])
                self.assertEqual(proc.returncode, 2)
                self.assertIn(marker, proc.stderr)
    # -- privacy defaults & explicit opt-ins -----------------------------------

    def test_default_config_sends_status_only_and_skips_summarizer(self):
        fake = """\
import os, sys
with open(os.environ["SUMMARY_COUNT"], "a") as f: f.write("1\\n")
sys.exit(1)
"""
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "private message body",
            "context": {"items": [{"role": "assistant", "text": "private context words"}]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            count = ts.path("count")
            config = {
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="k is idle", tags="idle")
            self.assertFalse(count.exists(), "summarizer must be skipped without allow_remote_context")

    def test_include_message_text_opt_in_restores_message_body(self):
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "task result body",
        }
        with CaptureServer() as server, TempState() as ts:
            config = {
                "include_message_text": True,
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="task result body", tags="idle")

    def test_privacy_flags_reject_wrong_types(self):
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle", "mode": "event"}
        cases = [
            ("include_message_text number", {"include_message_text": 1}),
            ("include_message_text string", {"include_message_text": "true"}),
            ("allow_remote_context number", {"allow_remote_context": 0}),
            ("allow_remote_context list", {"allow_remote_context": []}),
        ]
        for name, flag in cases:
            with self.subTest(case=name):
                with TempState() as ts:
                    config = {**flag, "transports": [{**self.NTFY, "url": "http://127.0.0.1:9"}]}
                    proc, _ = self.run_ingest(ts, config, [event])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("must be a boolean", proc.stderr)
                self.assertIn(next(iter(flag)), proc.stderr)

    def test_explicit_null_privacy_flags_fail(self):
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle", "mode": "event"}
        flags = [
            {"include_message_text": None},
            {"allow_remote_context": None},
        ]
        for flag in flags:
            with self.subTest(flag=next(iter(flag))):
                with TempState() as ts:
                    config = {**flag, "transports": [{**self.NTFY, "url": "http://127.0.0.1:9"}]}
                    proc, _ = self.run_ingest(ts, config, [event])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("must be a boolean", proc.stderr)
                self.assertIn(next(iter(flag)), proc.stderr)

    def test_summary_failure_falls_back_status_only_without_message_opt_in(self):
        fake = """\
import os, sys
with open(os.environ["SUMMARY_COUNT"], "a") as f: f.write("1\\n")
sys.exit(1)
"""
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "raw fallback text",
            "context": {"items": [{"role": "user", "text": "work"}]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            count = ts.path("count")
            config = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="k is idle", tags="idle")
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, "failed summary must be cached")
            self.assertEqual(count.read_text().splitlines(), ["1"])

    def test_summarizer_redirect_falls_back_status_only(self):
        adapter = ROOT / "summarizers" / "openai_compatible.py"
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "raw fallback text",
            "context": {"items": [{"role": "user", "text": "work"}]},
        }
        summarizer = {
            "command": [sys.executable, str(adapter)],
            "timeout_seconds": 10,
            "last_items": 6,
            "max_item_chars": 1200,
            "max_context_chars": 5000,
            "max_summary_chars": 450,
            "max_summary_output_bytes": 4096,
            "states": ["idle"],
        }
        with CaptureServer() as target, CaptureServer(status=302, location=target.url) as redirector, CaptureServer() as server, TempState() as ts:
            config = {
                "allow_remote_context": True,
                "summarizer": summarizer,
                "transports": [{**self.NTFY, "url": server.url}],
            }
            env = {
                "NOTIFIER_LLM_BASE_URL": f"http://127.0.0.1:{redirector.port}",
                "NOTIFIER_LLM_MODEL": "test-model",
                "NOTIFIER_LLM_API_KEY": "secret-key",
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="k is idle", tags="idle")
            self.assertEqual(len(redirector.requests), 1, redirector.requests)
            self.assertEqual(redirector.requests[0]["headers"].get("Authorization"), "Bearer secret-key")
            self.assertEqual(target.requests, [], "redirect target must not receive the request")

    def test_cache_identity_reacts_to_privacy_flag_change(self):
        fake = """\
import os, sys
path = os.environ["SUMMARY_COUNT"]
n = len(open(path).read().splitlines()) + 1 if os.path.exists(path) else 1
with open(path, "a") as f: f.write(str(n) + "\\n")
sys.exit(1)
"""
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "payload v1",
            "context": {"items": [{"role": "user", "text": "work"}]},
        }
        with CaptureServer(status=500) as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            count = ts.path("count")
            base = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(
                ts, {**base, "include_message_text": True}, [event], env_extra={"SUMMARY_COUNT": count}
            )
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, base, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, base, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(
                [request["body"] for request in server.requests],
                ["payload v1", "k is idle", "k is idle"],
            )
            self.assertEqual(
                count.read_text().splitlines(), ["1", "2"],
                "changing include_message_text must invalidate the cached summarizer decision",
            )

    def test_cache_identity_reacts_to_remote_context_toggle(self):
        fake = """\
import os, sys
path = os.environ["SUMMARY_COUNT"]
n = len(open(path).read().splitlines()) + 1 if os.path.exists(path) else 1
with open(path, "a") as f: f.write(str(n) + "\\n")
sys.exit(1)
"""
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "payload v1",
            "context": {"items": [{"role": "user", "text": "work"}]},
        }
        with CaptureServer(status=500) as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            count = ts.path("count")
            full = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            status_only = {"transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, full, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, status_only, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, full, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            proc, _ = self.run_ingest(ts, full, [event], env_extra={"SUMMARY_COUNT": count})
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(
                count.read_text().splitlines(), ["1", "2"],
                "re-enabling allow_remote_context must invalidate the cached summarizer decision",
            )
    # -- delivery policy identity ----------------------------------------------

    def test_privacy_toggle_redelivers_same_fingerprint(self):
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "now with body",
        }
        with CaptureServer() as server, TempState() as ts:
            default = {"transports": [{**self.NTFY, "url": server.url}]}
            opted_in = {"include_message_text": True, "transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, default, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="k is idle", tags="idle")
            proc, _ = self.run_ingest(ts, opted_in, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 2, "flag toggle must re-deliver the same fingerprint")
            self.assertEqual(server.requests[1]["body"], "now with body")
            proc, _ = self.run_ingest(ts, opted_in, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 2, "stable flags must keep suppressing duplicates")

    def test_legacy_deliveries_not_redelivered_under_default_policy(self):
        event = {
            "version": 1, "source": "s", "subject": "k", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "pre-existing",
        }
        with CaptureServer() as server, TempState() as ts:
            cfg, state = self._write_config(ts, {"transports": [{**self.NTFY, "url": server.url}]})
            state.write_text(json.dumps({
                "version": 1,
                "subjects": {"s\u0000k": {"fingerprint": "idle\u0000e1", "delivered": ["ntfy-a"]}},
            }), encoding="utf-8")
            proc = self._invoke(
                cfg, state, ["ingest"],
                stdin=(json.dumps(event) + "\n").encode("utf-8"),
                env=self._core_env(ts.root), cwd=str(ts.root),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(server.requests, [], "legacy deliveries must not duplicate under default policy")


    # -- content redaction before remote sinks --------------------------------

    def test_credentials_redacted_from_message_before_transport(self):
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDoT9ffM2rK\n"
            "7xYtV7f6Qn8j9mF0iLkFcNwZv9xUyLp2QG5sRzZkAxBqWtD4eHk3s8m1nT\n"
            "-----END PRIVATE KEY-----\n"
        )
        message = (
            "Deployed the fix; the test suite passes.\n"
            "Authorization: Bearer sk-secret-abc\n"
            "Cert material:\n" + pem +
            "OPENAI_API_KEY=sk-org-12345\n"
            "GITHUB_TOKEN=ghp_abcdef123456\n"
            "DB_PASSWORD=hunter2\n"
            "SESSION_COOKIE=abcd1234\n"
            "BASIC_AUTH=dXNlcjpwYXNz\n"
            "plain note: keep this line verbatim"
        )
        with CaptureServer() as server, TempState() as ts:
            event = {
                "version": 1, "source": "s", "subject": "creds", "state": "idle",
                "mode": "event", "event_id": "c1", "message": message,
            }
            config = {
                "include_message_text": True,
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, server.requests)
            body = server.requests[0]["body"]
            self.assertNotIn("sk-secret-abc", body)
            self.assertNotIn("sk-org-12345", body)
            self.assertNotIn("ghp_abcdef123456", body)
            self.assertNotIn("hunter2", body)
            self.assertNotIn("abcd1234", body)
            self.assertNotIn("dXNlcjpwYXNz", body)
            self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEF", body)
            self.assertIn("Deployed the fix; the test suite passes.", body)
            self.assertIn("plain note: keep this line verbatim", body)
            self.assertIn("Authorization: Bearer [redacted]", body)
            self.assertIn("OPENAI_API_KEY=[redacted]", body)
            self.assertIn("GITHUB_TOKEN=[redacted]", body)
            self.assertIn("DB_PASSWORD=[redacted]", body)
            self.assertIn("SESSION_COOKIE=[redacted]", body)
            self.assertIn("BASIC_AUTH=[redacted]", body)
            self.assertIn("-----BEGIN PRIVATE KEY-----", body)
            self.assertIn("-----END PRIVATE KEY-----", body)

    def test_context_credentials_redacted_before_summarizer_request(self):
        fake = """\
import json, os, sys
request = json.load(sys.stdin)
open(os.environ["SUMMARY_REQUEST"], "w").write(json.dumps(request))
print(json.dumps({"version": 1, "summary": "Work completed and verified."}))
"""
        event = {
            "version": 1, "source": "omp", "subject": "s", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "fallback",
            "context": {"items": [
                {"role": "user",
                 "text": "Set OPENAI_API_KEY=sk-leak-99 and GITHUB_TOKEN=ghp_leak in the config; run the deployment."},
                {"role": "assistant",
                 "text": "Done; Authorization: Bearer sk-leak-99 worked "
                         "and the key was saved as "
                         "-----BEGIN EC PRIVATE KEY----- MIIBcA==Q83 -----END EC PRIVATE KEY----- for later."},
            ]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            request_path = ts.path("request.json")
            config = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_REQUEST": request_path})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="omp: idle", body="Summary: Work completed and verified.", tags="idle")
            request = json.loads(request_path.read_text())
            items_text = " ".join(item["text"] for item in request["context"]["items"])
            self.assertNotIn("sk-leak-99", items_text)
            self.assertNotIn("ghp_leak", items_text)
            self.assertNotIn("MIIBcA==", items_text)
            self.assertIn("OPENAI_API_KEY=[redacted]", items_text)
            self.assertIn("GITHUB_TOKEN=[redacted]", items_text)
            self.assertIn("Authorization: Bearer [redacted]", items_text)
            self.assertIn("-----BEGIN EC PRIVATE KEY-----", items_text)
            self.assertIn("-----END EC PRIVATE KEY-----", items_text)
            self.assertIn("run the deployment", items_text)
            self.assertIn("the key was saved as", items_text)

    def test_api_key_spellings_redacted_from_message_before_transport(self):
        message = (
            "Deploy notes:\n"
            "API_KEY=sk-one-11111\n"
            "API-KEY=sk-two-22222\n"
            "apiKey=sk-three-33333\n"
            "apikey=sk-four-44444\n"
            "X-API-Key: sk-five-55555\n"
            "x-api-key=sk-six-66666\n"
        )
        with CaptureServer() as server, TempState() as ts:
            event = {
                "version": 1, "source": "s", "subject": "apikeys", "state": "idle",
                "mode": "event", "event_id": "ak1", "message": message,
            }
            config = {
                "include_message_text": True,
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1, server.requests)
            body = server.requests[0]["body"]
            for secret in ("sk-one-11111", "sk-two-22222", "sk-three-33333",
                           "sk-four-44444", "sk-five-55555", "sk-six-66666"):
                self.assertNotIn(secret, body)
            for redacted in ("API_KEY=[redacted]", "API-KEY=[redacted]", "apiKey=[redacted]",
                             "apikey=[redacted]", "X-API-Key: [redacted]", "x-api-key=[redacted]"):
                self.assertIn(redacted, body)

    def test_api_key_spellings_redacted_from_context_before_summarizer(self):
        fake = """\
import json, os, sys
request = json.load(sys.stdin)
open(os.environ["SUMMARY_REQUEST"], "w").write(json.dumps(request))
print(json.dumps({"version": 1, "summary": "Work completed and verified."}))
"""
        context_text = (
            "Set API_KEY=sk-ctx-one, API-KEY=sk-ctx-two, apiKey=sk-ctx-three, "
            "apikey=sk-ctx-four, X-API-Key: sk-ctx-five and x-api-key=sk-ctx-six "
            "before deploying."
        )
        event = {
            "version": 1, "source": "omp", "subject": "s", "state": "idle",
            "mode": "event", "event_id": "e1", "message": "fallback",
            "context": {"items": [{"role": "user", "text": context_text}]},
        }
        with CaptureServer() as server, TempState() as ts:
            summarizer = write_script(ts.path("summary.py"), fake)
            request_path = ts.path("request.json")
            config = {
                "allow_remote_context": True,
                "summarizer": self.summarizer_config(summarizer),
                "transports": [{**self.NTFY, "url": server.url}],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"SUMMARY_REQUEST": request_path})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="omp: idle", body="Summary: Work completed and verified.", tags="idle")
            request = json.loads(request_path.read_text())
            items_text = " ".join(item["text"] for item in request["context"]["items"])
            for secret in ("sk-ctx-one", "sk-ctx-two", "sk-ctx-three",
                           "sk-ctx-four", "sk-ctx-five", "sk-ctx-six"):
                self.assertNotIn(secret, items_text)
            for redacted in ("API_KEY=[redacted]", "API-KEY=[redacted]", "apiKey=[redacted]",
                             "apikey=[redacted]", "X-API-Key: [redacted]", "x-api-key=[redacted]"):
                self.assertIn(redacted, items_text)

    def test_transport_error_body_reflected_api_key_is_redacted(self):
        cases = [
            json.dumps({"error": "invalid credential", "apiKey": "sk-reflected-42"}),
            json.dumps({"error": "invalid credential", "X-API-Key": "sk-reflected-43"}),
            "X-API-Key: sk-reflected-44\nhint: check your key spelling",
        ]
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        for body in cases:
            with self.subTest(body=body):
                with CaptureServer(status=401, body=body) as server, TempState() as ts:
                    config = {"transports": [{**self.NTFY, "url": server.url}]}
                    proc, _ = self.run_ingest(ts, config, [event])
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("401", proc.stderr)
                self.assertIn("[redacted]", proc.stderr)
                for secret in ("sk-reflected-42", "sk-reflected-43", "sk-reflected-44"):
                    self.assertNotIn(secret, proc.stderr)
    def test_corrupt_state_fails_without_overwrite(self):
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            cfg, state = self._write_config(ts, {"transports": [{**self.NTFY, "url": server.url}]})
            state.write_text("{ definitely not json", encoding="utf-8")
            before = state.read_bytes()
            proc = self._invoke(
                cfg, state, ["ingest"],
                stdin=(json.dumps(event) + "\n").encode(),
                env=self._core_env(ts.root),
                cwd=str(ts.root),
            )
            after = state.read_bytes()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("corrupt", proc.stderr)
        self.assertEqual(after, before, "corrupt state must be left untouched")
        self.assertEqual(server.requests, [], "no delivery may occur on corrupt state")

    # -- strict parsing -------------------------------------------------------

    def test_invalid_event_line_rejected_without_delivery(self):
        with CaptureServer() as server, TempState() as ts:
            cfg, state = self._write_config(ts, {"transports": [{**self.NTFY, "url": server.url}]})
            proc = self._invoke(
                cfg, state, ["ingest"], stdin=b"{oops not json\n",
                env=self._core_env(ts.root), cwd=str(ts.root),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("stdin line 1 is not valid JSON", proc.stderr)
            self.assertFalse(state.exists(), "parse failure must not touch state")
        self.assertEqual(server.requests, [])

    def test_strict_event_parsing_rejects_invalid_records(self):
        valid = {"version": 1, "source": "s", "subject": "x", "state": "idle", "mode": "event"}
        cases = [
            ("unknown key", {**valid, "bogus": 1}, "unknown keys"),
            ("version not 1", {**valid, "version": 2}, "must equal 1"),
            ("missing source", {**valid, "source": None}, "source must be a non-empty string"),
            ("empty source", {**valid, "source": ""}, "source must be a non-empty string"),
            ("unknown state", {**valid, "state": "paused"}, "state must be one of"),
            ("unknown mode", {**valid, "mode": "pulse"}, "mode must be one of"),
            ("metadata not object", {**valid, "metadata": []}, "metadata must be an object"),
            ("empty event_id", {**valid, "event_id": ""}, "non-empty string"),
            ("non-object record", [42], "must be a JSON object"),
        ]
        for name, event, marker in cases:
            with self.subTest(case=name):
                with TempState() as ts:
                    proc, _ = self.run_ingest(
                        ts, {"transports": [{**self.NTFY, "url": "http://127.0.0.1:9"}]}, [event]
                    )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(marker, proc.stderr)

    def test_strict_config_parsing_rejects_invalid_configs(self):
        ntfy = {"type": "ntfy", "id": "n", "url": "http://127.0.0.1:9"}
        ha = {"type": "home_assistant", "id": "h", "url": "http://127.0.0.1:9",
              "service": "mobile_app_test_device", "token_env": "HASS_TOKEN"}
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle", "mode": "event"}
        cases = [
            ("non-object config",  [], "config must be a JSON object"),
            ("unknown top-level key",  {"transports": [ntfy], "extra": 1}, "unknown keys"),
            ("transports not array",  {"transports": {}}, "non-empty array"),
            ("empty transports",  {"transports": []}, "non-empty array"),
            ("unknown transport type",  {"transports": [{**ntfy, "type": "slack"}]},
             "must be ntfy or home_assistant"),
            ("transport unknown key",  {"transports": [{**ntfy, "topic": "t"}]}, "unknown keys"),
            ("url and url_env both",  {"transports": [{**ntfy, "url_env": "HASS_URL"}]},
             "only one of url or url_env"),
            ("no url",  {"transports": [{"type": "ntfy"}]}, "must be a non-empty string"),
            ("non-http scheme",  {"transports": [{**ntfy, "url": "ftp://host/x"}]},
             "absolute http(s) URL"),
            ("relative url",  {"transports": [{**ntfy, "url": "/api/x"}]}, "absolute http(s) URL"),
            ("url_env unset",  {"transports": [{"type": "ntfy", "id": "n", "url_env": "MISSING_URL"}]},
             "url_env names unset variable"),
            ("ha missing service",  {"transports": [{**ha, "service": None}]}, "service"),
            ("ha missing token_env",  {"transports": [ha | {"token_env": None}]},
             "token_env is required"),
            ("duplicate transport ids",  {"transports": [
                {"type": "ntfy", "id": "dup", "url": "http://127.0.0.1:9"},
                {"type": "ntfy", "id": "dup", "url": "http://127.0.0.1:8"},
            ]}, "duplicate transport id"),
        ]
        for name, config, marker in cases:
            with self.subTest(case=name):
                with TempState() as ts:
                    proc, _ = self.run_ingest(ts, config, [event])
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(marker, proc.stderr)

        # raw non-JSON file content, not a JSON-wrapped value
        with self.subTest(case="invalid config json"):
            with TempState() as ts:
                cfg, state = self._write_config(ts, {"transports": []})
                cfg.write_text("{{broken", encoding="utf-8")
                proc = self._invoke(
                    cfg, state, ["ingest"],
                    stdin=(json.dumps(event) + "\n").encode("utf-8"),
                    env=self._core_env(ts.root), cwd=str(ts.root),
                )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("not valid JSON", proc.stderr)

    def test_config_accepts_env_file_and_env_files_forms(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        for key in ("env_file", "env_files"):
            with self.subTest(config_field=key):
                with CaptureServer() as server, TempState() as ts:
                    env_file = ts.path("secrets.env")
                    env_file.write_text(
                        f"HASS_URL={server.url}\nHASS_TOKEN=file-token\n", encoding="utf-8"
                    )
                    config = {key: (str(env_file) if key == "env_file" else [str(env_file)])}
                    config["transports"] = [{**self.HASS, "url_env": "HASS_URL"}]
                    proc, _ = self.run_ingest(ts, config, [event])
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(len(server.requests), 1, config)
                    self.assertEqual(
                        self.header(server.requests[0], "Authorization"), "Bearer file-token"
                    )

    # -- transport wire shapes -------------------------------------------------

    def test_ntfy_request_method_headers_body(self):
        event = {"version": 1, "source": "testsrc", "subject": "alice", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            request = self.assertSinglePost(
                server, title="testsrc: idle", body="alice is idle", tags="idle"
            )
            self.assertTrue(
                (self.header(request, "Content-Type") or "").startswith("text/plain"),
                request,
            )
            self.assertIsNone(self.header(request, "Authorization"), request)

    def test_homeassistant_request_url_auth_json_body(self):
        event = {"version": 1, "source": "hassrc", "subject": "hallway", "state": "idle",
                 "mode": "event", "event_id": "h1"}
        with CaptureServer() as server, TempState() as ts:
            env_file = ts.path("home/.hermes/.env")
            env_file.parent.mkdir(parents=True)
            env_file.write_text(f"HASS_URL={server.url}\nHASS_TOKEN=sekrit-token-42\n", encoding="utf-8")
            config = {
                "env_files": ["~/.hermes/.env"],
                "transports": [{**self.HASS, "url_env": "HASS_URL"}],
            }
            proc, state = self.run_ingest(ts, config, [event], home=ts.path("home"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            requests = server.requests
            self.assertEqual(len(requests), 1, requests)
            request = requests[0]
            self.assertEqual(request["method"], "POST", request)
            self.assertEqual(
                request["path"], "/api/services/notify/mobile_app_test_device", request
            )
            self.assertEqual(self.header(request, "Authorization"), "Bearer sekrit-token-42")
            self.assertEqual(self.header(request, "Content-Type"), "application/json")
            self.assertEqual(
                json.loads(request["body"]),
                {"title": "hassrc: idle", "message": "hallway is idle"},
            )
            self.assertTrue(state.exists(), "delivery must be recorded in state")

    # -- env files ------------------------------------------------------------

    def test_env_file_secret_not_persisted_or_printed(self):
        event = {"version": 1, "source": "s", "subject": "hallway", "state": "idle",
                 "mode": "event", "event_id": "h1"}
        with CaptureServer() as server, TempState() as ts:
            env_file = ts.path("home/.hermes/.env")
            env_file.parent.mkdir(parents=True)
            env_file.write_text(f"HASS_URL={server.url}\nHASS_TOKEN=sekrit-token-42\n", encoding="utf-8")
            config = {
                "env_files": ["~/.hermes/.env"],
                "transports": [{**self.HASS, "url_env": "HASS_URL"}],
            }
            proc, state = self.run_ingest(ts, config, [event], home=ts.path("home"))
            state_text = state.read_text(encoding="utf-8")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout + proc.stderr, "", "secrets must not be echoed")
            self.assertNotIn("sekrit-token-42", state_text, "token leaked into state")
            self.assertEqual(
                json.loads(state_text)["subjects"],
                {"s\u0000hallway": {"fingerprint": "idle\u0000h1", "delivered": ["ha"]}},
            )

    def test_env_file_does_not_override_existing_environment(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            env_file = ts.path("vars.env")
            env_file.write_text(f"HASS_URL={server.url}\nHASS_TOKEN=file-token\n", encoding="utf-8")
            config = {
                "env_files": [str(env_file)],
                "transports": [{**self.HASS, "url_env": "HASS_URL"}],
            }
            proc, _ = self.run_ingest(ts, config, [event], env_extra={"HASS_TOKEN": "shell-token"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            request = server.requests[0]
            self.assertEqual(self.header(request, "Authorization"), "Bearer shell-token")

    def test_env_file_syntax_and_missing_file_fail(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle", "mode": "event"}
        with TempState() as ts:
            bad_lines = ts.path("bad.env")
            bad_lines.write_text("NOEQUALS\n", encoding="utf-8")
            bad_name = ts.path("bad-name.env")
            bad_name.write_text("1KEY=value\n", encoding="utf-8")
            cases = [
                ("malformed line", {"env_files": [str(bad_lines)]}, "expected KEY=VALUE"),
                ("invalid key name", {"env_files": [str(bad_name)]},
                 "invalid environment variable name"),
                ("missing env file", {"env_files": ["does-not-exist.env"]},
                 "cannot read env file"),
            ]
            for name, fragment, marker in cases:
                with self.subTest(case=name):
                    config = dict(fragment)
                    config["transports"] = [{**self.NTFY, "url": "http://127.0.0.1:9"}]
                    proc, _ = self.run_ingest(ts, config, [event])
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn(marker, proc.stderr)

    # -- source plugins ---------------------------------------------------------

    FAKE_SOURCE = """\
import json, os, sys
raw = sys.stdin.buffer.read()
with open(os.environ["FAKE_SRC_INPUT"], "wb") as f:
    f.write(raw)
print(json.dumps({
    "version": 1, "source": "fakesrc", "subject": "sk-1", "state": "idle",
    "mode": "event", "event_id": "fe-1", "message": "forward this",
    "metadata": {"origin": "fake"},
}))
"""

    FAKE_SOURCE_FAILING = """\
import sys
sys.stderr.write("kaput\\n")
sys.exit(1)
"""

    def test_source_plugin_executable_forwarding(self):
        with CaptureServer() as server, TempState() as ts:
            fake = write_script(ts.path("fake-src.py"), self.FAKE_SOURCE)
            config = {"include_message_text": True, "transports": [{**self.NTFY, "url": server.url}]}
            stdin_input = b"raw stdin bytes\\n"
            proc, state = self.run_source(
                ts, config, fake, "an-argument",
                stdin=stdin_input,
                env_extra={"FAKE_SRC_INPUT": str(ts.path("plugin-received.bin"))},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                ts.path("plugin-received.bin").read_bytes(), stdin_input,
                "plugin stdin must be forwarded verbatim",
            )
            self.assertSinglePost(server, title="fakesrc: idle", body="forward this", tags="idle")
            subjects = json.loads(state.read_text(encoding="utf-8"))["subjects"]
            self.assertEqual(set(subjects), {"fakesrc\u0000sk-1"})
            self.assertEqual(subjects["fakesrc\u0000sk-1"]["fingerprint"], "idle\u0000fe-1")
            self.assertEqual(subjects["fakesrc\u0000sk-1"]["delivered"], ["ntfy-a"])
            self.assertIsInstance(subjects["fakesrc\u0000sk-1"]["delivery_policy_hash"], str)

    def test_source_plugin_failure_propagates_without_delivery(self):
        with CaptureServer() as server, TempState() as ts:
            failing = write_script(ts.path("failing-src.py"), self.FAKE_SOURCE_FAILING)
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_source(ts, config, failing)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("source plugin", proc.stderr)
            self.assertIn("exited 1", proc.stderr)
            self.assertIn("kaput", proc.stderr)
            self.assertEqual(server.requests, [], "no delivery on source failure")

            missing = ts.path("no-such-plugin.py")
            proc, _ = self.run_source(ts, config, missing)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("source plugin not found", proc.stderr)

    def test_source_codex_argv_payload_normalization(self):
        payload = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "thr-9",
            "turn-id": "tn-3",
            "last-assistant-message": "ship it",
        })
        with CaptureServer() as server, TempState() as ts:
            config = {"include_message_text": True, "transports": [{**self.NTFY, "url": server.url}]}
            proc, state = self.run_source(ts, config, PLUGINS / "codex.py", payload)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="Codex turn complete", body="Result: ship it", tags="idle")
            subjects = json.loads(state.read_text(encoding="utf-8"))["subjects"]
            self.assertIn("codex\u0000thr-9", subjects)
            self.assertEqual(subjects["codex\u0000thr-9"]["fingerprint"], "idle\u0000thr-9:tn-3")

    def test_source_codex_stdin_payload_normalization(self):
        hook_payload = json.dumps({
            "hook_event_name": "Stop",
            "session_id": "session-5",
            "turn_id": "turn-2",
        })
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            proc, state = self.run_source(
                ts, config, PLUGINS / "codex.py", stdin=hook_payload.encode("utf-8")
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(
                server, title="Codex turn complete", body="session-5 is idle", tags="idle"
            )
            subjects = json.loads(state.read_text(encoding="utf-8"))["subjects"]
            self.assertIn("codex\u0000session-5", subjects)
            self.assertEqual(
                subjects["codex\u0000session-5"]["fingerprint"], "idle\u0000session-5:turn-2"
            )

    def test_missing_token_value_fails_transport(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            env_file = ts.path("home/.hermes/.env")
            env_file.parent.mkdir(parents=True)
            env_file.write_text(f"HASS_URL={server.url}\n", encoding="utf-8")
            config = {
                "env_files": ["~/.hermes/.env"],
                "transports": [{**self.HASS, "url_env": "HASS_URL"}],
            }
            proc, _ = self.run_ingest(ts, config, [event], home=ts.path("home"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("requires environment variable HASS_TOKEN", proc.stderr)
            self.assertEqual(server.requests, [])

    # -- endpoint transport security -----------------------------------------

    def test_https_transport_allowed_by_default(self):
        config = {"transports": [{**self.NTFY, "url": "https://ntfy.example.invalid/notifier-ng"}]}
        with TempState() as ts:
            proc, _ = self.run_ingest(ts, config, [])
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_loopback_urls_allowed_by_policy(self):
        for url in (
            "http://127.0.0.1:9",
            "http://127.0.0.2:9",
            "http://[::1]:9",
            "http://localhost:9",
            "http://LOCALHOST.:9",
        ):
            with self.subTest(url=url):
                with TempState() as ts:
                    proc, _ = self.run_ingest(
                        ts, {"transports": [{**self.NTFY, "url": url}]}, []
                    )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_loopback_http_transport_delivers_by_default(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": f"http://localhost:{server.port}"}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="x is idle", tags="idle")

    def test_loopback_ipv6_transport_delivers_by_default(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        try:
            server = CaptureServer(host="::1")
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        with server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": server.url}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="x is idle", tags="idle")

    def test_transport_redirect_not_followed_with_bearer_token(self):
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as target, CaptureServer(status=302, location=target.url) as redirector, TempState() as ts:
            config = {
                "transports": [{**self.NTFY, "url": redirector.url, "token_env": "NTFY_TOKEN"}],
            }
            env = {"NTFY_TOKEN": "bearer-secret"}
            proc, _ = self.run_ingest(ts, config, [event], env_extra=env)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("transport ntfy-a returned HTTP 302", proc.stderr)
            self.assertEqual(len(redirector.requests), 1, redirector.requests)
            self.assertEqual(self.header(redirector.requests[0], "Authorization"), "Bearer bearer-secret")
            self.assertEqual(target.requests, [], "redirect target must not receive the request")

            # a redirected transport is a failed delivery: the next event must retry it
            proc, _ = self.run_ingest(ts, config, [event], env_extra=env)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(len(redirector.requests), 2, redirector.requests)
            self.assertEqual(target.requests, [], "redirect target must never receive the request")

    def test_transport_redirect_not_followed_without_token(self):
        event = {"version": 1, "source": "s", "subject": "k", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as target, CaptureServer(status=302, location=target.url) as redirector, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": redirector.url}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("transport ntfy-a returned HTTP 302", proc.stderr)
            self.assertEqual(len(redirector.requests), 1, redirector.requests)
            self.assertEqual(self.header(redirector.requests[0], "Authorization"), None)
            self.assertEqual(target.requests, [], "redirect target must not receive the request")

    def test_remote_http_rejected_without_opt_in(self):
        for url in ("http://0.0.0.0:9", "http://example.com/topic"):
            with self.subTest(url=url):
                with TempState() as ts:
                    proc, _ = self.run_ingest(
                        ts, {"transports": [{**self.NTFY, "url": url}]}, []
                    )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("non-loopback host", proc.stderr)
                self.assertIn("allow_insecure_http", proc.stderr)

    def test_remote_http_without_opt_in_never_contacts_host(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{**self.NTFY, "url": f"http://0.0.0.0:{server.port}"}]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("allow_insecure_http", proc.stderr)
            self.assertEqual(server.requests, [], "remote cleartext must not be used accidentally")

    def test_remote_http_explicit_opt_in_delivers(self):
        event = {"version": 1, "source": "s", "subject": "x", "state": "idle",
                 "mode": "event", "event_id": "e1"}
        with CaptureServer() as server, TempState() as ts:
            config = {"transports": [{
                **self.NTFY, "url": f"http://0.0.0.0:{server.port}",
                "allow_insecure_http": True,
            }]}
            proc, _ = self.run_ingest(ts, config, [event])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertSinglePost(server, title="s: idle", body="x is idle", tags="idle")

    def test_remote_http_via_url_env_requires_opt_in(self):
        with TempState() as ts:
            env_file = ts.path("vars.env")
            env_file.write_text("HASS_URL=http://homeassistant.local:8123\n", encoding="utf-8")
            config = {"env_files": [str(env_file)], "transports": [self.HASS]}
            proc, _ = self.run_ingest(ts, config, [])
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("allow_insecure_http", proc.stderr)

    def test_credentials_in_transport_url_rejected(self):
        for url in (
            "https://user:pass@ntfy.example.invalid/notifier-ng",
            "https://tok@ntfy.example.invalid/x",
            "http://tok@127.0.0.1:9",
        ):
            with self.subTest(url=url):
                with TempState() as ts:
                    proc, _ = self.run_ingest(
                        ts, {"transports": [{**self.NTFY, "url": url}]}, []
                    )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("must not embed credentials", proc.stderr)

    def test_allow_insecure_http_must_be_boolean(self):
        for bad in ("true", "yes", 1, 0, None):
            with self.subTest(value=bad):
                with TempState() as ts:
                    proc, _ = self.run_ingest(
                        ts, {"transports": [{**self.NTFY, "url": "http://0.0.0.0:9",
                                             "allow_insecure_http": bad}]}, []
                    )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("allow_insecure_http must be a boolean", proc.stderr)


class OpenAICompatibleSummarizerTests(unittest.TestCase):
    """Contract tests for the OpenAI-compatible summarizer adapter."""

    ADAPTER = ROOT / "summarizers" / "openai_compatible.py"
    REQUEST = json.dumps({
        "version": 1, "source": "s", "state": "idle",
        "context": {"items": []}, "max_summary_chars": 100,
    })

    @classmethod
    def setUpClass(cls):
        if not cls.ADAPTER.exists():
            raise AssertionError(f"{cls.ADAPTER} is missing; adapter behavior cannot be exercised")

    def run_adapter(self, base_url, *, allow=None, api_key="secret-key", model="test-model"):
        """Run the adapter as a subprocess with a scrubbed, controlled environment."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("NOTIFIER_LLM_")}
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        env["NOTIFIER_LLM_BASE_URL"] = base_url
        env["NOTIFIER_LLM_MODEL"] = model
        env["NOTIFIER_LLM_API_KEY"] = api_key
        if allow is not None:
            env["NOTIFIER_LLM_ALLOW_INSECURE_HTTP"] = allow
        proc = subprocess.run(
            [sys.executable, str(self.ADAPTER)],
            input=self.REQUEST.encode("utf-8"),
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
            env=env,
        )
        return SimpleNamespace(
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
        )

    def assertSummary(self, proc, text):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"version": 1, "summary": text})

    def test_https_endpoint_accepted(self):
        # HTTPS passes the policy; the failure must be at the request layer.
        proc = self.run_adapter("https://127.0.0.1:9")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("endpoint request failed: ", proc.stderr)
        self.assertNotIn("non-loopback", proc.stderr)

    def test_loopback_http_endpoint_success(self):
        body = json.dumps({"choices": [{"message": {"content": "Completed work and verified it."}}]})
        with CaptureServer(body=body) as server:
            proc = self.run_adapter(f"http://127.0.0.1:{server.port}")
        self.assertSummary(proc, "Completed work and verified it.")
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer secret-key")
        self.assertEqual(json.loads(request["body"])["model"], "test-model")

    def test_loopback_hostname_endpoint_success(self):
        body = json.dumps({"choices": [{"message": {"content": "loopback ok"}}]})
        with CaptureServer(body=body) as server:
            proc = self.run_adapter(f"http://localhost:{server.port}")
        self.assertSummary(proc, "loopback ok")
        self.assertEqual(len(server.requests), 1)

    def test_remote_http_rejected_without_opt_in(self):
        for url in ("http://0.0.0.0:9", "http://example.com/v1"):
            with self.subTest(url=url):
                proc = self.run_adapter(url)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("non-loopback host", proc.stderr)
                self.assertIn("NOTIFIER_LLM_ALLOW_INSECURE_HTTP", proc.stderr)

    def test_remote_http_opt_in_true_delivers(self):
        body = json.dumps({"choices": [{"message": {"content": "Opted into remote cleartext."}}]})
        with CaptureServer(body=body) as server:
            proc = self.run_adapter(f"http://0.0.0.0:{server.port}", allow="true")
        self.assertSummary(proc, "Opted into remote cleartext.")
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["headers"].get("Authorization"), "Bearer secret-key")

    def test_allow_insecure_http_env_strict_semantics(self):
        # Only the exact lowercase strings "true"/"false" are honored; any
        # other value must be rejected, never silently interpreted.
        for value in ("TRUE", "True", " true ", "TRUE ", "1", "yes", "on", ""):
            with self.subTest(value=value, phase="reject"):
                proc = self.run_adapter("http://0.0.0.0:9", allow=value)
                self.assertEqual(proc.returncode, 1)
                self.assertIn('must be exactly "true" or "false"', proc.stderr)
        proc = self.run_adapter("http://0.0.0.0:9", allow="true")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("endpoint request failed: ", proc.stderr)
        proc = self.run_adapter("http://0.0.0.0:9", allow="false")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("non-loopback host", proc.stderr)

    def test_endpoint_credentials_in_url_rejected(self):
        for url in ("https://key:secret@127.0.0.1:9", "http://user@0.0.0.0:9"):
            with self.subTest(url=url):
                proc = self.run_adapter(url, allow="true")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("must not embed credentials", proc.stderr)

    def test_endpoint_scheme_and_netloc_required(self):
        for url in ("ftp://host/v1", "host/v1", "https://", "http://"):
            with self.subTest(url=url):
                proc = self.run_adapter(url)
                self.assertEqual(proc.returncode, 1)
                self.assertIn("absolute http(s) URL", proc.stderr)

    def test_http_error_reports_status_only_and_omits_body(self):
        body = json.dumps({
            "error": {
                "message": "Incorrect API key provided: sk-adapter-leak-99",
                "type": "invalid_request_error",
            },
            "apiKey": "sk-adapter-leak-99",
        })
        with CaptureServer(status=401, body=body) as server:
            proc = self.run_adapter(f"http://127.0.0.1:{server.port}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("endpoint returned HTTP 401", proc.stderr)
        self.assertNotIn("sk-adapter-leak-99", proc.stderr)
        self.assertNotIn("invalid_request_error", proc.stderr)

    def test_endpoint_redirect_not_followed(self):
        with CaptureServer() as target, CaptureServer(status=302, location=target.url) as redirector:
            proc = self.run_adapter(f"http://127.0.0.1:{redirector.port}")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("endpoint returned HTTP 302", proc.stderr)
        self.assertEqual(len(redirector.requests), 1, redirector.requests)
        self.assertEqual(redirector.requests[0]["headers"].get("Authorization"), "Bearer secret-key")
        self.assertEqual(target.requests, [], "redirect target must not receive the request")

    def test_endpoint_redirect_not_followed_without_api_key(self):
        with CaptureServer() as target, CaptureServer(status=302, location=target.url) as redirector:
            proc = self.run_adapter(f"http://127.0.0.1:{redirector.port}", api_key="")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("endpoint returned HTTP 302", proc.stderr)
        self.assertEqual(len(redirector.requests), 1, redirector.requests)
        self.assertNotIn("Authorization", redirector.requests[0]["headers"])
        self.assertEqual(target.requests, [], "redirect target must not receive the request")
@unittest.skipUnless(shutil.which("bun"), "bun is not installed")
class OmpAdapterSubjectPrivacyTests(unittest.TestCase):
    """Bun-exercised privacy contract for the OMP adapter.

    The adapter is a TypeScript/bun integration; the dedicated bun test file
    (integrations/omp-notifier.test.ts) drives the real adapter end to end
    and asserts the emitted event subject never contains the session-file
    path. This harness assertion runs that suite through bun: a default
    status-only notification for a session whose file lives at
    /home/alice/private/session.jsonl must derive a stable, path-free subject.
    """

    TEST_FILE = ROOT / "integrations" / "omp-notifier.test.ts"

    def test_bun_privacy_suite_passes(self):
        proc = subprocess.run(
            ["bun", "test", str(self.TEST_FILE)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"bun omp adapter privacy tests failed (exit {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
