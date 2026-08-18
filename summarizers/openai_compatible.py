#!/usr/bin/env python3
"""OpenAI-compatible summarizer (v1) for notifier-ng. Stdlib only.

Reads a strict five-field request object on stdin:
    {"version": 1,
     "source": str, "state": str,
     "context": {"items": [{"role": "user"|"assistant", "text": str}, ...]},
     "max_summary_chars": int > 0}
and prints exactly one line to stdout:
    {"version": 1, "summary": str}

Environment:
    NOTIFIER_LLM_BASE_URL     required; OpenAI-compatible endpoint base URL
    NOTIFIER_LLM_MODEL        required; model name
    NOTIFIER_LLM_API_KEY      optional; sent as a Bearer token only when
                              non-empty
    NOTIFIER_LLM_API_KEY_ENV  optional; value is the name of another env var
                              that holds the credential. Lets a non-secret
                              config file point at an already-existing secret
                              (e.g. NOTIFIER_LLM_API_KEY_ENV=OPENAI_API_KEY)
                              without copying it. When set, the named target
                              must be set to a non-empty value.
    NOTIFIER_LLM_ALLOW_INSECURE_HTTP  optional; defaults to false. Plain HTTP
                                      to a non-loopback host is rejected unless
                                      this is the exact lowercase string "true";
                                      any other value than "true" or "false" is
                                      a configuration error.

Failures exit nonzero after a short stderr line only: the conversation
context and the credential name/value are never echoed.
"""

import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROGRAM = "openai_compatible summarizer"
# Must remain <= the core's subprocess deadline (60s in notifier_ng.py).
REQUEST_TIMEOUT = 15.0

ROLES = {"user", "assistant"}
REQUEST_FIELDS = {"version", "source", "state", "context", "max_summary_chars"}
CONTEXT_FIELDS = {"items"}
ITEM_FIELDS = {"role", "text"}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx response surfaces as HTTPError.

    check_endpoint validates only the configured base URL; urllib's default
    handler would then replay a fresh request at Location, carrying the
    bearer credential to an origin or scheme that never passed the policy
    (and plain HTTP there would not get the insecure-http opt-in). Returning
    None makes http_error_30x give up so the redirect status is reported
    instead and the credential stays with the validated endpoint.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def die(message):
    print(f"{PROGRAM}: {message}", file=sys.stderr)
    sys.exit(1)


def parse_request(value):
    """Strictly validate the five-field request; return its contents."""
    if not isinstance(value, dict):
        die("request must be a JSON object")
    unknown = set(value) - REQUEST_FIELDS
    if unknown:
        die(f"unknown request field(s): {', '.join(sorted(unknown))}")
    missing = REQUEST_FIELDS - set(value)
    if missing:
        die(f"missing request field(s): {', '.join(sorted(missing))}")

    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        die("request version must be the integer 1")
    source = value["source"]
    if not isinstance(source, str) or not source.strip():
        die("request source must be a non-empty string")
    state = value["state"]
    if not isinstance(state, str) or not state.strip():
        die("request state must be a non-empty string")
    max_chars = value["max_summary_chars"]
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        die("request max_summary_chars must be a positive integer")

    context = value["context"]
    if not isinstance(context, dict):
        die("request context must be an object")
    unknown = set(context) - CONTEXT_FIELDS
    if unknown:
        die(f"unknown context field(s): {', '.join(sorted(unknown))}")
    items = context["items"]
    if not isinstance(items, list):
        die("context items must be an array")

    parsed_items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            die(f"context item {index} must be an object")
        unknown = set(item) - ITEM_FIELDS
        if unknown:
            die(f"unknown field(s) in context item {index}: {', '.join(sorted(unknown))}")
        role = item["role"]
        if role not in ROLES:
            die(f"context item {index} role must be 'user' or 'assistant'")
        text = item["text"]
        if not isinstance(text, str):
            die(f"context item {index} text must be a string")
        parsed_items.append((role, text))

    return source, state, parsed_items, max_chars


def api_token():
    """Resolve the bearer token, or None when no credential is configured.

    NOTIFIER_LLM_API_KEY_ENV (when non-empty) names the env var that holds the
    credential; the target must exist and be non-empty. Otherwise
    NOTIFIER_LLM_API_KEY is used when present and non-empty. Error messages
    never print the credential name or its value.
    """
    indirect = os.environ.get("NOTIFIER_LLM_API_KEY_ENV")
    if indirect is not None and indirect.strip():
        token = os.environ.get(indirect.strip())
        if token is None or not token.strip():
            die("API key env var is empty or unset")
        return token.strip()
    token = os.environ.get("NOTIFIER_LLM_API_KEY")
    if token is None:
        return None
    token = token.strip()
    return token or None


