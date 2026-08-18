#!/usr/bin/env python3
"""Pluggable idle-event notifier with durable deduplication."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

EVENT_KEYS = {
    "version", "source", "subject", "state", "mode", "event_id", "title", "message",
    "url", "timestamp", "metadata", "context",
}
STATES = {"active", "idle", "stopped", "error"}
MODES = {"event", "snapshot"}
NOTIFY_STATES = {"idle", "stopped", "error"}
TOP_CONFIG_KEYS = {"env_file", "env_files", "transports", "summarizer", "include_message_text", "allow_remote_context"}
COMMON_TRANSPORT_KEYS = {"id", "type", "url", "url_env", "token_env", "allow_insecure_http"}
SUMMARIZER_KEYS = {
    "command", "timeout_seconds", "last_items", "max_item_chars", "max_context_chars",
    "max_summary_chars", "max_summary_output_bytes", "states",
}
CONTEXT_KEYS = {"items"}
CONTEXT_ITEM_KEYS = {"role", "text"}


class NotifierError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class ContextItem:
    role: str
    text: str


@dataclasses.dataclass(frozen=True)
class Event:
    source: str
    subject: str
    state: str
    mode: str
    event_id: str | None = None
    title: str | None = None
    message: str | None = None
    url: str | None = None
    timestamp: str | None = None
    metadata: dict[str, Any] | None = None
    context: tuple[ContextItem, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.source}\0{self.subject}"

    @property
    def fingerprint(self) -> str:
        return f"{self.state}\0{self.event_id or ''}"


@dataclasses.dataclass(frozen=True)
class Transport:
    id: str
    type: str
    url: str
    token_env: str | None = None
    allow_insecure_http: bool = False
    service: str | None = None


@dataclasses.dataclass(frozen=True)
class Summarizer:
    command: tuple[str, ...]
    timeout_seconds: int = 8
    last_items: int = 6
    max_item_chars: int = 1200
    max_context_chars: int = 5000
    max_summary_chars: int = 450
    max_summary_output_bytes: int = 4096
    states: frozenset[str] = frozenset({"idle"})

    @property
    def fingerprint(self) -> str:
        value = dataclasses.asdict(self)
        value["states"] = sorted(self.states)
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class Config:
    transports: tuple[Transport, ...]
    summarizer: Summarizer | None = None
    include_message_text: bool = False
    allow_remote_context: bool = False


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotifierError(f"{path} must be a non-empty string")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, path)


def _bounded_int(value: Any, default: int, low: int, high: int, path: str) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise NotifierError(f"{path} must be an integer from {low} to {high}")
    return value


def _strict_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise NotifierError(f"{path} must be a boolean")
    return value

def _parse_context(value: Any, path: str) -> tuple[ContextItem, ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise NotifierError(f"{path} must be an object")
    unknown = set(value) - CONTEXT_KEYS
    if unknown:
        raise NotifierError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    items = value.get("items")
    if not isinstance(items, list):
        raise NotifierError(f"{path}.items must be an array")
    parsed: list[ContextItem] = []
    for index, item in enumerate(items):
        item_path = f"{path}.items[{index}]"
        if not isinstance(item, dict):
            raise NotifierError(f"{item_path} must be an object")
        unknown = set(item) - CONTEXT_ITEM_KEYS
        if unknown:
            raise NotifierError(f"{item_path} has unknown keys: {', '.join(sorted(unknown))}")
        role = item.get("role")
        if role not in {"user", "assistant"}:
            raise NotifierError(f"{item_path}.role must be user or assistant")
        text = _nonempty_string(item.get("text"), f"{item_path}.text")
        parsed.append(ContextItem(role, text))
    return tuple(parsed)


def parse_event(value: Any, path: str = "event") -> Event:
    if not isinstance(value, dict):
        raise NotifierError(f"{path} must be a JSON object")
    unknown = set(value) - EVENT_KEYS
    if unknown:
        raise NotifierError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    if value.get("version") != 1:
        raise NotifierError(f"{path}.version must equal 1")
    source = _nonempty_string(value.get("source"), f"{path}.source")
    subject = _nonempty_string(value.get("subject"), f"{path}.subject")
    state = _nonempty_string(value.get("state"), f"{path}.state")
    mode = _nonempty_string(value.get("mode"), f"{path}.mode")
    if state not in STATES:
        raise NotifierError(f"{path}.state must be one of {sorted(STATES)}")
    if mode not in MODES:
        raise NotifierError(f"{path}.mode must be one of {sorted(MODES)}")
    metadata = value.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise NotifierError(f"{path}.metadata must be an object")
    return Event(
        source, subject, state, mode,
        _optional_string(value.get("event_id"), f"{path}.event_id"),
        _optional_string(value.get("title"), f"{path}.title"),
        _optional_string(value.get("message"), f"{path}.message"),
        _optional_string(value.get("url"), f"{path}.url"),
        _optional_string(value.get("timestamp"), f"{path}.timestamp"),
        metadata,
        _parse_context(value.get("context"), f"{path}.context"),
    )


def parse_ndjson(raw: bytes | str, source: str) -> list[Event]:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise NotifierError(f"{source} is not valid UTF-8") from exc
    events: list[Event] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NotifierError(f"{source} line {number} is not valid JSON: {exc.msg}") from exc
        events.append(parse_event(value, f"{source} line {number}"))
    return events


def _load_dotenv(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise NotifierError(f"cannot read env file {path}: {exc}") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise NotifierError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_") or not all(c.isalnum() or c == "_" for c in key):
            raise NotifierError(f"{path}:{number}: invalid environment variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _transport_url(value: dict[str, Any], path: str) -> str:
    literal, env_name = value.get("url"), value.get("url_env")
    if literal is not None and env_name is not None:
        raise NotifierError(f"{path} must set only one of url or url_env")
    if env_name is not None:
        name = _nonempty_string(env_name, f"{path}.url_env")
        literal = os.environ.get(name)
        if not literal:
            raise NotifierError(f"{path}.url_env names unset variable {name}")
    return _nonempty_string(literal, f"{path}.url")


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    name = hostname.lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        return addr == ipaddress.IPv6Address("::1")
    return addr in ipaddress.ip_network("127.0.0.0/8")


def _check_transport_endpoint(url: str, allow_insecure_http: bool, path: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NotifierError(f"{path}.url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise NotifierError(f"{path}.url must not embed credentials (user:pass@); use token_env instead")
    if parsed.scheme == "https" or _is_loopback_host(parsed.hostname) or allow_insecure_http:
        return
    raise NotifierError(
        f"{path}.url uses plain HTTP to a non-loopback host; set allow_insecure_http: true to permit it"
    )


def _parse_summarizer(value: Any) -> Summarizer | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise NotifierError("config.summarizer must be an object")
    unknown = set(value) - SUMMARIZER_KEYS
    if unknown:
        raise NotifierError(f"config.summarizer has unknown keys: {', '.join(sorted(unknown))}")
    command = value.get("command")
    if not isinstance(command, list) or not command:
        raise NotifierError("config.summarizer.command must be a non-empty array")
    parsed_command = tuple(_nonempty_string(item, f"config.summarizer.command[{i}]") for i, item in enumerate(command))
    timeout = _bounded_int(value.get("timeout_seconds"), 8, 1, 60, "config.summarizer.timeout_seconds")
    last_items = _bounded_int(value.get("last_items"), 6, 1, 20, "config.summarizer.last_items")
    max_item = _bounded_int(value.get("max_item_chars"), 1200, 200, 8000, "config.summarizer.max_item_chars")
    max_context = _bounded_int(value.get("max_context_chars"), 5000, 1000, 50000, "config.summarizer.max_context_chars")
    max_summary = _bounded_int(value.get("max_summary_chars"), 450, 80, 1000, "config.summarizer.max_summary_chars")
    max_output = _bounded_int(value.get("max_summary_output_bytes"), 4096, 1024, 1048576, "config.summarizer.max_summary_output_bytes")
    if max_output < 4 * max_summary + 256:
        raise NotifierError("config.summarizer.max_summary_output_bytes must be at least 4 * max_summary_chars + 256")
    states = value.get("states", ["idle"])
    if not isinstance(states, list) or not states or not all(isinstance(item, str) for item in states):
        raise NotifierError("config.summarizer.states must be a non-empty array")
    parsed_states = frozenset(states)
    if not parsed_states <= NOTIFY_STATES:
        raise NotifierError("config.summarizer.states must contain only idle, stopped, or error")
    return Summarizer(parsed_command, timeout, last_items, max_item, max_context, max_summary, max_output, parsed_states)


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise NotifierError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise NotifierError(f"config {path} is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise NotifierError("config must be a JSON object")
    unknown = set(raw) - TOP_CONFIG_KEYS
    if unknown:
        raise NotifierError(f"config has unknown keys: {', '.join(sorted(unknown))}")
    env_paths: list[str] = []
    if "env_file" in raw:
        env_paths.append(_nonempty_string(raw["env_file"], "config.env_file"))
    if "env_files" in raw:
        if not isinstance(raw["env_files"], list):
            raise NotifierError("config.env_files must be an array")
        env_paths.extend(_nonempty_string(item, f"config.env_files[{i}]") for i, item in enumerate(raw["env_files"]))
    for env_path in env_paths:
        _load_dotenv(Path(env_path).expanduser())
    values = raw.get("transports")
    if not isinstance(values, list) or not values:
        raise NotifierError("config.transports must be a non-empty array")
    transports: list[Transport] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        item_path = f"config.transports[{index}]"
        if not isinstance(value, dict):
            raise NotifierError(f"{item_path} must be an object")
        kind = _nonempty_string(value.get("type"), f"{item_path}.type")
        allowed, service = set(COMMON_TRANSPORT_KEYS), None
        if kind == "home_assistant":
            allowed.add("service")
            service = _nonempty_string(value.get("service"), f"{item_path}.service")
        elif kind != "ntfy":
            raise NotifierError(f"{item_path}.type must be ntfy or home_assistant")
        unknown = set(value) - allowed
        if unknown:
            raise NotifierError(f"{item_path} has unknown keys: {', '.join(sorted(unknown))}")
        url = _transport_url(value, item_path)
        allow_insecure_http = value.get("allow_insecure_http", False)
        if not isinstance(allow_insecure_http, bool):
            raise NotifierError(f"{item_path}.allow_insecure_http must be a boolean")
        _check_transport_endpoint(url, allow_insecure_http, item_path)
        token_env = _optional_string(value.get("token_env"), f"{item_path}.token_env")
        if kind == "home_assistant" and token_env is None:
            raise NotifierError(f"{item_path}.token_env is required for home_assistant")
        configured_id = _optional_string(value.get("id"), f"{item_path}.id")
        identity = configured_id or hashlib.sha256(json.dumps({"type": kind, "url": url, "service": service, "token_env": token_env, "allow_insecure_http": allow_insecure_http}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        if identity in seen:
            raise NotifierError(f"duplicate transport id {identity!r}")
        seen.add(identity)
        transports.append(Transport(
            id=identity, type=kind, url=url, token_env=token_env,
            allow_insecure_http=allow_insecure_http, service=service,
        ))
    include_message_text = _strict_bool(raw["include_message_text"], "config.include_message_text") if "include_message_text" in raw else False
    allow_remote_context = _strict_bool(raw["allow_remote_context"], "config.allow_remote_context") if "allow_remote_context" in raw else False
    return Config(
        tuple(transports),
        _parse_summarizer(raw.get("summarizer")),
        include_message_text,
        allow_remote_context,
    )


def default_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "notifier-ng/config.json"


def default_state_path() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "notifier-ng/state.json"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "subjects": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotifierError(f"state {path} is corrupt or unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("subjects"), dict):
        raise NotifierError(f"state {path} has an unsupported shape")
    for key, entry in value["subjects"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise NotifierError(f"state {path} has an invalid subject entry")
        if not isinstance(entry.get("fingerprint"), str) or not isinstance(entry.get("delivered"), list):
            raise NotifierError(f"state {path} has an invalid subject entry for {key!r}")
        if not all(isinstance(item, str) for item in entry["delivered"]):
            raise NotifierError(f"state {path} has invalid delivery ids for {key!r}")
        optional_strings = ("summary", "summary_context_hash", "summary_body_hash", "summarizer_config_fingerprint", "summary_policy_hash", "delivery_policy_hash", "summary_status")
        if any(name in entry and not isinstance(entry[name], str) for name in optional_strings):
            raise NotifierError(f"state {path} has invalid summarizer fields for {key!r}")
        if entry.get("summary_status") not in {None, "success", "fallback"}:
            raise NotifierError(f"state {path} has invalid summary status for {key!r}")
    return value


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class StateLock:
    def __init__(self, state_path: Path):
        self.path = Path(f"{state_path}.lock")
        self.handle: Any = None

    def __enter__(self) -> StateLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: Any) -> None:
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


_REDACTED = "[redacted]"
_PEM_PRIVATE_KEY_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)(.*?)(-----END [A-Z0-9 ]*PRIVATE KEY-----)",
    re.DOTALL,
)
_BEARER_RE = re.compile(
    r"(?P<prefix>Authorization\s*:\s*Bearer\s+)(?P<value>[^\s,;]+)", re.IGNORECASE
)
_NAME = '"?[A-Za-z0-9_.-]+"?'
_VALUE = r"""(?P<value>"(?:\\.|[^"\\])*"|[^\s,;\]})"'`]+)"""
_ASSIGN_RE = re.compile("(?P<name>" + _NAME + r")(?P<sep>\s*=\s*)" + _VALUE)
_COLON_RE = re.compile("(?P<name>" + _NAME + r")(?P<sep>\s*:\s*)(?!Bearer\b)" + _VALUE, re.IGNORECASE)
_SECRET_WORDS = (
    "token", "api_key", "apikey", "secret", "password", "credential", "cookie", "auth",
)


