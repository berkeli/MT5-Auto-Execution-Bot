import pytest

from bot.config.settings import LotExceptionConfig
from bot.trading.lot_calculator import LotCalculator
from tests.conftest import make_account_info, make_settings, make_symbol_info


def _make_eurusd_info(**overrides):
    # EURUSD 5-digit: pip_sz=0.0001, pip_val=10 (tick_value=1, tick_size=0.00001)
    defaults = dict(
        name="EURUSD",
        digits=5,
        point=0.00001,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_tick_value=1.0,
        trade_tick_size=0.00001,
        trade_contract_size=100000.0,
    )
    defaults.update(overrides)
    return make_symbol_info(**defaults)


# ---------------------------------------------------------------------------
# Risk % mode
# ---------------------------------------------------------------------------


def test_risk_percent_basic(mock_mt5, sample_config) -> None:
    # balance=10000, risk=1% → risk_capital=100
    # 1 limit, stop_loss=1.09000, limit_price=1.09100 → sl_dist=0.001 → sl_pips=10
    # pip_val=10 → raw = 100 / (1 * 10 * 10) = 1.0 lot → clamp → 1.0
    mock_mt5.symbol_info.return_value = _make_eurusd_info()
    mock_mt5.account_info.return_value = make_account_info(balance=10000.0)

    calc = LotCalculator(mock_mt5, sample_config)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(1.0, abs=1e-6)


def test_risk_percent_multiple_limits(mock_mt5, sample_config) -> None:
    # 2 limits at same distance → same lot per limit
    mock_mt5.symbol_info.return_value = _make_eurusd_info()
    mock_mt5.account_info.return_value = make_account_info(balance=10000.0)

    calc = LotCalculator(mock_mt5, sample_config)
    # sl_pips = 10, num_limits=2 → raw = 100/(2*10*10) = 0.5 → 0.5 lot
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100, 1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Fixed lot mode
# ---------------------------------------------------------------------------


def test_fixed_lot_mode(mock_mt5) -> None:
    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(
            update={"mode": "fixed", "fixed_lot": 0.05}
        )
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.05, abs=1e-6)


def test_fixed_lot_clamped_to_volume_step(mock_mt5) -> None:
    # fixed_lot=0.037, volume_step=0.01 → floor(0.037/0.01)*0.01 = 0.03
    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(
            update={"mode": "fixed", "fixed_lot": 0.037}
        )
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info(volume_step=0.01)

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.03, abs=1e-6)


# ---------------------------------------------------------------------------
# Total lot mode — the value is split evenly across a signal's limits
# ---------------------------------------------------------------------------


def test_total_lot_split_across_limits(mock_mt5) -> None:
    # total_lot=0.09, 3 limits → each limit gets 0.03
    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(
            update={"mode": "total_lot", "total_lot": 0.09}
        )
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100, 1.09200, 1.09300], mt5_symbol="EURUSD"
    )

    assert lot == pytest.approx(0.03, abs=1e-6)


def test_total_lot_single_limit_is_whole_value(mock_mt5) -> None:
    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(
            update={"mode": "total_lot", "total_lot": 0.1}
        )
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.1, abs=1e-6)


def test_total_lot_exception_split_across_limits(mock_mt5) -> None:
    # A "total_lot" exception distributes its value the same way the global mode does.
    cfg = _cfg_with_exceptions(LotExceptionConfig(symbol="EURUSD", mode="total_lot", value=0.1))
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100, 1.09200], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# ladder_size — the declared limit count drives the split, not the visible rows
# ---------------------------------------------------------------------------


def _gold_info():
    return make_symbol_info(
        name="XAUUSD",
        digits=2,
        point=0.01,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_tick_value=1.0,
        trade_tick_size=0.01,
        trade_contract_size=100.0,
    )


def test_total_lot_splits_by_declared_ladder_size(mock_mt5) -> None:
    # Signal 3600: the TM posted seq 1 and 4 first, seq 2/3 landed 12s later. The
    # first placement sees 2 of 4 prices but total_limits already says 4, so both
    # batches size at 0.2/4 and the ladder totals the configured 0.2 (not 0.3).
    cfg = _cfg_with_exceptions(LotExceptionConfig(symbol="XAUUSD", mode="total_lot", value=0.2))
    mock_mt5.symbol_info.return_value = _gold_info()
    calc = LotCalculator(mock_mt5, cfg)

    first = calc.calculate(4324.5, [4305.2, 4314.5], "XAUUSD", "toll", ladder_size=4)
    rest = calc.calculate(4324.5, [4305.2, 4314.5, 4307.3, 4312.2], "XAUUSD", "toll", ladder_size=4)

    assert first == pytest.approx(0.05, abs=1e-6)
    assert rest == pytest.approx(0.05, abs=1e-6)
    assert first * 2 + rest * 2 == pytest.approx(0.2, abs=1e-6)


