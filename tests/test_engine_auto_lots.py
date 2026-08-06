import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.config.settings import AUTO_LOT_VALUE, LotExceptionConfig, LotSizingConfig
from bot.core.engine import Engine
from tests.conftest import make_settings, make_symbol_info

# ICMarkets XAUUSD: $1 per 0.01 move on a 100oz contract, so $100 per $1 of price.
_GOLD = make_symbol_info(
    name="XAUUSD", digits=2, point=0.01, trade_tick_size=0.01, trade_contract_size=100.0
)


def _engine(balance: float, exceptions: list[LotExceptionConfig]) -> Engine:
    engine = Engine.__new__(Engine)
    engine._config = make_settings(
        lot_sizing=LotSizingConfig(
            mode="risk_percent", risk_percent=2.5, max_lot_per_order=999.0, exceptions=exceptions
        )
    )
    engine.lot_specs = {"XAUUSD": _GOLD}
    engine.dashboard_cache = SimpleNamespace(data=SimpleNamespace(account={"balance": balance}))
    return engine


def _auto_gold() -> LotExceptionConfig:
    return LotExceptionConfig(symbol="XAUUSD", signal_type="all", mode="total_lot", value=0.0)


def test_autofill_sizes_gold_total_lot_from_balance(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"lot_sizing": {"exceptions": [_auto_gold().model_dump()]}}))

    with patch("bot.core.engine.persist_auto_lot_values") as persist:
        _engine(10_000.0, [_auto_gold()])._autofill_auto_lots()

    # 5% of 10,000 over the median gold signal (cumulative SL 25.00 at $100/unit)
    # is 0.20 per limit; the total-lot form scales that by the median limit count.
    persist.assert_called_once_with({"XAUUSD": 0.6})


def test_autofill_waits_for_a_balance() -> None:
    for balance in (0.0, None):
        with patch("bot.core.engine.persist_auto_lot_values") as persist:
            _engine(balance, [_auto_gold()])._autofill_auto_lots()
        persist.assert_not_called()


def test_autofill_skips_resolved_and_non_auto_exceptions() -> None:
    resolved = LotExceptionConfig(symbol="XAUUSD", signal_type="all", mode="total_lot", value=0.6)
    risk = LotExceptionConfig(symbol="US500", signal_type="all", mode="risk_percent", value=0.0)
    with patch("bot.core.engine.persist_auto_lot_values") as persist:
        _engine(10_000.0, [resolved, risk])._autofill_auto_lots()
    persist.assert_not_called()


def test_autofill_skips_symbols_the_broker_lacks() -> None:
    absent = LotExceptionConfig(symbol="XPTUSD", signal_type="all", mode="total_lot", value=0.0)
    with patch("bot.core.engine.persist_auto_lot_values") as persist:
        _engine(10_000.0, [absent])._autofill_auto_lots()
    persist.assert_not_called()


def test_autofill_survives_a_failed_write() -> None:
    with patch("bot.core.engine.persist_auto_lot_values", side_effect=OSError("locked")):
        _engine(10_000.0, [_auto_gold()])._autofill_auto_lots()  # logged, not raised


def test_persist_fills_only_the_auto_sentinel(tmp_path) -> None:
    from bot.config.settings import persist_auto_lot_values

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "lot_sizing": {
                    "exceptions": [
                        {"symbol": "XAUUSD", "mode": "total_lot", "value": AUTO_LOT_VALUE},
                        {"symbol": "US500", "mode": "total_lot", "value": 1.2},
                    ]
                },
                "license_key": "keep-me",
            }
        )
    )

    persist_auto_lot_values({"XAUUSD": 0.6, "US500": 9.9}, cfg)

    data = json.loads(cfg.read_text())
    assert data["lot_sizing"]["exceptions"][0]["value"] == 0.6
    assert data["lot_sizing"]["exceptions"][1]["value"] == 1.2  # already resolved
    assert data["license_key"] == "keep-me"


def test_placement_splits_the_resolved_total_across_limits() -> None:
    from bot.trading.lot_calculator import LotCalculator

    config = make_settings(
        lot_sizing=LotSizingConfig(
            mode="risk_percent",
            risk_percent=2.5,
            max_lot_per_order=999.0,
            exceptions=[
                LotExceptionConfig(symbol="XAUUSD", signal_type="all", mode="total_lot", value=0.6)
            ],
        )
    )
    client = MagicMock()
    client.symbol_info.return_value = _GOLD
    calc = LotCalculator(client, config)

    assert calc.calculate(3250.0, [3300.0], "XAUUSD", "standard") == 0.6
    # 0.6/3 lands a hair under 0.2 in binary float and the split floors to the volume
    # step, so three limits take 0.19 each — under-sized, never over.
    assert calc.calculate(3250.0, [3300.0, 3290.0, 3280.0], "XAUUSD", "standard") == 0.19
