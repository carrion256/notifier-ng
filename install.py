#!/usr/bin/env python3
"""Install and wire notifier-ng on this workstation.

Dependency-free: Python stdlib only. Default mode is a DRY RUN: nothing on
disk is touched and every change that WOULD be made is printed. Pass
``--apply`` to perform the printed changes. Running the installer twice is
safe: already-applied steps are reported as no-ops and never rewritten.

What the installer manages:

  1. User config/state directories and the example config.
     - Creates  $XDG_CONFIG_HOME/notifier-ng  and  $XDG_STATE_HOME/notifier-ng
       (defaults: ~/.config/notifier-ng, ~/.local/state/notifier-ng).
     - Copies config.example.json to ~/.config/notifier-ng/config.json ONLY
       when absent; an existing config is validated with the core schema and
       never overwritten. A malformed existing config (bad JSON, unknown
       keys, broken transports, unreadable env files) is refused.
  2. OMP post hook: symlink of integrations/omp-notifier.ts at
     ~/.omp/agent/hooks/post/notifier-ng.ts (harness auto-discovery). The
     adapter resolves its core via NOTIFIER_NG_INGEST or a sibling-relative
     fallback (its own directory's parent), never a machine-specific path;
     on filesystems where the symlink cannot be created, a small wrapper is
     written instead that pins NOTIFIER_NG_INGEST to this checkout and
     imports the adapter from it. A differing existing target is refused,
     never clobbered.
  3. Codex legacy notify: the top-level ``notify`` array in
     ~/.codex/config.toml (default) is ``[INGEST, "source", CODEX_PLUGIN]``,
     where both paths resolve to this checkout wherever it lives. The
     notify array IS the argv vector of one command; codex appends the
     payload JSON as the final argv element, which the core's "source"
     subcommand forwards to the plugin. The core ingests the plugin's NDJSON
     (the plugin's own stdout is fire-and-forget nulled by codex). Every
     other line of config.toml is preserved byte-for-byte. No-op when the
     value already equals that exact array; malformed TOML is refused.
  4. Hermes shell hooks (approval is external: the allowlist file, not a
     TTY prompt or hooks_auto_accept).
     - config.yaml is NEVER edited in place: if the resolved Hermes home
       already contains a config.yaml the exact fragment to merge is
       printed and the file is left untouched; a minimal hooks-only
       config.yaml is created only when none exists.
     - shell-hooks-allowlist.json entries for on_session_end and
       on_session_finalize are merged (event+command keyed) with hermes'
       documented schema (approved_at / script_mtime_at_approval as
       ISO-8601 UTC with a Z suffix); other approvals and top-level keys
       are preserved. Malformed JSON is refused.
     - Hook commands route through the core ("source" subcommand): the
       plugin only EMITS normalized NDJSON, and hermes consumes hook
       stdout as its own response, so the command is
       "notifier_ng.py source plugins/hermes.py" — the core forwards the
       stdin payload to the plugin and emits nothing on its own stdout
       (a no-op response for hermes).

Hermes home resolution (first match wins):
  --hermes-home PATH
  active profile: ~/.hermes/active_profile names <p> and
                  ~/.hermes/profiles/<p>/config.yaml exists  -> that home
  $HERMES_HOME when set
  ~/.hermes  (platform default)

  5. NZM managed timer: the user systemd units notifier-ng-nzm.service and
     notifier-ng-nzm.timer in the user unit directory ($XDG_CONFIG_HOME/
     systemd/user, else ~/.config/systemd/user, overridable with
     --unit-dir). The timer fires every minute (OnCalendar=*-*-* *:*:00,
     Persistent=true) invoking "notifier_ng.py source plugins/nzm.py" with
     an explicit PATH built from the independently resolved absolute
     directories of the nzm and zellij executables, the resolved python3
     directory, and the standard system directories; the installer refuses
     before writing when nzm or zellij cannot be resolved. Units are
     written atomically with --apply: an identical existing file is a
     no-op, a divergent one is refused (never clobbered; the generated
     content is printed for comparison), and existing notifier-ng-zellij
     units are never touched. The installer never runs systemctl: it
     prints the exact daemon-reload / enable commands and flags the
     optional disable of the legacy notifier-ng-zellij.timer as your
     decision.

Zellij / Covey scans are manual: polling is not implemented and no
external CLI is spawned during planning (preserving dry-run purity); the
installer prints ready-to-use manual commands. The NZM timer units above
are written by --apply but never activated — activation is the printed
systemctl commands, executed by you.

Exit status: 0 = every step ok (no-ops included); 1 = a step was refused
(broken existing config, malformed TOML/JSON target, unresolvable
nzm/zellij executable) or failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

# --- repo-absolute identities (also the literal contract values) -----------
REPO = str(Path(__file__).resolve().parent)
EXAMPLE_CONFIG = os.path.join(REPO, "config.example.json")
OMP_EXTENSION = os.path.join(REPO, "integrations", "omp-notifier.ts")
OMP_HOOK_NAME = "notifier-ng.ts"
INGEST = os.path.join(REPO, "notifier_ng.py")
HERMES_PLUGIN = os.path.join(REPO, "plugins", "hermes.py")
CODEX_PLUGIN = os.path.join(REPO, "plugins", "codex.py")
# Hook commands route through the core ("source" subcommand): the plugin only
# EMITS normalized NDJSON, and the sinks are not pass-throughs — codex's legacy
# notify nulls the hook's stdio (fire-and-forget) and hermes consumes hook
# stdout as its own response. The core therefore forwards the payload
# (codex: final argv argument; hermes: stdin) to the plugin, ingests the
# plugin's NDJSON, and emits nothing on its own stdout so hermes sees a
# no-op response. Allowlist/TOML comparisons must use these exact strings.
ZELLIJ_TEMPLATE = f"{INGEST} source {os.path.join(REPO, 'plugins', 'zellij.py')}"
COVEY_TEMPLATE = f"{INGEST} source {os.path.join(REPO, 'plugins', 'covey.py')}"
HERMES_COMMAND = f"{INGEST} source {HERMES_PLUGIN}"
# Codex legacy notify: the TOML array IS the argv vector of one command;
# codex appends the payload JSON as the final argv element, which the core's
# "source" subcommand forwards to the plugin (documented codex legacy path).
# Compare against and serialize this exact list — never a joined string.
CODEX_NOTIFY = [INGEST, "source", CODEX_PLUGIN]
# NZM managed timer (step 5): the core "source" subcommand forwards to the
# plugin; both paths are repo-absolute so the generated units never depend
# on WorkingDirectory or ambient resolution. OnCalendar/Persistent are
# frozen contract values; the timer unit name is paired with the service.
NZM_PLUGIN = os.path.join(REPO, "plugins", "nzm.py")
NZM_TEMPLATE = f"{INGEST} source {NZM_PLUGIN}"
NZM_SERVICE_NAME = "notifier-ng-nzm.service"
NZM_TIMER_NAME = "notifier-ng-nzm.timer"
ZELLIJ_TIMER_NAME = "notifier-ng-zellij.timer"
# Standard system directories appended to the unit PATH after the resolved
# binary directories (the same set NZM's own service installer uses). User
# profile directories are reached through the resolved binary directories
# themselves, never hardcoded.
_STANDARD_PATH_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


def _toml_array(items):
    """Serialize a list of strings as a TOML array of basic strings
    (json quoting is a safe subset for the plain paths we emit)."""
    return "[" + ", ".join(json.dumps(item) for item in items) + "]"

HERMES_EVENTS = ("on_session_end", "on_session_finalize")

# Exact YAML for Hermes hooks. Command strings must equal the allowlist
# command strings: shell_hooks.py matches on (event, command).
HERMES_YAML_FRAGMENT = """\
# notifier-ng: forward Hermes session lifecycle events to notifier-ng ingest.
# This whole block applies when config.yaml has no top-level "hooks:" key.
# Restart hermes after applying. Allowlist approvals (same command strings)
# are managed separately in shell-hooks-allowlist.json by this installer.
hooks:
  on_session_end:
    - command: {command}
  on_session_finalize:
    - command: {command}
