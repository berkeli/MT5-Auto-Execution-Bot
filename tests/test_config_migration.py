import json
from pathlib import Path

from bot.config.settings import (
    _CRYPTO_PROXIMITY_OVERRIDES,
    _GOLD_TOLL_TP,
    _GOLD_TOLL_TP_SHIPPED,
    _GOLD_TOTAL_LOT_EXCEPTION,
    _MIGRATION_CRYPTO_PROXIMITY,
    _MIGRATION_GOLD_TOLL_TP,
    _MIGRATION_GOLD_TOTAL_LOT,
    _MIGRATION_INDEX_F40,
    _MIGRATION_JP225_OFFSET,
    _MIGRATION_LIVE_PRICE_INTERVAL,
    _MIGRATION_OANDA_INDICES,
    _MIGRATION_OFFSET_BACKFILL,
    _MIGRATION_PARTIAL_CLOSE_50,
    _MIGRATION_PROXIMITY_BUMP,
    _MIGRATION_RISKY_GOLD_ENABLED,
    _MIGRATION_SEMI_SWING_PA_DISABLED,
    _MIGRATION_SPREAD_HOUR_LATE,
    _MIGRATION_STOCK_PROXIMITY,
    _MIGRATION_STOCK_SPREAD_EARLY,
    _MIGRATION_SYMBOL_MAP_BACKFILL,
    _MIGRATION_TP_INSTRUMENT_OVERRIDES,
    _MIGRATION_UK100_SYMBOL_FIX,
    _OANDA_INDEX_DRIFT,
    _OANDA_INDEX_OFFSETS,
    _OANDA_INDEX_PROXIMITY,
    _OANDA_INDEX_TP,
    _OFFSET_BACKFILL_SYMBOLS,
    _RISKY_GOLD_CHANNEL_ID,
    _SEMI_SWING_PA_CHANNEL_ID,
    _SHIPPED_GOLD_EXCEPTION,
    _STOCK_PROXIMITY_OVERRIDES,
    _TP_INSTRUMENT_OVERRIDES,
    AUTO_LOT_VALUE,
    DEFAULT_OFFSET_INSTRUMENTS,
    migrate_config,
)

_EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.json"


