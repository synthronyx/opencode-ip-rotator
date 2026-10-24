# opencode-ip-rotator (working fork)

Cloudflare WARP IP rotator + OpenAI/Anthropic-compatible proxy for OpenCode Zen,
with a management dashboard and SQLite metrics.

This is a fork of [alztrk/opencode-ip-rotator](https://github.com/alztrk/opencode-ip-rotator)
with fixes required to make it work again against the current upstream API.
See [Fixes in this fork](#fixes-in-this-fork) — without them the proxy returns
empty responses and the log fills with `FreeTierError`.

---

## Why this fork exists

The upstream project currently fails with:

```
FreeTierError: OpenCode's free tier can only be used from within OpenCode.
```

and the proxy turns that into a `200` stream with no content, so clients report
`OpenAI Chat stream ended without finish_reason` / empty replies.

This fork fixes the upstream request fingerprint and the SSE framing bugs. It is
verified with OpenCode **v2.1.x**, Cloudflare WARP, Docker Compose, streaming
responses, reasoning models, and tool calls.

---

## Requirements

| Requirement | Notes |
| --- | --- |
| Docker Engine 20.10+ | Recommended path; WARP runs inside the container |
| Docker Compose v2 | `docker compose` (plugin), not `docker-compose` |
| `/dev/net/tun` | Needed by WARP. On Linux: `sudo modprobe tun` if missing |
| OpenCode v2.x | Any recent provider setup (this fork is tested on 2.1.1) |
| Ports 8000 | Free on `127.0.0.1` (proxy + dashboard) |

Native (no Docker) is also possible but needs `warp-cli` installed on the host.
See [Native installation](#native-installation-optional).

---

## Quick start (Docker, recommended)

```bash
git clone https://github.com/synthronyx/opencode-ip-rotator.git
cd opencode-ip-rotator

# docker-compose bind-mounts ./proxies.txt; it must exist as a file
touch proxies.txt
mkdir -p data

# build + start proxy and WARP rotator
docker compose up -d --build

# wait for health
docker compose ps
curl -s http://127.0.0.1:8000/health
```

Open the dashboard: <http://127.0.0.1:8000/dashboard>

Verify the egress IP is a WARP address and that models are discovered:

```bash
curl -s http://127.0.0.1:8000/metrics | jq '.verified_public_ip, .location, .discovered_models_count'
curl -s http://127.0.0.1:8000/v1/models | jq -r '.data[].id'
```

---

## Configure OpenCode

OpenCode V2 uses `providers` (plural) with `package` and `settings`.
Add this to `~/.config/opencode/opencode.json` (or `opencode.jsonc`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "zen-local": {
      "name": "OpenCode Zen (WARP Proxy)",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "public"
      },
      "models": {
        "glm-5.3-flash-free": { "name": "Glm 5.3 Flash Free" },
        "deepseek-v4.1-flash-free": { "name": "Deepseek V4.1 Flash Free" }
      }
    }
  }
}
```

V2 custom providers need an explicit model list. The proxy discovers models
dynamically, so sync them automatically instead of editing by hand:

```bash
./sync-models.sh
```

That rewrites `providers.zen-local.models` from `GET /v1/models`, preserving
every other setting in the file.

Then in OpenCode:

```
/models            # pick a zen-local/... model
```

or run a single prompt:

```bash
opencode run -m zen-local/glm-5.3-flash-free "hello"
```

<details>
<summary>OpenCode V1 config (legacy)</summary>

```jsonc
{
  "provider": {
    "opencode-zen-local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenCode Zen Local Proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "any"
      }
    }
  }
}
```

V1 discovers models from `/v1/models`, so no model list is needed.
</details>

<details>
<summary>Anthropic-compatible clients</summary>

The proxy also exposes `/v1/messages`:

```jsonc
{
  "providers": {
    "zen-anthropic": {
      "name": "Zen Anthropic Proxy",
      "package": "@opencode/ai/providers/anthropic-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8000", "apiKey": "public" },
      "models": { "glm-5.3-flash-free": {} }
    }
  }
}
```
</details>

---

## Standalone OpenAI-compatible API

`/v1/chat/completions` also works with any generic OpenAI client — curl, the
OpenAI SDK, LangChain, and so on. The proxy detects non-OpenCode requests and
supplies the upstream free-tier identity itself (a valid session, project ID,
trace headers, and the `read` tool the gate expects), so callers do not need
OpenCode installed.

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4.1-flash-free",
    "messages": [{"role": "user", "content": "Reply with exactly: hello"}]
  }'
```