def test_ladder_size_below_visible_count_is_ignored(mock_mt5) -> None:
    # A stale/short declared count must never divide by less than what we can see.
    cfg = _cfg_with_exceptions(LotExceptionConfig(symbol="XAUUSD", mode="total_lot", value=0.2))
    mock_mt5.symbol_info.return_value = _gold_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(4324.5, [4305.2, 4314.5, 4307.3, 4312.2], "XAUUSD", ladder_size=2)

    assert lot == pytest.approx(0.05, abs=1e-6)


def test_risk_percent_splits_by_declared_ladder_size(mock_mt5) -> None:
    # Same staggered-insert protection for the risk_percent denominator:
    # balance=10000, risk=1%, sl_pips=10, pip_val=10 → 100/(4*10*10) = 0.25.
    mock_mt5.symbol_info.return_value = _make_eurusd_info()
    mock_mt5.account_info.return_value = make_account_info(balance=10000.0)

    calc = LotCalculator(mock_mt5, make_settings())
    lot = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", ladder_size=4
    )

    assert lot == pytest.approx(0.25, abs=1e-6)


# ---------------------------------------------------------------------------
# Volume step flooring (not rounding)
# ---------------------------------------------------------------------------


def test_volume_step_floor_not_round(mock_mt5, sample_config) -> None:
    # Construct scenario where raw lot ≈ 0.059 with step=0.01
    # floor → 0.05, round → 0.06 — test that we get 0.05
    # balance=1000, risk=1% → 10; 1 limit, sl_pips=17, pip_val=10 → raw=10/170≈0.0588
    mock_mt5.symbol_info.return_value = _make_eurusd_info(volume_step=0.01)
    mock_mt5.account_info.return_value = make_account_info(balance=1000.0)

    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(update={"risk_percent": 1.0})
    )
    calc = LotCalculator(mock_mt5, cfg)
    # stop_loss=1.09000, limit_price=1.09170 → sl_dist=0.0017 → sl_pips=17
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09170], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.05, abs=1e-6)  # floored, not rounded


# ---------------------------------------------------------------------------
# Clamp to volume_min and volume_max
# ---------------------------------------------------------------------------


def test_clamp_to_volume_min(mock_mt5, sample_config) -> None:
    # Very wide SL → raw lot tiny → clamped up to volume_min
    mock_mt5.symbol_info.return_value = _make_eurusd_info(volume_min=0.01, volume_step=0.01)
    mock_mt5.account_info.return_value = make_account_info(balance=100.0)

    calc = LotCalculator(mock_mt5, sample_config)
    # balance=100, risk=1% → 1; sl_pips=10000 → raw = 1/(1*10000*10) = 0.00001 < 0.01
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[2.09000], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.01, abs=1e-6)


def test_clamp_to_max_lot_per_order(mock_mt5) -> None:
    # Tiny SL → huge raw lot → capped by max_lot_per_order=5.0, then volume_max
    cfg = make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(
            update={"risk_percent": 10.0, "max_lot_per_order": 2.0}
        )
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info(volume_max=100.0, volume_step=0.01)
    mock_mt5.account_info.return_value = make_account_info(balance=100000.0)

    calc = LotCalculator(mock_mt5, cfg)
    # Very small SL → enormous raw → capped at max_lot_per_order=2.0
    lot = calc.calculate(stop_loss=1.08999, limit_prices=[1.09000], mt5_symbol="EURUSD")

    assert lot == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# Per-symbol exceptions, optionally scoped to a signal type
# ---------------------------------------------------------------------------


def _cfg_with_exceptions(*exceptions: LotExceptionConfig):
    return make_settings(
        lot_sizing=make_settings().lot_sizing.model_copy(update={"exceptions": list(exceptions)})
    )


