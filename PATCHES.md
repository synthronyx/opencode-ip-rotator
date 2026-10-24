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
   - The blocked address is the public IP the upstream actually sees. This was
     initially derived from `response.local_ip`; see item 12 for why that was
     wrong and how the Cloudflare trace endpoint replaced it.
   - The proxy checks the 429 class: a quota error or
     `Retry-After >= IP_EXHAUSTION_RETRY_AFTER` (default 300s) marks the current
     egress IP, rotates, and retries the request on the fresh IP. Short
     transient limits are still returned as-is.
   - Rotation on exhaustion is **forced**: a stale streaming lease must not pin
     the proxy to an address that cannot serve new requests.
   - `rotator.py` refuses to accept a blocklisted IP during rotation (tries
     another registration) and rotates away from a blocklisted address at
     startup. Scheduled rotation is disabled by default; see item 12.
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

11. **Standalone OpenAI-compatible endpoint**
    `/v1/chat/completions` now works for clients that are not OpenCode (curl,
    OpenAI SDK, LangChain, ...).

    - The free-tier gate requires a syntactically valid OpenCode session ID
      (`ses_` + 12 inverted hex timestamp digits + 14 base62 characters), an
      `opencode/latest/<version>/cli` user agent, an `x-opencode-project` ID,
      `stream: true`, and a tool named `read`. The proxy synthesises all of
      these for non-OpenCode clients (`standalone.py`).
    - `read` is added only when missing; when it is the only tool the proxy sets
      `tool_choice: "none"` so the model never calls the placeholder.
    - Because the gate rejects non-streaming requests, the proxy always streams
      upstream and aggregates the SSE chunks into a normal `chat.completion`
      JSON object for callers that requested `stream: false`.
    - Sessions are reused per `user` field (or API key), and can be pinned with
      `X-Session-Id`. Optional `STANDALONE_API_KEY` protects every `/v1/*`
      endpoint.

12. **Pin-until-exhausted, atomic rotation**
    Scheduled rotation every 300s rotated healthy IPs and could collide with
    request-driven rotation (multiple separate `warp-cli` registrations while
    chats were in flight).

    - `WARP_ROTATION_INTERVAL` now defaults to `0`. The egress IP is pinned until
      the upstream reports quota exhaustion. A positive value only re-checks the
      blocklist and rotates an already-exhausted IP.
    - The "current egress IP" is read from
      `https://opencode.ai/cdn-cgi/trace` (`ip=...`). `response.local_ip` is the
      WARP tunnel address (`172.16.0.2`), not the public IP, so keying the
      blocklist on it blocked the wrong address and made every rotation appear
      to land on an exhausted IP.
    - `rotate_warp()` accepts `expected_ip` / `only_if_blocked`; the HTTP
      `/rotate` endpoint forwards them. If another session already moved the
      egress, the queued rotation is skipped instead of burning another
      registration.
    - The proxy serialises exhaustion handling with a lock, re-checks the trace
      IP after acquiring it, and applies `MIN_ROTATION_INTERVAL_SECONDS`
      (default 10s) so concurrent sessions cannot trigger a rotation storm.
    - Health-check 429s no longer rotate (the probe is a public page, not the
      model API).
    - Expired `active_flow_leases` are pruned on startup and at most once a
      minute when a new stream lease is acquired, so the leaked lease table
      cannot block rotation after scheduled rotation is disabled.

`sync-models.sh` is an extra helper (not upstream) that copies the models
discovered by the proxy into `~/.config/opencode/opencode.json`, because
OpenCode V2 custom providers need an explicit model list.


