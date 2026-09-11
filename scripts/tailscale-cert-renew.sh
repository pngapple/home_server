#!/bin/bash
# Renews the Tailscale HTTPS cert nginx serves for raspberrypi.tail4ce93a.ts.net
# (see /etc/nginx/sites-available/status's default_server block — that's what
# unlocks getUserMedia() for the /voice/ dashboard's browser-mic toggle, which
# needs a secure context). `tailscale cert` is safe to run repeatedly: it
# only actually reissues when the current cert is close to expiring, so this
# is meant to run unattended on a timer (see tailscale-cert-renew.timer),
# not just once by hand.
set -euo pipefail

DOMAIN="raspberrypi.tail4ce93a.ts.net"
CERT_DIR="/etc/nginx/tailscale-certs"

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-run with sudo: sudo $0" >&2
  exit 1
fi

mkdir -p "$CERT_DIR"

before_hash="$(sha256sum "$CERT_DIR/$DOMAIN.crt" 2>/dev/null || true)"

tailscale cert \
  --cert-file "$CERT_DIR/$DOMAIN.crt" \
  --key-file "$CERT_DIR/$DOMAIN.key" \
  "$DOMAIN"

after_hash="$(sha256sum "$CERT_DIR/$DOMAIN.crt" 2>/dev/null || true)"

if [ "$before_hash" != "$after_hash" ]; then
  echo "Cert changed — reloading nginx"
  systemctl reload nginx
else
  echo "Cert unchanged, nginx not touched"
fi