def test_exception_all_applies_to_any_signal_type(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(
        LotExceptionConfig(symbol="EURUSD", signal_type="all", mode="fixed", value=0.5)
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", signal_type="scalp"
    )

    assert lot == pytest.approx(0.5, abs=1e-6)


def test_exception_signal_type_specific_beats_all(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(
        LotExceptionConfig(symbol="EURUSD", signal_type="all", mode="fixed", value=0.5),
        LotExceptionConfig(symbol="EURUSD", signal_type="scalp", mode="fixed", value=0.2),
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    scalp = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", signal_type="scalp"
    )
    swing = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", signal_type="swing"
    )

    assert scalp == pytest.approx(0.2, abs=1e-6)  # scalp-specific
    assert swing == pytest.approx(0.5, abs=1e-6)  # falls back to "all"


def test_exception_signal_type_only_does_not_apply_to_others(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(
        LotExceptionConfig(symbol="EURUSD", signal_type="scalp", mode="fixed", value=0.2)
    )
    cfg = cfg.model_copy(
        update={
            "lot_sizing": cfg.lot_sizing.model_copy(update={"mode": "fixed", "fixed_lot": 0.05})
        }
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    # swing has no matching exception → global fixed_lot applies
    lot = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", signal_type="swing"
    )

    assert lot == pytest.approx(0.05, abs=1e-6)


def test_channel_only_beats_symbol_and_type(mock_mt5) -> None:
    # Legends (channel) -> 0.3 beats XAUUSD swing (symbol+type) -> 0.5 for a
    # Legends gold-swing trade, per the channel > symbol > type weighting.
    cfg = _cfg_with_exceptions(
        LotExceptionConfig(channel="legends", mode="fixed", value=0.3),
        LotExceptionConfig(symbol="EURUSD", signal_type="swing", mode="fixed", value=0.5),
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(
        stop_loss=1.09000,
        limit_prices=[1.09100],
        mt5_symbol="EURUSD",
        signal_type="swing",
        channel_id="legends",
    )

    assert lot == pytest.approx(0.3, abs=1e-6)


def test_channel_symbol_type_beats_channel_only(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(
        LotExceptionConfig(channel="legends", mode="fixed", value=0.3),
        LotExceptionConfig(
            channel="legends", symbol="EURUSD", signal_type="swing", mode="fixed", value=0.5
        ),
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(
        stop_loss=1.09000,
        limit_prices=[1.09100],
        mt5_symbol="EURUSD",
        signal_type="swing",
        channel_id="legends",
    )

    assert lot == pytest.approx(0.5, abs=1e-6)


def test_blank_symbol_is_wildcard(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(LotExceptionConfig(symbol="", mode="fixed", value=0.4))
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD")

    assert lot == pytest.approx(0.4, abs=1e-6)


def test_channel_rule_ignored_when_trade_has_no_channel(mock_mt5) -> None:
    cfg = _cfg_with_exceptions(LotExceptionConfig(channel="legends", mode="fixed", value=0.3))
    cfg = cfg.model_copy(
        update={
            "lot_sizing": cfg.lot_sizing.model_copy(update={"mode": "fixed", "fixed_lot": 0.05})
        }
    )
    mock_mt5.symbol_info.return_value = _make_eurusd_info()

    calc = LotCalculator(mock_mt5, cfg)
    lot = calc.calculate(
        stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="EURUSD", channel_id=None
    )

    assert lot == pytest.approx(0.05, abs=1e-6)


def test_legacy_dict_exceptions_coerced(mock_mt5) -> None:
    from bot.config.settings import LotSizingConfig

    cfg = LotSizingConfig.model_validate(
        {"mode": "fixed", "exceptions": {"EURUSD": {"mode": "fixed", "value": 0.3}}}
    )

    assert cfg.exceptions[0].symbol == "EURUSD"
    assert cfg.exceptions[0].signal_type == "all"
    assert cfg.exceptions[0].value == 0.3


# ---------------------------------------------------------------------------
# Fallback when symbol_info unavailable
# ---------------------------------------------------------------------------


def test_fallback_when_symbol_info_none(mock_mt5, sample_config) -> None:
    mock_mt5.symbol_info.return_value = None

    calc = LotCalculator(mock_mt5, sample_config)
    lot = calc.calculate(stop_loss=1.09000, limit_prices=[1.09100], mt5_symbol="UNKNOWN")

    assert lot == pytest.approx(sample_config.lot_sizing.fixed_lot, abs=1e-6)


# ---------------------------------------------------------------------------
# resolve_lot_mode — config snapshot for tp_outcomes final rows
# ---------------------------------------------------------------------------


def test_resolve_lot_mode_global(sample_config) -> None:
    from bot.trading.lot_calculator import resolve_lot_mode

    out = resolve_lot_mode(sample_config, "EURUSD", "standard", None)
    assert out == {"mode": "risk_percent", "value": 1.0, "source": "global"}


def test_resolve_lot_mode_exception_wins(sample_config) -> None:
    from bot.config.settings import LotExceptionConfig
    from bot.trading.lot_calculator import resolve_lot_mode

    cfg = sample_config.model_copy(deep=True)
    cfg.lot_sizing.exceptions.append(
        LotExceptionConfig(symbol="XAUUSD", mode="total_lot", value=0.6)
    )

    out = resolve_lot_mode(cfg, "XAUUSD", "toll", None)
    assert out == {"mode": "total_lot", "value": 0.6, "source": "exception"}
    # Other symbols still resolve globally
    assert resolve_lot_mode(cfg, "EURUSD", "toll", None)["source"] == "global"
