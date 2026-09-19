import pytest

from okxbot.config import RiskLimits, load_config, load_credentials
from okxbot.errors import ConfigError


def test_a_typo_in_a_risk_key_is_an_error_not_a_silent_default():
    with pytest.raises(ConfigError, match="unknown risk limit"):
        RiskLimits.from_dict({"max_notional_per_trad": 100})


def test_whitelist_entries_are_upper_cased():
    limits = RiskLimits.from_dict({"symbol_whitelist": ["btc-usdt"]})
    assert limits.symbol_whitelist == ("BTC-USDT",)


def test_credentials_default_to_the_demo_environment(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    monkeypatch.delenv("OKX_LIVE_TRADING", raising=False)
    assert load_credentials().simulated is True


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "live", ""])
def test_near_miss_values_do_not_unlock_live_trading(monkeypatch, value):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    monkeypatch.setenv("OKX_LIVE_TRADING", value)
    assert load_credentials().simulated is True


def test_live_trading_needs_the_exact_phrase(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    monkeypatch.setenv("OKX_LIVE_TRADING", "i-understand-the-risk")
    assert load_credentials().simulated is False


def test_missing_variables_are_named_individually(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.delenv("OKX_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_PASSPHRASE", raising=False)
    with pytest.raises(ConfigError, match="OKX_API_SECRET.*OKX_PASSPHRASE"):
        load_credentials()


def test_credentials_are_optional_when_not_required(monkeypatch):
    for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    assert load_credentials(require=False) is None


def test_a_missing_explicit_config_path_is_an_error(monkeypatch):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config("/nonexistent/config.yaml", require_credentials=False)


def test_read_only_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("OKX_READ_ONLY", raising=False)
    assert load_config(require_credentials=False).read_only is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_read_only_accepts_the_usual_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("OKX_READ_ONLY", value)
    assert load_config(require_credentials=False).read_only is True


def test_read_only_is_independent_of_the_live_switch(monkeypatch):
    """"Point at the real account but refuse every write" must be expressible."""
    for name, value in (("OKX_API_KEY", "k"), ("OKX_API_SECRET", "s"),
                        ("OKX_PASSPHRASE", "p"),
                        ("OKX_LIVE_TRADING", "i-understand-the-risk"),
                        ("OKX_READ_ONLY", "1")):
        monkeypatch.setenv(name, value)
    config = load_config()
    assert config.simulated is False
    assert config.read_only is True
    assert config.environment_label == "LIVE, read-only"
