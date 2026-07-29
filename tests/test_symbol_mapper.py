import pytest
from pydantic import ValidationError

from bot.config.constants import AssetClass
from bot.config.settings import OffsetDriftConfig, ProximityConfig
from bot.trading.symbol_mapper import (
    db_symbol_from_mt5,
    detect_asset_class,
    instrument_under_news,
    map_symbol,
    offset_drift_threshold,
    parse_news_symbols,
    pip_size,
    proximity_threshold,
)
from tests.conftest import make_settings

# ---------------------------------------------------------------------------
# detect_asset_class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol,expected",
    [
        # Metals
        ("XAUUSD", AssetClass.METALS),
        ("XAGUSD", AssetClass.METALS),
        ("GOLD", AssetClass.METALS),
        ("SILVER", AssetClass.METALS),
        # Gold futures contracts (e.g. MGCQ6, GCZ6) — also metals
        ("MGCQ6", AssetClass.METALS),
        ("MGCZ6", AssetClass.METALS),
        ("GCG7", AssetClass.METALS),
        # Oil
        ("WTI", AssetClass.OIL),
        ("BRENTUSD", AssetClass.OIL),
        ("USOIL", AssetClass.OIL),
        # Stocks — must beat indices (AMD.NAS contains "NAS")
        ("AMD.NAS", AssetClass.STOCKS),
        ("MSFT.NYSE", AssetClass.STOCKS),
        # Indices
        ("US500", AssetClass.INDICES),
        ("USTEC", AssetClass.INDICES),
        (
            "NAS100USD",
            AssetClass.INDICES,
        ),  # contains NAS keyword; NOT crypto (len>6 check comes after)
        ("DAX40", AssetClass.INDICES),
        ("DE30EUR", AssetClass.INDICES),  # German index — DE30 keyword
        ("F40", AssetClass.INDICES),  # CAC 40 — F40 keyword
        ("JP225", AssetClass.INDICES),
        ("AUS2000", AssetClass.INDICES),
        ("HK50", AssetClass.INDICES),
        ("CHINA50", AssetClass.INDICES),
        ("CHINAH", AssetClass.INDICES),
        ("USOILSPOT", AssetClass.OIL),  # offset-fed oil instrument
        # Crypto — ends USD/USDT and len > 6
        ("BTCUSDT", AssetClass.CRYPTO),  # len 7
        ("ETHUSDT", AssetClass.CRYPTO),  # len 7
        ("BTCUSD", AssetClass.FOREX),  # len 6 — falls through to FOREX
        # Forex JPY
        ("USDJPY", AssetClass.FOREX_JPY),
        ("EURJPY", AssetClass.FOREX_JPY),
        # Forex default
        ("EURUSD", AssetClass.FOREX),
        ("GBPUSD", AssetClass.FOREX),
    ],
)
def test_detect_asset_class(symbol: str, expected: AssetClass) -> None:
    assert detect_asset_class(symbol) == expected


def test_stocks_beats_indices_for_nas_suffix() -> None:
    # AMD.NAS contains "NAS" which is in _INDEX_KEYWORDS, but .NAS suffix check comes first
    assert detect_asset_class("AMD.NAS") == AssetClass.STOCKS
    assert detect_asset_class("MSFT.NYSE") == AssetClass.STOCKS


def test_btcusd_is_forex_not_crypto() -> None:
    # BTCUSD has len==6 so the crypto rule (len > 6) does not match
    assert detect_asset_class("BTCUSD") == AssetClass.FOREX


# ---------------------------------------------------------------------------
# offset_drift_threshold — price units (dollars/points), never pips
# ---------------------------------------------------------------------------


def test_offset_drift_threshold_resolves_per_asset_class() -> None:
    drift = OffsetDriftConfig()
    # Index keyword match (NAS), then the per-class scalars, then default for an
    # unrecognised index keyword.
    assert offset_drift_threshold(AssetClass.INDICES, drift, "NAS100USD") == drift.indices["NAS"]
    assert offset_drift_threshold(AssetClass.INDICES, drift, "DE30EUR") == drift.indices["DE30"]
    assert offset_drift_threshold(AssetClass.INDICES, drift, "FTSE100GBP") == drift.default
    assert offset_drift_threshold(AssetClass.CRYPTO, drift, "BTCUSDT") == drift.crypto
    assert offset_drift_threshold(AssetClass.OIL, drift, "USOILSPOT") == drift.oil
    assert offset_drift_threshold(AssetClass.METALS, drift, "XAUUSD") == drift.metals


def test_index_lookup_prefers_the_longest_matching_keyword() -> None:
    # "AUS2000" contains "US2000"; the longer key must win in both tables.
    drift = OffsetDriftConfig()
    prox = ProximityConfig()
    assert drift.indices["AUS2000"] != drift.indices["US2000"]
    assert offset_drift_threshold(AssetClass.INDICES, drift, "AUS2000") == drift.indices["AUS2000"]
    assert proximity_threshold(AssetClass.INDICES, None, prox, "AUS2000") == prox.indices["AUS2000"]


