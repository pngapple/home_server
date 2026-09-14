#!/bin/bash
# One-shot setup: installs/updates the systemd units and drop-ins this repo
# depends on, so a fresh boot (or a fresh SD card) brings everything up
# without manual intervention. Safe to re-run any time after editing a
# tracked unit file or drop-in in scripts/systemd/ or the repo root.
#
# Does NOT restart the already-running discord bot or start the voice
# listener (which turns the mic on) — those are left to a manual
# `systemctl restart/start` so this script never disrupts a live session.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo: sudo $0" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== Installing unit files =="
install -m 644 "$REPO_DIR/discord-llm-bot.service" /etc/systemd/system/discord-llm-bot.service
install -m 644 "$REPO_DIR/voice-listener.service" /etc/systemd/system/voice-listener.service

echo "== Installing dnsmasq drop-in (wait for tailscale0, retry on failure) =="
mkdir -p /etc/systemd/system/dnsmasq.service.d
install -m 644 "$REPO_DIR/scripts/systemd/dnsmasq-override.conf" /etc/systemd/system/dnsmasq.service.d/override.conf

echo "== Clearing stray files out of /etc/dnsmasq.d =="
# dnsmasq's ExecStart passes `-7 /etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new`,
# which loads EVERY file in that directory except those three suffixes. A
# status.conf.bak-* left alongside is live config, not a backup - two of them
# had been silently merged into the running config for weeks, and one carried
# the stale bind-interfaces line this script exists to replace.
mkdir -p /etc/dnsmasq.d.backups
while IFS= read -r stray; do
  [ -n "$stray" ] || continue
  echo "  moving $(basename "$stray") -> /etc/dnsmasq.d.backups/ (it was being loaded as config)"
  mv "$stray" /etc/dnsmasq.d.backups/
done < <(find /etc/dnsmasq.d -maxdepth 1 -type f ! -name '*.conf' ! -name README)

echo "== Installing tailnet shortcut resolver =="
# The records point at this machine's own tailnet IP, which is different on
# every machine - render it in rather than shipping whatever the last host had.
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "$TAILSCALE_IP" ]; then
  echo "  FAILED: tailscale has no IPv4 address yet, so the shortcut records" >&2
  echo "  would render empty. Bring tailscale up, then re-run this." >&2
  exit 1
fi
echo "  rendering shortcut records to $TAILSCALE_IP"
sed "s/__TAILSCALE_IP__/$TAILSCALE_IP/g" "$REPO_DIR/scripts/dnsmasq/status.conf" \
  > /etc/dnsmasq.d/status.conf
chmod 644 /etc/dnsmasq.d/status.conf
dnsmasq --test

echo "== Installing persistent journal config =="
mkdir -p /etc/systemd/journald.conf.d
install -m 644 "$REPO_DIR/scripts/systemd/journald-persistent.conf" /etc/systemd/journald.conf.d/persistent.conf

echo "== Reloading systemd =="
systemctl daemon-reload
systemctl restart systemd-journald

echo "== Enabling services for boot (not starting anything live) =="
systemctl enable tailscaled.service dnsmasq.service discord-llm-bot.service voice-listener.service

echo "== Applying dnsmasq fix now (only rebinds the tailscale0-only resolver, no LAN impact) =="
systemctl restart dnsmasq.service

echo
echo "Done. discord-llm-bot and voice-listener are enabled for next boot but"
echo "were left running as-is. To start the voice listener now:"
echo "  sudo systemctl start voice-listener.service"
echo
echo "Run scripts/check_startup.sh any time to see the health of everything."