""".format(command=HERMES_COMMAND)

# Entries-only fragment for configs that already have a top-level hooks: key.
HERMES_ENTRIES_FRAGMENT = """\
# notifier-ng: append these entries under the existing top-level "hooks:" key,
# then restart hermes. Allowlist approvals are managed by this installer.
  on_session_end:
    - command: {command}
  on_session_finalize:
    - command: {command}
""".format(command=HERMES_COMMAND)

TOML_NOTIFY_ASSIGN = re.compile(r"^(\s*)notify(\s*)=(\s*)")
TOML_TABLE_HEADER = re.compile(r"^\s*\[[^\[\]]*\](\s*#.*)?$")
# Import the sibling core for authoritative config validation and default
# paths. Never write __pycache__ while doing so: a dry run must make zero
# filesystem changes even on a fresh checkout.
sys.dont_write_bytecode = True
try:
    import notifier_ng as _core
except ImportError:  # pragma: no cover — installer run outside the repo
    _core = None


class InstallError(Exception):
    """A step failed or was refused; reported, never fatal to other steps."""


# ---------------------------------------------------------------------------
# Config validation: authoritative schema comes from the sibling core.
# A conservative fallback (shape-only) keeps the installer usable if the
# core is ever missing; it never accepts configs the core would reject.
# ---------------------------------------------------------------------------

def validate_config(path):
    if _core is not None:
        try:
            _core.load_config(Path(path).expanduser())
        except _core.NotifierError as exc:
            raise InstallError(str(exc)) from exc
        return
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise InstallError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InstallError(f"config {path} is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise InstallError("config must be a JSON object")
    values = raw.get("transports")
    if not isinstance(values, list) or not values:
        raise InstallError("config.transports must be a non-empty array")
    for index, value in enumerate(values):
        item_path = f"config.transports[{index}]"
        if not isinstance(value, dict):
            raise InstallError(f"{item_path} must be an object")
        kind = value.get("type")
        if kind == "home_assistant":
            if not isinstance(value.get("service"), str) or not value["service"].strip():
                raise InstallError(f"{item_path}.service is required for home_assistant")
            if not isinstance(value.get("token_env"), str) or not value["token_env"].strip():
                raise InstallError(f"{item_path}.token_env is required for home_assistant")
        elif kind != "ntfy":
            raise InstallError(f"{item_path}.type must be ntfy or home_assistant")
    for key in ("env_file", "env_files"):
        if key in raw and key == "env_files" and not isinstance(raw[key], list):
            raise InstallError("config.env_files must be an array")
        break


def default_config_path():
    if _core is not None:
        return _core.default_config_path()
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "notifier-ng" / "config.json"


def default_state_dir():
    if _core is not None:
        return _core.default_state_path().parent
    base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    return Path(base) / "notifier-ng"

def default_unit_dir():
    """$XDG_CONFIG_HOME/systemd/user, else ~/.config/systemd/user (mirrors
    the unit directory NZM's own service installer resolves)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


# ---------------------------------------------------------------------------
# Change reporting
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self, apply):
        self.apply = apply
        self.lines = []
        self.refused = 0

    def _tag(self, verb, path, detail=""):
        prefix = f"[{verb}]" if self.apply else f"[dry-run] would {verb}"
        text = f"{prefix} {path}"
        if detail:
            text = f"{text}: {detail}"
        self.lines.append(("change", text))

    def noop(self, text):
        self.lines.append(("noop", f"[noop]     {text}"))

    def info(self, text):
        self.lines.append(("info", f"[info]     {text}"))

    def manual(self, text):
        self.lines.append(("manual", f"[manual]   {text}"))

    def block(self, text):
        """Raw (unprefixed) text block — used for copy-pasteable fragments."""
        self.lines.append(("block", text))

    def refuse(self, text):
        self.refused += 1
        self.lines.append(("refuse", f"[refuse]   {text}"))

    def emit(self):
        for _kind, text in self.lines:
            print(text)  # "block" entries already carry their raw text
        if not self.apply:
            print("[info]     dry run: no changes were made; re-run with --apply to apply.")


