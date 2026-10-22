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

`sync-models.sh` is an extra helper (not upstream) that copies the models
discovered by the proxy into `~/.config/opencode/opencode.json`, because
OpenCode V2 custom providers need an explicit model list.