def insecure_http_opt_in():
    """Parse NOTIFIER_LLM_ALLOW_INSECURE_HTTP strictly; default is False.

    Only the exact lowercase strings "true" and "false" are accepted; any
    other value (including "TRUE", " true ", "1", or "yes") exits with a
    configuration error so a typo cannot silently widen the transport policy.
    """
    raw = os.environ.get("NOTIFIER_LLM_ALLOW_INSECURE_HTTP")
    if raw is None:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    die('NOTIFIER_LLM_ALLOW_INSECURE_HTTP must be exactly "true" or "false"')


def is_loopback_host(hostname):
    """True only for localhost or the loopback literals 127.0.0.0/8 and ::1."""
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


def check_endpoint(base_url, allow_insecure_http):
    """Validate the endpoint URL; dies with a clear message when unsafe.

    HTTPS and loopback HTTP (localhost, 127.0.0.0/8, ::1) are always allowed.
    Plain HTTP to any other host is allowed only when
    NOTIFIER_LLM_ALLOW_INSECURE_HTTP=true, so the bearer credential and the
    conversation context cannot travel in cleartext by accident. Credentials
    embedded in the URL are rejected outright (NOTIFIER_LLM_API_KEY is the
    supported channel).
    """
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        die("NOTIFIER_LLM_BASE_URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        die("NOTIFIER_LLM_BASE_URL must not embed credentials; use NOTIFIER_LLM_API_KEY instead")
    if parsed.scheme == "https" or is_loopback_host(parsed.hostname) or allow_insecure_http:
        return
    die("NOTIFIER_LLM_BASE_URL uses plain HTTP to a non-loopback host; set NOTIFIER_LLM_ALLOW_INSECURE_HTTP=true to permit it")


def build_prompt(source, state, items):
    parts = [f"Source: {source}", f"State: {state}"]
    if items:
        parts.append("Recent activity:")
        parts.extend(f"{role}: {text}" for role, text in items)
    parts.append(
        "Reply with exactly one short factual sentence stating what work was "
        "completed, how it was verified, and what is blocked or waiting. "
        "Do not use markdown. Do not repeat secrets (API keys or tokens) verbatim."
    )
    return "\n".join(parts)


def main():
    try:
        raw = sys.stdin.buffer.read()
    except OSError as exc:
        die(f"cannot read request from stdin: {exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        die("request on stdin is not valid UTF-8")
    if not text.strip():
        die("empty request on stdin")
    try:
        request = json.loads(text)
    except ValueError as exc:
        die(f"request on stdin is not valid JSON: {exc}")
    source, state, items, max_chars = parse_request(request)

    base_url = os.environ.get("NOTIFIER_LLM_BASE_URL")
    if base_url is None or not base_url.strip():
        die("NOTIFIER_LLM_BASE_URL must be set to a non-empty value")
    model = os.environ.get("NOTIFIER_LLM_MODEL")
    if model is None or not model.strip():
        die("NOTIFIER_LLM_MODEL must be set to a non-empty value")
    token = api_token()
    allow_insecure_http = insecure_http_opt_in()
    check_endpoint(base_url, allow_insecure_http)

    # Conservative token budget: headroom for the full char budget, so the
    # model can never be cut off short of max_summary_chars. The one-sentence
    # prompt makes real usage far smaller.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": build_prompt(source, state, items)}],
        "temperature": 0,
        "max_tokens": max_chars,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            data = response.read()
    except TimeoutError:
        die(f"endpoint request timed out after {REQUEST_TIMEOUT}s")
    except urllib.error.HTTPError as exc:
        # Report status only: error bodies are remote-controlled and may echo
        # the API key; the core's redaction helper is not available here.
        die(f"endpoint returned HTTP {exc.code}")
    except urllib.error.URLError as exc:
        die(f"endpoint request failed: {exc.reason}")

    try:
        response = json.loads(data)
    except ValueError:
        die("endpoint returned a response that is not valid JSON")
    if not isinstance(response, dict):
        die("endpoint response must be a JSON object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        die("endpoint response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        die("endpoint response choices[0].message.content is not a string")

    summary = re.sub(r"\s+", " ", content).strip()
    sys.stdout.write(json.dumps({"version": 1, "summary": summary}, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