def _write(path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _read(path) -> dict:
    return json.loads(path.read_text())


def test_backfill_adds_missing_offset_symbols(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"]})

    migrate_config(cfg)

    data = _read(cfg)
    for sym in _OFFSET_BACKFILL_SYMBOLS:
        assert sym in data["offset_instruments"]
    assert "SPX500USD" in data["offset_instruments"]  # existing entries preserved
    assert _MIGRATION_OFFSET_BACKFILL in data["config_migrations"]


def test_backfill_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"]})
    migrate_config(cfg)

    # User removes a backfilled symbol after the one-time migration.
    data = _read(cfg)
    data["offset_instruments"].remove("USOILSPOT")
    _write(cfg, data)

    # Re-running must NOT re-add it — the marker records the migration already ran.
    migrate_config(cfg)
    assert "USOILSPOT" not in _read(cfg)["offset_instruments"]


def test_backfill_no_duplicate_when_already_present(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": list(_OFFSET_BACKFILL_SYMBOLS)})

    migrate_config(cfg)

    offset = _read(cfg)["offset_instruments"]
    for sym in _OFFSET_BACKFILL_SYMBOLS:
        assert offset.count(sym) == 1


def test_backfill_seeds_offset_when_key_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    offset = _read(cfg)["offset_instruments"]
    for sym in _OFFSET_BACKFILL_SYMBOLS:
        assert sym in offset


def test_proximity_bump_sets_forex_metals_and_doubles_indices(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(
        cfg,
        {
            "proximity": {
                "forex_pips": 10.0,
                "forex_jpy_pips": 10.0,
                "metals": 15.0,
                "indices": {"SPX": 20.0, "NAS": 50.0, "CUSTOM": 7.0},
            }
        },
    )

    migrate_config(cfg)

    prox = _read(cfg)["proximity"]
    assert prox["forex_pips"] == 15.0
    assert prox["forex_jpy_pips"] == 15.0
    assert prox["metals"] == 25.0
    # F40 and the OANDA indices are also backfilled (their migrations run in the same pass).
    assert prox["indices"] == {
        "SPX": 40.0,
        "NAS": 100.0,
        "CUSTOM": 14.0,
        "F40": 40.0,
        **_OANDA_INDEX_PROXIMITY,
    }
    assert _MIGRATION_PROXIMITY_BUMP in _read(cfg)["config_migrations"]


def test_proximity_bump_is_idempotent_and_respects_retuning(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"forex_pips": 10.0, "indices": {"SPX": 20.0}}})
    migrate_config(cfg)

    # User re-tunes after the one-time migration.
    data = _read(cfg)
    data["proximity"]["forex_pips"] = 8.0
    data["proximity"]["indices"]["SPX"] = 12.0
    _write(cfg, data)

    migrate_config(cfg)
    prox = _read(cfg)["proximity"]
    assert prox["forex_pips"] == 8.0  # not re-applied
    assert prox["indices"]["SPX"] == 12.0  # not re-doubled


def test_proximity_bump_noop_when_key_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    # No proximity block to migrate — new defaults apply at load — but the marker is
    # still recorded so it never runs against a later-added custom block.
    data = _read(cfg)
    assert "proximity" not in data
    assert _MIGRATION_PROXIMITY_BUMP in data["config_migrations"]


def test_spread_hour_late_moves_daily_start(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"spread_hour": {"daily_start": "16:45", "daily_end": "18:00"}})

    migrate_config(cfg)

    sh = _read(cfg)["spread_hour"]
    assert sh["daily_start"] == "15:55"
    assert sh["daily_end"] == "18:00"  # other keys untouched
    assert _MIGRATION_SPREAD_HOUR_LATE in _read(cfg)["config_migrations"]


def test_spread_hour_late_is_idempotent_and_respects_retuning(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"spread_hour": {"daily_start": "16:45"}})
    migrate_config(cfg)

    # User re-tunes after the one-time migration.
    data = _read(cfg)
    data["spread_hour"]["daily_start"] = "16:30"
    _write(cfg, data)

    migrate_config(cfg)
    assert _read(cfg)["spread_hour"]["daily_start"] == "16:30"  # not re-applied


def test_spread_hour_late_noop_when_key_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    data = _read(cfg)
    assert "spread_hour" not in data  # new default (15:55) applies at load
    assert _MIGRATION_SPREAD_HOUR_LATE in data["config_migrations"]


def test_stock_spread_hour_early_moves_both_stock_starts(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(
        cfg,
        {"spread_hour": {"stock_daily_start": "15:45", "sl_strip_stock_start": "15:55"}},
    )

    migrate_config(cfg)

    sh = _read(cfg)["spread_hour"]
    assert sh["stock_daily_start"] == "15:40"
    assert sh["sl_strip_stock_start"] == "15:40"
    assert _MIGRATION_STOCK_SPREAD_EARLY in _read(cfg)["config_migrations"]


def test_stock_spread_hour_early_is_idempotent_and_respects_retuning(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"spread_hour": {"sl_strip_stock_start": "15:55"}})
    migrate_config(cfg)

    # User re-tunes after the one-time migration.
    data = _read(cfg)
    data["spread_hour"]["sl_strip_stock_start"] = "15:50"
    _write(cfg, data)

    migrate_config(cfg)
    assert _read(cfg)["spread_hour"]["sl_strip_stock_start"] == "15:50"  # not re-applied


