#!/bin/bash
# Publishes the dashboards and webhooks over the tailnet using Tailscale Serve,
# replacing the hand-rolled nginx vhost (/etc/nginx/sites-available/status) and
# the cert-renew timer it needed. Serve terminates TLS itself with a cert it
# obtains and renews on its own.
#
# This covers the PATH form only - https://<host>.<tailnet>.ts.net/llm/ and so
# on. The bare-name shortcuts (http://llm) are DNS, handled separately by
# dnsmasq via scripts/dnsmasq/status.conf; no certificate can cover a bare name,
# which is why those stay plain HTTP and why Serve cannot do them.
#
# Requires tailscale >= 1.48 for --set-path. Idempotent: re-running resets the
# serve config and rebuilds it, so edit the table below and re-run.
set -euo pipefail

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale is not installed or not on PATH" >&2
  exit 1
fi

if ! tailscale status >/dev/null 2>&1; then
  echo "tailscale is not connected - run 'tailscale up' first" >&2
  exit 1
fi

# path : local port. Ports mirror the defaults in bot/config.py; if you have
# overridden any of them in .env, override them here too.
ROUTES=(
  "/llm:8791"        # bot/llm_status_server.py     - LLM metrics dashboard
  "/cigboard:8792"   # cigboard/server.py           - cigarette leaderboard
  "/voice:8795"      # bot/voice_status_server.py   - live mic activity
  "/status:19999"    # netdata (not part of this repo)
)

# Webhooks rather than dashboards: these are hit by Google's OAuth redirect and
# by each resident's iOS Shortcuts automation, so they need the same public
# path treatment even though nobody opens them in a browser.
WEBHOOKS=(
  "/calendar/oauth/callback:8788"  # bot/oauth_server.py
  "/geofence/webhook:8793"         # bot/geofence_server.py
)

echo "== Resetting existing serve config =="
tailscale serve reset

for entry in "${ROUTES[@]}" "${WEBHOOKS[@]}"; do
  path="${entry%%:*}"
  port="${entry##*:}"
  echo "  $path -> localhost:$port"
  tailscale serve --bg --set-path "$path" "localhost:$port"
done

echo
echo "== Current serve config =="
tailscale serve status

cat <<'EOF'

Done. Two things Serve changes that you have to follow up on:

1. These URLs are https, and the old nginx vhost answered the oauth callback
   over http. Update GOOGLE_REDIRECT_URI in .env to the https form AND add
   that exact URL to the authorized redirect URIs in the Google Cloud console,
   or calendar linking fails with redirect_uri_mismatch.

2. Each resident's iOS geofence Shortcut posts to the old http URL. Re-point
   those at the https one.

The bot's sidecars still bind 127.0.0.1, which is what you want: Serve reaches
them over loopback, and nothing else can.
EOF