def _ensure_dir(plan, directory, description):
    directory = Path(directory)
    if directory.exists():
        if directory.is_dir():
            plan.noop(f"directory exists: {directory}")
            return True
        plan.refuse(f"{description} exists but is not a directory: {directory}")
        return False
    if plan.apply:
        directory.mkdir(parents=True, exist_ok=True)
        plan._tag("create", f"directory {directory}")
    else:
        plan._tag("create", f"directory {directory}")
    return True


def _atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _iso_z(moment):
    return moment.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Step 1: config/state directories + example config
# ---------------------------------------------------------------------------

def step_example_config(plan, config_path, state_dir):
    _ensure_dir(plan, config_path.parent, "notifier-ng config directory")
    _ensure_dir(plan, state_dir, "notifier-ng state directory")

    if not os.path.isfile(EXAMPLE_CONFIG):
        plan.refuse(f"example config not found: {EXAMPLE_CONFIG}")
        return
    try:
        validate_config(EXAMPLE_CONFIG)
    except InstallError as exc:
        plan.refuse(f"example config invalid: {exc}")
        return

    if config_path.exists():
        if not config_path.is_file():
            plan.refuse(f"existing config is not a file: {config_path}")
            return
        try:
            validate_config(config_path)
        except InstallError as exc:
            plan.refuse(
                f"existing config {config_path} is malformed: {exc} "
                "(not overwritten; fix or remove it and re-run)"
            )
            return
        plan.noop(f"config already installed and valid: {config_path}")
        return

    plan._tag("create", f"file {config_path}", "copy of config.example.json (validated)")
    if plan.apply:
        shutil.copy2(EXAMPLE_CONFIG, config_path)