def test_symbol_map_backfill_adds_uk100_offset_and_default_maps(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"], "symbol_map": {"SPX500USD": "US500"}})

    migrate_config(cfg)

    data = _read(cfg)
    assert "UK100GBP" in data["offset_instruments"]
    # Every missing default map is backfilled, not just the indices.
    assert data["symbol_map"]["UK100GBP"] == "UK100"
    assert data["symbol_map"]["DE30EUR"] == "DE40"
    assert data["symbol_map"]["USOILSPOT"] == "XTIUSD"
    assert data["symbol_map"]["BTCUSDT"] == "BTCUSD"
    assert _MIGRATION_SYMBOL_MAP_BACKFILL in data["config_migrations"]


def test_symbol_map_backfill_preserves_existing_maps(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # User already re-mapped some symbols to their broker's names.
    _write(
        cfg,
        {"offset_instruments": ["DE30EUR"], "symbol_map": {"DE30EUR": "GER40", "UK100USD": "FTSE"}},
    )

    migrate_config(cfg)

    smap = _read(cfg)["symbol_map"]
    assert smap["DE30EUR"] == "GER40"  # custom map not forced to DE40
    # The typo'd UK100USD key is renamed to UK100GBP, carrying the user's custom value.
    assert "UK100USD" not in smap
    assert smap["UK100GBP"] == "FTSE"  # custom map not forced to UK100
    assert smap["USOILSPOT"] == "XTIUSD"  # absent one still backfilled


def test_symbol_map_backfill_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"], "symbol_map": {}})
    migrate_config(cfg)

    # User removes the UK100 offset row after the one-time migration.
    data = _read(cfg)
    data["offset_instruments"].remove("UK100GBP")
    _write(cfg, data)

    migrate_config(cfg)
    assert "UK100GBP" not in _read(cfg)["offset_instruments"]  # not re-added


def test_uk100_symbol_fix_renames_typo_on_existing_installs(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # An install that already carried the UK100USD typo across all three locations.
    _write(
        cfg,
        {
            "offset_instruments": ["SPX500USD", "UK100USD"],
            "symbol_map": {"UK100USD": "UK100"},
            "tp_config": {
                "instrument_overrides": {"UK100USD": {"standard": {"profit_threshold": 12.0}}}
            },
        },
    )

    migrate_config(cfg)

    data = _read(cfg)
    assert "UK100USD" not in data["offset_instruments"]
    assert "UK100GBP" in data["offset_instruments"]
    assert "UK100USD" not in data["symbol_map"]
    assert data["symbol_map"]["UK100GBP"] == "UK100"
    overrides = data["tp_config"]["instrument_overrides"]
    assert "UK100USD" not in overrides
    assert overrides["UK100GBP"] == {"standard": {"profit_threshold": 12.0}}
    assert _MIGRATION_UK100_SYMBOL_FIX in data["config_migrations"]


def test_uk100_symbol_fix_drops_duplicate_when_correct_key_present(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # Both keys present: the typo is dropped, the correct entry's value is kept.
    _write(
        cfg,
        {
            "offset_instruments": ["UK100USD", "UK100GBP"],
            "symbol_map": {"UK100USD": "OLD", "UK100GBP": "UK100"},
        },
    )

    migrate_config(cfg)

    data = _read(cfg)
    assert data["offset_instruments"].count("UK100GBP") == 1
    assert "UK100USD" not in data["offset_instruments"]
    assert "UK100USD" not in data["symbol_map"]
    assert data["symbol_map"]["UK100GBP"] == "UK100"


def test_jp225_offset_backfilled_on_existing_installs(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"]})

    migrate_config(cfg)

    data = _read(cfg)
    assert "JP225" in data["offset_instruments"]
    assert "SPX500USD" in data["offset_instruments"]  # existing entries preserved
    assert _MIGRATION_JP225_OFFSET in data["config_migrations"]


def test_jp225_offset_no_duplicate_when_already_present(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["JP225"]})

    migrate_config(cfg)

    assert _read(cfg)["offset_instruments"].count("JP225") == 1


def test_jp225_offset_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"]})
    migrate_config(cfg)

    # User switches JP225 back to direct-feed after the one-time migration.
    data = _read(cfg)
    data["offset_instruments"].remove("JP225")
    _write(cfg, data)

    migrate_config(cfg)
    assert "JP225" not in _read(cfg)["offset_instruments"]  # not re-added


def test_example_config_ships_every_default_offset_instrument() -> None:
    # DEFAULT_OFFSET_INSTRUMENTS only applies when `offset_instruments` is absent, and
    # the bundled template always sets it — so a symbol listed only in the default is
    # silently direct-feed for every install (how JP225 placed in the wrong frame).
    shipped = json.loads(_EXAMPLE_CONFIG.read_text())["offset_instruments"]
    assert set(DEFAULT_OFFSET_INSTRUMENTS) == set(shipped)


def test_risky_gold_channel_enabled_on_update(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"disabled_channels": [_RISKY_GOLD_CHANNEL_ID, "1512881096650391582"]})

    migrate_config(cfg)

    data = _read(cfg)
    assert _RISKY_GOLD_CHANNEL_ID not in data["disabled_channels"]
    assert "1512881096650391582" in data["disabled_channels"]  # other channels untouched
    assert _MIGRATION_RISKY_GOLD_ENABLED in data["config_migrations"]


def test_risky_gold_enable_is_idempotent_and_respects_redisable(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"disabled_channels": [_RISKY_GOLD_CHANNEL_ID]})
    migrate_config(cfg)

    # User turns the channel back off after the one-time migration.
    data = _read(cfg)
    data["disabled_channels"].append(_RISKY_GOLD_CHANNEL_ID)
    _write(cfg, data)

    migrate_config(cfg)
    assert _RISKY_GOLD_CHANNEL_ID in _read(cfg)["disabled_channels"]  # not re-removed


def test_semi_swing_pa_channel_disabled_on_update(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"disabled_channels": ["1512881096650391582"]})

    migrate_config(cfg)

    data = _read(cfg)
    assert data["disabled_channels"] == ["1512881096650391582", _SEMI_SWING_PA_CHANNEL_ID]
    assert _MIGRATION_SEMI_SWING_PA_DISABLED in data["config_migrations"]


def test_semi_swing_pa_disabled_when_list_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    # No list at all means every channel is on, so the opt-in channel must still be
    # written out — otherwise an existing install silently starts trading it.
    assert _read(cfg)["disabled_channels"] == [_SEMI_SWING_PA_CHANNEL_ID]


def test_semi_swing_pa_respects_reenable(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})
    migrate_config(cfg)

    # User ticks the channel on after the one-time migration.
    _write(cfg, {**_read(cfg), "disabled_channels": []})

    migrate_config(cfg)
    assert _read(cfg)["disabled_channels"] == []


