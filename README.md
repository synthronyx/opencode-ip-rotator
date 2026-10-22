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
| `/v1/chat/completions` | POST | OpenAI-compatible (used by OpenCode) |
| `/v1/messages` | POST | Anthropic-compatible |
| `/v1/models` | GET | Currently discovered free models |
| `/dashboard` | GET | Web UI (IP, usage, rotation history) |
| `/metrics` | GET | JSON metrics |
| `/health` | GET | Health + WARP quality |
| `/api/rotate` | POST | Manual WARP rotation |

Tuning (edit `docker-compose.yml`, then `docker compose up -d`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARP_ROTATION_INTERVAL` | `300` | Seconds between rotations |
| `WARP_CHECK_INTERVAL` | `15` | WARP health check interval |
| `OPENCODE_ZEN_PORT` | `8000` | Proxy port |
| `UPSTREAM_STREAM_TIMEOUT` | `150` | Seconds without upstream bytes before the proxy aborts and retries |
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

All changes are in `server.py`; details in [PATCHES.md](PATCHES.md).

1. **Upstream `Accept-Encoding`** — the free-tier endpoint rejects requests that
   do not advertise compression. The proxy now sends
   `Accept-Encoding: gzip, deflate, br`.
2. **Client `User-Agent` forwarding** — the proxy forwards
   `opencode/latest/<version>/cli` instead of `OpenCode-IP-Rotator/1.0`.
3. **SSE framing** — the first buffered chunks no longer lose their blank-line
   separators. Without this, reasoning models (e.g. DeepSeek) fail with
   `Invalid openai-compatible-chat stream event`.
4. **Single `[DONE]`** — no duplicate terminator when upstream already sent one.
5. **`sync-models.sh`** — keeps the V2 explicit model list in sync with the
   models discovered by the proxy.

Files changed vs upstream: `server.py` (4 fixes), plus new `sync-models.sh`,
`PATCHES.md`, and this README.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `FreeTierError` in logs, empty replies | You are on upstream code without the patches. Apply the `server.py` fixes in `PATCHES.md`. |
| `OpenAI Chat stream ended without finish_reason` | Same as above: an upstream error is being streamed as an empty body. Check `docker compose logs proxy-server`. |
| `Invalid ... stream event` | SSE separator bug — apply fix 3 (blank lines while buffering). |
| `Retry due · attempt N · Timed out waiting for response headers` | OpenCode gives up after ~5 min per attempt. A WARP path stalled and the proxy waited too long before retrying. Fixed by fixes 5 and 6; tune `UPSTREAM_STREAM_TIMEOUT` if you still see it. |
| Dashboard/`/health` slow or unresponsive while chatting | Blocking upstream calls froze the event loop. Fixed by fix 5 — check you are running this fork, not upstream. |
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
