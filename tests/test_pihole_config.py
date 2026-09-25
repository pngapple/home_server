"""Guards on the shipped tailnet resolver config (scripts/pihole/pihole.toml).

The resolver this replaced (plain dnsmasq) lived only on the Pi for months,
hand-edited and untracked, which is how it drifted into the state that took
DNS down for every device accepting Tailscale DNS - not just the shortcuts,
but ordinary internet lookups, since this resolver forwards those too. The
same traps exist in Pi-hole under different names.
"""

import re
import tomllib
from pathlib import Path

from scripts.pihole.apply_settings import flatten

REPO = Path(__file__).resolve().parent.parent
CONF = REPO / "scripts" / "pihole" / "pihole.toml"
INSTALL = REPO / "scripts" / "install_services.sh"


def _settings():
    return tomllib.loads(CONF.read_text())


def test_listening_mode_is_not_bind():
    """BIND emits dnsmasq's bind-interfaces, which snapshots interface
    addresses at startup - so FTL binds nothing when tailscale0 has no IPv4
    yet, and stays "active" while doing it, so systemd reports healthy."""
    assert _settings()["dns"]["listeningMode"] == "SINGLE"


def test_stays_bound_to_the_tailnet_only():
    """An open resolver on the LAN or a public interface is a DNS amplification
    vector. The interface restriction is the only thing preventing that."""
    assert _settings()["dns"]["interface"] == "tailscale0"


def test_web_ui_is_loopback_only():
    """FTL's default binds 80/443 on every address, colliding with nginx on
    the tailnet IP. nginx publishes it as http://pihole instead."""
    assert _settings()["webserver"]["port"].startswith("127.0.0.1:")


def test_does_not_read_the_old_dnsmasq_dir():
    """/etc/dnsmasq.d held the retired resolver's config, including the
    `interface`/`bind-*` lines Pi-hole now owns."""
    assert _settings()["misc"]["etc_dnsmasq_d"] is False


def test_query_log_retention_stays_short():
    """Housemates were told the log keeps a week. Raising it quietly would
    break that promise."""
    assert _settings()["database"]["maxDBdays"] <= 7


def test_records_are_templated_not_a_hardcoded_address():
    """Every machine gets a different tailnet IP. A literal address here would
    survive a move and point the new host's shortcuts at the old one."""
    hosts = _settings()["dns"]["hosts"]
    assert hosts, "expected some shortcut records"
    addresses = {entry.split()[0] for entry in hosts}
    assert addresses == {"__TAILSCALE_IP__"}, f"hardcoded address: {addresses}"
    names = {entry.split()[1] for entry in hosts}
    assert {"llm", "cigboard", "voice", "status"} <= names


def test_installer_renders_the_placeholder():
    """The template is inert until install_services.sh substitutes it; if that
    step is ever dropped, FTL gets '__TAILSCALE_IP__' as an address."""
    assert "__TAILSCALE_IP__" in INSTALL.read_text()


def test_installer_keeps_system_dnsmasq_masked():
    """FTL and the dnsmasq package's service both want port 53. Disabled
    alone, a package upgrade can start dnsmasq again and FTL loses the port."""
    assert re.search(r"systemctl mask dnsmasq\.service", INSTALL.read_text())


def test_flatten_emits_what_ftl_cli_parses():
    """pihole-FTL --config takes dotted keys, JSON arrays and true/false."""
    out = dict(flatten({"dns": {"hosts": ["1.2.3.4 a"], "bogusPriv": True, "port": 53}}))
    assert out == {
        "dns.hosts": '["1.2.3.4 a"]',
        "dns.bogusPriv": "true",
        "dns.port": "53",
    }
