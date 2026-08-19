#!/usr/bin/env python3
"""NZM adapter for notifier-ng (frozen V9 contract).

Consumes two machine commands resolved via PATH:

  * `nzm robot snapshot`     -- authoritative registry lifecycle truth
  * `nzm activity snapshot`  -- stateless probe evidence

Both are validated against the frozen V9 wire schemas (activity evidence:
envelope/fields/enums/RFC3339 enforced exactly, unknown fields rejected;
robot snapshot: required fields/types/enums enforced, extras tolerated),
joined by the stable `<session>:<label>` subject -- a subject present in
either snapshot but missing from the other is a hard failure -- and one
normalized snapshot NDJSON record is emitted per subject:

  * session stopped / agent Stopped          -> state stopped
  * agent Error                              -> state error (needs attention)
  * probe unknown, lifecycle running         -> no record (missing evidence
    must never fabricate a notification)
  * probe observed, first observation        -> baseline, no record
  * probe observed, screen hash changed      -> active (rearm; resets the
    quiet clock)
  * probe observed, unchanged below
      NZM_QUIET_SECONDS (default 900)        -> no record (still active)
  * probe observed, unchanged crossing
      NZM_QUIET_SECONDS                      -> idle with
      metadata.signal = "quiet_only"

While a notifiable state persists (quiet past threshold, stopped, error)
the current evidence is re-emitted on every run: the notifier core owns
delivery deduplication (state/event_id fingerprint plus delivered
transports) and requires consecutive identical emissions before the first
delivery, so one-shot emissions would never be delivered. Rearms always
carry state "active" so the core re-arms its fingerprint state. Records
carry no `event_id` (frozen contract).

Hysteresis lives in `$XDG_STATE_HOME/notifier-ng/nzm-activity.json` (default
state home: `~/.local/state`). A sibling flock target
`nzm-activity.json.lock` is opened with O_CREAT once and never replaced or
removed; LOCK_EX is held through the read -> decide -> atomic-replace-write
-> emit sequence. A corrupt state file exits nonzero and is never
overwritten. Unknown probe evidence leaves state untouched.

Exit status: 0 = complete run (records may be empty); 1 = nzm missing or
failing, schema violation, join failure, corrupt state, or state write
failure, with diagnostics on stderr and empty stdout.
"""

import datetime
import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SOURCE = "nzm"
QUERY_TIMEOUT = 30
DEFAULT_QUIET_SECONDS = 900
STATE_DIR_NAME = "notifier-ng"  # under $XDG_STATE_HOME
STATE_FILE_NAME = "nzm-activity.json"
LOCK_FILE_NAME = "nzm-activity.json.lock"
STATE_VERSION = 1

PROBES = {"observed", "unknown"}
SIGNALS = {"screen_observed", "lifecycle", "probe_failed"}
LIFECYCLES = {"running", "stopped", "error"}
ROBOT_SESSION_STATUSES = {"running", "stopped"}
ROBOT_AGENT_STATUSES = {"Idle", "Busy", "Waiting", "Error", "Unknown", "Stopped"}
STATE_EMITTED = {"active", "idle", "stopped", "error"}

ACTIVITY_ENVELOPE_KEYS = frozenset(("version", "generated_at", "agents"))
ACTIVITY_AGENT_KEYS = frozenset(
    (
        "subject",
        "session",
        "agent_label",
        "agent_type",
        "probe",
        "signal",
        "observed_at",
        "screen_hash",
        "zellij_pane_id",
        "lifecycle",
        "detail",
    )
)
ROBOT_SNAPSHOT_REQUIRED = frozenset(("sessions", "generated_at"))
ROBOT_SESSION_KEYS = frozenset(("name", "status", "layout_path", "created_at", "agents"))
ROBOT_AGENT_KEYS = frozenset(
    (
        "id",
        "label",
        "agent_type",
        "status",
        "zellij_pane_id",
        "session_name",
        "created_at",
    )
)
STATE_ENTRY_KEYS = frozenset(("screen_hash", "hash_seen_at", "emitted"))

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_SCREEN_HASH = re.compile(r"^[0-9a-f]{16}$")
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC = datetime.timezone.utc


class NzmError(Exception):
    """Fatal adapter failure; reported as exit 1 with empty stdout."""


def die(message):
    print(f"{SOURCE}: {message}", file=sys.stderr)
    sys.exit(1)


def fail(path, detail):
    raise NzmError(f"{path}: {detail}")


