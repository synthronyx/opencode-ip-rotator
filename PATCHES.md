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

`sync-models.sh` is an extra helper (not upstream) that copies the models
discovered by the proxy into `~/.config/opencode/opencode.json`, because
OpenCode V2 custom providers need an explicit model list.