# ---------------------------------------------------------------------------
# proximity_threshold — crypto per-symbol overrides fall back to the flat value
# ---------------------------------------------------------------------------


def test_crypto_proximity_override_beats_flat_default() -> None:
    prox = ProximityConfig(crypto=1000.0, crypto_overrides={"ETHUSDT": 40.0})
    # ETH resolves to its override; BTC (no override) keeps the flat crypto value.
    assert proximity_threshold(AssetClass.CRYPTO, None, prox, "ETHUSDT") == 40.0
    assert proximity_threshold(AssetClass.CRYPTO, None, prox, "BTCUSDT") == 1000.0


# ---------------------------------------------------------------------------
# parse_news_symbols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, frozenset()),
        ("", frozenset()),
        ("USD", frozenset({"USD"})),
        ("usd", frozenset({"USD"})),  # uppercased
        ("USD, GOLD, EUR", frozenset({"USD", "GOLD", "EUR"})),
        ("USD,GOLD", frozenset({"USD", "GOLD"})),  # no spaces
        (" USD ,  JPY ", frozenset({"USD", "JPY"})),  # trimmed
        ("ALL", frozenset({"ALL"})),
    ],
)
def test_parse_news_symbols(raw, expected) -> None:
    assert parse_news_symbols(raw) == expected


# ---------------------------------------------------------------------------
# instrument_under_news
# ---------------------------------------------------------------------------


def test_instrument_under_news_no_news() -> None:
    assert instrument_under_news("EURUSD", frozenset()) is False


def test_instrument_under_news_all_matches_everything() -> None:
    assert instrument_under_news("EURUSD", frozenset({"ALL"})) is True
    assert instrument_under_news("XAUUSD", frozenset({"ALL"})) is True
    assert instrument_under_news("USOILSPOT", frozenset({"ALL"})) is True


@pytest.mark.parametrize(
    "instrument,expected",
    [
        # USD substring matches forex, gold, and USD-quoted indices
        ("EURUSD", True),
        ("USDJPY", True),
        ("USDCAD", True),
        ("XAUUSD", True),
        ("SPX500USD", True),
        ("NAS100USD", True),
        ("US2000USD", True),
        # Oil is USD-denominated but has no 'USD' substring → asset-class path
        ("USOILSPOT", True),
        # US stocks are USD-denominated but have no 'USD' substring → asset-class path
        ("AMD.NAS", True),
        ("AAPL.NYSE", True),
        # Not USD-related
        ("EURGBP", False),
        ("EURJPY", False),
    ],
)
def test_instrument_under_usd_news(instrument: str, expected: bool) -> None:
    assert instrument_under_news(instrument, frozenset({"USD"})) is expected


def test_gold_token_aliases_to_xau() -> None:
    # 'GOLD' news targets XAUUSD only, not arbitrary USD instruments
    assert instrument_under_news("XAUUSD", frozenset({"GOLD"})) is True
    assert instrument_under_news("EURUSD", frozenset({"GOLD"})) is False
    assert instrument_under_news("USOILSPOT", frozenset({"GOLD"})) is False


def test_news_currency_component_match() -> None:
    assert instrument_under_news("EURUSD", frozenset({"EUR"})) is True
    assert instrument_under_news("EURJPY", frozenset({"JPY"})) is True
    assert instrument_under_news("EURJPY", frozenset({"GBP"})) is False


def test_news_multiple_tokens() -> None:
    news = frozenset({"GOLD", "EUR"})
    assert instrument_under_news("XAUUSD", news) is True  # GOLD→XAU
    assert instrument_under_news("EURJPY", news) is True  # EUR
    assert instrument_under_news("USDCAD", news) is False


# ---------------------------------------------------------------------------
# map_symbol
# ---------------------------------------------------------------------------


def test_map_symbol_uses_symbol_map() -> None:
    cfg = make_settings()
    assert map_symbol("BTCUSDT", cfg) == "BTCUSD"
    assert map_symbol("SPX500USD", cfg) == "US500"


def test_map_symbol_appends_stock_suffix() -> None:
    cfg = make_settings()
    assert map_symbol("AMD.NAS", cfg) == "AMD.NAS-24"
    assert map_symbol("MSFT.NYSE", cfg) == "MSFT.NYSE-24"


def test_map_symbol_passthrough() -> None:
    cfg = make_settings()
    assert map_symbol("EURUSD", cfg) == "EURUSD"
    assert map_symbol("XAUUSD", cfg) == "XAUUSD"


def test_map_symbol_appends_broker_suffix() -> None:
    cfg = make_settings(
        symbol_suffixes=[{"suffix": "m", "asset_classes": ["forex", "crypto", "stocks"]}]
    )
    # Plain passthrough, symbol-map target, and stock suffix all get the broker suffix.
    assert map_symbol("EURUSD", cfg) == "EURUSDm"
    assert map_symbol("BTCUSDT", cfg) == "BTCUSDm"
    assert map_symbol("AMD.NAS", cfg) == "AMD.NAS-24m"