def nonempty_string(value, path):
    if not isinstance(value, str) or not value:
        fail(path, "must be a non-empty string")
    return value


def nullable_string(value, path):
    if value is not None and (not isinstance(value, str) or not value):
        fail(path, "must be a string or null")
    return value


def nonnegative_int(value, path):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        fail(path, f"must be a non-negative integer or null, got {value!r}")


def parse_rfc3339(value, path, *, utc=False):
    """Parse one RFC3339 timestamp; require UTC when `utc` (frozen contract)."""
    if not isinstance(value, str) or not _RFC3339.match(value):
        fail(path, f"must be an RFC3339 string, got {value!r}")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        fail(path, f"not a valid RFC3339 timestamp {value!r}: {exc}")
    offset = parsed.utcoffset()
    if offset is None:
        fail(path, f"must include a timezone offset, got {value!r}")
    if utc and offset != datetime.timedelta(0):
        fail(path, f"must be expressed in UTC, got {value!r}")
    return parsed


def reject_unknown_keys(mapping, allowed, path):
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        fail(path, f"unknown field(s): {', '.join(unknown)}")


# ---------------------------------------------------------------------------
# robot snapshot: registry lifecycle truth (extras tolerated)
# ---------------------------------------------------------------------------

def validate_robot(doc):
    if not isinstance(doc, dict) or "snapshot" not in doc:
        fail("robot snapshot", "top-level object requires 'snapshot'")
    snap = doc["snapshot"]
    if not isinstance(snap, dict):
        fail("robot snapshot.snapshot", "must be an object")
    for key in ROBOT_SNAPSHOT_REQUIRED:
        if key not in snap:
            fail("robot snapshot.snapshot", f"missing required field {key!r}")
    parse_rfc3339(snap["generated_at"], "robot snapshot.snapshot.generated_at", utc=True)
    sessions = snap["sessions"]
    if not isinstance(sessions, list):
        fail("robot snapshot.snapshot.sessions", "must be an array")
    names = set()
    out = []
    for number, session in enumerate(sessions):
        path = f"robot snapshot.snapshot.sessions[{number}]"
        if not isinstance(session, dict):
            fail(path, "must be an object")
        for key in ROBOT_SESSION_KEYS:
            if key not in session:
                fail(path, f"missing required field {key!r}")
        name = nonempty_string(session["name"], f"{path}.name")
        if name in names:
            fail(f"{path}.name", f"duplicate session {name!r}")
        names.add(name)
        status = session["status"]
        if not isinstance(status, str) or status not in ROBOT_SESSION_STATUSES:
            fail(f"{path}.status", f"unknown session status {status!r}")
        nullable_string(session["layout_path"], f"{path}.layout_path")
        parse_rfc3339(session["created_at"], f"{path}.created_at")
        agents = session["agents"]
        if not isinstance(agents, list):
            fail(f"{path}.agents", "must be an array")
        agent_out = []
        for agent_number, agent in enumerate(agents):
            apath = f"{path}.agents[{agent_number}]"
            if not isinstance(agent, dict):
                fail(apath, "must be an object")
            for key in ROBOT_AGENT_KEYS:
                if key not in agent:
                    fail(apath, f"missing required field {key!r}")
            agent_id = agent["id"]
            if not isinstance(agent_id, str) or not _CANONICAL_UUID.match(agent_id):
                fail(f"{apath}.id", f"must be a canonical UUID string, got {agent_id!r}")
            nonempty_string(agent["label"], f"{apath}.label")
            nonempty_string(agent["agent_type"], f"{apath}.agent_type")
            astatus = agent["status"]
            if not isinstance(astatus, str) or astatus not in ROBOT_AGENT_STATUSES:
                fail(f"{apath}.status", f"unknown agent status {astatus!r}")
            nonnegative_int(agent["zellij_pane_id"], f"{apath}.zellij_pane_id")
            session_name = nonempty_string(agent["session_name"], f"{apath}.session_name")
            if session_name != name:
                fail(
                    f"{apath}.session_name",
                    f"{session_name!r} does not match containing session {name!r}",
                )
            parse_rfc3339(agent["created_at"], f"{apath}.created_at")
            agent_out.append(
                {
                    "id": agent_id,
                    "label": agent["label"],
                    "agent_type": agent["agent_type"],
                    "status": astatus,
                    "zellij_pane_id": agent["zellij_pane_id"],
                    "session_name": session_name,
                    "created_at": agent["created_at"],
                }
            )
        out.append(
            {
                "name": name,
                "status": status,
                "layout_path": session["layout_path"],
                "created_at": session["created_at"],
                "agents": agent_out,
            }
        )
    return out