# ---------------------------------------------------------------------------
# Step 2: OMP post hook (regular wrapper; ambient discovery skips symlinks)
# ---------------------------------------------------------------------------


def _omp_wrapper():
    """Regular hook file that pins this checkout and imports the adapter.

    OMP's ambient ``hooks/post`` discovery enumerates regular files only, so
    symlinks are silently skipped. The adapter reads NOTIFIER_NG_INGEST lazily
    at delivery time, after this wrapper sets it. Re-run ``install.py --apply``
    after moving the checkout.
    """
    return (
        "/** Generated by install.py (OMP hook wrapper).\n"
        " * OMP ambient hook discovery requires a regular file.\n"
        " * Pins the notifier-ng checkout; re-run install.py --apply after moving it.\n"
        " */\n"
        f"process.env.NOTIFIER_NG_INGEST ??= {json.dumps(INGEST)};\n"
        f"export {{ default }} from {json.dumps(OMP_EXTENSION)};\n"
    )


def step_omp_hook(plan, hooks_post_dir):
    target = Path(hooks_post_dir) / OMP_HOOK_NAME
    source = Path(OMP_EXTENSION)

    if not source.is_file():
        plan.refuse(f"OMP extension source not found: {source}")
        return
    if not _ensure_dir(plan, hooks_post_dir, "OMP hooks/post directory"):
        return

    try:
        same_content = target.is_file() and target.read_bytes() == source.read_bytes()
    except OSError:
        same_content = False
    try:
        wrapper_installed = target.is_file() and target.read_text(encoding="utf-8") == _omp_wrapper()
    except OSError:
        wrapper_installed = False

    legacy_install = False
    if target.is_symlink():
        try:
            legacy_install = target.resolve() == source.resolve()
        except OSError:
            legacy_install = False
        if not legacy_install:
            plan.refuse(
                f"OMP hook symlink {target} exists but points elsewhere; "
                "remove it and re-run to install the hook"
            )
            return
    elif wrapper_installed:
        plan.noop(f"OMP hook regular wrapper already installed: {target}")
        return
    elif same_content:
        legacy_install = True
    elif target.exists():
        if target.is_dir():
            plan.refuse(f"OMP hook target is a directory: {target}")
            return
        plan.refuse(
            f"OMP hook target {target} exists with different content; "
            "remove it and re-run to install the hook"
        )
        return

    action = "replace" if legacy_install else "create"
    detail = "migrated legacy symlink/copy to discoverable regular wrapper" if legacy_install else "regular wrapper required by OMP hook discovery"
    plan._tag(action, f"file {target}", detail)
    if plan.apply:
        _atomic_write(target, _omp_wrapper(), mode=0o644)


# ---------------------------------------------------------------------------
# Step 3: Codex legacy notify array
# ---------------------------------------------------------------------------

def _toml_prefix_until_table(lines):
    """Index of the first [table] header. Top-level keys (the notify key we
    manage) can only legally appear before the first table header in TOML;
    malformed files are refused by tomllib on the whole document."""
    for index, line in enumerate(lines):
        if TOML_TABLE_HEADER.match(line.strip()):
            return index
    return len(lines)