def test_map_symbol_suffix_scoped_to_asset_class() -> None:
    # "m" only on forex/metals/crypto — indices and stocks stay bare.
    cfg = make_settings(
        symbol_suffixes=[
            {"suffix": "m", "asset_classes": ["forex", "forex_jpy", "metals", "crypto"]}
        ]
    )
    assert map_symbol("EURUSD", cfg) == "EURUSDm"
    assert map_symbol("XAUUSD", cfg) == "XAUUSDm"
    assert map_symbol("SPX500USD", cfg) == "US500"
    assert map_symbol("AMD.NAS", cfg) == "AMD.NAS-24"


def test_map_symbol_multiple_suffix_rules() -> None:
    cfg = make_settings(
        symbol_suffixes=[
            {"suffix": "m", "asset_classes": ["forex", "forex_jpy"]},
            {"suffix": ".r", "asset_classes": ["crypto"]},
        ]
    )
    assert map_symbol("EURUSD", cfg) == "EURUSDm"
    assert map_symbol("BTCUSDT", cfg) == "BTCUSD.r"


def test_map_symbol_does_not_double_suffix_explicit_mapping() -> None:
    # Mapping straight to "SPX500m" while indices also carries an "m" rule must not
    # produce "SPX500mm".
    cfg = make_settings(
        symbol_map={"SPX500USD": "SPX500m"},
        symbol_suffixes=[{"suffix": "m", "asset_classes": ["indices"]}],
    )
    assert map_symbol("SPX500USD", cfg) == "SPX500m"


def test_symbol_suffix_class_conflict_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            symbol_suffixes=[
                {"suffix": "m", "asset_classes": ["forex"]},
                {"suffix": "s", "asset_classes": ["forex"]},
            ]
        )


def test_symbol_suffix_unknown_class_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(symbol_suffixes=[{"suffix": "m", "asset_classes": ["stonks"]}])


def test_legacy_universal_suffix_migrated_to_all_classes() -> None:
    cfg = make_settings(universal_suffix="m")
    assert map_symbol("EURUSD", cfg) == "EURUSDm"
    assert map_symbol("SPX500USD", cfg) == "US500m"
    assert map_symbol("AMD.NAS", cfg) == "AMD.NAS-24m"


# ---------------------------------------------------------------------------
# db_symbol_from_mt5 (reverse mapping)
# ---------------------------------------------------------------------------


def test_db_symbol_from_mt5_reverses_symbol_map() -> None:
    cfg = make_settings()
    assert db_symbol_from_mt5("BTCUSD", cfg) == "BTCUSDT"
    assert db_symbol_from_mt5("US500", cfg) == "SPX500USD"


def test_db_symbol_from_mt5_strips_stock_suffix() -> None:
    cfg = make_settings()
    assert db_symbol_from_mt5("AMD.NAS-24", cfg) == "AMD.NAS"
    assert db_symbol_from_mt5("MSFT.NYSE-24", cfg) == "MSFT.NYSE"


def test_db_symbol_from_mt5_passthrough_unknown() -> None:
    cfg = make_settings()
    assert db_symbol_from_mt5("EURUSD", cfg) == "EURUSD"


def test_db_symbol_from_mt5_strips_broker_suffix() -> None:
    cfg = make_settings(
        symbol_suffixes=[{"suffix": "m", "asset_classes": ["forex", "crypto", "stocks"]}]
    )
    assert db_symbol_from_mt5("EURUSDm", cfg) == "EURUSD"
    assert db_symbol_from_mt5("BTCUSDm", cfg) == "BTCUSDT"  # reverse symbol_map after strip
    assert db_symbol_from_mt5("AMD.NAS-24m", cfg) == "AMD.NAS"  # strip broker then stock


def test_db_symbol_from_mt5_no_suffix_for_unscoped_class() -> None:
    # "m" only covers forex, so an indices symbol carries no suffix to strip.
    cfg = make_settings(symbol_suffixes=[{"suffix": "m", "asset_classes": ["forex"]}])
    assert db_symbol_from_mt5("US500", cfg) == "SPX500USD"
    assert db_symbol_from_mt5("EURUSDm", cfg) == "EURUSD"


def _sym_info(digits: int, point: float):
    from bot.mt5.types import SymbolInfo

    return SymbolInfo(
        name="X",
        digits=digits,
        point=point,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_tick_value=1.0,
        trade_tick_size=point,
        trade_contract_size=100000.0,
    )


def test_pip_size_five_digit_forex() -> None:
    assert pip_size(_sym_info(5, 0.00001)) == pytest.approx(0.0001)


def test_pip_size_three_digit_jpy() -> None:
    assert pip_size(_sym_info(3, 0.001)) == pytest.approx(0.01)


def test_pip_size_two_digit_metals() -> None:
    assert pip_size(_sym_info(2, 0.01)) == pytest.approx(0.01)


def test_pip_size_whole_point_indices() -> None:
    assert pip_size(_sym_info(1, 0.1)) == pytest.approx(0.1)