def test_semi_swing_pa_ships_disabled_in_template() -> None:
    shipped = json.loads(_EXAMPLE_CONFIG.read_text())["disabled_channels"]
    assert _SEMI_SWING_PA_CHANNEL_ID in shipped


def test_stock_proximity_replaces_bare_keys_with_canonical(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # Old shipped set: sparse bare tickers that would shadow full-symbol keys.
    _write(cfg, {"proximity": {"stocks": 5.0, "stock_overrides": {"AAPL": 0.64, "MU": 1.0}}})

    migrate_config(cfg)

    overrides = _read(cfg)["proximity"]["stock_overrides"]
    assert overrides == _STOCK_PROXIMITY_OVERRIDES  # bare keys dropped, canonical applied
    assert "AAPL" not in overrides  # old bare key gone (would shadow "AAPL.NAS")
    assert overrides["AAPL.NAS"] == 2.0
    assert overrides["GOOGL.NAS"] == 5.0  # real traded symbol covered alongside GOOG.NAS
    assert _MIGRATION_STOCK_PROXIMITY in _read(cfg)["config_migrations"]


def test_stock_proximity_preserves_custom_full_symbol_keys(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"stock_overrides": {"AAPL": 0.64, "SMCI.NAS": 3.3}}})

    migrate_config(cfg)

    overrides = _read(cfg)["proximity"]["stock_overrides"]
    assert overrides["SMCI.NAS"] == 3.3  # user's custom full-symbol override kept
    assert "AAPL" not in overrides  # bare key still dropped


def test_stock_proximity_is_idempotent_and_respects_retuning(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"stock_overrides": {"AAPL": 0.64}}})
    migrate_config(cfg)

    # User re-tunes after the one-time migration.
    data = _read(cfg)
    data["proximity"]["stock_overrides"]["AAPL.NAS"] = 3.5
    _write(cfg, data)

    migrate_config(cfg)
    assert _read(cfg)["proximity"]["stock_overrides"]["AAPL.NAS"] == 3.5  # not re-applied


def test_stock_proximity_noop_when_proximity_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    data = _read(cfg)
    assert "proximity" not in data  # new default applies at load
    assert _MIGRATION_STOCK_PROXIMITY in data["config_migrations"]


