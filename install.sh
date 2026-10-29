#!/usr/bin/env bash
# Install the Mercury hub on a Linux server (or macOS / WSL2).
#
#   ./install.sh [--home DIR] [--with-whatsapp] [--no-env]
#
# What it does, in order:
#   1. Prepares the engine's pinned Python 3.14 environment (engine/scripts/run-in-hermes-env;
#      this downloads the pinned interpreter and packages from the engine's lock file).
#   2. Creates MERCURY_HOME (default ~/.mercury): config.yaml, .env (chmod 600), SOUL.md,
#      the Mercury skin, the mercury-devices plugin link and the skill sandbox. Existing
#      files are never overwritten.
#   3. Generates the relay admin token and the API server key if they are empty.
#   4. With --with-whatsapp: installs the WhatsApp bridge's Node packages (npm ci).
#   5. Links bin/mercury into ~/.local/bin.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
home="${MERCURY_HOME:-$HOME/.mercury}"
with_whatsapp=false
prepare_env=true

while [ $# -gt 0 ]; do
    case "$1" in
        --home) home="$2"; shift 2 ;;
        --with-whatsapp) with_whatsapp=true; shift ;;
        --no-env) prepare_env=false; shift ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing prerequisite: $1" >&2; exit 1; }; }
need git
need bash

# The engine keeps its pinned tools (uv, Python 3.14, packages) here instead of ~/.hermes/tools.
export MERCURY_HOME="$home" HERMES_HOME="$home" HERMES_RUNTIME_DIR="$home/tools"
# Fetch pinned binaries (uv, Python) from the engine's checksum-verified mirror first; some networks
# throttle GitHub release downloads to a crawl. Set to 0 to try GitHub first.
export HERMES_PREFER_ARTIFACT_MIRROR="${HERMES_PREFER_ARTIFACT_MIRROR:-1}"

if $prepare_env; then
    say "Preparing the engine environment (first run downloads pinned Python 3.14 and packages)"
    "$repo/engine/scripts/run-in-hermes-env" python3 -c 'import sys; print("engine python", sys.version.split()[0])'
fi

say "Setting up $home"
mkdir -p "$home/plugins" "$home/skins" "$home/sandbox" "$home/mercury"
chmod 700 "$home"

if [ ! -f "$home/config.yaml" ]; then
    sed -e "s|@MERCURY_REPO@|$repo|g" -e "s|@MERCURY_HOME@|$home|g" "$repo/config/config.yaml" > "$home/config.yaml"
    echo "   wrote config.yaml"
else
    echo "   kept existing config.yaml"
fi
[ -f "$home/SOUL.md" ] || cp "$repo/config/SOUL.md" "$home/SOUL.md"
cp "$repo/config/skins/mercury.yaml" "$home/skins/mercury.yaml"
ln -sfn "$repo/plugin/mercury-devices" "$home/plugins/mercury-devices"

if [ ! -f "$home/.env" ]; then
    cp "$repo/config/env.example" "$home/.env"
    echo "   wrote .env from config/env.example"
fi
chmod 600 "$home/.env"

fill_secret() {  # fill KEY= in .env with a random token if it is empty
    local key="$1" token
    if grep -qE "^$key=$" "$home/.env"; then
        token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        sed -i.bak "s|^$key=$|$key=$token|" "$home/.env" && rm -f "$home/.env.bak"
        echo "   generated $key"
    fi
}
fill_secret MERCURY_ADMIN_TOKEN
fill_secret API_SERVER_KEY

if $with_whatsapp; then
    need npm
    say "Installing the WhatsApp bridge packages"
    npm ci --prefix "$repo/engine/scripts/whatsapp-bridge"
fi

mkdir -p "$HOME/.local/bin"
ln -sfn "$repo/bin/mercury" "$HOME/.local/bin/mercury"

cat <<EOF

Mercury hub installed. Next steps:

  1. Choose a model:            mercury model
     (default: local Ollama qwen3:8b at http://localhost:11434/v1)
  2. Set the public URL devices will use, in $home/.env:
         MERCURY_PUBLIC_URL=https://mercury.example.com
     and put TLS in front of the relay (deploy/Caddyfile).
  3. Start the services:        see deploy/ (systemd units) or run in two terminals:
         mercury relay
         mercury gateway run
  4. Pair a device:             mercury pair laptop
  5. Talk to Mercury:           mercury            (CLI chat)
     WhatsApp, once Mercury's number is ready:  set WHATSAPP_ENABLED=true and
     WHATSAPP_ALLOWED_USERS in .env, then run: mercury whatsapp

EOF
