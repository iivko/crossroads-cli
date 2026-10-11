import pytest

from market_cli.client import Client, CLIError


@pytest.mark.parametrize("host", ["10.1.2.3", "172.16.1.2", "172.31.255.254", "192.168.68.109"])
def test_private_http_needs_explicit_opt_in(monkeypatch, host):
    monkeypatch.delenv("MARKET_ALLOW_LAN_HTTP", raising=False)
    with pytest.raises(CLIError, match="require HTTPS"):
        Client(f"http://{host}:8100")
    monkeypatch.setenv("MARKET_ALLOW_LAN_HTTP", "true")
    assert Client(f"http://{host}:8100").base_url == f"http://{host}:8100/api/v1"


@pytest.mark.parametrize(
    "host", ["example.com", "8.8.8.8", "169.254.169.254", "172.32.0.1", "100.64.0.1", "0.0.0.0", "[fd00::1]"]
)
def test_opt_in_does_not_enable_arbitrary_insecure_hosts(monkeypatch, host):
    monkeypatch.setenv("MARKET_ALLOW_LAN_HTTP", "true")
    with pytest.raises(CLIError, match="require HTTPS"):
        Client(f"http://{host}:8100")


def test_only_true_enables_lan_and_https_still_works(monkeypatch):
    monkeypatch.setenv("MARKET_ALLOW_LAN_HTTP", "false")
    with pytest.raises(CLIError):
        Client("http://192.168.68.109:8100")
    assert Client("https://market.example").base_url == "https://market.example/api/v1"
    assert Client("http://127.0.0.1:8100").base_url == "http://127.0.0.1:8100/api/v1"