def _secret_name(name: str) -> bool:
    """True when an assignment/header name contains a credential word.

    Case-insensitive substring match on the requested words (TOKEN, API_KEY,
    APIKEY, SECRET, PASSWORD, CREDENTIAL, COOKIE, AUTH): a naked or joined
    name such as MYTOKEN or GITHUBTOKEN is masked, per the redaction
    contract. Hyphens are normalized to underscores so API-KEY / X-API-Key
    and apiKey/apikey spellings match the same API_KEY / APIKEY words.
    """
    lower = name.strip("\"'").lower().replace("-", "_")
    return any(word in lower for word in _SECRET_WORDS)


def _redact_name_values(pattern: re.Pattern[str], text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if not _secret_name(match.group("name")):
            return match.group(0)
        return f"{match.group('name')}{match.group('sep')}{_REDACTED}"

    return pattern.sub(replace, text)


def redact_text(text: str) -> str:
    """Deterministically mask obvious credentials before any remote sink.

    Masks PEM private-key payloads, `Authorization: Bearer <token>`, and
    `name=value` / `name: value` assignments whose identifier contains a
    credential word (TOKEN, API_KEY, APIKEY, SECRET, PASSWORD, CREDENTIAL,
    COOKIE, AUTH, ...). Hyphenated and joined API-key spellings (API-KEY,
    apiKey/apikey, X-API-Key) are matched through name normalization.
    Surrounding text and assignment names stay intact; this is syntactic
    redaction, not entropy guessing, and is idempotent.
    """
    def pem_replace(match: re.Match[str]) -> str:
        body = match.group(2)
        separator = "\n[redacted]\n" if "\n" in body else " [redacted] "
        return f"{match.group(1)}{separator}{match.group(3)}"

    text = _PEM_PRIVATE_KEY_RE.sub(pem_replace, text)
    text = _BEARER_RE.sub(lambda match: f"{match.group('prefix')}{_REDACTED}", text)
    text = _redact_name_values(_ASSIGN_RE, text)
    text = _redact_name_values(_COLON_RE, text)
    return text


def notification_text(event: Event, include_message: bool = False) -> tuple[str, str]:
    title = event.title or f"{event.source}: {event.state}"
    if include_message and event.message:
        message = event.message
    else:
        message = f"{event.subject} is {event.state}"
    if event.url:
        message = f"{message}\n{event.url}"
    return title, message


def _bounded_context(event: Event, config: Summarizer) -> tuple[list[dict[str, str]], str]:
    candidates: list[dict[str, str]] = []
    for item in event.context:
        text = redact_text(" ".join(item.text.split()))
        if text:
            candidates.append({"role": item.role, "text": text[:config.max_item_chars]})
    candidates = candidates[-config.last_items:]
    kept: list[dict[str, str]] = []
    total = 0
    for item in reversed(candidates):
        room = config.max_context_chars - total
        if room <= 0:
            break
        text = item["text"][:room]
        if text:
            kept.insert(0, {"role": item["role"], "text": text})
            total += len(text)
    canonical = json.dumps({"items": kept}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return kept, hashlib.sha256(canonical.encode()).hexdigest()


def _privacy_policy_hash(include_message_text: bool, allow_remote_context: bool) -> str:
    return hashlib.sha256(json.dumps([include_message_text, allow_remote_context], separators=(",", ":")).encode()).hexdigest()


DEFAULT_PRIVACY_POLICY_HASH = _privacy_policy_hash(False, False)


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_summarizer(config: Summarizer, event: Event, items: list[dict[str, str]]) -> str | None:
    request_bytes = json.dumps({"version": 1, "source": event.source, "state": event.state, "context": {"items": items}, "max_summary_chars": config.max_summary_chars}, ensure_ascii=False, separators=(",", ":")).encode()
    try:
        proc = subprocess.Popen(config.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    assert proc.stdin is not None and proc.stdout is not None
    os.set_blocking(proc.stdin.fileno(), False)
    os.set_blocking(proc.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdin, selectors.EVENT_WRITE)
    selector.register(proc.stdout, selectors.EVENT_READ)
    sent, output = 0, bytearray()
    deadline = time.monotonic() + config.timeout_seconds
    stdin_open = True
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_and_reap(proc)
                return None
            for key, mask in selector.select(remaining):
                if key.fileobj is proc.stdin and mask & selectors.EVENT_WRITE:
                    try:
                        sent += os.write(proc.stdin.fileno(), request_bytes[sent:])
                    except BlockingIOError:
                        pass
                    except OSError:
                        _kill_and_reap(proc)
                        return None
                    if sent == len(request_bytes):
                        selector.unregister(proc.stdin)
                        try:
                            proc.stdin.close()
                        except OSError:
                            _kill_and_reap(proc)
                            return None
                        stdin_open = False
                elif key.fileobj is proc.stdout and mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(proc.stdout.fileno(), 4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        _kill_and_reap(proc)
                        return None
                    if chunk:
                        output.extend(chunk)
                        if len(output) > config.max_summary_output_bytes:
                            _kill_and_reap(proc)
                            return None
                    else:
                        selector.unregister(proc.stdout)
                        try:
                            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            _kill_and_reap(proc)
                            return None
                        if proc.returncode != 0:
                            return None
                        try:
                            value = json.loads(output.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            return None
                        if not isinstance(value, dict) or set(value) != {"version", "summary"} or value.get("version") != 1:
                            return None
                        summary = value.get("summary")
                        if not isinstance(summary, str):
                            return None
                        summary = " ".join(summary.split())
                        return summary if summary and len(summary) <= config.max_summary_chars else None
    finally:
        selector.close()
        if stdin_open:
            try:
                proc.stdin.close()
            except OSError:
                pass


def _token(transport: Transport) -> str | None:
    if transport.token_env is None:
        return None
    token = os.environ.get(transport.token_env)
    if not token:
        raise NotifierError(f"transport {transport.id} requires environment variable {transport.token_env}")
    return token

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx response surfaces as HTTPError.

    urllib's default handler replays a fresh request at Location, silently
    dropping the Authorization header (or worse, replaying the bearer token
    onto an origin or scheme that never passed _check_transport_endpoint).
    Returning None makes http_error_30x give up so the redirect status is
    reported instead and the credential never leaves the validated endpoint.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def send_notification(transport: Transport, event: Event, message_override: str | None = None, timeout: float = 10.0, include_message: bool = False) -> None:
    title, fallback = notification_text(event, include_message)
    message = message_override if message_override is not None else fallback
    title = redact_text(title)
    message = redact_text(message)
    token = _token(transport)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if transport.type == "ntfy":
        headers.update({"Title": title, "Tags": event.state, "Content-Type": "text/plain; charset=utf-8"})
        request = urllib.request.Request(transport.url, data=message.encode(), headers=headers, method="POST")
    else:
        service = urllib.parse.quote(transport.service or "", safe="")
        url = f"{transport.url.rstrip('/')}/api/services/notify/{service}"
        headers["Content-Type"] = "application/json"
        body = json.dumps({"title": title, "message": message}, separators=(",", ":")).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise NotifierError(f"transport {transport.id} returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = redact_text(exc.read(512).decode("utf-8", "replace").strip())
        raise NotifierError(f"transport {transport.id} returned HTTP {exc.code}{f': {detail}' if detail else ''}") from exc
    except urllib.error.URLError as exc:
        raise NotifierError(f"transport {transport.id} failed: {exc.reason}") from exc


def process_event(event: Event, config: Config, state_path: Path) -> list[str]:
    errors: list[str] = []
    with StateLock(state_path):
        state = load_state(state_path)
        subjects = state["subjects"]
        previous = subjects.get(event.key)
        fingerprint = event.fingerprint
        if previous is None:
            entry = {"fingerprint": fingerprint, "delivered": []}
            subjects[event.key] = entry
            should_notify = event.mode == "event" and event.state in NOTIFY_STATES
        elif previous["fingerprint"] != fingerprint:
            entry = {"fingerprint": fingerprint, "delivered": []}
            subjects[event.key] = entry
            should_notify = event.state in NOTIFY_STATES
        else:
            entry, should_notify = previous, event.state in NOTIFY_STATES
        if not should_notify:
            save_state(state_path, state)
            return errors
        privacy_policy = _privacy_policy_hash(config.include_message_text, config.allow_remote_context)
        if (entry.get("delivery_policy_hash") or DEFAULT_PRIVACY_POLICY_HASH) != privacy_policy:
            entry["delivered"] = []
            entry["delivery_policy_hash"] = privacy_policy
            save_state(state_path, state)

        _, fallback_body = notification_text(event, config.include_message_text)
        message_override: str | None = None
        summarizer = config.summarizer
        if config.allow_remote_context and summarizer is not None and event.state in summarizer.states and event.context:
            items, context_hash = _bounded_context(event, summarizer)
            body_hash = hashlib.sha256(fallback_body.encode()).hexdigest()
            policy_hash = _privacy_policy_hash(config.include_message_text, config.allow_remote_context)
            identity_matches = (
                entry.get("summary_context_hash") == context_hash
                and entry.get("summary_body_hash") == body_hash
                and entry.get("summarizer_config_fingerprint") == summarizer.fingerprint
                and entry.get("summary_policy_hash") == policy_hash
                and entry.get("summary_status") in {"success", "fallback"}
            )
            if not identity_matches:
                summary = run_summarizer(summarizer, event, items) if items else None
                entry.update({
                    "summary_context_hash": context_hash,
                    "summary_body_hash": body_hash,
                    "summarizer_config_fingerprint": summarizer.fingerprint,
                    "summary_policy_hash": policy_hash,
                    "summary_status": "success" if summary else "fallback",
                })
                if summary:
                    entry["summary"] = summary
                else:
                    entry.pop("summary", None)
                save_state(state_path, state)
            if entry.get("summary_status") == "success":
                message_override = f"Summary: {entry['summary']}"
        elif not config.allow_remote_context:
            summary_keys = ("summary", "summary_context_hash", "summary_body_hash", "summarizer_config_fingerprint", "summary_policy_hash", "summary_status")
            if any(name in entry for name in summary_keys):
                for name in summary_keys:
                    entry.pop(name, None)
                save_state(state_path, state)

        delivered = set(entry["delivered"])
        for transport in config.transports:
            if transport.id in delivered:
                continue
            try:
                send_notification(transport, event, message_override, include_message=config.include_message_text)
            except NotifierError as exc:
                errors.append(str(exc))
                continue
            delivered.add(transport.id)
            entry["delivered"] = sorted(delivered)
            save_state(state_path, state)
        if not delivered and not state_path.exists():
            save_state(state_path, state)
    return errors


def process_events(events: Iterable[Event], config: Config, state_path: Path) -> list[str]:
    errors: list[str] = []
    for event in events:
        errors.extend(process_event(event, config, state_path))
    return errors


def run_source(command: list[str], raw_input: bytes) -> list[Event]:
    if not command:
        raise NotifierError("source requires a plugin executable")
    try:
        result = subprocess.run(command, input=raw_input, capture_output=True, timeout=60)
    except FileNotFoundError as exc:
        raise NotifierError(f"source plugin not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NotifierError(f"source plugin timed out: {command[0]}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:1000]
        raise NotifierError(f"source plugin {command[0]} exited {result.returncode}{f': {detail}' if detail else ''}")
    return parse_ndjson(result.stdout, f"source plugin {command[0]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--state", type=Path, default=default_state_path())
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ingest", help="read normalized NDJSON from stdin")
    source = subparsers.add_parser("source", help="run a source plugin and consume its NDJSON")
    source.add_argument("plugin")
    source.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = load_config(args.config.expanduser())
        raw_input = sys.stdin.buffer.read()
        events = parse_ndjson(raw_input, "stdin") if args.command == "ingest" else run_source([args.plugin, *args.args], raw_input)
        errors = process_events(events, config, args.state.expanduser())
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    except NotifierError as exc:
        print(f"notifier-ng: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