def test_index_f40_backfilled_when_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # Pre-apply the index-doubling bump so it doesn't skew this migration's assertions.
    _write(
        cfg,
        {"config_migrations": [_MIGRATION_PROXIMITY_BUMP], "proximity": {"indices": {"SPX": 40.0}}},
    )

    migrate_config(cfg)

    indices = _read(cfg)["proximity"]["indices"]
    assert indices["F40"] == 40.0
    assert indices["SPX"] == 40.0  # existing entries preserved
    assert _MIGRATION_INDEX_F40 in _read(cfg)["config_migrations"]


def test_index_f40_respects_existing_value(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(
        cfg,
        {"config_migrations": [_MIGRATION_PROXIMITY_BUMP], "proximity": {"indices": {"F40": 60.0}}},
    )

    migrate_config(cfg)

    assert _read(cfg)["proximity"]["indices"]["F40"] == 60.0  # user's value not overwritten


def test_live_price_interval_bumped_from_pinned_default(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"polling": {"live_price_interval_seconds": 2}})

    migrate_config(cfg)

    assert _read(cfg)["polling"]["live_price_interval_seconds"] == 5
    assert _MIGRATION_LIVE_PRICE_INTERVAL in _read(cfg)["config_migrations"]


def test_live_price_interval_respects_custom_value(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"polling": {"live_price_interval_seconds": 10}})

    migrate_config(cfg)

    assert _read(cfg)["polling"]["live_price_interval_seconds"] == 10  # user's value untouched


def test_live_price_interval_is_idempotent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"polling": {"live_price_interval_seconds": 2}})
    migrate_config(cfg)

    # User later drops back to 2 after the one-time migration — must not be re-bumped.
    data = _read(cfg)
    data["polling"]["live_price_interval_seconds"] = 2
    _write(cfg, data)
    migrate_config(cfg)
    assert _read(cfg)["polling"]["live_price_interval_seconds"] == 2


def test_crypto_proximity_seeds_eth_when_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"crypto": 1000.0}})

    migrate_config(cfg)

    overrides = _read(cfg)["proximity"]["crypto_overrides"]
    assert overrides["ETHUSDT"] == _CRYPTO_PROXIMITY_OVERRIDES["ETHUSDT"]
    assert _MIGRATION_CRYPTO_PROXIMITY in _read(cfg)["config_migrations"]


def test_crypto_proximity_respects_existing_value(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"crypto_overrides": {"ETHUSDT": 25.0}}})

    migrate_config(cfg)

    assert _read(cfg)["proximity"]["crypto_overrides"]["ETHUSDT"] == 25.0  # not overwritten


def test_crypto_proximity_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"proximity": {"crypto": 1000.0}})
    migrate_config(cfg)

    # User removes the seeded ETH override after the one-time migration.
    data = _read(cfg)
    del data["proximity"]["crypto_overrides"]["ETHUSDT"]
    _write(cfg, data)

    migrate_config(cfg)
    assert "ETHUSDT" not in _read(cfg)["proximity"]["crypto_overrides"]  # not re-seeded


def test_missing_file_is_noop(tmp_path) -> None:
    cfg = tmp_path / "does_not_exist.json"
    migrate_config(cfg)  # must not raise
    assert not cfg.exists()


