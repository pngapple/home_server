#!/bin/bash
# Quick post-reboot sanity check: is everything the bot depends on actually
# up? Read-only, no sudo needed. Run this instead of poking each service by
# hand after a restart.
set -uo pipefail

ok=0
fail=0

check_service() {
  local unit="$1"
  if systemctl is-active --quiet "$unit"; then
    echo "OK    $unit is running"
    ok=$((ok + 1))
  else
    echo "FAIL  $unit is $(systemctl is-active "$unit" 2>&1)"
    fail=$((fail + 1))
  fi
}

echo "== Services =="
check_service tailscaled.service
check_service dnsmasq.service
check_service discord-llm-bot.service
check_service voice-listener.service

echo
echo "== Tailscale =="
if tailscale_ip="$(tailscale ip -4 2>/dev/null)"; then
  echo "OK    tailscale connected, ip=$tailscale_ip"
  ok=$((ok + 1))
else
  echo "FAIL  tailscale not connected"
  fail=$((fail + 1))
fi

echo
echo "== DNS resolution (what the bot needs to reach Discord/OpenRouter) =="
for host in discord.com openrouter.ai; do
  if getent hosts "$host" >/dev/null 2>&1; then
    echo "OK    resolves $host"
    ok=$((ok + 1))
  else
    echo "FAIL  cannot resolve $host"
    fail=$((fail + 1))
  fi
done

echo
echo "$ok ok, $fail failed"
[ "$fail" -eq 0 ]
