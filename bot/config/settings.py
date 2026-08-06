import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from bot.config.constants import (
    _PRODUCTION_DSN,
    _PRODUCTION_LICENSE_URL,
    _PRODUCTION_UPDATE_MANIFEST_URL,
    AssetClass,
)

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config.json")

_VALID_ASSET_CLASSES = frozenset(a.value for a in AssetClass)

# Feed-offset instruments: DB symbols whose Supabase price comes from an external
# feed (OANDA indices/oil, Binance crypto), so the bot derives a broker-vs-feed
# offset before placing. Forex and gold are direct-feed (broker price) and absent.
DEFAULT_OFFSET_INSTRUMENTS = [
    "SPX500USD",
    "NAS100USD",
    "BTCUSDT",
    "ETHUSDT",
    "US30USD",
    "US2000USD",
    "USOILSPOT",
    "DE30EUR",
    "UK100GBP",
    "JP225",
    "F40",
    "AUS2000",
    "HK50",
    "CHINA50",
    "CHINAH",
]

# Default DB→broker symbol map (DB symbol → MT5/broker symbol). Single source for both
# the Settings.symbol_map default and the symbol-map backfill migration, so an install
# missing an entry gets the default without overriding any the user has customised.
_DEFAULT_SYMBOL_MAP = {
    "SPX500USD": "US500",
    "NAS100USD": "USTEC",
    "BTCUSDT": "BTCUSD",
    "ETHUSDT": "ETHUSD",
    "USOILSPOT": "XTIUSD",
    "US30USD": "US30",
    "US2000USD": "US2000",
    "DE30EUR": "DE40",
    "UK100GBP": "UK100",
    "AUS2000": "AUS200",
}

# Symbols every existing install should carry as offset-feed after updating. Applied
# once per install via `migrate_config` (tracked in `config_migrations`), so a user
# may still remove them afterwards without the migration re-adding them.
_OFFSET_BACKFILL_SYMBOLS = ("USOILSPOT", "DE30EUR", "US2000USD")
_MIGRATION_OFFSET_BACKFILL = "offset_feed_backfill_v1"

# Wider placement proximity for existing installs: forex/JPY → 15 pips, metals →
# $25, indices doubled. Applied once per install via `migrate_config`, so a user may
# still re-tune any value afterwards without the migration re-applying.
_MIGRATION_PROXIMITY_BUMP = "proximity_bump_v1"

# Move the forex pending-cancel/placement-block start earlier (16:45 → 15:55) so
# late-market signals stop activating well before the spread spike. SL stripping is
# unchanged (sl_strip_start 16:55). Applied once per install via `migrate_config`, so
# a user may still re-tune daily_start afterwards without the migration re-applying.
_MIGRATION_SPREAD_HOUR_LATE = "spread_hour_late_market_v1"

# Backfill any missing _DEFAULT_SYMBOL_MAP entry for installs that predate it: without
# the DB→broker map the offset can't resolve to the broker symbol. Applied via
# setdefault, so a user who already re-mapped an entry keeps their own choice. UK100 is
# also added to offset_instruments (it postdates the earlier offset backfill).
_MIGRATION_SYMBOL_MAP_BACKFILL = "symbol_map_backfill_v1"

# Ship the "Risky Gold" channel disabled (unchecked) for existing installs: it appears
# in the By-channel list after updating but its signals are skipped until the user opts
# in. Applied once, so a user who later enables it keeps it enabled.
_RISKY_GOLD_CHANNEL_ID = "1522144546299838524"
_MIGRATION_RISKY_GOLD_DISABLED = "risky_gold_channel_disabled_v1"

# Move the stock spread-hour windows earlier (both to 15:40) for existing installs. The
# old sl_strip_stock_start (15:55) fired after the broker had already shut the symbol at
# the 16:00 close, so the SL strip was rejected with MARKET_CLOSED; 15:40 lands while the
# session is still open. stock_daily_start moves in step so cancellation stays aligned.
# Applied once per install, so a user may re-tune either value afterwards.
_MIGRATION_STOCK_SPREAD_EARLY = "stock_spread_hour_early_v1"