def test_migration_leaves_lot_sizing_and_tp_config_values_untouched(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # A user still on the original shipped defaults must keep their lot-sizing / TP
    # *values* after an update — only "Restore defaults" changes those. Exceptions:
    # the additive instrument_overrides backfill and the partial_close_50 flip (both
    # asserted separately), neither of which touches a deliberate non-zero value.
    _write(
        cfg,
        {
            "lot_sizing": {"mode": "risk_percent", "risk_percent": 5.0},
            "tp_config": {
                "partial_close_percent": 50,
                "metals": {"partial_close_percent": 75},
            },
        },
    )

    migrate_config(cfg)

    data = _read(cfg)
    assert "skip_limits_at" not in data["lot_sizing"]  # not injected
    assert data["lot_sizing"]["mode"] == "risk_percent"  # untouched
    assert data["tp_config"]["partial_close_percent"] == 50  # not flipped
    assert data["tp_config"]["metals"]["partial_close_percent"] == 75  # not flipped


def test_partial_close_50_flips_zeros_only(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(
        cfg,
        {
            "tp_config": {
                "partial_close_percent": 0,  # top level → flipped
                "forex": {"profit_threshold": 10.0, "partial_close_percent": 0},  # flipped
                "metals": {"profit_threshold": 7.0, "partial_close_percent": 30},  # kept
                "swing_overrides": {"forex_jpy": {"partial_close_percent": 0}},  # flipped
                "risky": {"partial_close_percent": 0},  # skipped — risky keeps trail-full
                "instrument_overrides": {
                    "XAUUSD": {"standard": {"partial_close_percent": 0}},  # flipped (nested)
                    "CRWD.NAS": {"partial_close_percent": 50},  # kept (flat)
                },
            }
        },
    )

    migrate_config(cfg)

    tp = _read(cfg)["tp_config"]
    assert tp["partial_close_percent"] == 50
    assert tp["forex"]["partial_close_percent"] == 50
    assert tp["metals"]["partial_close_percent"] == 30  # deliberate value preserved
    assert tp["swing_overrides"]["forex_jpy"]["partial_close_percent"] == 50
    assert tp["risky"]["partial_close_percent"] == 0  # risky untouched
    assert tp["instrument_overrides"]["XAUUSD"]["standard"]["partial_close_percent"] == 50
    assert tp["instrument_overrides"]["CRWD.NAS"]["partial_close_percent"] == 50
    assert _MIGRATION_PARTIAL_CLOSE_50 in _read(cfg)["config_migrations"]


def test_partial_close_50_is_idempotent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"tp_config": {"forex": {"partial_close_percent": 0}}})
    migrate_config(cfg)

    # User re-sets it to trail-full after the one-time migration; must not re-flip.
    data = _read(cfg)
    data["tp_config"]["forex"]["partial_close_percent"] = 0
    _write(cfg, data)

    migrate_config(cfg)
    assert _read(cfg)["tp_config"]["forex"]["partial_close_percent"] == 0


def test_partial_close_50_noop_when_tp_config_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    assert _MIGRATION_PARTIAL_CLOSE_50 in _read(cfg)["config_migrations"]


def test_tp_instrument_overrides_backfilled_when_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"tp_config": {"partial_close_percent": 0}})

    migrate_config(cfg)

    overrides = _read(cfg)["tp_config"]["instrument_overrides"]
    for sym, override in _TP_INSTRUMENT_OVERRIDES.items():
        assert overrides[sym] == override
    assert _MIGRATION_TP_INSTRUMENT_OVERRIDES in _read(cfg)["config_migrations"]


def test_tp_instrument_overrides_preserve_existing_and_custom_keys(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # User already tuned DE40 and has an unrelated custom instrument.
    custom_de40 = {"standard": {"profit_threshold": 99.0, "trailing_distance": 99.0}}
    _write(
        cfg,
        {
            "tp_config": {
                "instrument_overrides": {
                    "DE40": custom_de40,
                    "SMCI.NAS": {"profit_threshold": 3.0, "trailing_distance": 3.0},
                }
            }
        },
    )

    migrate_config(cfg)

    overrides = _read(cfg)["tp_config"]["instrument_overrides"]
    assert overrides["DE40"] == custom_de40  # user's tuning not overwritten
    assert overrides["SMCI.NAS"]["profit_threshold"] == 3.0  # unrelated custom kept
    assert overrides["CRWD.NAS"] == _TP_INSTRUMENT_OVERRIDES["CRWD.NAS"]  # gap still filled


def test_tp_instrument_overrides_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"tp_config": {}})
    migrate_config(cfg)

    # User removes a backfilled override after the one-time migration.
    data = _read(cfg)
    del data["tp_config"]["instrument_overrides"]["XOM.NYSE"]
    _write(cfg, data)

    migrate_config(cfg)
    overrides = _read(cfg)["tp_config"]["instrument_overrides"]
    assert "XOM.NYSE" not in overrides  # not re-added


