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
