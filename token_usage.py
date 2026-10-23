"""Token accounting helpers for the proxy.

Kept dependency-free (stdlib only) so they can be unit-tested without the
proxy's FastAPI/curl stack. The proxy prefers the upstream's own usage report
(``stream_options.include_usage``) and falls back to estimates when a provider
does not send one.
"""

import json


def estimate_text_tokens(text: str) -> int:
    """Approximate a token count for text when upstream omits usage.

    ASCII text averages ~4 characters per token; CJK and other non-ASCII text is
    closer to one token per character. Slightly conservative on purpose.
    """
    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars)


def estimate_prompt_tokens(payload: dict) -> int:
    """Estimate the full input context (system prompt + history + tools)."""
    if not isinstance(payload, dict):
        return 0
    parts = [
        payload[key]
        for key in ("messages", "system", "tools", "input", "instructions")
        if payload.get(key)
    ]
    if not parts:
        return 0
    try:
        serialized = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = str(parts)
    return estimate_text_tokens(serialized)


def estimate_completion_tokens(res_json) -> int:
    """Estimate generated tokens from a non-streamed response body."""
    if not isinstance(res_json, dict):
        return 0
    chunks = []
    for choice in res_json.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or choice.get("delta") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                chunks.append(content)
            for tool_call in message.get("tool_calls") or []:
                function = (tool_call or {}).get("function") or {}
                try:
                    chunks.append(json.dumps(function, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
    # Anthropic Messages shape.
    for block in res_json.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            chunks.append(block["text"])
    return estimate_text_tokens("".join(chunks))


def new_stream_stats() -> dict:
    return {"usage": {}, "ascii_chars": 0, "non_ascii_chars": 0}


def _add_stream_text(stats: dict, text) -> None:
    if not isinstance(text, str) or not text:
        return
    ascii_chars = len(text.encode("ascii", "ignore"))
    stats["ascii_chars"] += ascii_chars
    stats["non_ascii_chars"] += len(text) - ascii_chars


def _merge_stream_usage(stats: dict, usage) -> None:
    if not isinstance(usage, dict):
        return
    merged = stats.setdefault("usage", {})
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            # Anthropic's message_start reports input_tokens while later
            # deltas report only output_tokens; never drop a known input count.
            if key == "input_tokens" and not value and merged.get("input_tokens"):
                continue
            merged[key] = value


def observe_stream_event(raw_text: str, stats: dict) -> None:
    """Collect usage and generated-text volume from one upstream SSE line."""
    text = raw_text.strip()
    if not text.startswith("data:"):
        return
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return
    try:
        obj = json.loads(data)
    except (TypeError, ValueError):
        return
    if not isinstance(obj, dict):
        return

    # OpenAI-compatible chunks.
    _merge_stream_usage(stats, obj.get("usage"))
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or choice.get("message") or {}
        if not isinstance(delta, dict):
            continue
        for key in ("content", "reasoning_content", "reasoning"):
            _add_stream_text(stats, delta.get(key))
        for tool_call in delta.get("tool_calls") or []:
            function = (tool_call or {}).get("function") or {}
            _add_stream_text(stats, function.get("arguments"))

    # Anthropic Messages stream.
    event_type = obj.get("type")
    if event_type == "message_start":
        message = obj.get("message") or {}
        _merge_stream_usage(stats, message.get("usage"))
        content = message.get("content")
        if isinstance(content, str):
            _add_stream_text(stats, content)
    elif event_type == "message_delta":
        _merge_stream_usage(stats, obj.get("usage"))
    elif event_type == "content_block_delta":
        delta = obj.get("delta") or {}
        _add_stream_text(stats, delta.get("text"))
        _add_stream_text(stats, delta.get("partial_json"))
    # OpenAI Responses API stream.
    elif event_type in (
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.function_call_arguments.delta",
    ):
        _add_stream_text(stats, obj.get("delta"))
    elif event_type == "response.completed":
        response_obj = obj.get("response") or {}
        _merge_stream_usage(stats, response_obj.get("usage"))


def finalize_usage(stats: dict, prompt_tokens_estimate: int):
    """Resolve the usage numbers to record for a completed stream.

    Returns ``(prompt_tokens, completion_tokens)`` or ``None`` when the stream
    produced neither usage nor content (e.g. it was aborted before any output).
    """
    usage = stats.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

    if prompt_tokens is None:
        prompt_tokens = prompt_tokens_estimate
    if completion_tokens is None:
        ascii_chars = stats.get("ascii_chars", 0)
        non_ascii_chars = stats.get("non_ascii_chars", 0)
        completion_tokens = (
            max(1, ascii_chars // 4 + non_ascii_chars)
            if (ascii_chars or non_ascii_chars)
            else 0
        )

    if not prompt_tokens and not completion_tokens:
        return None
    return int(prompt_tokens or 0), int(completion_tokens or 0)