def test_tp_instrument_overrides_noop_when_tp_config_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {})

    migrate_config(cfg)

    data = _read(cfg)
    assert "tp_config" not in data  # nothing to backfill onto; template applies at load
    assert _MIGRATION_TP_INSTRUMENT_OVERRIDES in data["config_migrations"]


def test_oanda_indices_backfilled_when_absent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(
        cfg,
        {
            # Pre-applied so the index-doubling bump doesn't skew this migration's assertions.
            "config_migrations": [_MIGRATION_PROXIMITY_BUMP],
            "offset_instruments": ["SPX500USD"],
            "symbol_map": {"SPX500USD": "US500"},
            "proximity": {"indices": {"US2000": 20.0}},
            "offset_drift": {"indices": {"US2000": 2.0}},
            "tp_config": {"instrument_overrides": {}},
        },
    )

    migrate_config(cfg)

    data = _read(cfg)
    for sym in _OANDA_INDEX_OFFSETS:
        assert sym in data["offset_instruments"]
    assert data["symbol_map"]["AUS2000"] == "AUS200"
    assert data["proximity"]["indices"] == {
        "US2000": 20.0,
        "F40": 40.0,  # backfilled by the F40 migration in the same pass
        **_OANDA_INDEX_PROXIMITY,
    }
    assert data["offset_drift"]["indices"] == {"US2000": 2.0, **_OANDA_INDEX_DRIFT}
    overrides = data["tp_config"]["instrument_overrides"]
    assert {k: overrides[k] for k in _OANDA_INDEX_TP} == _OANDA_INDEX_TP
    assert _MIGRATION_OANDA_INDICES in data["config_migrations"]


def test_oanda_indices_respect_existing_values(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    custom_hk = {"default": {"profit_threshold": 40.0, "trailing_distance": 40.0}}
    _write(
        cfg,
        {
            "config_migrations": [_MIGRATION_PROXIMITY_BUMP],
            "offset_instruments": ["HK50"],
            "symbol_map": {"AUS2000": "AUS200.a"},
            "proximity": {"indices": {"CHINA50": 25.0}},
            "offset_drift": {"indices": {"CHINA50": 3.0}},
            "tp_config": {"instrument_overrides": {"HK50": custom_hk}},
        },
    )

    migrate_config(cfg)

    data = _read(cfg)
    assert data["offset_instruments"].count("HK50") == 1  # not duplicated
    assert data["symbol_map"]["AUS2000"] == "AUS200.a"
    assert data["proximity"]["indices"]["CHINA50"] == 25.0
    assert data["offset_drift"]["indices"]["CHINA50"] == 3.0
    assert data["tp_config"]["instrument_overrides"]["HK50"] == custom_hk


def test_oanda_indices_is_idempotent_and_respects_removal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"offset_instruments": ["SPX500USD"]})
    migrate_config(cfg)

    data = _read(cfg)
    data["offset_instruments"].remove("CHINAH")
    _write(cfg, data)

    migrate_config(cfg)
    assert "CHINAH" not in _read(cfg)["offset_instruments"]  # not re-added


# ---------------------------------------------------------------------------
# 2.5% risk + auto-sized gold total lot, and gold's tighter toll TP
# ---------------------------------------------------------------------------


def _shipped_lot_sizing(**overrides) -> dict:
    lot = {
        "mode": "total_lot",
        "risk_percent": 5.0,
        "fixed_lot": 0.01,
        "total_lot": 0.1,
        "max_lot_per_order": 999.0,
        "exceptions": [dict(_SHIPPED_GOLD_EXCEPTION)],
        "skip_limits_at": 0,
    }
    lot.update(overrides)
    return lot


def test_gold_total_lot_migrates_untouched_lot_sizing(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"lot_sizing": _shipped_lot_sizing()})

    migrate_config(cfg)

    lot = _read(cfg)["lot_sizing"]
    assert lot["mode"] == "risk_percent"
    assert lot["risk_percent"] == 2.5
    assert lot["exceptions"] == [dict(_GOLD_TOTAL_LOT_EXCEPTION)]
    assert lot["exceptions"][0]["value"] == AUTO_LOT_VALUE  # engine sizes it from balance
    assert lot["max_lot_per_order"] == 999.0  # untouched keys survive
    assert _MIGRATION_GOLD_TOTAL_LOT in _read(cfg)["config_migrations"]


