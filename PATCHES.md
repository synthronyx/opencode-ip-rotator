# Local patches

This working copy differs from upstream `alztrk/opencode-ip-rotator` in `server.py`.
Keep these when updating, otherwise the proxy will fail with
`FreeTierError` / empty streams.

1. **Compression header on upstream requests** (`get_realistic_headers`)
   Adds `Accept-Encoding: gzip, deflate, br`. The upstream free-tier endpoint
   rejects requests that do not advertise a compressing encoding.

2. **Forward the client `User-Agent`**
   OpenCode sends `opencode/latest/<version>/cli`; the proxy forwards it instead
   of the hardcoded `OpenCode-IP-Rotator/1.0`.

3. **Preserve SSE blank lines while buffering the first chunks**
   Dropping the blank line separators merged several `data:` frames into one
   malformed event. Clients failed with `Invalid ... stream event` for models
   whose first chunks arrive slowly (e.g. DeepSeek reasoning models).

4. **Do not append a duplicate `data: [DONE]`**
   The proxy now only emits `[DONE]` when the upstream stream did not already
   send one.

5. **Upstream HTTP calls moved off the event loop**
   `session.post(...)` is a synchronous `curl_cffi` call. Running it directly in
   the async handlers froze the entire server for the duration of the upstream
   request (a 20s upstream delay made `/health` take 20s). All three endpoints
   now use `await asyncio.to_thread(...)`, so requests are handled concurrently.

6. **Faster failure on stalled upstream connections**
   The libcurl low-speed timeout was 600s, so a dead WARP path blocked a request
   for ten minutes before retrying — longer than OpenCode's 5-minute header
   timeout, which surfaced as `Timed out waiting for response headers`.
   It is now `UPSTREAM_STREAM_TIMEOUT` (default `150` seconds), so the proxy
   retries well before the client gives up.

7. **Cached public IP for `/health`**
   `get_public_ip()` performs a blocking HTTP lookup (5s timeout, plus a 5s
   fallback). `/health` called it synchronously on every poll; the result is now
   cached for 15s and resolved in a worker thread.

8. **Quota-aware egress blocklist (`ip_blocklist.py`)**
   WARP re-assigns IPs whose daily free-tier quota is already spent, so a
   "successful" rotation can land on a dead address and every request keeps
   returning `429` (typically with a multi-hour `Retry-After`).

   - `blocked_egress_ips` in the shared `metrics.db` records each exhausted IP
     until the upstream reset time (`Retry-After`), or 24h when absent.
   - The blocked address is the family the upstream actually used
     (`response.local_ip`), not whichever address an IP-echo service happens to
     return. WARP can keep a sticky IPv4 while rotating IPv6, which previously
     made successful rotations look "identical".
   - The proxy checks the 429 class: a quota error or
     `Retry-After >= IP_EXHAUSTION_RETRY_AFTER` (default 300s) marks the current
     egress IP, rotates, and retries the request on the fresh IP. Short
     transient limits are still returned as-is.
   - Rotation on exhaustion is **forced**: a stale streaming lease must not pin
     the proxy to an address that cannot serve new requests.
   - `rotator.py` refuses to accept a blocklisted IP during rotation (tries
     another registration) and rotates away from a blocklisted address at
     startup. Periodic rotation also forces when the current IP is blocked.
   - `/metrics` and the dashboard expose the active blocklist.

9. **Model names prefixed with `WARP · `**
   `sync-models.sh` now labels proxied models so they can be told apart from the
   identical built-in `opencode` provider entries in `/models`.

10. **Real token accounting for streams (`token_usage.py`)**
    Streamed requests recorded `prompt_tokens=100, completion_tokens=150` at
    response-creation time, so the dashboard showed neither the real input
    context nor the real completion.

    - The proxy now relies on the upstream `usage` report. OpenCode already
      sends `stream_options.include_usage`; the proxy adds it when missing.
      Parsing covers OpenAI chunks, Anthropic `message_start`/`message_delta`,
      and Responses `response.completed` events.
    - If a provider omits usage, input tokens are estimated from the entire
      request payload (system prompt, messages, tools, instructions) and output
      tokens from the streamed text, reasoning, and tool-call arguments.
    - Usage is recorded once when the stream finishes, in the generator's
      `finally`, so aborted/failed requests are not counted twice.
    - Prompt-cache reads are tracked separately and billed at 10% of the input
      rate by default (`cache_read_per_1m` / `cache_read_ratio` override), using
      `prompt_tokens_details.cached_tokens`, Anthropic
      `cache_read_input_tokens`, or DeepSeek cache-hit counters.
    - `MODEL_PRICING_JSON` can override the reference USD-per-1M rates used for
      the paid-equivalent cost estimate.

`sync-models.sh` is an extra helper (not upstream) that copies the models
discovered by the proxy into `~/.config/opencode/opencode.json`, because
OpenCode V2 custom providers need an explicit model list.