# Canonical per-symbol stock placement-proximity distances (price units), keyed by DB
# symbol and substring-matched in proximity_threshold. Ships in config.example.json for
# new installs and is applied to existing installs via _MIGRATION_STOCK_PROXIMITY. The
# old shipped set was sparse bare tickers (AAPL, AMD, …), so any unlisted stock fell
# through to the wide `stocks` default (5.0) and placed regardless of distance — churning
# Supabase/MT5 calls. Full-symbol keys avoid the bare-ticker substring collisions (e.g.
# "MU" inside "TMUS"). A user may re-tune any value afterwards.
_STOCK_PROXIMITY_OVERRIDES = {
    "AAPL.NAS": 2.0,
    "AAXJ.NAS": 0.45,
    "ABNB.NAS": 0.5,
    "ACLS.NAS": 4.0,
    "ACMR.NAS": 4.0,
    "AMD.NAS": 3.0,
    "AMZN.NAS": 2.0,
    "ANF.NAS": 1.0,
    "ANF.NYSE": 1.0,
    "AVGO.NAS": 3.0,
    "BAC.NYSE": 0.6,
    "BIDU.NAS": 1.0,
    "CLSK.NAS": 0.5,
    "COIN.NAS": 3.0,
    "COST.NAS": 5.0,
    "CRWD.NAS": 8.0,
    "CSCO.NAS": 0.51,
    "CTAS.NAS": 2.0,
    "GGLS.NAS": 0.7,
    "GILD.NAS": 1.5,
    "GOOG.NAS": 5.0,
    "GOOGL.NAS": 5.0,
    "INTC.NAS": 1.0,
    "ISRG.NAS": 3.0,
    "LIN.NAS": 3.0,
    "LRCX.NAS": 2.0,
    "MARA.NAS": 0.2,
    "MRNA.NAS": 1.0,
    "MSFT.NAS": 4.0,
    "MSTR.NAS": 3.0,
    "MU.NAS": 7.5,
    "NFLX.NAS": 0.75,
    "NKE.NYSE": 2.0,
    "NVDA.NAS": 2.0,
    "PDD.NAS": 0.5,
    "PEP.NAS": 1.0,
    "PLTR.NAS": 2.0,
    "PTON.NAS": 0.1,
    "PYPL.NAS": 1.0,
    "QCOM.NAS": 1.0,
    "QQQ.NAS": 2.5,
    "RIVN.NAS": 0.15,
    "RKLB.NAS": 1.0,
    "SBUX.NAS": 0.5,
    "SNDK.NAS": 40.0,
    "SNPS.NAS": 10.0,
    "SOFI.NAS": 0.3,
    "SPY.NYSE": 3.0,
    "TMUS.NAS": 1.5,
    "TSLA.NAS": 2.5,
    "UAL.NAS": 1.0,
    "WMT.NAS": 0.66,
    "XOM.NYSE": 1.0,
    "XYZ.NYSE": 1.5,
}
_MIGRATION_STOCK_PROXIMITY = "stock_proximity_overrides_v1"

# Backfill the F40 (CAC 40) index proximity for existing installs. F40 is now classified
# as an index (added to _INDEX_KEYWORDS); without a proximity.indices entry the index gate
# returns None (no filter) and F40 limits place unconditionally. setdefault, so a user who
# set their own F40 value keeps it.
_MIGRATION_INDEX_F40 = "index_proximity_f40_v1"

# Slow the live-price feed pull from 2s to 5s to cut Supabase pooler egress. Existing
# installs that saved settings via the UI have the old value pinned in config.json
# (the full model is written back), so the changed default alone never reaches them.
# Only touch the pinned old default (2) — a user who deliberately set another value keeps it.
_MIGRATION_LIVE_PRICE_INTERVAL = "live_price_interval_5s_v1"

# Seed a per-symbol crypto proximity for ETH. The flat `crypto` threshold (1000) is
# tuned for BTC (~$100k); on ETH (~$3k) it's ~30% of price, so ETH limits place far from
# the market. setdefault, so a user who set their own ETHUSDT value keeps it.
_CRYPTO_PROXIMITY_OVERRIDES = {"ETHUSDT": 40.0}
_MIGRATION_CRYPTO_PROXIMITY = "crypto_proximity_overrides_v1"

# Backfill TP instrument_overrides for traded instruments that were riding the
# asset-class base: index aliases (DE40 == DE30EUR, UK100/UK100GBP, F40), the XTIUSD
# oil alias (toll, mirroring USOILSPOT), GOOGL (mirrors GOOG), and higher-priced stocks
# whose $5 stock-base trail closed them far too early. setdefault per instrument, so a
# user who already tuned any of these keeps their own value.
_TP_INSTRUMENT_OVERRIDES: dict[str, dict] = {
    "DE40": {
        "standard": {"profit_threshold": 50.0, "trailing_distance": 50.0},
        "scalp": {"profit_threshold": 30.0, "trailing_distance": 30.0},
        "swing": {"profit_threshold": 250.0, "trailing_distance": 250.0},
    },
    "UK100": {"standard": {"profit_threshold": 10.0, "trailing_distance": 10.0}},
    "UK100GBP": {"standard": {"profit_threshold": 10.0, "trailing_distance": 10.0}},
    "F40": {"standard": {"profit_threshold": 7.0, "trailing_distance": 7.0}},
    "XTIUSD": {"toll": {"profit_threshold": 0.4, "trailing_distance": 0.4}},
    "GOOGL.NAS": {
        "standard": {"profit_threshold": 2.5, "trailing_distance": 2.5},
        "scalp": {"profit_threshold": 1.5, "trailing_distance": 1.5},
        "swing": {"profit_threshold": 12.5, "trailing_distance": 12.5},
    },
    "CRWD.NAS": {"profit_threshold": 1.0, "trailing_distance": 1.0, "partial_close_percent": 50},
    "LRCX.NAS": {"profit_threshold": 1.5, "trailing_distance": 1.5, "partial_close_percent": 50},
    "SNPS.NAS": {"profit_threshold": 2.5, "trailing_distance": 2.5, "partial_close_percent": 50},
    "CTAS.NAS": {"profit_threshold": 1.0, "trailing_distance": 1.0, "partial_close_percent": 50},
    "ANF.NYSE": {"profit_threshold": 0.75, "trailing_distance": 0.75, "partial_close_percent": 50},
    "XOM.NYSE": {"profit_threshold": 0.3, "trailing_distance": 0.3, "partial_close_percent": 50},
}
_MIGRATION_TP_INSTRUMENT_OVERRIDES = "tp_instrument_overrides_backfill_v1"

