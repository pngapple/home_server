"""Guards on the shipped tailnet resolver config (scripts/dnsmasq/status.conf).

This config used to live only on the Pi, hand-edited and untracked, which is
how it drifted into the state that took DNS down for every device accepting
Tailscale DNS - not just the shortcuts, but ordinary internet lookups, since
this resolver forwards those too. It is in the repo now so the failure is a
test away instead of an outage away.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "scripts" / "dnsmasq" / "status.conf"
INSTALL = REPO / "scripts" / "install_services.sh"


def _directives():
    """Config lines only, comments stripped - the comments discuss the bug."""
    return [
        line.strip()
        for line in CONF.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_uses_bind_dynamic_not_bind_interfaces():
    """bind-interfaces snapshots interface addresses at startup, so dnsmasq
    binds nothing when tailscale0 has no IPv4 yet - and stays "active" while
    doing it, so systemd reports healthy. bind-dynamic follows netlink."""
    directives = _directives()
    assert "bind-dynamic" in directives
    assert "bind-interfaces" not in directives


def test_stays_bound_to_the_tailnet_only():
    """An open resolver on the LAN or a public interface is a DNS amplification
    vector. The interface restriction is the only thing preventing that."""
    assert "interface=tailscale0" in _directives()


def test_records_are_templated_not_a_hardcoded_address():
    """Every machine gets a different tailnet IP. A literal address here would
    survive a move and point the new host's shortcuts at the old one."""
    text = CONF.read_text()
    assert "__TAILSCALE_IP__" in text

    literals = re.findall(r"^address=/[^/]+/(\S+)", text, flags=re.MULTILINE)
    assert literals, "expected some address= records"
    assert set(literals) == {"__TAILSCALE_IP__"}, f"hardcoded address: {literals}"


def test_installer_renders_the_placeholder():
    """The template is inert until install_services.sh substitutes it; if that
    step is ever dropped, dnsmasq gets '__TAILSCALE_IP__' as an address."""
    assert "__TAILSCALE_IP__" in INSTALL.read_text()


def test_installer_clears_stray_files_from_the_config_dir():
    """dnsmasq reads /etc/dnsmasq.d with `-7 <dir>,.dpkg-dist,.dpkg-old,.dpkg-new`,
    so anything not matching those suffixes is live config - a `.bak` copy next
    to the real file is merged in, not ignored."""
    installer = INSTALL.read_text()
    assert "/etc/dnsmasq.d.backups" in installer
    assert "! -name '*.conf'" in installer