def test_gold_total_lot_migrates_when_exceptions_empty(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"lot_sizing": _shipped_lot_sizing(mode="risk_percent", exceptions=[])})

    migrate_config(cfg)

    assert _read(cfg)["lot_sizing"]["exceptions"] == [dict(_GOLD_TOTAL_LOT_EXCEPTION)]


def test_gold_total_lot_skips_tuned_risk_percent(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    lot = _shipped_lot_sizing(risk_percent=1.0)
    _write(cfg, {"lot_sizing": lot})

    migrate_config(cfg)

    assert _read(cfg)["lot_sizing"] == lot


def test_gold_total_lot_skips_user_exceptions(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    # What the "Calculate approximate sizes" button leaves behind.
    lot = _shipped_lot_sizing(
        mode="fixed",
        exceptions=[
            {"symbol": "XAUUSD", "signal_type": "all", "mode": "fixed", "value": 0.3},
            {"symbol": "US500", "signal_type": "all", "mode": "fixed", "value": 1.2},
        ],
    )
    _write(cfg, {"lot_sizing": lot})

    migrate_config(cfg)

    assert _read(cfg)["lot_sizing"] == lot


def test_gold_total_lot_skips_per_channel_exception(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    scoped = dict(_SHIPPED_GOLD_EXCEPTION, channel="1522144546299838524")
    lot = _shipped_lot_sizing(exceptions=[scoped])
    _write(cfg, {"lot_sizing": lot})

    migrate_config(cfg)

    assert _read(cfg)["lot_sizing"] == lot


def test_gold_total_lot_is_idempotent_and_respects_reversal(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    _write(cfg, {"lot_sizing": _shipped_lot_sizing()})
    migrate_config(cfg)

    # User goes back to a flat fixed lot after the one-time migration.
    data = _read(cfg)
    data["lot_sizing"]["mode"] = "fixed"
    data["lot_sizing"]["exceptions"] = []
    _write(cfg, data)

    migrate_config(cfg)
    assert _read(cfg)["lot_sizing"]["mode"] == "fixed"
    assert _read(cfg)["lot_sizing"]["exceptions"] == []


def test_gold_toll_tp_tightened(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    gold = {
        "standard": {"profit_threshold": 7.0, "trailing_distance": 5.0},
        "toll": dict(_GOLD_TOLL_TP_SHIPPED),
    }
    _write(cfg, {"tp_config": {"instrument_overrides": {"XAUUSD": gold}}})

    migrate_config(cfg)

    overrides = _read(cfg)["tp_config"]["instrument_overrides"]["XAUUSD"]
    assert overrides["toll"] == _GOLD_TOLL_TP == {"profit_threshold": 4.0, "trailing_distance": 2.0}
    assert overrides["standard"] == {"profit_threshold": 7.0, "trailing_distance": 5.0}
    assert _MIGRATION_GOLD_TOLL_TP in _read(cfg)["config_migrations"]


def test_gold_toll_tp_respects_tuned_value(tmp_path) -> None:
    cfg = tmp_path / "config.json"
    tuned = {"profit_threshold": 8.0, "trailing_distance": 3.0}
    _write(cfg, {"tp_config": {"instrument_overrides": {"XAUUSD": {"toll": tuned}}}})

    migrate_config(cfg)

    assert _read(cfg)["tp_config"]["instrument_overrides"]["XAUUSD"]["toll"] == tuned


def test_example_config_carries_the_new_defaults() -> None:
    data = json.loads(_EXAMPLE_CONFIG.read_text())
    lot = data["lot_sizing"]
    assert lot["mode"] == "risk_percent" and lot["risk_percent"] == 2.5
    assert lot["exceptions"] == [
        {"symbol": "XAUUSD", "signal_type": "all", "mode": "total_lot", "value": AUTO_LOT_VALUE}
    ]
    gold = data["tp_config"]["instrument_overrides"]["XAUUSD"]
    assert gold["toll"] == _GOLD_TOLL_TP