def robot_index(sessions):
    index = {}
    for session in sessions:
        for agent in session["agents"]:
            subject = f"{session['name']}:{agent['label']}"
            if subject in index:
                fail("robot snapshot", f"duplicate subject {subject!r}")
            index[subject] = {
                "session_status": session["status"],
                "agent_status": agent["status"],
            }
    return index


# ---------------------------------------------------------------------------
# activity snapshot: stateless probe evidence (unknown fields rejected)
# ---------------------------------------------------------------------------

def validate_activity(doc):
    if not isinstance(doc, dict):
        fail("activity snapshot", "must be a JSON object")
    reject_unknown_keys(doc, ACTIVITY_ENVELOPE_KEYS, "activity snapshot")
    version = doc.get("version")
    if isinstance(version, bool) or version != 1:
        fail("activity snapshot.version", "must equal 1")
    parse_rfc3339(doc["generated_at"], "activity snapshot.generated_at", utc=True)
    agents = doc["agents"]
    if not isinstance(agents, list):
        fail("activity snapshot.agents", "must be an array")
    subjects = set()
    out = []
    for number, agent in enumerate(agents):
        path = f"activity snapshot.agents[{number}]"
        if not isinstance(agent, dict):
            fail(path, "must be an object")
        reject_unknown_keys(agent, ACTIVITY_AGENT_KEYS, path)
        subject = nonempty_string(agent["subject"], f"{path}.subject")
        if subject in subjects:
            fail(f"{path}.subject", f"duplicate subject {subject!r}")
        subjects.add(subject)
        session = nonempty_string(agent["session"], f"{path}.session")
        label = nonempty_string(agent["agent_label"], f"{path}.agent_label")
        expected = f"{session}:{label}"
        if subject != expected:
            fail(f"{path}.subject", f"{subject!r} does not match session:agent_label {expected!r}")
        nonempty_string(agent["agent_type"], f"{path}.agent_type")
        probe = agent["probe"]
        signal = agent["signal"]
        lifecycle = agent["lifecycle"]
        if not isinstance(probe, str) or probe not in PROBES:
            fail(f"{path}.probe", f"unknown probe {probe!r}")
        if not isinstance(signal, str) or signal not in SIGNALS:
            fail(f"{path}.signal", f"unknown signal {signal!r}")
        if not isinstance(lifecycle, str) or lifecycle not in LIFECYCLES:
            fail(f"{path}.lifecycle", f"unknown lifecycle {lifecycle!r}")
        observed = parse_rfc3339(agent["observed_at"], f"{path}.observed_at")
        nonnegative_int(agent["zellij_pane_id"], f"{path}.zellij_pane_id")
        screen_hash = agent["screen_hash"]
        if screen_hash is not None and (
            not isinstance(screen_hash, str) or not _SCREEN_HASH.match(screen_hash)
        ):
            fail(f"{path}.screen_hash", f"must be 16 lowercase hex digits or null, got {screen_hash!r}")
        detail = agent["detail"]
        if detail is not None and not isinstance(detail, str):
            fail(f"{path}.detail", "must be a string or null")
        truth = (probe, signal, lifecycle, screen_hash is not None)
        allowed = {
            ("observed", "screen_observed", "running", True),
            ("unknown", "lifecycle", "stopped", False),
            ("unknown", "lifecycle", "error", False),
            ("unknown", "probe_failed", "running", False),
        }
        if truth not in allowed:
            fail(
                path,
                "evidence combination "
                f"(probe={probe!r}, signal={signal!r}, lifecycle={lifecycle!r}, "
                f"screen_hash={'set' if screen_hash is not None else 'null'}) "
                "is outside the frozen V9 producer contract",
            )
        out.append(
            {
                "subject": subject,
                "session": session,
                "agent_label": label,
                "agent_type": agent["agent_type"],
                "probe": probe,
                "signal": signal,
                "observed_at": agent["observed_at"],
                "observed_dt": observed,
                "screen_hash": screen_hash,
                "zellij_pane_id": agent["zellij_pane_id"],
                "lifecycle": lifecycle,
                "detail": detail,
            }
        )
    return {"generated_at": doc["generated_at"], "agents": out}


