# notifier-ng

Pluggable idle-event notifier with durable deduplication.

notifier-ng turns "an agent or terminal session became idle, stopped, or
failed" into a single push notification. Adapters for agent runtimes and
terminal tools emit normalized NDJSON events; the core validates, redacts,
deduplicates, optionally summarizes, and forwards them to push transports.
The core, the installer, and every source plugin are Python 3 with the
standard library only (no third-party dependencies, no daemon, no network
polling), on a POSIX platform: state locking uses `fcntl.flock`, so native
Windows is not supported. An optional adapter for the Oh My Pi harness
("OMP") is provided, and the OMP adapter is exercised under the Bun runtime.

This README stands alone: every command, path, schema, and default below is
taken from the code in this repository.

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
  - [What the installer manages](#what-the-installer-manages)
- [Quick start (offline smoke test)](#quick-start-offline-smoke-test)
- [Configuration](#configuration)
  - [Config and state paths](#config-and-state-paths)
  - [Config schema](#config-schema)
  - [Transports](#transports)
  - [Network and transport security policy](#network-and-transport-security-policy)
  - [Environment indirection](#environment-indirection)
- [Event schema (normalized NDJSON)](#event-schema-normalized-ndjson)
- [Deduplication and delivery semantics](#deduplication-and-delivery-semantics)
- [Summarizer (optional)](#summarizer-optional)
  - [Summarizer configuration](#summarizer-configuration)
  - [Summarizer protocol (five-field request / response)](#summarizer-protocol-five-field-request--response)
  - [OpenAI-compatible adapter environment](#openai-compatible-adapter-environment)
  - [Summarizer failure semantics](#summarizer-failure-semantics)
- [Privacy and security defaults](#privacy-and-security-defaults)
  - [Redaction boundaries](#redaction-boundaries)
- [Source plugins](#source-plugins)
  - [Source plugin contract](#source-plugin-contract)
  - [Codex](#codex)
  - [Hermes](#hermes)
  - [Oh My Pi (OMP)](#oh-my-pi-omp)
  - [Covey](#covey)
  - [Zellij](#zellij)
- [Operational commands](#operational-commands)
- [Troubleshooting](#troubleshooting)
- [Development and verification](#development-and-verification)
- [Repository layout](#repository-layout)
- [Limitations](#limitations)
- [License](#license)

## How it works

Source adapters translate harness or terminal signals into normalized NDJSON
lines. The core (`notifier_ng.py`) ingests those lines, rejects anything that
does not match the strict event schema, redacts obvious credentials, applies
durable per-subject deduplication, optionally asks a summarizer subprocess for
a short summary when context is present and enabled, and POSTs to each
configured transport once per distinct event. Delivery state is persisted
atomically to a single state file so duplicate or repeated ingests are
no-ops, and a delivery that failed is retried on the next matching ingest
without re-sending to transports that already succeeded.

```mermaid
flowchart LR
    subgraph Sources
        A[Codex CLI hooks]
        B[Hermes shell hooks]
        C[OMP agent_end]
        D[Zellij sessions]
        E[Covey database]
        F[stdin / custom producer]
    end
    A --> P1[plugins/codex.py]
    B --> P2[plugins/hermes.py]
    C --> P3[integrations/omp-notifier.ts]
    D --> P4[plugins/zellij.py]
    E --> P5[plugins/covey.py]
    P1 --> N[(normalized NDJSON)]
    P2 --> N
    P3 --> N
    P4 --> N
    P5 --> N
    F --> N
    N --> CORE[notifier_ng.py]
    CORE --> R[validate + redact + dedupe]
    R --> SUM[optional summarizer subprocess]
    R --> OUT1[ntfy]
    R --> OUT2[Home Assistant]
    SUM --> OUT1
    SUM --> OUT2
    CORE -.-> ST[state.json]
```

Everything runs locally and on demand. Nothing polls: the core is a
single-shot CLI that processes whatever NDJSON it is handed (stdin for
`ingest`, a source plugin's stdout for `source`) and exits. The installer
wires up the hooks that call it.

## Requirements

- A POSIX/Unix platform providing Python's `fcntl` module. The core and the
  installer use `fcntl.flock` for state locking, which is not available on
  native Windows; Linux is the supported target.
- Python 3.11 or newer. The core, installer, and plugins use only the
  standard library (`tomllib` in the installer requires 3.11). No `pip`
  install is needed.
- Optional, for the OMP integration: the Bun runtime (package metadata
  declares `bun >= 1.0.0` and the OMP adapter's types come from
  `@oh-my-pi/pi-coding-agent`, provided by the OMP harness at runtime).
  Nothing in the Python side ever touches Node.
- Optional, for the Zellij plugin: the `zellij` executable on `PATH`.
- Optional, for the Covey plugin: a read-only accessible Covey SQLite
  database.
- Push endpoints you control: an [ntfy](https://docs.ntfy.sh/) server/topic
  and/or a [Home Assistant](https://www.home-assistant.io/integrations/notify/)
  instance with a notify service. Loopback HTTP endpoints work without any
  further setup (see [Network and transport security
  policy](#network-and-transport-security-policy)).

## Installation

The installer (`install.py`) wires notifier-ng into the OMP, Codex, and
Hermes harnesses and manages the NZM source timer units in one run. It
never touches your shell profile, never
replaces files it does not own, and performs **zero writes by default**:

```bash
cd /path/to/notifier-ng

# Dry run: prints every change that WOULD be made; nothing on disk is touched.
python3 install.py

# Review the printed plan, then apply it:
python3 install.py --apply
```

`--apply` performs the whole plan (harness wiring plus writing the NZM
timer units) together; there is no per-source flag. Activating the timer
is a printed manual step — the installer never runs `systemctl`. If you
want only one harness wired, do not apply the all-harness plan: follow
that adapter's manual recipe under
[Source plugins](#source-plugins).

Application is safe and idempotent: running the installer twice reports
already-applied steps as no-ops, and a step that cannot be applied safely is
refused (reported with `[refuse]`, exit status 1) rather than clobbered.
Exit status is 0 when every step succeeded or was already in place, and 1
when any step was refused.

### Configure a live endpoint before applying

`config.example.json` is a shape-valid example, not a live config: its
transport URLs use the placeholder domains
`home-assistant.example.invalid` and `ntfy.example.invalid`, which resolve to
no service you control. A hook that fires before you replace them fails at
delivery time. Because the installer validates an existing config with the
core schema during the dry run, the safe order is:

1. Put a config that points at endpoints you control in place first --
   `~/.config/notifier-ng/config.json` by default, or anywhere via
   `--config PATH` -- with tokens supplied through `env_file` / `token_env`
   (see [Configuration](#configuration)). For a first test, a loopback
   plain-HTTP ntfy-style transport needs no external service at all; the
   [Quick start](#quick-start-offline-smoke-test) shows one.
2. Run `python3 install.py` (dry run). If the config path is absent, the plan
   includes copying the non-live example config; create or edit the real config,
   then rerun the dry run until the existing config validates cleanly.
3. Only when the printed plan references your live config and is otherwise
   clean, run `python3 install.py --apply`.

A config the installer created from the example is never overwritten: edit
it in place and re-run the installer to have it re-validated.

Customize what the installer touches with its flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--apply` | (dry run) | Perform the printed changes instead of only printing them |
| `--config PATH` | `$XDG_CONFIG_HOME/notifier-ng/config.json` | User config path the installer validates/creates |
| `--state-dir PATH` | `$XDG_STATE_HOME/notifier-ng` | User state directory |
| `--omp-hooks-dir PATH` | `~/.omp/agent/hooks/post` | OMP hooks/post directory |
| `--codex-config PATH` | `~/.codex/config.toml` | Codex TOML config to manage |
| `--hermes-home PATH` | resolved (see below) | Hermes home directory |
| `--unit-dir PATH` | `$XDG_CONFIG_HOME/systemd/user`, else `~/.config/systemd/user` | User systemd unit directory receiving the NZM timer units |
| `--covey-db PATH` | `$XDG_STATE_HOME/notifier-ng/covey.db` | Covey database path for the printed manual scan. This default is the XDG state location, independent of `--state-dir` |

When `--hermes-home` is not given, resolution is first-match-wins: the
active profile (`~/.hermes/active_profile` naming an existing
`~/.hermes/profiles/<profile>/config.yaml`), then `$HERMES_HOME`, then
`~/.hermes`.

### What the installer manages

| Step | Target | Effect |
| --- | --- | --- |
| Config/state dirs and example config | XDG config/state dirs, config path | Creates the directories; copies [config.example.json](config.example.json) to the config path only when absent; an existing config is validated with the core schema and never overwritten |
| OMP post hook | `~/.omp/agent/hooks/post/notifier-ng.ts` | Symlinks [integrations/omp-notifier.ts](integrations/omp-notifier.ts) for harness auto-discovery; on symlink-less filesystems writes a small wrapper instead. Both forms reference this checkout: a moved checkout requires re-running the installer (see [Oh My Pi](#oh-my-pi-omp)) |
| Codex legacy notify | `~/.codex/config.toml` | Sets the top-level `notify` array to this checkout's `notifier_ng.py source plugins/codex.py` (the array IS the argv vector of one command; Codex appends the payload JSON, which the core forwards to the plugin). Every other line is preserved byte-for-byte; no-op when the value already equals that exact array |
| Hermes shell hooks | resolved Hermes home | Never edits an existing `config.yaml` (prints the exact fragment to merge); creates a minimal hooks-only `config.yaml` when none exists; merges allowlist entries for `on_session_end` and `on_session_finalize` using Hermes' documented schema. Hook commands route through the core so Hermes sees a no-op response on stdout |
| NZM source timer | `$XDG_CONFIG_HOME/systemd/user/notifier-ng-nzm.service` + `.timer` | Writes the every-minute (`OnCalendar=*-*-* *:*:00`, `Persistent=true`) user units invoking `notifier_ng.py source plugins/nzm.py` under an explicit PATH of the resolved nzm/zellij/python3/system directories; refuses before writing when `nzm` or `zellij` is unresolvable. Identical existing units are no-ops, divergent ones are refused (never clobbered); existing `notifier-ng-zellij` units are untouched. Activation is manual: the installer prints the exact `systemctl --user daemon-reload` / `enable` commands and flags the optional disable of the legacy zellij timer |
| Zellij / Covey scans | nothing | Prints ready-to-use manual scan commands (see [Zellij](#zellij) and [Covey](#covey)); no external CLI is spawned during planning |

A step that cannot be applied safely is refused, never clobbered: malformed
existing config (bad JSON, unknown keys, broken transports, unreadable env
files), malformed TOML or allowlist JSON, or a foreign file at the OMP hook
path (remove it and re-run).

For a subset install, skip `--apply` and use the complete manual recipe in the
wanted adapter's [Source plugins](#source-plugins) section.

## Quick start (offline smoke test)

This exercises the real CLI end to end against a loopback capture endpoint:
ingest one event, observe exactly one delivery with the status-only default
body, then observe that the identical event is suppressed by durable
deduplication. No network access and no external service are involved.

```bash
cd /path/to/notifier-ng

# 0. Private scratch directory. Everything lives below it and the EXIT/INT/TERM
#    trap removes it (and stops the capture server) no matter how the script
#    ends, so no fixed /tmp paths are touched and concurrent runs do not collide.
DEMO=$(mktemp -d)
trap 'kill "$CAP_PID" 2>/dev/null; rm -rf "$DEMO"' EXIT INT TERM

# 1. Local capture endpoint on an OS-assigned loopback port (binding port 0
#    lets the OS pick a free one; loopback plain HTTP is allowed by default).
#    The server records its port in the scratch directory, and the script
#    waits for that file before ingesting, so no hardcoded or guessed port is
#    used.
python3 - "$DEMO" <<'EOF' &
import http.server, os, sys
demo = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        with open(os.path.join(demo, "capture.log"), "a", buffering=1) as log:
            log.write(f"{self.path} Title={self.headers.get('Title','')!r} "
                      f"body={self.rfile.read(n).decode()!r}\n")
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
with open(os.path.join(demo, "port"), "w") as f:
    f.write(str(httpd.server_address[1]))
httpd.serve_forever()
EOF
CAP_PID=$!
for _ in $(seq 1 50); do [ -s "$DEMO/port" ] && break; sleep 0.1; done
PORT=$(cat "$DEMO/port")
[ -n "$PORT" ] || { echo "capture server did not start" >&2; exit 1; }

# 2. Minimal config: one loopback ntfy-style transport. The core defaults
#    (include_message_text=false, allow_remote_context=false) apply.
cat > "$DEMO/config.json" <<EOF
{
  "transports": [
    { "id": "capture", "type": "ntfy", "url": "http://127.0.0.1:$PORT/demo" }
  ]
}
EOF

# 3. Ingest one idle event.
printf '%s\n' '{"version":1,"source":"manual","subject":"demo","state":"idle","mode":"event","event_id":"1"}' \
  | python3 notifier_ng.py --config "$DEMO/config.json" --state "$DEMO/state.json" ingest
cat "$DEMO/capture.log"
#   Expected: exactly one POST; Title='manual: idle'; body='demo is idle'
#   (message text is NOT delivered by default).

# 4. The identical event again: suppressed by durable dedup.
printf '%s\n' '{"version":1,"source":"manual","subject":"demo","state":"idle","mode":"event","event_id":"1"}' \
  | python3 notifier_ng.py --config "$DEMO/config.json" --state "$DEMO/state.json" ingest
cat "$DEMO/capture.log"
#   Expected: unchanged -- no second POST.

kill "$CAP_PID"
```

The delivery state lives in `$DEMO/state.json` (created by the ingest, mode
0600). To see a re-delivery, change the policy (for example add
`"include_message_text": true` to the config): toggling a privacy flag
resets per-key delivery state so the next matching event is delivered again
under the new policy. The scratch directory and capture server are cleaned
up by the trap regardless of how the script ends.

## Configuration

### Config and state paths

- Config: `$XDG_CONFIG_HOME/notifier-ng/config.json`, or
  `~/.config/notifier-ng/config.json` when `XDG_CONFIG_HOME` is unset.
  Override with `--config` on every invocation.
- State: `$XDG_STATE_HOME/notifier-ng/state.json`, or
  `~/.local/state/notifier-ng/state.json` when `XDG_STATE_HOME` is unset.
  Override with `--state` on every invocation.

The installer creates both directories and places
[config.example.json](config.example.json) at the config path when no config
exists. [config.summarizer.example.json](config.summarizer.example.json) is
the same config with the summarizer enabled and the two privacy flags set to
`true`.

### Config schema

The config is a JSON object with only these top-level keys; anything else is
rejected:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `env_file` | string | — | dotenv-style file loaded before validation (see [Environment indirection](#environment-indirection)) |
| `env_files` | array of strings | — | multiple env files, loaded in order |
| `transports` | non-empty array | — | delivery transports (see below) |
| `summarizer` | object | absent | optional summarizer (see [Summarizer](#summarizer-optional)) |
| `include_message_text` | boolean | `false` | send `event.message` in the notification body |
| `allow_remote_context` | boolean | `false` | permit sending conversation context to the summarizer |

Boolean keys must be JSON booleans; a string, number, or explicit `null` is
rejected.

### Transports

Each transport object accepts exactly these keys (unknown keys are
rejected):

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | string (optional) | explicit transport id; when omitted, a stable 16-hex SHA-256 of `(type, url, service, token_env, allow_insecure_http)` is derived. Duplicate ids are rejected. |
| `type` | `"ntfy"` \| `"home_assistant"` | transport protocol |
| `url` | string | absolute http(s) URL; must NOT embed `user:pass@` credentials (use `token_env`) |
| `url_env` | string | alternative to `url`: name of an environment variable holding the URL; exactly one of `url`/`url_env` may be set, and the named variable must be set and non-empty |
| `token_env` | string | name of an environment variable holding the bearer token; required for `home_assistant` |
| `allow_insecure_http` | boolean, default `false` | permit plain HTTP to a non-loopback host |
| `service` | string | `home_assistant` only: the notify service name (e.g. `mobile_app_your_device`) |

Request shapes (as built by the core):

- **ntfy**: `POST <url>` with headers `Authorization: Bearer <token>` (when
  `token_env` is configured), `Title: <title>`, `Tags: <state>`, and
  `Content-Type: text/plain; charset=utf-8`; the body is the notification
  text. Compatible with [ntfy](https://docs.ntfy.sh/) servers.
- **home_assistant**: `POST <url>/api/services/notify/<service>` with
  `Authorization: Bearer <token>`, `Content-Type: application/json`, and a
  JSON body `{"title": <title>, "message": <message>}`. Compatible with the
  [Home Assistant notify
  integration](https://www.home-assistant.io/integrations/notify/).

### Network and transport security policy

- HTTPS endpoints are always allowed.
- Plain HTTP to `localhost`, any `127.0.0.0/8` literal, or `::1` is always
  allowed.
- Plain HTTP to any other host is rejected unless `allow_insecure_http:
  true` (transport) or `NOTIFIER_LLM_ALLOW_INSECURE_HTTP=true`
  (summarizer). The rejection happens at config load, before any request is
  made.
- Credentials embedded in a URL (`https://user:pass@host/...`) are rejected;
  the supported channel is a bearer token read from the environment by
  `token_env`.
- Redirects are never followed. A 3xx response is reported as an error
  instead of replaying the request at `Location`, so the bearer token is
  never sent to an origin or scheme that did not pass the endpoint policy.

### Environment indirection

Secrets and machine-dependent values stay out of config files:

- `env_file` / `env_files` load a dotenv subset before validation:
  `KEY=VALUE` lines, `#` comments, an optional `export ` prefix, and
  single/double-quoted values are stripped. Loaded variables only fill
  unset entries (`setdefault`) and are never printed or persisted.
- `url_env` and `token_env` name environment variables that supply the
  endpoint URL and bearer token at runtime.
- `NOTIFIER_NG_INGEST` overrides where the OMP adapter finds
  `notifier_ng.py` (see [Oh My Pi](#oh-my-pi-omp)).
- `NOTIFIER_LLM_*` variables configure the OpenAI-compatible summarizer
  adapter (see [OpenAI-compatible adapter environment](#openai-compatible-adapter-environment)).

## Event schema (normalized NDJSON)

Events are newline-delimited JSON objects. One event per line; blank lines
are ignored. Every field is validated strictly and unknown fields are
rejected.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | `1` | yes | must equal `1`. The check is an equality test, not a type check: JSON `1.0` or `true` would also pass, so emit the literal integer `1` |
| `source` | non-empty string | yes | producer name (e.g. `codex`, `hermes`, `omp`, `covey`, `zellij`) |
| `subject` | non-empty string | yes | stable identity of the thing that became idle/stopped; with `source` it forms the dedup key |
| `state` | `"active"` \| `"idle"` \| `"stopped"` \| `"error"` | yes | current state; only `idle`, `stopped`, `error` trigger notifications |
| `mode` | `"event"` \| `"snapshot"` | yes | `event` = a discrete signal; `snapshot` = a scan result |
| `event_id` | string | no | event identity; with `state` it forms the dedup fingerprint |
| `title` | string | no | notification title; falls back to `source: state` |
| `message` | string | no | free text; only delivered when `include_message_text` is true |
| `url` | string | no | appended to the notification body |
| `timestamp` | string | no | free-form timestamp (plugins emit ISO-8601) |
| `metadata` | object | no | free-form non-secret context; never sent to transports and never persisted |
| `context` | object | no | `{"items": [{"role": "user" \| "assistant", "text": string}]}`; the only data a summarizer may see |

Minimal valid event:

```json
{"version":1,"source":"manual","subject":"demo","state":"idle","mode":"event"}
```

Validation rules enforced by the core: `version` must be 1; `state` and
`mode` must be one of the enumerated values; `context.items[].role` must be
`user` or `assistant`; `metadata` must be an object; every other field must
be a string when present. A malformed line anywhere in the input aborts the
whole ingest (exit status 2) and nothing is delivered.

## Deduplication and delivery semantics

Delivery state is persisted in the state file with the shape
`{"version": 1, "subjects": {<key>: {"fingerprint": ..., "delivered": [transport ids, ...], ...}}}`.

- **Identity**: `key = source + "\0" + subject`; `fingerprint = state +
  "\0" + (event_id or "")`.
- **First sighting of a key**: an `event`-mode record in a notify state
  (`idle`, `stopped`, `error`) is delivered. A first `snapshot` record
  (e.g. an `active` baseline) is not delivered but establishes the entry --
  it re-arms the subject.
- **Fingerprint change** (state or `event_id` changed): the entry resets and
  the record is delivered when its state is a notify state, for any mode.
- **Same fingerprint**: a record whose state is a notify state is considered
  deliverable, but a transport already listed in `delivered` is skipped; the
  net effect is that repeated identical records (e.g. repeated scans, a
  hook firing twice) are silent. An `active` record under any fingerprint
  never notifies and clears the delivered sets, so the next `idle`/`stopped`/
  `error` for the same key delivers again.
- **Partial failure and retries**: a transport that raises during delivery is
  NOT added to `delivered`; its error is printed to stderr and the process
  exits 1. The next ingest of a record with the same fingerprint retries
  only the failed transport(s); transports that already succeeded are never
  re-sent.
- **Concurrency**: the state file is guarded by an advisory `flock`
  (`<state>.lock`); concurrent identical ingests serialize and the second
  observes the first's deliveries, producing a single request.
- **Durable writes**: state is written atomically (temp file, `fsync`,
  rename, directory `fsync`) with mode 0600. A corrupt or shape-invalid
  state file is detected and reported as an error -- it is never silently
  overwritten.
- **Privacy-policy changes**: toggling `include_message_text` or
  `allow_remote_context` changes the recorded `delivery_policy_hash`, which
  resets each key's `delivered` list, so the next matching event is
  redelivered under the new policy. The same hashing gates summarizer cache
  identity (see below).

## Summarizer (optional)

The summarizer is completely off by default: with `allow_remote_context` at
its default `false`, conversation context never leaves the machine and no
subprocess is spawned. Summarization happens only when all four of these
hold: `"allow_remote_context": true` in the config, a `summarizer` block,
an incoming event whose state is in `summarizer.states`, and non-empty
`context` on that event. When all
conditions hold, the core redacts and bounds the context, runs the
summarizer, and on success replaces the notification body with the summary.

### Summarizer configuration

| Key | Type | Default | Bounds | Meaning |
| --- | --- | --- | --- | --- |
| `command` | non-empty array of strings | — | — | argv of the summarizer executable |
| `timeout_seconds` | integer | 8 | 1–60 | deadline; the process group is SIGKILLed on expiry |
| `last_items` | integer | 6 | 1–20 | newest context items to keep |
| `max_item_chars` | integer | 1200 | 200–8000 | per-item character cap |
| `max_context_chars` | integer | 5000 | 1000–50000 | aggregate context character cap |
| `max_summary_chars` | integer | 450 | 80–1000 | summary length cap (also sent in the request) |
| `max_summary_output_bytes` | integer | 4096 | 1024–1048576 | stdout cap; must be at least `4 * max_summary_chars + 256` |
| `states` | non-empty array | `["idle"]` | subset of `idle`, `stopped`, `error` | which event states are allowed to trigger summarization |

Context sent to the summarizer is first redacted (see [Redaction
boundaries](#redaction-boundaries)), whitespace-collapsed, trimmed per item
to `max_item_chars`, cut to the newest `last_items` items, and bounded in
aggregate to `max_context_chars`, keeping newest items first subject to the
cap.

### Summarizer protocol (five-field request / response)

The core writes exactly one JSON object to the summarizer's stdin:

```json
{
  "version": 1,
  "source": "codex",
  "state": "idle",
  "context": {
    "items": [
      {"role": "user", "text": "…"},
      {"role": "assistant", "text": "…"}
    ]
  },
  "max_summary_chars": 450
}
```

The summarizer must print exactly one JSON object line to stdout:

```json
{"version": 1, "summary": "one short factual sentence"}
```

Contract details: the request has exactly the five fields above (unknown or
missing fields are a failure); `context.items[].role` is restricted to
`user`/`assistant`; the response must be an object with exactly `version`
(1) and `summary` (string). The summary is whitespace-normalized and is
accepted only when non-empty and no longer than `max_summary_chars`. Output
larger than `max_summary_output_bytes`, a nonzero exit, a subprocess timeout,
or invalid/mismatched JSON all count as failure. The core applies a single
deadline — `timeout_seconds` (default 8, maximum 60) — after which the
summarizer's process group is SIGKILLed; nothing the summarizer writes to
stderr is echoed.

### OpenAI-compatible adapter environment

[summarizers/openai_compatible.py](summarizers/openai_compatible.py) is an
included, stdlib-only summarizer for any OpenAI-compatible chat-completions
endpoint. Its environment contract:

| Variable | Meaning |
| --- | --- |
| `NOTIFIER_LLM_BASE_URL` | required; base URL of the endpoint (the adapter POSTs to `<base>/chat/completions`). Must be an absolute http(s) URL with no embedded credentials. |
| `NOTIFIER_LLM_MODEL` | required; model name. |
| `NOTIFIER_LLM_API_KEY` | optional; sent as `Authorization: Bearer <value>` only when non-empty. |
| `NOTIFIER_LLM_API_KEY_ENV` | optional; value is the name of another environment variable that holds the credential (e.g. `NOTIFIER_LLM_API_KEY_ENV=OPENAI_API_KEY`). Lets a non-secret config file point at an already-existing secret without copying it. When set, the named target must be set and non-empty. |
| `NOTIFIER_LLM_ALLOW_INSECURE_HTTP` | optional, default `false`; must be exactly `"true"` or `"false"` — any other value (including `TRUE`, `"true "`, `1`, `yes`) is a configuration error. Plain HTTP to a non-loopback host is rejected unless `"true"`. |

Requests are sent with `temperature: 0` and `max_tokens` equal to
`max_summary_chars`; a one-sentence, no-markdown, no-verbatim-secrets prompt
is built from the request fields. The adapter's HTTP timeout is 15 seconds.
The core's deadline is `timeout_seconds` (default 8, maximum 60): with the
default configuration the core kills the adapter at 8 seconds, before the
adapter's own timeout can govern — set `timeout_seconds` to at least 15 if
the adapter's timeout should govern instead. Endpoint errors report the
HTTP status only (error bodies may echo the API key and are deliberately
omitted), redirects are not followed, and failure output never echoes the
conversation context or the credential.

A minimal config enabling it:

```json
{
  "include_message_text": true,
  "allow_remote_context": true,
  "summarizer": {
    "command": ["/path/to/notifier-ng/summarizers/openai_compatible.py"],
    "states": ["idle"]
  },
  "transports": [
    {"id": "ntfy", "type": "ntfy", "url": "https://ntfy.example.invalid/notifier-ng"}
  ]
}
```

with the environment set as:

```bash
export NOTIFIER_LLM_BASE_URL="https://api.example.invalid/v1"
export NOTIFIER_LLM_MODEL="your-model"
export NOTIFIER_LLM_API_KEY="…"            # or NOTIFIER_LLM_API_KEY_ENV=OPENAI_API_KEY
```

### Summarizer failure semantics

A failed summarization never blocks delivery: the notification is sent with
the fallback body (status-only, or the event message under
`include_message_text`). The failure is cached in the state entry together
with hashes of the exact context, fallback body, summarizer configuration,
and privacy policy, so identical inputs do not re-invoke the summarizer
(no repeat cost on re-ingests). A successful summary is cached the same way
and reused verbatim as the body (`Summary: <summary>`). Any change to the
context, the fallback body, the summarizer configuration, or the privacy
flags invalidates the cache and triggers a new summarization. With
`allow_remote_context` back at `false`, the summary cache fields are removed
from the state entry entirely.

## Privacy and security defaults

- **Status-only bodies by default**: `include_message_text` defaults to
  `false`, so the delivered body is `<subject> is <state>` (plus the event
  `url` when present). The event's `message` text is only sent when you
  opt in.
- **No remote context by default**: `allow_remote_context` defaults to
  `false`. Conversation context stays on the machine unless both the flag
  and a `summarizer` block are configured, and even then only a bounded,
  redacted excerpt is sent to the summarizer subprocess; the transport
  receives only the resulting one-line summary. `metadata` is never sent to
  transports at all.
- **Tokens stay in the environment**: bearer tokens are read by name
  (`token_env`) at delivery time, never stored in config or state, never
  embedded in URLs, and never printed. Env files fill unset variables only
  and are never echoed.
- **Transport policy**: HTTPS always; loopback HTTP always; remote plain
  HTTP only with an explicit opt-in; redirects refused so credentials never
  leave the validated endpoint.
- **State file** is mode 0600 and atomically written.
- **The OMP adapter never emits the session-file path**: the subject is the
  session id (`session:<id>`) or, when no id exists, a one-way short SHA-256
  of the session file (`session-file:<12 hex chars>`), so the raw session-file
  path cannot leak into subjects or dedup keys. The only path-like metadata
  is the working directory (`cwd`), attached deliberately. Session metadata is
  limited to a fixed non-secret environment allowlist
  (`ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID`, `NZM_SESSION_NAME`,
  `NZM_FLEET_PANE`, `NZM_FLEET_ROLE`) plus `cwd` -- never a wildcard export,
  so token-like variables stay out.

### Redaction boundaries

`redact_text` deterministically masks obvious credentials before ANY remote
sink: the notification title and body before a transport request, context
items before a summarizer request, and transport error bodies echoed to
stderr. It exactly masks:

- PEM private-key payloads (`-----BEGIN * PRIVATE KEY----- … -----END * PRIVATE KEY-----`);
- `Authorization: Bearer <token>` values;
- `name=value` and `name: value` assignments whose name contains — case-
  insensitive, after hyphen-to-underscore normalization and quote stripping —
  any of `token`, `api_key`, `apikey`, `secret`, `password`, `credential`,
  `cookie`, `auth` (so `API-KEY`, `apiKey`, `X-API-Key`, `GITHUBTOKEN` all
  match).

It is idempotent and purely syntactic: surrounding text and assignment names
stay intact, and it does not guess secrets by entropy. That boundary is why
free-form event `message` text is off by default — a prose sentence that
merely mentions a key name in another position is not masked, so status-only
bodies are the safe default.

## Source plugins

Each plugin is a small Python executable that converts a producer-specific
payload or scan into normalized NDJSON. Plugins are run through the core
(`notifier_ng.py source PLUGIN [ARGS...]`, which forwards stdin and argv)
or directly.

The installer wires OMP, Codex, and Hermes together. For a subset install, use
only the self-contained wiring recipe in the wanted section below; see
[Installation](#installation) for the all-harness path.

### Source plugin contract

A source plugin prints zero or more normalized event lines (see [Event
schema](#event-schema-normalized-ndjson)) to stdout and exits:

- Exit 0 = processed successfully; the record list may be empty when the
  payload does not describe an idle/stopped/failed state (e.g. a hook event
  that is not relevant).
- Exit nonzero = failure; a short one-line stderr explanation. The core
  aborts the whole ingest (exit 2) and delivers nothing.

When run through the `source` subcommand, the core runs the plugin as a
subprocess with stdio connected, a 60-second deadline, and the core's own
stdin forwarded to the plugin. This is why hook payloads can be handed over
as the final argv element (Codex legacy notify) or on stdin (Hermes, OMP
writes directly to `ingest`). Plugins are invoked through Python here, but
the contract is just an executable emitting NDJSON; the core does not import
plugin code.

When writing your own producer: keep `source` and `subject` stable and
unique per entity, put the event identity in `event_id` (it participates in
dedup), emit `context` items only with `role` `user`/`assistant`, and treat
`metadata` as never-leaving-the-machine.

### Codex

[plugins/codex.py](plugins/codex.py) accepts one of two payload shapes and
emits exactly one `idle` event:

- **Legacy notify payload** (`type: "agent-turn-complete"`, passed as a JSON
  object on the final argv element, as [OpenAI
  Codex](https://github.com/openai/codex) does): requires `thread-id` and
  `turn-id`; the event is keyed by the turn id, the subject is the thread
  id; `cwd` and `client` are attached as metadata when present in the
  payload.
- **Stop / SubagentStop hook payload** (JSON object on stdin, `hook_event_name`
  `Stop` or `SubagentStop`): requires `session_id` and `turn_id`
  (`agent_id` additionally for `SubagentStop`); the subject is the session id
  (`session:agent` for subagents) and the event id includes the turn.
  `cwd`, `model`, `transcript_path`, `agent_type`, and `agent_id` attach as
  metadata.

Other Codex hook events produce no output. When message text is available,
the event may carry a bounded `context` built **from payload message fields
only** (the plugin never reads transcripts): chronological `user`/`assistant`
items, capped at the last 20 items and 20000 aggregate characters.
Non-secret session context is attached from the fixed environment allowlist
(`ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID`, `NZM_SESSION_NAME`,
`NZM_FLEET_PANE`, `NZM_FLEET_ROLE`); empty values are dropped.

The installer's Codex step sets the exact notify array; to configure by hand
in `~/.codex/config.toml`:

```toml
notify = ["/path/to/notifier-ng/notifier_ng.py", "source", "/path/to/notifier-ng/plugins/codex.py"]
```

### Hermes

[plugins/hermes.py](plugins/hermes.py) accepts a Hermes shell-hook payload
(a single JSON object on stdin) and emits:

- `on_session_end` → `idle` event, **only** when the turn `completed` is
  true and not `interrupted`; keyed by the turn id. Requires the payload
  `extra` object and a non-empty `turn_id`.
- `on_session_finalize` with reason `session_expired` → `stopped` event.
- Any other hook event or finalization reason → no output.

Metadata uses the same fixed non-secret environment allowlist as the other
adapters, plus optional `task_id`, `model`, `platform`, and `cwd` from the
payload. To wire the hook by hand, merge the exact block below into
`config.yaml` as (or inside) the top-level `hooks:` key — a minimal
hooks-only `config.yaml` is valid, since Hermes merges its defaults — then
restart Hermes. The hook command routes through the core and emits no
response text, which Hermes treats as a no-op response:

```yaml
hooks:
  on_session_end:
    - command: /path/to/notifier-ng/notifier_ng.py source /path/to/notifier-ng/plugins/hermes.py
  on_session_finalize:
    - command: /path/to/notifier-ng/notifier_ng.py source /path/to/notifier-ng/plugins/hermes.py
```

Hermes gates shell hooks through `shell-hooks-allowlist.json` — an external
allowlist file, never an interactive prompt — and the installer performs
that approval merge itself using Hermes' documented schema
(`approved_at` / `script_mtime_at_approval` as ISO-8601 UTC with a `Z`
suffix). A hand-wired hook therefore needs matching entries for
`on_session_end` and `on_session_finalize` keyed by the exact command
strings above; for a subset install, apply the Hermes steps from the
installer's printed dry-run plan, which prints the exact fragment to merge
and records the approvals.

### Oh My Pi (OMP)

[integrations/omp-notifier.ts](integrations/omp-notifier.ts) is a thin
factory for the documented OMP extension API. It turns a terminal
`agent_end` (never a non-terminal agent-loop notification, and deliberately
no `session_shutdown` handler — that would duplicate the terminal `agent_end`
record) into exactly one `idle` event (source `omp`) and hands it to
`notifier_ng.py ingest` over stdin using Bun's process API.

- **Subject**: the session id when available (`session:<id>`), else a
  stable one-way short hash of the session file (`session-file:<12 hex>`);
  the raw session-file path is never emitted (see [Privacy and security
  defaults](#privacy-and-security-defaults)).
- **Event id**: `omp:agent_end:<responseId | timestamp | message fingerprint>`,
  stable across repeated deliveries of the same session.
- **Context**: the newest `user`/`assistant` messages with text, oldest
  first, capped at 20 items and 20000 aggregate characters.
- **Metadata**: `cwd`, plus `zellij`/`nzm` groups from the fixed
  non-secret environment allowlist.
- **Locating the core**: `NOTIFIER_NG_INGEST` (absolute path to
  `notifier_ng.py`) is the explicit override, read at delivery time, not
  module load; without it the core resolves sibling-relative to the adapter,
  so the hook works from any checkout location with no machine-specific
  path. Delivery failures are logged, never thrown.

The installer places the adapter at `~/.omp/agent/hooks/post/notifier-ng.ts`
(harness auto-discovery) as a symlink, or — only when the filesystem cannot
create a symlink — as a wrapper file that pins `NOTIFIER_NG_INGEST` to this
checkout. Both forms reference this checkout: the symlink targets it and
the wrapper pins the ingest path to it. **Moving the checkout therefore
requires re-running `install.py --apply` regardless of which form is
installed** (remove the stale hook first if the installer refuses it; the
refusal message names the path).

To wire this source by hand (or to enable it without the installer):

```bash
mkdir -p ~/.omp/agent/hooks/post
ln -s /path/to/notifier-ng/integrations/omp-notifier.ts ~/.omp/agent/hooks/post/notifier-ng.ts

# removal:
rm ~/.omp/agent/hooks/post/notifier-ng.ts
```

The manual symlink is the supported form; on a symlink-less filesystem,
either run `python3 install.py --apply` once to have it emit the wrapper,
or hand-write the hook as a module that pins `NOTIFIER_NG_INGEST` to this
checkout before re-exporting the adapter.

### Covey

[plugins/covey.py](plugins/covey.py) performs a **read-only** scan of a
Covey SQLite database (`--db PATH`, default `./covey.db`; the installer's
printed manual-scan command uses `$XDG_STATE_HOME/notifier-ng/covey.db`,
independent of `--state-dir`) and emits one event per session row, opening
the database with `mode=ro`:

- state `active` → `active` snapshot (re-arms; never notifies);
- state `stale` / `exited` → `stopped` snapshot keyed by `updated_at`
  (epoch ms, as `event_id` and an ISO-8601 `timestamp`);
- any other state → the plugin fails loudly (exit 1).

`principal`, `role`, and the active subtask title are attached as metadata.
The event id for stopped rows changes with `updated_at`. After a prior
snapshot has established the subject, a transition to `stopped` or a changed
`updated_at` notifies; unchanged repeats are suppressed. The first snapshot
for any subject is a silent baseline — including a first scan that already
finds a stopped session.

```bash
python3 notifier_ng.py source plugins/covey.py --db /path/to/covey.db
```

### Zellij

[plugins/zellij.py](plugins/zellij.py) queries the
[Zellij](https://zellij.dev/) CLI (`zellij -s <session> action list-panes
--json`, 30-second timeout) and emits one snapshot per pane:

- a running pane → `active` snapshot (never notifies);
- an exited pane → `stopped` snapshot with `exit_status` / `is_held`
  metadata when present.

Zellij exposes no prompt-idle signal, only pane lifecycle, so `idle` is
never claimed and interactive panes stay `active`. The session comes from
`--session`, `--all` (enumerates running sessions via
`zellij list-sessions --short`), or `$ZELLIJ_SESSION_NAME` inside a zellij
session. A missing session, missing `zellij` executable, query failure, or
malformed output exits 1 with a one-line error.

```bash
python3 notifier_ng.py source plugins/zellij.py --session work
python3 notifier_ng.py source plugins/zellij.py --all
```

## Operational commands

The only delivery path is `ingest`; the source sections above give the exact
invocation for each source. The commands not shown elsewhere:

```bash
# Ingest normalized NDJSON from stdin (the only delivery path).
python3 notifier_ng.py --config /path/to/config.json --state /path/to/state.json ingest < events.ndjson

# Manual tap-in without any harness:
printf '%s\n' '{"version":1,"source":"manual","subject":"demo","state":"idle","mode":"event"}' \
  | python3 notifier_ng.py ingest
```

Without `--config`/`--state`, the defaults in [Config and state
paths](#config-and-state-paths) apply; a config must exist at the default
path or be supplied with `--config` (a missing config file is an error,
exit 2, never silently ignored).

Exit statuses are contractual for the core's handled error paths: `0` =
processed, all deliveries attempted (nothing to deliver included); `1` = at
least one transport delivery error (the error lines are printed to stderr);
`2` = configuration, event-parse, state-content, or source-plugin error (a
single `notifier-ng: <reason>` line on stderr, nothing delivered).
Low-level filesystem failures are not wrapped: a failure creating the state
directory or `<state>.lock`, or in the atomic write (`fsync`, rename), aborts
with an uncaught Python `OSError` traceback rather than the single-line
exit-2 form.

## Troubleshooting

Each item is an observed error followed by the action; policy details live
in the linked sections.

- **`notifier-ng: config ... unknown keys` / `must be an integer ...` /
  `transports must be a non-empty array`** (exit 2): the config is invalid.
  Validate against the [config schema](#config-schema), fix it, re-run.
- **Transport errors (exit 1) against `example.invalid` endpoints**: the
  shipped example config is schema-valid but not live — point the
  transport `url` at real endpoints and set the named `token_env`
  variables first (see [Transports](#transports)).
- **`notifier-ng: <path> line N is not valid JSON` / `unknown keys` /
  `state must be one of ...`** (exit 2): the whole batch was rejected and
  nothing delivered; fix the offending line and re-ingest the batch.
- **`transport <id> returned HTTP 3xx`**: redirects are refused by design;
  point the transport at the final URL (see [Network and transport security
  policy](#network-and-transport-security-policy)).
- **`transport <id> failed: ...`**: connection-level failure; the transport
  was not marked delivered and retries on the next matching ingest (see
  [Deduplication and delivery semantics](#deduplication-and-delivery-semantics)).
  Check reachability and the token environment variable.
- **`url uses plain HTTP to a non-loopback host; set allow_insecure_http:
  true`**: see [Network and transport security
  policy](#network-and-transport-security-policy).
- **`requires environment variable <VAR>`**: set the named variable at
  ingest time; tokens are never stored in config.
- **`state ... is corrupt or unreadable`** (exit 2): the state file failed
  shape validation and was not overwritten; fix or remove it (dedup
  restarts — see [Deduplication and delivery
  semantics](#deduplication-and-delivery-semantics)).
- **Uncaught `OSError` traceback**: low-level filesystem failure while
  locking or writing state; check the state directory's permissions and
  free space (see [Operational commands](#operational-commands)).
- **Summarizer silently falls back**: fallback delivery is by design; the
  notification goes out with the fallback body and the failure is cached.
  Debug by running the adapter by hand with the documented five-field
  request and checking the `NOTIFIER_LLM_*` variables (see [OpenAI-compatible
  adapter environment](#openai-compatible-adapter-environment) and
  [Summarizer failure semantics](#summarizer-failure-semantics)).
- **Nothing was delivered for an event I expected**: `active` re-arms and
  identical fingerprints are suppressed by design, and privacy-flag toggles
  redeliver — see [Deduplication and delivery
  semantics](#deduplication-and-delivery-semantics).
- **Installer refuses a step**: remove the conflicting target the refusal
  message names and re-run `python3 install.py --apply` (see [What the
  installer manages](#what-the-installer-manages)); the installer never
  rewrites files it does not own.

## Development and verification

The test suite exercises everything through real processes and loopback
HTTP servers built only on the standard library: each plugin is run as a
subprocess with real argv/stdin/stdout/stderr, and the core CLI is driven
with its documented argv/config/stdin contract against local capture
servers. No public network is used, no external user state is touched (all
databases, configs, and state files live in temporary directories), and the
suite is fully deterministic.

```bash
# Full behavioral suite (core CLI, all source plugins, summarizer contract,
# redaction, dedup/retry semantics, HTTP policy): stdlib only.
python3 test_notifier_ng.py

# OMP adapter privacy contract (Bun-exercised): subject path privacy,
# subject stability, and the normalized schema. Requires bun.
bun test integrations/omp-notifier.test.ts

# Installer sanity: dry run prints a plan and touches nothing.
python3 install.py
```

`python3 test_notifier_ng.py` includes the OMP privacy contract as a test
that is skipped automatically when `bun` is not installed. The repository
keeps `.gitignore` entries for Python bytecode and standard cache
directories; there are no lint, format, or build steps to run.

## Repository layout

| Path | Contents |
| --- | --- |
| [notifier_ng.py](notifier_ng.py) | Core: config/state handling, event validation, dedup, redaction, summarizer orchestration, transports, CLI |
| [install.py](install.py) | Dry-run-first installer and harness wiring (OMP hook, Codex notify, Hermes hooks, NZM timer units, manual-command printing) |
| [plugins/codex.py](plugins/codex.py) | Codex legacy-notify and Stop/SubagentStop hook adapter |
| [plugins/hermes.py](plugins/hermes.py) | Hermes shell-hook adapter |
| [plugins/covey.py](plugins/covey.py) | Read-only Covey database scan adapter |
| [plugins/zellij.py](plugins/zellij.py) | Zellij pane scan adapter |
| [summarizers/openai_compatible.py](summarizers/openai_compatible.py) | OpenAI-compatible summarizer adapter (stdlib only) |
| [integrations/omp-notifier.ts](integrations/omp-notifier.ts) | OMP extension adapter (Bun runtime; `@oh-my-pi/pi-coding-agent` types) |
| [integrations/omp-notifier.test.ts](integrations/omp-notifier.test.ts) | Bun-exercised privacy contract for the OMP adapter |
| [config.example.json](config.example.json) | Example config with privacy defaults and two example transports |
| [config.summarizer.example.json](config.summarizer.example.json) | Example config with the summarizer and remote context enabled |
| [test_notifier_ng.py](test_notifier_ng.py) | Full behavioral test suite (stdlib only) |
| [package.json](package.json) | Declarative metadata for this repository (`"private": true` — this is not an npm package; the only Bun-touching surface is the optional OMP adapter, declared as an engine and peer dependency) |
| [LICENSE](LICENSE) / [NOTICE](NOTICE) | GNU GPL version 3 and attribution/dependency notices |

## Limitations

- **No daemon, no polling.** Nothing watches or schedules; harness hooks,
  manual scans, and the optional every-minute NZM source timer must invoke
  the CLI. Zellij and Covey are snapshot scans you run yourself; the
  installer only writes (never activates) the NZM timer units, and no
  background processes are otherwise added.
- **Two transports.** `ntfy` and `home_assistant` are the only transport
  types. Adding a transport means extending `notifier_ng.py`.
- **Zellij cannot detect idle prompts.** Zellij exposes pane lifecycle only,
  so interactive panes report `active`; there is no `idle` claim for a pane
  sitting at a prompt.
- **No transcript reading or context retention.** The core never reads
  transcripts; summarizer input is limited to the context a plugin embeds
  in an event, and the state file records only hashes, never the context
  itself.
- **Dedup is per (source, subject, fingerprint), not time-based**, and is
  scoped to a single machine and a single state file — there is no cluster
  coordination, retention policy, or history beyond the current entry.
- **Summary quality is not verified.** Any response that is valid JSON,
  non-empty, and within `max_summary_chars` is used verbatim.

## License

GPL-3.0-only. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The core,
installer, plugins, and summarizer use the Python standard library only;
no third-party Python dependency is installed or vendored. The optional OMP
adapter requires the Bun runtime and the OMP harness's
`@oh-my-pi/pi-coding-agent` extension API (declared as a peer dependency in
[package.json](package.json)); no JavaScript dependency source is vendored
in this repository.
