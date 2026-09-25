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
check_service pihole-FTL.service
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
echo "== Tailnet resolver (the llm/cigboard/status/voice shortcuts) =="
# "systemctl is-active pihole-FTL" is not enough: the resolver can be up and
# bound to nothing on the tailnet, which is silent from systemd's side but breaks every
# device that accepts Tailscale DNS -- including their plain internet lookups,
# since this resolver is what forwards those too.
if ! command -v dig >/dev/null 2>&1; then
  echo "SKIP  dig not installed, cannot probe the resolver"
elif [ -z "${tailscale_ip:-}" ]; then
  echo "SKIP  no tailscale ip to probe"
else
  for name in llm openrouter.ai; do
    if [ -n "$(dig +short +time=3 +tries=1 "@$tailscale_ip" "$name" A 2>/dev/null)" ]; then
      echo "OK    $tailscale_ip resolves $name"
      ok=$((ok + 1))
    else
      echo "FAIL  $tailscale_ip does not answer for $name (pihole-FTL bound to the wrong interface?)"
      fail=$((fail + 1))
    fi
  done
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