# JP225 is OANDA-fed but was only ever listed in DEFAULT_OFFSET_INSTRUMENTS — never in
# the shipped config.example.json nor any backfill — and that default only applies when
# `offset_instruments` is absent entirely, which no install has. So every install treated
# it as direct-feed and placed its limits/SL in the OANDA frame on a broker quoting ~150
# points away. Applied once per install, so a user may still set it back to direct-feed.
_MIGRATION_JP225_OFFSET = "jp225_offset_backfill_v1"

# Fix the UK 100 DB symbol typo (UK100USD → UK100GBP, the actual OANDA feed symbol) for
# existing installs that carried the wrong key in offset_instruments, symbol_map, and
# tp_config.instrument_overrides. Renames the entry in place (preserving any user-set
# value) unless a correct UK100GBP entry already exists. Applied once per install.
_MIGRATION_UK100_SYMBOL_FIX = "uk100_symbol_fix_v1"

# Turn on Follow Server TP for existing installs — outcome data showed the signal
# service's TP timing beats the local profit threshold. Forced on once; a user who
# later unchecks it keeps it off, since the migration runs at most once per install.
_MIGRATION_FOLLOW_SERVER_TP = "follow_server_tp_enable_v1"

# OANDA-fed indices the signal service now emits: HK50 (Hang Seng), CHINA50 (A50),
# CHINAH (HSCEI) and AUS2000 (ASX 200), plus F40 — which has been OANDA-fed all along
# but was never listed as an offset instrument, so its limits went in on the OANDA frame
# against a broker quoting a few points away. Thresholds mirror the signal service's own
# per-instrument tables; only AUS2000 needs a broker mapping (AUS200 at ICMarkets).
# setdefault throughout, so a user who already tuned any of these keeps their value.
_OANDA_INDEX_OFFSETS = ("F40", "AUS2000", "HK50", "CHINA50", "CHINAH")
_OANDA_INDEX_PROXIMITY = {"AUS2000": 40.0, "HK50": 100.0, "CHINA50": 60.0, "CHINAH": 40.0}
_OANDA_INDEX_DRIFT = {"AUS2000": 5.0, "HK50": 12.0, "CHINA50": 8.0, "CHINAH": 5.0}
_OANDA_INDEX_TP: dict[str, dict] = {
    "AUS2000": {"default": {"profit_threshold": 10.0, "trailing_distance": 10.0}},
    "HK50": {"default": {"profit_threshold": 25.0, "trailing_distance": 25.0}},
    "CHINA50": {"default": {"profit_threshold": 15.0, "trailing_distance": 15.0}},
    "CHINAH": {"default": {"profit_threshold": 10.0, "trailing_distance": 10.0}},
}
_MIGRATION_OANDA_INDICES = "oanda_indices_backfill_v1"

# Make 50% partial-close the default for existing installs — outcome data now favours
# banking half at the TP trigger and trailing the rest over trailing the full position.
# Forced once, but only flips values still at the old default of 0, so a user who
# deliberately chose another partial-close % keeps it. The dedicated `risky` mode is
# skipped (its own trail-full default stands).
_MIGRATION_PARTIAL_CLOSE_50 = "partial_close_50_v1"

# Gold toll signals trail too wide off the metals base — tighten to a $4 trigger with a
# $2 trail. Only rewrites the value every shipped config has carried ($5/$5), so a user
# who tuned gold's toll TP keeps theirs.
_GOLD_TOLL_TP_SHIPPED = {"profit_threshold": 5.0, "trailing_distance": 5.0}
_GOLD_TOLL_TP = {"profit_threshold": 4.0, "trailing_distance": 2.0}
_MIGRATION_GOLD_TOLL_TP = "gold_toll_tp_v1"

# A total-lot value of 0 means "auto": the engine sizes it from the account balance on
# the first cycle where the balance is known, using the same median-signal model as the
# "Calculate approximate sizes" button, then writes the resolved lots back to
# config.json. It's a one-shot — once filled the value is no longer 0.
AUTO_LOT_VALUE = 0.0

# New risk defaults: 2.5% per limit globally, with gold on an auto-sized total lot (its
# signals ladder enough limits that per-limit risk sizing overshoots). Applied to
# existing installs only when lot_sizing still matches a shipped default exactly — a
# 5% global risk plus at most the one XAUUSD risk-% exception every template has
# carried. Anything else (a tuned percentage, a "fixed" mode, or the exception rows the
# approximate-sizes button writes) is a deliberate choice and is left alone.
_SHIPPED_GOLD_EXCEPTION = {
    "symbol": "XAUUSD",
    "signal_type": "all",
    "mode": "risk_percent",
    "value": 5.0,
}
_GOLD_TOTAL_LOT_EXCEPTION = {
    "symbol": "XAUUSD",
    "signal_type": "all",
    "mode": "total_lot",
    "value": AUTO_LOT_VALUE,
}
_MIGRATION_GOLD_TOTAL_LOT = "risk_2_5_gold_total_lot_v1"


