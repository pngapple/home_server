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

echo "== Retiring the old dnsmasq resolver (Pi-hole replaces it) =="
# Pi-hole's FTL is a dnsmasq fork and needs port 53 on the same addresses, so
# the dnsmasq package's own service has to stay off. Masked, not just
# disabled, so a package upgrade can't quietly start it again.
if [ -e /lib/systemd/system/dnsmasq.service ]; then
  systemctl disable --now dnsmasq.service 2>/dev/null || true
  systemctl mask dnsmasq.service
fi
rm -rf /etc/systemd/system/dnsmasq.service.d
if [ -f /etc/dnsmasq.d/status.conf ]; then
  mkdir -p /etc/dnsmasq.d.backups
  mv /etc/dnsmasq.d/status.conf /etc/dnsmasq.d.backups/status.conf.retired-for-pihole
fi

echo "== Installing Pi-hole drop-in (start after tailscaled) =="
mkdir -p /etc/systemd/system/pihole-FTL.service.d
install -m 644 "$REPO_DIR/scripts/systemd/pihole-FTL-override.conf" /etc/systemd/system/pihole-FTL.service.d/override.conf

echo "== Rendering Pi-hole settings =="
# The shortcut records point at this machine's own tailnet IP, which is
# different on every machine - render it in rather than shipping whatever the
# last host had.
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "$TAILSCALE_IP" ]; then
  echo "  FAILED: tailscale has no IPv4 address yet, so the shortcut records" >&2
  echo "  would render empty. Bring tailscale up, then re-run this." >&2
  exit 1
fi
echo "  rendering shortcut records to $TAILSCALE_IP"
PIHOLE_SETTINGS="$(mktemp)"
trap 'rm -f "$PIHOLE_SETTINGS"' EXIT
sed "s/__TAILSCALE_IP__/$TAILSCALE_IP/g" "$REPO_DIR/scripts/pihole/pihole.toml" > "$PIHOLE_SETTINGS"

if ! command -v pihole-FTL >/dev/null 2>&1; then
  echo "== Installing Pi-hole =="
  # A pihole.toml already in place is what makes the installer run without
  # dialogs, and means FTL's very first start is already tailnet-only.
  install -d -m 755 /etc/pihole
  [ -f /etc/pihole/pihole.toml ] || install -m 644 "$PIHOLE_SETTINGS" /etc/pihole/pihole.toml
  # Only the initial subscription - after install, blocklists are managed in the web UI.
  [ -f /etc/pihole/adlists.list ] || \
    echo "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/multi.txt" > /etc/pihole/adlists.list
  curl -sSL https://install.pi-hole.net | bash -s -- --unattended
  # FTL starts before gravity finishes building, and doesn't block anything
  # until it's told to pick the new lists up.
  pihole reloadlists
  echo "  set the web UI password with: sudo pihole setpassword"
fi

echo "== Applying Pi-hole settings =="
python3 "$REPO_DIR/scripts/pihole/apply_settings.py" "$PIHOLE_SETTINGS" |
  while IFS=$'\t' read -r key value; do
    pihole-FTL --config "$key" "$value" >/dev/null
  done

echo "== Enabling Tailscale exit node =="
# Advertising only offers it. No device routes through here until someone
# switches "Use exit node" on in their Tailscale app, and the first advertise
# also needs a one-time approval in the admin console (Machines -> this host
# -> Edit route settings -> Use as exit node).
install -m 644 "$REPO_DIR/scripts/sysctl/99-tailscale-exit-node.conf" /etc/sysctl.d/99-tailscale-exit-node.conf
sysctl -q -p /etc/sysctl.d/99-tailscale-exit-node.conf
tailscale set --advertise-exit-node

echo "== Installing persistent journal config =="
mkdir -p /etc/systemd/journald.conf.d
install -m 644 "$REPO_DIR/scripts/systemd/journald-persistent.conf" /etc/systemd/journald.conf.d/persistent.conf

echo "== Reloading systemd =="
systemctl daemon-reload
systemctl restart systemd-journald

echo "== Enabling services for boot (not starting anything live) =="
systemctl enable tailscaled.service pihole-FTL.service discord-llm-bot.service voice-listener.service

echo "== Restarting Pi-hole (only the tailscale0-only resolver, no LAN impact) =="
systemctl restart pihole-FTL.service

echo
echo "Done. discord-llm-bot and voice-listener are enabled for next boot but"
echo "were left running as-is. To start the voice listener now:"
echo "  sudo systemctl start voice-listener.service"
echo
echo "Run scripts/check_startup.sh any time to see the health of everything."
