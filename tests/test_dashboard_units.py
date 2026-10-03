"""The two dashboard units: only the SSH-tunnel one may trust a request with no Tailscale header."""

from pathlib import Path

SYSTEMD = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def test_the_tailscale_served_dashboard_never_trusts_a_missing_header():
    # tailscale serve sends no identity header for tagged devices, the VPS itself included.
    assert "TRADER_DASHBOARD_ALLOW_LOCAL" not in (SYSTEMD / "trader-dashboard.service").read_text()


def test_the_ssh_dashboard_is_confined_to_the_dashview_directory():
    unit = (SYSTEMD / "trader-dashboard-ssh.service").read_text()
    assert "Environment=TRADER_DASHBOARD_ALLOW_LOCAL=1" in unit
    assert "--uds /srv/trading-dashview/dashboard.sock" in unit
    assert "ConditionPathIsDirectory=/srv/trading-dashview" in unit
    setup = (SYSTEMD.parent / "setup" / "1-host.sh").read_text()
    assert "install -d -o runner -g dashview -m 0750 /srv/trading-dashview" in setup
    assert "(root|trader|runner)" in setup  # never put the strategist in dashview


def test_deploy_restarts_the_ssh_dashboard_only_where_it_runs():
    deploy = (SYSTEMD.parent / "trading-deploy").read_text()
    assert "systemctl --user try-restart trader-dashboard-ssh.service" in deploy