Streaming works as usual:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4.1-flash-free",
    "stream": true,
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

Python with the OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="public")
response = client.chat.completions.create(
    model="deepseek-v4.1-flash-free",
    messages=[{"role": "user", "content": "hello"}],
)
print(response.choices[0].message.content)
```

How the compatibility layer behaves:

- **Streaming upstream, JSON to the caller.** The free-tier gate only accepts
  streaming requests. The proxy always streams upstream and, for callers that
  send `"stream": false` (the OpenAI default), folds the SSE chunks back into a
  single `chat.completion` JSON response.
- **Synthetic `read` tool.** The gate requires a tool named `read`. If a request
  has no tools, the proxy adds a placeholder and sets `tool_choice: "none"` so
  the model cannot call it. If you pass your own tools, the placeholder is added
  only when your set does not already contain `read`, and your `tool_choice` is
  preserved.
- **Session affinity.** Standalone sessions are reused per `user` field (or per
  API key) to keep upstream prompt caches warm. Send
  `X-Session-Id: ses_...` to pin a session explicitly.
- **OpenCode is untouched.** Requests that carry OpenCode headers (or an
  `opencode` user agent) are forwarded with their original identity.
- **Optional auth.** Set `STANDALONE_API_KEY` to require
  `Authorization: Bearer <key>` (or `x-api-key`) on every `/v1/*` endpoint.
  When set, configure the same key in OpenCode's provider `apiKey`.
- `/v1/models` lists the currently discovered free models.

---

## Day-to-day operations

```bash
# status / logs
docker compose ps
docker compose logs -f proxy-server
docker compose logs -f warp-rotator

# stop / start / restart
docker compose down
docker compose up -d
docker compose restart proxy-server

# force an IP rotation now
curl -X POST http://127.0.0.1:8000/api/rotate

# live metrics (IP, location, rotations, tokens, cost estimate)
curl -s http://127.0.0.1:8000/metrics | jq

# refresh the OpenCode model list after upstream adds models
./sync-models.sh
```

Endpoints:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/v1/chat/completions` | POST | OpenAI-compatible (OpenCode and standalone clients) |
| `/v1/messages` | POST | Anthropic-compatible |
| `/v1/models` | GET | Currently discovered free models |
| `/dashboard` | GET | Web UI (IP, usage, rotation history) |
| `/metrics` | GET | JSON metrics |
| `/health` | GET | Health + WARP quality |
| `/api/rotate` | POST | Manual WARP rotation |

Tuning (edit `docker-compose.yml`, then `docker compose up -d`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARP_ROTATION_INTERVAL` | `0` | `0` pins the egress IP until its upstream quota is exhausted. A positive value only re-checks the blocklist on that interval (it never rotates a healthy IP). |
| `MIN_ROTATION_INTERVAL_SECONDS` | `10` | Cooldown after a rotation; concurrent sessions cannot trigger a rotation storm |
| `GLOBAL_EXHAUSTION_IDENTITIES` | `3` | Distinct quota identities that must fail before the proxy concludes the limit is not IP-scoped and stops rotating |
| `WARP_CHECK_INTERVAL` | `15` | WARP health check interval |
| `OPENCODE_ZEN_PORT` | `8000` | Proxy port |
| `UPSTREAM_STREAM_TIMEOUT` | `150` | Seconds without upstream bytes before the proxy aborts and retries |
| `IP_EXHAUSTION_RETRY_AFTER` | `300` | A 429 `Retry-After` at/above this many seconds means the IP's daily quota is gone |
| `IP_EXHAUSTION_BLOCK_SECONDS` | `86400` | Fallback block duration when upstream sends no reset time |
| `MAX_EGRESS_ROTATIONS_PER_REQUEST` | `3` | Exhausted IPs one request may rotate away from before returning 429 |
| `CLIENT_EXHAUSTION_RETRY_AFTER` | `60` | `Retry-After` sent to clients when every IP was exhausted, instead of the dead IP's multi-hour reset |
| `MODEL_PRICING_JSON` | – | JSON overrides for the USD-per-1M reference rates used in the cost estimate, e.g. `{"glm-5.3-flash-free":{"input_per_1m":0.2,"output_per_1m":0.8,"cache_read_per_1m":0.02}}` |
| `STANDALONE_API_KEY` | – | When set, `/v1/*` requires `Authorization: Bearer <key>` (or `x-api-key`) |
| `OPENCODE_EMULATED_VERSION` | `2.1.1` | OpenCode version advertised to upstream for non-OpenCode clients |
| `AUTO_RECYCLE_THRESHOLD` | `50` | Rotations before environment recycle |
| `PROXY_LIST` / `data/proxies.txt` | – | Extra round-robin upstream proxies |

If you use your own proxy pool, add one per line to `proxies.txt`
(`http://user:pass@host:8080`, `socks5://host:1080`).

---

## Verification checklist

Run these after setup; each should print what is noted.

```bash
curl -s http://127.0.0.1:8000/health | jq .status
# "healthy"

curl -s http://127.0.0.1:8000/metrics | jq -r .verified_public_ip
# a Cloudflare WARP address (104.x.x.x or similar)

curl -s http://127.0.0.1:8000/v1/models | jq -r '.data[].id' | head
# list of *-free models

opencode run -m zen-local/glm-5.3-flash-free "Reply with exactly: OK"
# OK
```

Check the proxy is actually handling your traffic:

```bash
docker compose logs --tail 20 proxy-server | grep -E 'Received request|status=200'
```

---

## Fixes in this fork

Details in [PATCHES.md](PATCHES.md).

1. **Upstream `Accept-Encoding`** — the free-tier endpoint rejects requests that
   do not advertise compression. The proxy now sends
   `Accept-Encoding: gzip, deflate, br`.
2. **Client `User-Agent` forwarding** — the proxy forwards
   `opencode/latest/<version>/cli` instead of `OpenCode-IP-Rotator/1.0`.
3. **SSE framing** — the first buffered chunks no longer lose their blank-line
   separators. Without this, reasoning models (e.g. DeepSeek) fail with
   `Invalid openai-compatible-chat stream event`.
4. **Single `[DONE]`** — no duplicate terminator when upstream already sent one.
5. **Non-blocking upstream calls** — `curl_cffi` calls run in worker threads, so
   one slow request no longer freezes `/health`, the dashboard, or other chats.
6. **Fast-fail stalled upstreams** — `UPSTREAM_STREAM_TIMEOUT` (default 150s)
   replaces libcurl's 600s low-speed timeout, so retries happen before the
   OpenCode client gives up.
7. **Cached public IP** — `/health` no longer performs an external IP lookup on
   every poll.
8. **Quota-aware egress blocklist** — an IP whose daily quota is exhausted is
   recorded in `blocked_egress_ips` (until the upstream reset time) and never
   reused by the rotator; the proxy rotates away and retries on a fresh IP.
9. **`sync-models.sh`** — keeps the V2 explicit model list in sync with the
   models discovered by the proxy (prefixed `WARP · `).
10. **Real token accounting** — streaming requests used to record a hardcoded
    `100`/`150` tokens. The proxy now records the provider's `usage` report
    (including the whole input context) and falls back to payload/stream
    estimates, so the dashboard's tokens and cost reflect actual usage.

Files changed vs upstream: `server.py`, `rotator.py`, `rate_limits.py`,
`token_usage.py` (new), `ip_blocklist.py` (new), `sync-models.sh` (new),
`tests/` (new), plus `PATCHES.md` and this README.

---

## Quota-aware IP blocking

WARP hands out a small pool of addresses and will re-assign an IP whose daily
free-tier quota is already spent. Rotating onto such an IP "succeeds" (the
address changed) but every request still returns `429` with a multi-hour
`Retry-After`.

This fork keeps those addresses in a shared SQLite table
(`blocked_egress_ips` in `data/metrics.db`) until the upstream reset time:

- the proxy detects the exhausted IP from the 429 (quota error, or
  `Retry-After >= IP_EXHAUSTION_RETRY_AFTER`), records it, rotates, and retries
  the request on the fresh IP;
- the rotator refuses to accept any IP from the blocklist during rotation and at
  startup, trying another registration instead;
- transient limits (short `Retry-After`, e.g. 30–60s) are **not** blocked.

The blocklist records the upstream **quota identity**, read from
`https://opencode.ai/cdn-cgi/trace` (`ip=...`, `warp=on`):

- for IPv6 it stores only the first four hextets (the `/64` prefix), because
  OpenCode's `ipRateLimiter` collapses IPv6 addresses to that prefix before it
  builds the Redis counter key;
- for IPv4 it stores the full address.

`response.local_ip` is only the WARP tunnel address (`172.16.0.2`), so it is
never used. Blocking a full IPv6 address while the upstream buckets by `/64`
previously made every sibling address look fresh, which is why the blocklist
kept growing without restoring access.

Rotation is pinned and atomic:

- the egress IP is kept until the upstream reports quota exhaustion; there is no
  scheduled rotation (`WARP_ROTATION_INTERVAL=0`);
- when several sessions hit the exhausted IP together, only one rotates. The
  others wait on a shared lock, then re-check the trace identity and retry on the
  fresh address instead of rotating again;
- the rotator also receives `expected_ip` and skips the rotation if that
  identity is already gone, so a queued rotation cannot bounce twice;
- a rotation only counts as successful when it changes the **quota identity**;
  landing on another address in the same `/64` is retried;
- a short cooldown (`MIN_ROTATION_INTERVAL_SECONDS`) coalesces bursts; and
- if `GLOBAL_EXHAUSTION_IDENTITIES` (default 3) *different* identities all
  report exhaustion, the proxy concludes the free-tier limit is no longer
  IP-scoped (which is what the upstream does today) and stops rotating and
  extending the blocklist until the reported reset time. Requests get a prompt
  `429` meanwhile instead of burning registrations every few seconds.

Health-check 429s (the probe hits `opencode.ai`, not the model API) never
rotate.

Inspect the blocklist:

```bash
curl -s http://127.0.0.1:8000/metrics |
  python3 -c "import json,sys; [print(e['ip'], 'until', e['blocked_until_iso'], '-', e['reason']) for e in json.load(sys.stdin)['blocked_egress_ips']]"
```

It is also shown as **Blocked Egress IPs** on the dashboard. Entries expire
automatically; to clear one early:

```bash
docker exec opencode_proxy_server python3 -c "
import ip_blocklist
with ip_blocklist._connect() as c:
    c.execute('DELETE FROM blocked_egress_ips WHERE ip = ?', ('104.28.x.x',))
"
```

---

## Token and cost accounting

The dashboard's **Input**, **Output**, and **Total** columns describe the whole
request, not just the new message:

- **Input** is the full context sent upstream — system prompt, conversation
  history, tool definitions, and the new user message.
- **Output** is everything the model generated, including reasoning text and
  streamed tool-call arguments.
- **Equivalent OpenCode cost** multiplies those counts by the reference rates in
  `MODEL_PRICING` (default `$0.20`/`$0.80` per 1M input/output tokens). Free
  models have no real charge, so this is a paid-equivalent estimate. Override
  the rates per model with `MODEL_PRICING_JSON` if you want exact figures.
- **Cached input is discounted.** Prompt-cache reads (`cached_tokens`,
  Anthropic `cache_read_input_tokens`, DeepSeek cache hits) are billed at 10% of
  the input rate by default instead of the full rate. Set
  `cache_read_per_1m` per model, or `cache_read_ratio`, to change that. The
  **Cached In** column shows how much of the prompt came from cache.

How the numbers are obtained:

1. **Upstream usage first.** OpenCode already sends
   `stream_options: {"include_usage": true}`; the proxy also adds it when a
   client omits it. When the provider reports `usage` in the final SSE chunk
   (OpenAI, Anthropic `message_start`/`message_delta`, or Responses
   `response.completed`), those exact counts are recorded once per stream.
2. **Estimates as fallback.** If a provider sends no usage, the proxy estimates
   input tokens from the complete request payload (`messages`, `system`,
   `tools`, `input`, `instructions`) and output tokens from the text it
   actually streamed. Estimates use ~4 ASCII characters per token and ~1 token
   per non-ASCII character.
3. **One entry per request.** Usage is recorded when the stream finishes (or
   when the client disconnects), not when the response headers are created, so
   aborted and failed streams are not double-counted.

`token_usage.py` holds this logic and is covered by `tests/test_token_usage.py`.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `FreeTierError` in logs, empty replies | You are on upstream code without the patches. Apply the `server.py` fixes in `PATCHES.md`. |
| `OpenAI Chat stream ended without finish_reason` | Same as above: an upstream error is being streamed as an empty body. Check `docker compose logs proxy-server`. |
| `Invalid ... stream event` | SSE separator bug — apply fix 3 (blank lines while buffering). |
| `Retry due · attempt N · Timed out waiting for response headers` | OpenCode gives up after ~5 min per attempt. A WARP path stalled and the proxy waited too long before retrying. Fixed by fixes 5 and 6; tune `UPSTREAM_STREAM_TIMEOUT` if you still see it. |
| Dashboard/`/health` slow or unresponsive while chatting | Blocking upstream calls froze the event loop. Fixed by fix 5 — check you are running this fork, not upstream. |
| `429` with a multi-hour `Retry-After` | That egress IP burned its daily quota. The proxy blocks the family upstream actually uses, force-rotates, and retries. If the 429 still reaches you, rotation could not obtain a new address; check **Blocked Egress IPs** on the dashboard or `/metrics`. |
| Dashboard shows blocked IPs but chats still 429 | All rotation attempts landed on exhausted addresses. Wait for the reset shown in *Blocked Until*, or add residential proxies via `PROXY_LIST`. |
| Dashboard up, `/v1/models` empty | Upstream discovery failed or WARP not connected: `docker compose logs warp-rotator`. |
| Container exits: cannot create `/dev/net/tun` | Enable the tun module on the host: `sudo modprobe tun`, then `docker compose up -d`. |
| `bind: address already in use` on 8000 | Something else uses port 8000. Change the `8000:8000` mapping in `docker-compose.yml` and the provider `baseURL`. |
| Models missing in OpenCode after upstream update | `./sync-models.sh` then restart OpenCode. |
| Session titles not generated | Expected: the title request sends no tools and upstream rejects it; the chat itself works. |
| `docker compose` complains `proxies.txt` is a directory | Delete it and create an empty file: `rm -rf proxies.txt && touch proxies.txt`. |

Logs and data live in `./data` (`metrics.db`, SQLite).

---

## Native installation (optional)

Skip this if you use Docker — it needs `warp-cli` on the host and changes your
host network configuration.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# terminal 1
python rotator.py

# terminal 2
python server.py
```

Then use the same OpenCode provider config as above.
`setup.py` from upstream also exists, but it writes a V1-style config and edits
`opencode.jsonc`, so prefer the manual config shown in this README.

---

## Updating from upstream

`origin` points at this fork and `upstream` at the original repo:

```bash
git fetch upstream
git log --oneline HEAD..upstream/master   # inspect first

git merge upstream/master                 # or cherry-pick
```

Then re-check `git diff` for `server.py` — the four fixes in `PATCHES.md` must
survive, otherwise the proxy will break again.

---

## Security notes

- Bind the proxy to `127.0.0.1` (default). Do not expose port 8000 or 8001 to a
  network — the rotator's control endpoint has no user authentication.
- `data/metrics.db` contains usage history; treat it as local-only.
- Free-tier gating is enforced by the upstream service. Using IP rotation to
  circumvent rate limits may violate its terms of service. Use at your own risk;
  the authors accept no liability for account restrictions.

---

## Credits and license

Original project: [alztrk/opencode-ip-rotator](https://github.com/alztrk/opencode-ip-rotator)
(MIT). This fork keeps the same [MIT license](LICENSE).