def _lot_sizing_untouched(lot: dict) -> bool:
    if lot.get("risk_percent") != 5.0 or lot.get("mode") not in ("risk_percent", "total_lot"):
        return False
    exceptions = lot.get("exceptions")
    if not isinstance(exceptions, list):  # legacy dict form — predates the shipped list
        return False
    if not exceptions:
        return True
    if len(exceptions) != 1 or not isinstance(exceptions[0], dict):
        return False
    ex = exceptions[0]
    return not ex.get("channel") and all(
        ex.get(key) == value for key, value in _SHIPPED_GOLD_EXCEPTION.items()
    )


def _flip_tp_partial_close_zeros(tp: dict) -> None:
    """Recursively flip every partial_close_percent == 0 to 50 within a tp_config
    subtree — top level, asset-class blocks, signal-type overrides, and instrument
    overrides — leaving deliberate non-zero values and the `risky` mode untouched."""
    for key, value in tp.items():
        if key == "risky":
            continue
        if key == "partial_close_percent":
            if value == 0:
                tp[key] = 50
        elif isinstance(value, dict):
            _flip_tp_partial_close_zeros(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _flip_tp_partial_close_zeros(item)


class SymbolSuffixRule(BaseModel):
    suffix: str
    asset_classes: list[str]  # AssetClass values this suffix applies to

    @field_validator("asset_classes")
    @classmethod
    def _validate_classes(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in _VALID_ASSET_CLASSES]
        if unknown:
            raise ValueError(f"unknown asset class(es): {', '.join(unknown)}")
        return v


class LotExceptionConfig(BaseModel):
    symbol: str = ""  # "" or "all" applies to every symbol
    channel: str = ""  # "" or "all" applies to every channel (stores channel_id)
    signal_type: str = "all"  # "all" applies to every signal type
    mode: str  # "risk_percent" | "fixed" | "total_lot"
    value: float  # percent for risk_percent, lots for fixed/total_lot


class ExcludedTradeConfig(BaseModel):
    symbol: str
    signal_type: str = "all"  # "all" excludes every signal type for this symbol


class ExcludedChannelAssetConfig(BaseModel):
    channel: str = ""  # "" or "all" = every channel (stores channel_id)
    asset_class: str = ""  # "" or "all" = every asset class


class LotSizingConfig(BaseModel):
    mode: str = "risk_percent"
    risk_percent: float | dict[str, float] = 2.5
    fixed_lot: float | dict[str, float] = 0.01
    # Total lots for a signal, split evenly across its limits (more limits = less per
    # limit = lower risk). Same per-instrument dict form as fixed_lot.
    total_lot: float | dict[str, float] = 0.1
    max_lot_per_order: float = 5.0
    # Skip signals carrying this many limits or more at placement (0 = off).
    skip_limits_at: int = 0
    exceptions: list[LotExceptionConfig] = []

    @field_validator("exceptions", mode="before")
    @classmethod
    def _coerce_exceptions(cls, v: object) -> object:
        # Back-compat: legacy `{symbol: {mode, value}}` dict → list of entries.
        if isinstance(v, dict):
            return [{"symbol": sym, **ex} for sym, ex in v.items()]
        return v


class PollingConfig(BaseModel):
    supabase_interval_seconds: int = 30
    tp_active_interval_seconds: int = 1
    tp_trailing_interval_seconds: int = 2
    license_heartbeat_seconds: int = 900
    # The active-signal set, force-exit statuses, and news/vol gates are no longer
    # interval knobs: the sync cycle polls one tiny bot_mode_status row per cycle and
    # refetches the heavy queries only when its signals_rev watermark moves (see
    # sync_cycle for the fallback max-ages).
    # Feed health flips only on feed degradation and is a dashboard indicator, not a
    # trading gate, so it's pulled far less often than the news/vol mode gates.
    feed_health_interval_seconds: int = 60
    # Feed prices are cached this long between pulls. The offset is anchored to each
    # row's updated_at and cached 300s, so a few seconds of fetch staleness is
    # immaterial; a newly-appeared offset symbol still forces an immediate refetch.
    live_price_interval_seconds: int = 5


class SpreadHourConfig(BaseModel):
    daily_start: str = (
        "15:55"  # cancel pending / block placement 15 min ahead of the SL-strip window
    )
    stock_daily_start: str = "15:40"  # stocks close at 16:00 — cancel 20 min before
    daily_end: str = "18:00"
    # Filled positions have their SL stripped from here to daily_end so a spread spike
    # can't stop them out, then it's restored. Starts ~5 min before the spread spike for
    # forex (17:00 rollover). Stocks strip at 15:40 — the broker shuts the symbol at the
    # 16:00 close and rejects SL modifications past then, so the strip must land while the
    # session is still open.
    sl_strip_start: str = "16:55"
    sl_strip_stock_start: str = "15:40"
    timezone: str = "US/Eastern"
    weekend_start_day: str = "Friday"
    weekend_end_day: str = "Sunday"


class ProximityConfig(BaseModel):
    forex_pips: float = 15.0
    forex_jpy_pips: float = 15.0
    metals: float = 25.0
    crypto: float = 1000.0
    oil: float = 1.0
    stocks: float = 5.0
    indices: dict[str, float] = {
        "SPX": 40.0,
        "US500": 40.0,
        "NAS": 100.0,
        "USTEC": 100.0,
        "DAX": 100.0,
        "DE30": 100.0,
        "DE40": 100.0,
        "US30": 100.0,
        "US2000": 20.0,
        "AUS2000": 40.0,
        "UK100": 50.0,
        "F40": 40.0,
        "JP225": 200.0,
        "HK50": 100.0,
        "CHINA50": 60.0,
        "CHINAH": 40.0,
    }
    stock_overrides: dict[str, float] = {}
    crypto_overrides: dict[str, float] = {}


class OffsetDriftConfig(BaseModel):
    """Drift thresholds in the instrument's own price units (dollars/points), never
    pips. A still-pending offset order is cancelled for re-placement when its
    broker-vs-feed offset has drifted beyond this since placement. Offset
    instruments are all non-forex, so a pip has no meaning here."""

    indices: dict[str, float] = {
        "SPX": 3.0,
        "US500": 3.0,
        "NAS": 8.0,
        "USTEC": 8.0,
        "DAX": 8.0,
        "DE30": 8.0,
        "US30": 5.0,
        "US2000": 2.0,
        "AUS2000": 5.0,
        "JP225": 15.0,
        "HK50": 12.0,
        "CHINA50": 8.0,
        "CHINAH": 5.0,
    }
    crypto: float = 25.0
    oil: float = 0.15
    metals: float = 2.0
    default: float = 5.0


class AssetTPConfig(BaseModel):
    profit_threshold: float
    threshold_unit: str
    trailing_distance: float
    # Close this % at the TP trigger, trail the remainder. 0 = trail the full position.
    partial_close_percent: int = 50


class ScalpOverrideConfig(BaseModel):
    profit_threshold: float
    trailing_distance: float
    partial_close_percent: int | None = None


class OneToOneConfig(BaseModel):
    profit_threshold: float = 10.0  # global default in account dollars
    overrides: dict[str, float] = {}  # per asset_class override (dollars)


class RiskyConfig(BaseModel):
    # TP behaves like a normal trailing type (dollar threshold, partial close, trailing),
    # not a fixed full-close like 1-1. Currently only gold, but keyed on signal_type so
    # any instrument tagged 'risky' in the DB is covered.
    profit_threshold: float = 4.0
    threshold_unit: str = "dollars"
    trailing_distance: float = 2.0
    partial_close_percent: int = 0
    # Custom stop-loss distance in the instrument's price units, measured from the
    # signal's deepest limit. None = use the signal's DB stop-loss. When set it overrides
    # the DB SL for every limit of a risky signal: SL = deepest_limit - distance (long),
    # deepest_limit + distance (short), where deepest = lowest (long) / highest (short).
    stop_loss: float | None = None
    # UTC windows "HH:MM-HH:MM" during which risky signals are disabled entirely —
    # pending limits cancelled, filled positions force-closed, placement blocked. They
    # re-place after the window if still active in the DB.
    disabled_windows: list[str] = [
        "21:55-23:10",
        "00:55-02:00",
        "11:55-14:00",
    ]
    overrides: dict[str, ScalpOverrideConfig] = {}  # per asset_class


class TPConfig(BaseModel):
    # When true the signal service becomes the TP trigger authority: instead of the
    # per-asset profit_threshold below, a position takes profit the moment its signal
    # flips to 'profit' (closed_reason 'automatic') in the DB. Only the trigger changes —
    # trailing distance, partial close %, and every other exit path stay as configured.
    follow_server_tp: bool = True
    partial_close_percent: int = 50
    forex: AssetTPConfig
    forex_jpy: AssetTPConfig
    metals: AssetTPConfig
    indices: AssetTPConfig
    stocks: AssetTPConfig
    crypto: AssetTPConfig
    oil: AssetTPConfig
    scalp_overrides: dict[str, ScalpOverrideConfig] = {}
    toll_overrides: dict[str, ScalpOverrideConfig] = {}
    swing_overrides: dict[str, ScalpOverrideConfig] = {}
    pa_overrides: dict[str, ScalpOverrideConfig] = {}
    one_to_one: OneToOneConfig = OneToOneConfig()
    risky: RiskyConfig = RiskyConfig()
    instrument_overrides: dict[str, dict] = {}


class Settings(BaseModel):
    license_key: str = ""
    mt5_terminal_path: str = ""
    # Which copy of the bot this is when running several side by side on one machine.
    # Left at 1 (or absent) it behaves exactly like a lone install: single-instance
    # lock "mt5bot.lock" and UI port 8501. Give a *separate* install folder — with its
    # own config.json, orders.db, and mt5_terminal_path — instance_id 2, 3, ... to run
    # concurrently: each instance N takes lock "mt5bot-N.lock" and port 8500+N. Two
    # folders sharing an instance_id still exclude each other (same lock), so the
    # double-open guard holds per instance.
    instance_id: int = 1
    lot_sizing: LotSizingConfig = LotSizingConfig()
    polling: PollingConfig = PollingConfig()
    symbol_map: dict[str, str] = dict(_DEFAULT_SYMBOL_MAP)
    stock_suffix: str = "-24"
    # Per-asset-class broker suffix rules. Each rule appends `suffix` to every MT5
    # symbol whose detected asset class is listed in `asset_classes` (e.g. Exness
    # "m" on forex/metals/crypto: EURUSD -> EURUSDm). An asset class may appear in
    # at most one rule.
    symbol_suffixes: list[SymbolSuffixRule] = []
    stock_no_suffix: list[str] = []
    excluded_symbols: list[str] = []
    # Per-(symbol, signal_type) exclusions. signal_type "all" drops every type.
    excluded_trades: list[ExcludedTradeConfig] = []
    # Per-(channel, asset_class) exclusions. A blank/"all" dimension is a wildcard;
    # a signal is dropped if any rule matches both its channel and its asset class.
    excluded_channel_assets: list[ExcludedChannelAssetConfig] = []
    # Signal types and channel ids that are skipped wholesale (empty = none skipped).
    disabled_signal_types: list[str] = []
    disabled_channels: list[str] = []
    offset_instruments: list[str] = list(DEFAULT_OFFSET_INSTRUMENTS)
    offset_drift: OffsetDriftConfig = OffsetDriftConfig()
    offset_drift_check_interval_seconds: int = 1800
    # Offset is a slow-moving broker-vs-feed property — recompute at most this often
    # per symbol and serve the cached value in between (the feed itself refreshes ~5s).
    offset_recompute_interval_seconds: int = 300
    # Dead-feed bound: while a signal is active the feed refreshes every few seconds,
    # so a stale updated_at means the feed updater has stalled — skip placement past
    # this age.
    feed_max_staleness_seconds: int = 120
    spread_hour: SpreadHourConfig = SpreadHourConfig()
    proximity: ProximityConfig = ProximityConfig()
    # One-time config migrations already applied to this install (see migrate_config).
    config_migrations: list[str] = []
    # Set true once the user accepts the in-app risk disclaimer. Default false (and
    # absent in existing installs' config.json) means the disclaimer still shows, so
    # existing users see it once and acceptance is backfilled by /api/disclaimer/accept.
    disclaimer_accepted: bool = False
    # When true the TP engine never trails or closes — the user owns every exit. The
    # bot still places, cancels, and updates limits, and cancels a signal's remaining
    # pending limits once its filled positions are all closed.
    disable_auto_tp: bool = False
    # When true the bot also reads bot_mode_status.vol_guard (the TM volatility guard,
    # same token format as news_mode) and gates on it exactly like news mode — cancelling
    # matching pending orders and force-closing matching filled positions. Off by default.
    volatility_guard: bool = False
    tp_config: TPConfig

    @model_validator(mode="before")
    @classmethod
    def _migrate_universal_suffix(cls, data: object) -> object:
        # Back-compat: a legacy flat `universal_suffix` becomes one rule covering
        # every asset class (its original all-symbols behaviour).
        if (
            isinstance(data, dict)
            and "symbol_suffixes" not in data
            and data.get("universal_suffix")
        ):
            data["symbol_suffixes"] = [
                {"suffix": data["universal_suffix"], "asset_classes": sorted(_VALID_ASSET_CLASSES)}
            ]
        return data

    @field_validator("instance_id")
    @classmethod
    def _positive_instance_id(cls, v: int) -> int:
        if v < 1:
            raise ValueError("instance_id must be >= 1")
        return v

    @field_validator("symbol_suffixes")
    @classmethod
    def _no_class_conflicts(cls, v: list[SymbolSuffixRule]) -> list[SymbolSuffixRule]:
        seen: set[str] = set()
        for rule in v:
            for ac in rule.asset_classes:
                if ac in seen:
                    raise ValueError(f"asset class '{ac}' assigned to multiple suffix rules")
                seen.add(ac)
        return v


def migrate_config(path: Path = _CONFIG_PATH) -> None:
    """Apply one-time, idempotent config rewrites that must survive an update. Each
    migration is recorded in `config_migrations` so it runs at most once per install
    — a user who later removes a backfilled symbol keeps it removed. Runs before
    load_config at startup; silently no-ops if config.json is missing or unparseable
    (load_config surfaces those)."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return

    applied = data.get("config_migrations")
    if not isinstance(applied, list):
        applied = []

    changed = False
    if _MIGRATION_OFFSET_BACKFILL not in applied:
        offset = data.get("offset_instruments")
        if not isinstance(offset, list):
            offset = list(DEFAULT_OFFSET_INSTRUMENTS)
        for sym in _OFFSET_BACKFILL_SYMBOLS:
            if sym not in offset:
                offset.append(sym)
        data["offset_instruments"] = offset
        applied.append(_MIGRATION_OFFSET_BACKFILL)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_PROXIMITY_BUMP not in applied:
        prox = data.get("proximity")
        if isinstance(prox, dict):
            prox["forex_pips"] = 15.0
            prox["forex_jpy_pips"] = 15.0
            prox["metals"] = 25.0
            indices = prox.get("indices")
            if isinstance(indices, dict):
                for sym, value in indices.items():
                    if isinstance(value, (int, float)):
                        indices[sym] = value * 2
            data["proximity"] = prox
        applied.append(_MIGRATION_PROXIMITY_BUMP)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_SPREAD_HOUR_LATE not in applied:
        sh = data.get("spread_hour")
        if isinstance(sh, dict):
            sh["daily_start"] = "15:55"
            data["spread_hour"] = sh
        applied.append(_MIGRATION_SPREAD_HOUR_LATE)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_UK100_SYMBOL_FIX not in applied:
        offset = data.get("offset_instruments")
        if isinstance(offset, list) and "UK100USD" in offset:
            if "UK100GBP" in offset:
                offset.remove("UK100USD")
            else:
                offset[offset.index("UK100USD")] = "UK100GBP"
            data["offset_instruments"] = offset
        for container, sub in (("symbol_map", None), ("tp_config", "instrument_overrides")):
            node = data.get(container)
            target = node.get(sub) if sub and isinstance(node, dict) else node
            if isinstance(target, dict) and "UK100USD" in target:
                target.setdefault("UK100GBP", target.pop("UK100USD"))
        applied.append(_MIGRATION_UK100_SYMBOL_FIX)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_SYMBOL_MAP_BACKFILL not in applied:
        offset = data.get("offset_instruments")
        if isinstance(offset, list) and "UK100GBP" not in offset:
            offset.append("UK100GBP")
            data["offset_instruments"] = offset
        smap = data.get("symbol_map")
        if isinstance(smap, dict):
            for db_sym, mt5_sym in _DEFAULT_SYMBOL_MAP.items():
                smap.setdefault(db_sym, mt5_sym)
            data["symbol_map"] = smap
        applied.append(_MIGRATION_SYMBOL_MAP_BACKFILL)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_JP225_OFFSET not in applied:
        offset = data.get("offset_instruments")
        if isinstance(offset, list) and "JP225" not in offset:
            offset.append("JP225")
            data["offset_instruments"] = offset
        applied.append(_MIGRATION_JP225_OFFSET)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_RISKY_GOLD_DISABLED not in applied:
        disabled = data.get("disabled_channels")
        if not isinstance(disabled, list):
            disabled = []
        if _RISKY_GOLD_CHANNEL_ID not in disabled:
            disabled.append(_RISKY_GOLD_CHANNEL_ID)
        data["disabled_channels"] = disabled
        applied.append(_MIGRATION_RISKY_GOLD_DISABLED)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_STOCK_SPREAD_EARLY not in applied:
        sh = data.get("spread_hour")
        if isinstance(sh, dict):
            sh["stock_daily_start"] = "15:40"
            sh["sl_strip_stock_start"] = "15:40"
            data["spread_hour"] = sh
        applied.append(_MIGRATION_STOCK_SPREAD_EARLY)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_STOCK_PROXIMITY not in applied:
        prox = data.get("proximity")
        if isinstance(prox, dict):
            existing = prox.get("stock_overrides")
            existing = existing if isinstance(existing, dict) else {}
            # Drop the old bare-ticker keys (AAPL, AMD, …): left in place they'd shadow
            # the new full-symbol keys via substring matching (first match wins). Custom
            # full-symbol overrides the user added are kept, then the canonical set is
            # applied on top.
            merged = {k: v for k, v in existing.items() if "." in k}
            merged.update(_STOCK_PROXIMITY_OVERRIDES)
            prox["stock_overrides"] = merged
            data["proximity"] = prox
        applied.append(_MIGRATION_STOCK_PROXIMITY)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_INDEX_F40 not in applied:
        prox = data.get("proximity")
        if isinstance(prox, dict):
            indices = prox.get("indices")
            if isinstance(indices, dict):
                indices.setdefault("F40", 40.0)
                data["proximity"] = prox
        applied.append(_MIGRATION_INDEX_F40)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_LIVE_PRICE_INTERVAL not in applied:
        polling = data.get("polling")
        if isinstance(polling, dict) and polling.get("live_price_interval_seconds") == 2:
            polling["live_price_interval_seconds"] = 5
            data["polling"] = polling
        applied.append(_MIGRATION_LIVE_PRICE_INTERVAL)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_CRYPTO_PROXIMITY not in applied:
        prox = data.get("proximity")
        if isinstance(prox, dict):
            existing = prox.get("crypto_overrides")
            existing = existing if isinstance(existing, dict) else {}
            for sym, value in _CRYPTO_PROXIMITY_OVERRIDES.items():
                existing.setdefault(sym, value)
            prox["crypto_overrides"] = existing
            data["proximity"] = prox
        applied.append(_MIGRATION_CRYPTO_PROXIMITY)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_TP_INSTRUMENT_OVERRIDES not in applied:
        tp = data.get("tp_config")
        if isinstance(tp, dict):
            overrides = tp.get("instrument_overrides")
            overrides = overrides if isinstance(overrides, dict) else {}
            for sym, override in _TP_INSTRUMENT_OVERRIDES.items():
                overrides.setdefault(sym, override)
            tp["instrument_overrides"] = overrides
            data["tp_config"] = tp
        applied.append(_MIGRATION_TP_INSTRUMENT_OVERRIDES)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_FOLLOW_SERVER_TP not in applied:
        tp = data.get("tp_config")
        if isinstance(tp, dict):
            tp["follow_server_tp"] = True
            data["tp_config"] = tp
        applied.append(_MIGRATION_FOLLOW_SERVER_TP)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_OANDA_INDICES not in applied:
        offset = data.get("offset_instruments")
        if isinstance(offset, list):
            offset.extend(sym for sym in _OANDA_INDEX_OFFSETS if sym not in offset)
            data["offset_instruments"] = offset
        smap = data.get("symbol_map")
        if isinstance(smap, dict):
            smap.setdefault("AUS2000", "AUS200")
            data["symbol_map"] = smap
        for container, table in (
            ("proximity", _OANDA_INDEX_PROXIMITY),
            ("offset_drift", _OANDA_INDEX_DRIFT),
        ):
            node = data.get(container)
            if isinstance(node, dict) and isinstance(node.get("indices"), dict):
                for sym, value in table.items():
                    node["indices"].setdefault(sym, value)
                data[container] = node
        tp = data.get("tp_config")
        if isinstance(tp, dict):
            overrides = tp.get("instrument_overrides")
            overrides = overrides if isinstance(overrides, dict) else {}
            for sym, override in _OANDA_INDEX_TP.items():
                overrides.setdefault(sym, override)
            tp["instrument_overrides"] = overrides
            data["tp_config"] = tp
        applied.append(_MIGRATION_OANDA_INDICES)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_PARTIAL_CLOSE_50 not in applied:
        tp = data.get("tp_config")
        if isinstance(tp, dict):
            _flip_tp_partial_close_zeros(tp)
            data["tp_config"] = tp
        applied.append(_MIGRATION_PARTIAL_CLOSE_50)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_GOLD_TOLL_TP not in applied:
        tp = data.get("tp_config")
        overrides = tp.get("instrument_overrides") if isinstance(tp, dict) else None
        gold = overrides.get("XAUUSD") if isinstance(overrides, dict) else None
        if isinstance(gold, dict) and gold.get("toll") == _GOLD_TOLL_TP_SHIPPED:
            gold["toll"] = dict(_GOLD_TOLL_TP)
            data["tp_config"] = tp
        applied.append(_MIGRATION_GOLD_TOLL_TP)
        data["config_migrations"] = applied
        changed = True

    if _MIGRATION_GOLD_TOTAL_LOT not in applied:
        lot = data.get("lot_sizing")
        if isinstance(lot, dict) and _lot_sizing_untouched(lot):
            lot["mode"] = "risk_percent"
            lot["risk_percent"] = 2.5
            lot["exceptions"] = [dict(_GOLD_TOTAL_LOT_EXCEPTION)]
            data["lot_sizing"] = lot
        applied.append(_MIGRATION_GOLD_TOTAL_LOT)
        data["config_migrations"] = applied
        changed = True

    if changed:
        path.write_text(json.dumps(data, indent=2))
        logger.info("Applied config migration(s): %s", ", ".join(applied))


def persist_auto_lot_values(values: dict[str, float], path: Path = _CONFIG_PATH) -> None:
    """Write resolved lots into the AUTO-sentinel total-lot exceptions (`values` maps
    broker symbol → total lots). Targeted raw-JSON edit so nothing else in config.json
    is rewritten, and only still-unresolved entries are touched."""
    data = json.loads(path.read_text())
    exceptions = data.get("lot_sizing", {}).get("exceptions")
    if not isinstance(exceptions, list):
        return
    for ex in exceptions:
        lot = values.get(ex.get("symbol", ""))
        if lot and ex.get("mode") == "total_lot" and ex.get("value") == AUTO_LOT_VALUE:
            ex["value"] = lot
    path.write_text(json.dumps(data, indent=2))


def load_config(path: Path = _CONFIG_PATH) -> Settings | None:
    try:
        data = json.loads(path.read_text())
        return Settings.model_validate(data)
    except FileNotFoundError:
        logger.error("config.json not found at %s", path.resolve())
        return None
    except json.JSONDecodeError as e:
        logger.error("config.json parse error: %s", e)
        return None
    except ValidationError as e:
        logger.error("config.json validation error: %s", e)
        return None


def load_dsn() -> str:
    return _PRODUCTION_DSN


def load_license_url() -> str:
    return _PRODUCTION_LICENSE_URL


def load_update_manifest_url() -> str:
    # Env override eases testing against a scratch bucket without rebuilding the constant.
    return os.environ.get("MT5BOT_UPDATE_URL") or _PRODUCTION_UPDATE_MANIFEST_URL
