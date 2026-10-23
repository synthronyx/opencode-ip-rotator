"""Helpers for exposing the proxy as a general OpenAI-compatible API.

OpenCode's free tier rejects requests that do not look like they came from the
OpenCode client. The gate accepts any syntactically valid session ID, so a
generic client (curl, the OpenAI SDK, LangChain, ...) can use the proxy once the
server synthesises the expected headers.

A session ID is ``ses_`` followed by 26 characters:

* 12 hex digits: the inverted 48-bit value of ``(milliseconds << 12) | counter``
* 14 base62 characters: random suffix

This module is dependency-free so it can be unit-tested on the host.
"""

import re
import secrets
import string
import time

SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_MASK48 = (1 << 48) - 1


def mint_session_id(now_ms=None, counter: int = 1, suffix=None) -> str:
    """Create an OpenCode-shaped session ID for a standalone client."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    packed = ((now_ms << 12) + (int(counter) & 0xFFF)) & _MASK48
    inverted = (~packed) & _MASK48
    if suffix is None:
        suffix = "".join(secrets.choice(_BASE62) for _ in range(14))
    return "ses_" + f"{inverted:012x}" + suffix


def is_session_id(value) -> bool:
    return bool(SESSION_ID_RE.match(value or ""))


def decode_session_timestamp(session_id):
    """Return the low 36 bits of the encoded millisecond timestamp."""
    if not is_session_id(session_id):
        return None
    inverted = int(session_id[4:16], 16)
    packed = (~inverted) & _MASK48
    return packed >> 12


def is_opencode_client(headers, payload=None) -> bool:
    """True when the request already looks like it came from OpenCode."""
    if headers.get("x-opencode-client"):
        return True
    if is_session_id(headers.get("x-opencode-session")):
        return True
    if is_session_id(headers.get("x-opencode-session-id")):
        return True
    if "opencode" in (headers.get("user-agent") or "").lower():
        return True
    return False


def requested_session_id(headers, payload=None):
    """Session ID supplied by a standalone client, if it is well formed."""
    for key in ("x-session-id", "x-opencode-session-id", "x-opencode-session"):
        value = headers.get(key)
        if is_session_id(value):
            return value
    return None


# The free-tier gate requires the request to carry a tool named "read"; the
# schema itself is not validated. Generic clients rarely send one, so add a
# harmless placeholder (and forbid tool calls when it is the only tool).
COMPAT_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Compatibility placeholder. Do not call this tool.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def ensure_standalone_tools(payload: dict) -> None:
    """Make sure a standalone request carries the gate-required read tool."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    names = {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    }
    if "read" not in names:
        tools = tools + [COMPAT_READ_TOOL]
    payload["tools"] = tools
    if len(tools) == 1:
        payload.setdefault("tool_choice", "none")
