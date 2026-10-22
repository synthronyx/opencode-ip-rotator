#!/usr/bin/env bash
# Sync the models discovered by the local Zen proxy into the OpenCode config.
# OpenCode V2 custom providers need an explicit model list, but the proxy
# discovers free models dynamically, so run this after the list changes.
set -euo pipefail

CONFIG="${HOME}/.config/opencode/opencode.json"
PROXY="${1:-http://127.0.0.1:8000}"

python3 - "$CONFIG" "$PROXY" <<'PY'
import json, sys, urllib.request

config_path, proxy = sys.argv[1], sys.argv[2].rstrip("/")
with urllib.request.urlopen(f"{proxy}/v1/models", timeout=30) as r:
    models = json.load(r)["data"]

with open(config_path, encoding="utf-8") as f:
    config = json.load(f)

provider = config.setdefault("providers", {}).setdefault("zen-local", {})
provider.setdefault("name", "OpenCode Zen (WARP Proxy)")
provider.setdefault("package", "@opencode/ai/providers/openai-compatible")
provider.setdefault("settings", {})["baseURL"] = f"{proxy}/v1"
provider["settings"].setdefault("apiKey", "public")

current = provider.get("models", {})
synced = {}
for m in models:
    mid = m["id"]
    label = (current.get(mid) or {}).get("name") or m.get("name", mid)
    # Prefix so the proxied entry is distinguishable from the identical
    # built-in "opencode" provider entry in /models.
    if not label.startswith("WARP · "):
        label = "WARP · " + label
    synced[mid] = {**(current.get(mid) or {}), "name": label}

provider["models"] = synced
with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print(f"Synced {len(synced)} models into {config_path}")
for mid in synced:
    print(f"  zen-local/{mid}")
PY