def _locate_notify_value(lines, line_index, offset):
    """Return (end_line_index, end_offset, trailing) of the value span, or
    None when the value cannot be delimited safely (unbalanced brackets)."""
    in_string = None
    escape = False
    depth = 0
    start = offset
    for next_index in range(line_index, len(lines)):
        line = lines[next_index]
        for index in range(start, len(line)):
            char = line[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == in_string:
                    in_string = None
                continue
            if char in {'"', "'"}:
                in_string = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return next_index, index + 1, line[index + 1 :]
        start = 0
    return None


def step_codex_notify(plan, codex_config):
    config_path = Path(codex_config)
    if not config_path.exists():
        content = f"notify = {_toml_array(CODEX_NOTIFY)}\n"
        plan._tag(
            "create", f"file {config_path}",
            f"codex config absent; creating with notify = {_toml_array(CODEX_NOTIFY)}",
        )
        if plan.apply:
            _atomic_write(config_path, content, mode=0o600)
        return

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        plan.refuse(f"cannot read {config_path}: {exc}")
        return
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        plan.refuse(f"codex config {config_path} is malformed TOML: {exc}")
        return

    notify_value = parsed.get("notify") if isinstance(parsed, dict) else None
    if notify_value is not None and not isinstance(notify_value, list):
        plan.refuse(
            f"codex config {config_path}: top-level notify is not a TOML array "
            "(leave as-is; cannot update it safely)"
        )
        return
    if notify_value == CODEX_NOTIFY:
        plan.noop(f"codex notify already exactly {_toml_array(CODEX_NOTIFY)}: {config_path}")
        return

    lines = text.splitlines(keepends=True)
    prefix_end = _toml_prefix_until_table(lines)
    location = None
    for index in range(prefix_end):
        if TOML_NOTIFY_ASSIGN.match(lines[index].rstrip("\r\n")):
            location = index
            break
    if location is None:
        # Top-level notify absent — inject at the very top; a valid position
        # regardless of what tables follow.
        canonical = f"notify = {_toml_array(CODEX_NOTIFY)}\n"
        plan._tag(
            "create", f"top-level notify in {config_path}",
            f"inserted {canonical.strip()} (was absent)",
        )
        if plan.apply:
            _atomic_write(config_path, canonical + text, mode=0o600)
        return

    match = TOML_NOTIFY_ASSIGN.match(lines[location].rstrip("\r\n"))
    span = _locate_notify_value(lines, location, match.end())
    if span is None:
        plan.refuse(
            f"codex config {config_path}: cannot delimit the notify value safely; "
            "leave as-is and edit it by hand"
        )
        return
    end_line, _end_offset, trailing = span
    comment = ""
    if trailing.strip().startswith("#"):
        comment = " " + trailing.strip()
    canonical = f"notify = {_toml_array(CODEX_NOTIFY)}{comment}"
    plan._tag(
        "replace",
        f"notify value in {config_path}",
        f"after line {location + 1}; unrelated settings preserved",
    )
    if not plan.apply:
        return
    if end_line > location:
        del lines[location + 1 : end_line + 1]
    lines[location] = canonical + lines[location][len(lines[location].rstrip("\r\n")) :]
    _atomic_write(config_path, "".join(lines), mode=0o600)


# ---------------------------------------------------------------------------
# Step 4: Hermes shell hooks (fragment print / minimal create + allowlist)
# ---------------------------------------------------------------------------

def resolve_hermes_home(explicit=None):
    """Return (home, active_profile_or_None). First match wins: explicit,
    active profile (live evidence: gateway.pid records the profile home),
    $HERMES_HOME, platform default ~/.hermes."""
    if explicit:
        return Path(explicit).expanduser(), None
    account_home = Path.home() / ".hermes"
    active = None
    try:
        active = (account_home / "active_profile").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if active and active != "default":
        profile_home = account_home / "profiles" / active
        if (profile_home / "config.yaml").is_file():
            return profile_home, active
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser(), None
    return account_home, None


def _yaml_block_range(lines):
    """Index range [start, end) of the first top-level ``hooks:`` block, or
    None when no top-level hooks: key exists."""
    hooks_index = None
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if not stripped.strip() or stripped[0].isspace() or stripped.lstrip().startswith("#"):
            continue
        if stripped.startswith("hooks:"):
            hooks_index = index
            break
    if hooks_index is None:
        return None
    end_index = len(lines)
    for index in range(hooks_index + 1, len(lines)):
        stripped = lines[index].rstrip("\r\n")
        if stripped and not stripped[0].isspace() and not stripped.lstrip().startswith("#"):
            end_index = index
            break
    return hooks_index, end_index


def _hooks_already_configured(text):
    """True when the top-level hooks: block lists HERMES_COMMAND for every
    HERMES_EVENTS event (exact command strings, quotes optional)."""
    lines = text.splitlines()
    block = _yaml_block_range(lines)
    if block is None:
        return False
    start, end = block
    block_lines = lines[start:end]
    command_re = re.compile(
        r"^\s*-\s+command\s*:\s*['\"]?" + re.escape(HERMES_COMMAND) + r"['\"]?\s*$"
    )
    event_headers = tuple(event + ":" for event in HERMES_EVENTS)
    for event in HERMES_EVENTS:
        event_index = None
        for index, line in enumerate(block_lines):
            if line.rstrip("\r\n").strip() == event + ":":
                event_index = index
                break
        if event_index is None:
            return False
        found = False
        for line in block_lines[event_index + 1 :]:
            stripped = line.rstrip("\r\n")
            stripped_l = stripped.lstrip()
            if not stripped_l or stripped_l.startswith("#"):
                continue
            if stripped.strip() in event_headers:
                break  # next hook event block begins
            if stripped[0].isspace():
                if command_re.match(stripped):
                    found = True
                    break
                continue
            break  # block ended (col-0 key)
        if not found:
            return False
    return True


def step_hermes(plan, hermes_home):
    home = Path(hermes_home)
    config_yaml = home / "config.yaml"
    allowlist = home / "shell-hooks-allowlist.json"

    if not os.path.isfile(HERMES_PLUGIN):
        plan.refuse(f"Hermes hook plugin not found: {HERMES_PLUGIN}")
        return

    # --- config.yaml: never edit an existing file -------------------------
    if config_yaml.exists():
        if not config_yaml.is_file():
            plan.refuse(f"Hermes config is not a file: {config_yaml}")
        else:
            try:
                text = config_yaml.read_text(encoding="utf-8")
            except OSError as exc:
                plan.refuse(f"cannot read Hermes config {config_yaml}: {exc}")
            else:
                if _hooks_already_configured(text):
                    plan.noop(f"Hermes hooks already configured in {config_yaml}")
                elif _yaml_block_range(text.splitlines()) is not None:
                    plan.manual(
                        f"merge exactly this block into the existing top-level "
                        f"'hooks:' key of {config_yaml} (preserving everything else):"
                    )
                    plan.block(HERMES_ENTRIES_FRAGMENT.rstrip("\n"))
                else:
                    plan.manual(
                        f"merge exactly this block into {config_yaml} as a top-level "
                        f"'hooks:' key (preserving everything else):"
                    )
                    plan.block(HERMES_YAML_FRAGMENT.rstrip("\n"))
    else:
        plan._tag(
            "create", f"file {config_yaml}",
            "minimal hooks-only config.yaml (hermes merges defaults)",
        )
        if plan.apply:
            _ensure_dir(plan, home, "Hermes home directory")
            _atomic_write(config_yaml, HERMES_YAML_FRAGMENT, mode=0o644)

    # --- allowlist: JSON merge (safe: stdlib-validatable) ------------------
    # Hermes `hooks doctor` gates script_mtime_at_approval against the mtime of
    # the EXECUTED command token (argv[0] of the hook command = INGEST), not
    # the plugin. Record INGEST's mtime so the gate is meaningful and clears;
    # a plugins/*.py change surfaces as a core (executed) change, over-inclusive
    # by design — never silently stale.
    mtime = _iso_z(
        datetime.fromtimestamp(os.path.getmtime(INGEST), tz=timezone.utc)
    )
    now = _iso_z(datetime.now(timezone.utc))
    entries = [
        {
            "event": event,
            "command": HERMES_COMMAND,
            "approved_at": now,
            "script_mtime_at_approval": mtime,
        }
        for event in HERMES_EVENTS
    ]

    if allowlist.exists():
        if not allowlist.is_file():
            plan.refuse(f"Hermes allowlist is not a file: {allowlist}")
            return
        try:
            data = json.loads(allowlist.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            plan.refuse(f"Hermes allowlist {allowlist} is malformed JSON: {exc}")
            return
        if not isinstance(data, dict):
            plan.refuse(f"Hermes allowlist {allowlist} must be a JSON object")
            return
        approvals = data.get("approvals")
        if approvals is None:
            approvals = []
        if not isinstance(approvals, list):
            plan.refuse(
                f"Hermes allowlist {allowlist}: 'approvals' must be an array; "
                "leave as-is (hermes itself resets it, but refusing is safer)"
            )
            return
        existing = {
            (entry.get("event"), entry.get("command"))
            for entry in approvals
            if isinstance(entry, dict)
        }
        missing = [
            entry
            for entry in entries
            if (entry["event"], entry["command"]) not in existing
        ]
        if not missing:
            plan.noop(f"Hermes allowlist already approved: {allowlist}")
            return
        plan._tag(
            "update", f"allowlist {allowlist}",
            f"adding {len(missing)} approval(s), preserving {len(approvals)} existing "
            "entry/entries",
        )
        if plan.apply:
            data["approvals"] = approvals + missing
            _atomic_write(allowlist, json.dumps(data, indent=2, sort_keys=True), mode=0o600)
    else:
        plan._tag(
            "create", f"file {allowlist}",
            "shell-hooks-allowlist.json with 2 approvals (documented schema)",
        )
        if plan.apply:
            _ensure_dir(plan, home, "Hermes home directory")
            data = {"approvals": entries}
            _atomic_write(allowlist, json.dumps(data, indent=2, sort_keys=True), mode=0o600)


# ---------------------------------------------------------------------------
# Step 5: NZM managed timer (notifier-ng-nzm.service + .timer)
# ---------------------------------------------------------------------------

def _find_on_path(name, env_path):
    """First executable file named ``name`` on ``env_path``, or None.
    Filesystem scan only: resolution never spawns a process."""
    for directory in env_path.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _unit_path_dirs(env_path):
    """Resolve the unit PATH directories for the NZM timer units.

    Each required binary (nzm, zellij) is located by its own independent
    PATH search and pinned by the absolute path of the directory it was
    found in, so the generated unit keeps working even when a symlinked
    profile directory is replaced. The resolved python3 directory
    (falling back to the interpreter actually running this installer) and
    the standard system directories are appended after them, de-duplicated
    in order. Returns (dirs, missing) where ``missing`` names the first
    required binary that could not be resolved, or None.
    """
    parts = []
    missing = None
    for name in ("nzm", "zellij"):
        found = _find_on_path(name, env_path)
        if found is None:
            missing = name
            break
        directory = os.path.dirname(os.path.abspath(found))
        if directory and directory not in parts:
            parts.append(directory)
    python3 = _find_on_path("python3", env_path)
    python_dir = (
        os.path.dirname(os.path.abspath(python3))
        if python3
        else os.path.dirname(sys.executable)
    )
    for directory in [python_dir, *_STANDARD_PATH_DIRS]:
        if directory and directory not in parts:
            parts.append(directory)
    return parts, missing


def _quote_if_needed(value):
    """Systemd parses Environment= with shell-like word splitting, so a PATH
    containing whitespace is double-quoted (backslash/quote escaped inside).
    Mirrors the escaping NZM's own service installer applies to unit PATHs."""
    if re.search(r"\s", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _nzm_units(path_dirs):
    """(service, timer) unit content for a resolved directory list.

    Frozen contract values emitted verbatim: ExecStart is the repo-absolute
    core + NZM plugin command, OnCalendar is ``*-*-* *:*:00`` and
    Persistent is true. Deterministic: identical inputs produce identical
    bytes, which is what makes repeated installs no-ops.
    """
    path_value = _quote_if_needed(os.pathsep.join(path_dirs))
    service = (
        "[Unit]\n"
        "Description=Scan NZM-registered agents for notifier-ng\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={REPO}\n"
        f"Environment=PATH={path_value}\n"
        f"ExecStart={NZM_TEMPLATE}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Scan NZM-registered agents for notifier-ng every minute\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* *:*:00\n"
        "Persistent=true\n"
        f"Unit={NZM_SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _manage_unit(plan, path, content):
    """Plan one unit file: identical -> noop, divergent -> refuse (never
    clobbered; the exact generated content is printed for comparison),
    absent -> print the exact file and create it under --apply."""
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        plan.refuse(f"cannot read existing unit {path}: {exc}")
        return
    if existing == content:
        plan.noop(f"unit already installed and identical: {path}")
        return
    if existing is not None:
        plan.refuse(
            f"existing unit {path} differs from the generated content; "
            "existing units are never clobbered — remove it or edit it by "
            "hand, then re-run. Exact generated content for comparison:"
        )
        plan.block(content)
        return
    plan._tag("create", f"file {path}")
    plan.block(content)
    if plan.apply:
        _atomic_write(path, content, mode=0o644)


def step_nzm_timer(plan, unit_dir):
    """Manage the NZM source timer units in the user unit directory.

    Pure planning plus optional writes: no external CLI is ever spawned
    (PATH resolution is filesystem scans only) and systemctl is never
    executed — the exact daemon-reload / enable commands are printed, and
    the legacy notifier-ng-zellij.timer is flagged as an optional disable
    decision, never touched. Refuses before writing anything when nzm or
    zellij cannot be resolved from PATH.
    """
    path_dirs, missing = _unit_path_dirs(os.environ.get("PATH", ""))
    if missing is not None:
        plan.refuse(
            f"cannot resolve the `{missing}` executable on PATH; refusing to "
            "write the NZM timer units (a unit whose ExecStart/PATH cannot "
            f"run is worse than no unit). Install `{missing}` or add its "
            "directory to PATH, then re-run this installer."
        )
        return
    if not _ensure_dir(plan, unit_dir, "user systemd unit directory"):
        return
    service_content, timer_content = _nzm_units(path_dirs)
    _manage_unit(plan, Path(unit_dir) / NZM_SERVICE_NAME, service_content)
    _manage_unit(plan, Path(unit_dir) / NZM_TIMER_NAME, timer_content)
    plan.manual(
        "this installer never runs systemctl; activating the NZM timer is "
        "your manual step after reviewing the units above:"
    )
    plan.block("systemctl --user daemon-reload")
    plan.block(f"systemctl --user enable --now {NZM_TIMER_NAME}")
    if (Path(unit_dir) / ZELLIJ_TIMER_NAME).exists():
        plan.manual(
            f"legacy {ZELLIJ_TIMER_NAME} is installed (left untouched); after "
            "verifying the NZM timer delivers, disable it — your decision:"
        )
        plan.block(f"systemctl --user disable --now {ZELLIJ_TIMER_NAME}")


# ---------------------------------------------------------------------------
# Informational: manual Zellij / Covey scan commands
# ---------------------------------------------------------------------------

def print_manual_scans(plan, covey_db):
    plan.manual(
        "notifier-ng has no polling: zellij/covey scans are manual commands. "
        "Run any of these when you want a snapshot (each pipes into ingest):"
    )
    plan.manual(f"  covey:  {COVEY_TEMPLATE} --db {covey_db}")
    plan.manual(
        f"  zellij: {ZELLIJ_TEMPLATE} --session <NAME>   "
        "(zellij sessions are not enumerated: running an external CLI during "
        "planning could write to HOME and break dry-run purity; inside a zellij "
        "session the plugin also reads $ZELLIJ_SESSION_NAME and needs no --session)"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Install and wire notifier-ng (default: dry run, no writes)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the changes (default: print them without touching disk)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="user config path (default: $XDG_CONFIG_HOME/notifier-ng/config.json)",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="user state directory (default: $XDG_STATE_HOME/notifier-ng)",
    )
    parser.add_argument(
        "--omp-hooks-dir",
        type=str,
        default=None,
        help="OMP hooks/post directory (default: ~/.omp/agent/hooks/post)",
    )
    parser.add_argument(
        "--codex-config",
        type=str,
        default=None,
        help="codex config.toml path (default: ~/.codex/config.toml)",
    )
    parser.add_argument(
        "--hermes-home",
        type=str,
        default=None,
        help="hermes home (default: resolved — active profile, $HERMES_HOME, or ~/.hermes)",
    )
    parser.add_argument(
        "--unit-dir",
        type=str,
        default=None,
        help="user systemd unit directory for the NZM timer units "
             "(default: $XDG_CONFIG_HOME/systemd/user, else ~/.config/systemd/user)",
    )
    parser.add_argument(
        "--covey-db",
        type=str,
        default=None,
        help="Covey SQLite database path for the printed manual scan "
             "(default: the XDG state directory, notifier-ng/covey.db)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    plan = Plan(apply=args.apply)

    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    omp_hooks_dir = (
        Path(args.omp_hooks_dir).expanduser()
        if args.omp_hooks_dir
        else Path.home() / ".omp" / "agent" / "hooks" / "post"
    )
    codex_config = (
        Path(args.codex_config).expanduser()
        if args.codex_config
        else Path.home() / ".codex" / "config.toml"
    )
    covey_db = (
        Path(args.covey_db).expanduser()
        if args.covey_db
        else default_state_dir() / "covey.db"
    )
    unit_dir = (
        Path(args.unit_dir).expanduser()
        if args.unit_dir
        else default_unit_dir()
    )

    if args.hermes_home:
        hermes_home, _active = Path(args.hermes_home).expanduser(), None
        plan.info(f"hermes home (explicit): {hermes_home}")
    else:
        hermes_home, active = resolve_hermes_home()
        if active:
            plan.info(f"hermes home: {hermes_home} (active profile {active!r})")
        else:
            plan.info(f"hermes home: {hermes_home}")

    step_example_config(plan, config_path, state_dir)
    step_omp_hook(plan, omp_hooks_dir)
    step_codex_notify(plan, codex_config)
    step_hermes(plan, hermes_home)
    step_nzm_timer(plan, unit_dir)
    print_manual_scans(plan, covey_db)

    plan.emit()
    return 1 if plan.refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