def join(robot, activity):
    robot_subjects = set(robot)
    activity_subjects = [agent["subject"] for agent in activity["agents"]]
    only_activity = sorted(set(activity_subjects) - robot_subjects)
    only_robot = sorted(robot_subjects - set(activity_subjects))
    problems = []
    if only_activity:
        problems.append("in activity but missing from robot snapshot: " + ", ".join(only_activity))
    if only_robot:
        problems.append("in robot snapshot but missing from activity: " + ", ".join(only_robot))
    if problems:
        raise NzmError("join failure: " + "; ".join(problems) + " (frozen contract: both complete snapshots must agree on every subject)")


# ---------------------------------------------------------------------------
# nzm command execution
# ---------------------------------------------------------------------------

def run_nzm_json(nzm, *argv, label):
    try:
        proc = subprocess.run(
            [nzm, *argv],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        fail(label, f"timed out after {QUERY_TIMEOUT}s")
    except OSError as exc:
        fail(label, f"could not execute {nzm!r}: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        fail(label, f"exited {proc.returncode}{f': {detail}' if detail else ''}")
    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        fail(label, f"stdout is not one JSON document (protocol must be stdout-pure): {exc}")
    return doc


# ---------------------------------------------------------------------------
# hysteresis state: XDG state file under a stable sibling flock
# ---------------------------------------------------------------------------

def state_paths():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return (
        os.path.join(base, STATE_DIR_NAME, STATE_FILE_NAME),
        os.path.join(base, STATE_DIR_NAME, LOCK_FILE_NAME),
    )


def read_state(path):
    if not os.path.exists(path):
        return {"version": STATE_VERSION, "agents": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise NzmError(f"corrupt state file {path} (not overwritten): {exc}")
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise NzmError(f"corrupt state file {path} (not overwritten): expected version {STATE_VERSION} object")
    agents = value.get("agents")
    if not isinstance(agents, dict):
        raise NzmError(f"corrupt state file {path} (not overwritten): 'agents' must be an object")
    for subject, entry in agents.items():
        if not isinstance(subject, str) or not isinstance(entry, dict):
            raise NzmError(f"corrupt state file {path} (not overwritten): non-object entry for {subject!r}")
        reject_unknown_keys(entry, STATE_ENTRY_KEYS, f"state agents[{subject!r}]")
        screen_hash = entry.get("screen_hash")
        if screen_hash is not None and (
            not isinstance(screen_hash, str) or not _SCREEN_HASH.match(screen_hash)
        ):
            raise NzmError(f"corrupt state file {path} (not overwritten): bad screen_hash for {subject!r}")
        seen = entry.get("hash_seen_at")
        if seen is not None:
            parse_rfc3339(seen, f"state agents[{subject!r}].hash_seen_at")
        emitted = entry.get("emitted")
        if emitted is not None and emitted not in STATE_EMITTED:
            raise NzmError(f"corrupt state file {path} (not overwritten): bad emitted state for {subject!r}")
    return value


def write_state(path, state):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".nzm-activity.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise NzmError(f"could not write state file {path}: {exc}")


def lock_exclusive(lock_path):
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise NzmError(f"could not open lock file {lock_path}: {exc}")
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            os.close(fd)
            raise NzmError(f"could not lock {lock_path}: {exc}")


# ---------------------------------------------------------------------------
# emission + quiet policy (NZM_QUIET_SECONDS, default 900)
# ---------------------------------------------------------------------------

def quiet_seconds_from_env():
    raw = os.environ.get("NZM_QUIET_SECONDS")
    if raw is None:
        return DEFAULT_QUIET_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise NzmError(f"NZM_QUIET_SECONDS must be a non-negative integer, got {raw!r}")
    if value < 0:
        raise NzmError(f"NZM_QUIET_SECONDS must be a non-negative integer, got {raw!r}")
    return value


def record(subject, state, *, robot_session_status, robot_agent_status, activity, generated_at, quiet=None):
    meta = {
        "robot_status": robot_agent_status,
        "session_status": robot_session_status,
        "probe": activity["probe"],
        "evidence_signal": activity["signal"],
        "lifecycle": activity["lifecycle"],
        "observed_at": activity["observed_at"],
        "generated_at": generated_at,
    }
    screen_hash = activity["screen_hash"]
    if screen_hash is not None:
        meta["screen_hash"] = screen_hash
    pane_id = activity["zellij_pane_id"]
    if pane_id is not None:
        meta["pane_id"] = pane_id
    detail = activity["detail"]
    if detail is not None:
        meta["detail"] = detail
    if quiet:
        meta.update(quiet)
    return {
        "version": 1,
        "source": SOURCE,
        "subject": subject,
        "state": state,
        "mode": "snapshot",
        "metadata": meta,
    }


def utc_iso(when):
    return when.astimezone(_UTC).isoformat()




def main():
    try:
        nzm = shutil.which("nzm")
        if nzm is None:
            raise NzmError("nzm executable not found on PATH")
        quiet_seconds = quiet_seconds_from_env()
        robot_doc = run_nzm_json(nzm, "robot", "snapshot", label="nzm robot snapshot")
        activity_doc = run_nzm_json(nzm, "activity", "snapshot", label="nzm activity snapshot")
        sessions = validate_robot(robot_doc)
        activity = validate_activity(activity_doc)
        robot = robot_index(sessions)
        join(robot, activity)

        state_path, lock_path = state_paths()
        directory = os.path.dirname(state_path)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise NzmError(f"could not create state directory {directory}: {exc}")

        lock_fd = lock_exclusive(lock_path)
        try:
            state = read_state(state_path)
            previous = state["agents"]
            new_agents = {}
            out = []
            for agent in activity["agents"]:
                subject = agent["subject"]
                info = robot[subject]
                session_status = info["session_status"]
                robot_status = info["agent_status"]
                entry = previous.get(subject)
                if session_status == "stopped" or robot_status == "Stopped":
                    decision = "stopped"
                elif robot_status == "Error":
                    decision = "error"
                elif agent["probe"] == "unknown":
                    # No evidence: never fabricate a record; preserve any
                    # existing hysteresis state untouched.
                    if entry is not None:
                        new_agents[subject] = dict(entry)
                    continue
                elif entry is None:
                    # First observation baselines as active: no record.
                    new_agents[subject] = {
                        "screen_hash": agent["screen_hash"],
                        "hash_seen_at": utc_iso(agent["observed_dt"]),
                        "emitted": None,
                    }
                    continue
                else:
                    if entry["screen_hash"] != agent["screen_hash"]:
                        # Rearm: changed screen evidence resets the quiet
                        # clock; emit active on the transition into a
                        # different emitted state (churn while already
                        # active stays silent).
                        new_agents[subject] = {
                            "screen_hash": agent["screen_hash"],
                            "hash_seen_at": utc_iso(agent["observed_dt"]),
                            "emitted": "active",
                        }
                        if entry["emitted"] != "active":
                            out.append(
                                record(
                                    subject,
                                    "active",
                                    robot_session_status=session_status,
                                    robot_agent_status=robot_status,
                                    activity=agent,
                                    generated_at=activity["generated_at"],
                                    quiet={
                                        "quiet_for_seconds": 0,
                                        "quiet_threshold": quiet_seconds,
                                        "quiet_since": new_agents[subject]["hash_seen_at"],
                                    },
                                )
                            )
                        continue
                    # Unchanged hash: quiet calculus from the first
                    # observation of the current hash.
                    seen_at = parse_rfc3339(entry["hash_seen_at"], f"state agents[{subject!r}].hash_seen_at")
                    elapsed = max(0.0, (agent["observed_dt"] - seen_at).total_seconds())
                    if entry["emitted"] == "idle" or elapsed >= quiet_seconds:
                        new_agents[subject] = {
                            "screen_hash": agent["screen_hash"],
                            "hash_seen_at": entry["hash_seen_at"],
                            "emitted": "idle",
                        }
                        out.append(
                            record(
                                subject,
                                "idle",
                                robot_session_status=session_status,
                                robot_agent_status=robot_status,
                                activity=agent,
                                generated_at=activity["generated_at"],
                                quiet={
                                    "signal": "quiet_only",
                                    "quiet_for_seconds": int(elapsed),
                                    "quiet_threshold": quiet_seconds,
                                    "quiet_since": entry["hash_seen_at"],
                                },
                            )
                        )
                    else:
                        new_agents[subject] = dict(entry)
                    continue

                # Lifecycle stopped/error: authoritative override of probe
                # evidence; re-emitted every run while it persists so the
                # core's prime-then-deliver flow can deliver exactly once.
                new_agents[subject] = {
                    "screen_hash": None,
                    "hash_seen_at": None,
                    "emitted": decision,
                }
                out.append(
                    record(
                        subject,
                        decision,
                        robot_session_status=session_status,
                        robot_agent_status=robot_status,
                        activity=agent,
                        generated_at=activity["generated_at"],
                    )
                )

            write_state(state_path, {"version": STATE_VERSION, "agents": new_agents})
            for rec in out:
                print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
    except NzmError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
