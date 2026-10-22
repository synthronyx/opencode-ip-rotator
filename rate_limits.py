def classify_upstream_429(response) -> tuple[str, int | None, object]:
    """Preserve upstream limit semantics; a 429 is not evidence of an IP block."""
    retry_after = response.headers.get("retry-after")
    try:
        retry_seconds = max(0, int(float(retry_after))) if retry_after else None
    except (TypeError, ValueError):
        retry_seconds = None

    try:
        payload = response.json()
    except Exception:
        payload = {"error": {"type": "upstream_rate_limit", "message": response.text}}

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error_type = str(error.get("type", "upstream_rate_limit"))
    if error_type in {"FreeUsageLimitError", "GoUsageLimitError", "BlackUsageLimitError"}:
        category = "quota"
    elif error_type == "RateLimitError":
        category = "rate_limit"
    else:
        category = "upstream_rate_limit"
    return category, retry_seconds, payload


def is_quota_exhausted(
    category: str, retry_seconds: int | None, threshold: int = 300
) -> bool:
    """Decide whether a 429 means the egress IP's budget is spent.

    Transient limits are reported with short Retry-After values (seconds or a
    few minutes).  Daily quotas report the time until the reset — often many
    hours — and must not be treated as a burst limit.
    """
    return category == "quota" or (
        retry_seconds is not None and retry_seconds >= threshold
    )
