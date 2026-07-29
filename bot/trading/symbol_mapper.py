from bot.config.constants import AssetClass
from bot.config.settings import OffsetDriftConfig, ProximityConfig, Settings
from bot.mt5.types import SymbolInfo

_METALS = frozenset({"XAUUSD", "XAGUSD", "GOLD", "SILVER"})
_OIL_KEYWORDS = ("OIL", "WTI", "BRENT", "XTI")
_INDEX_KEYWORDS = (
    "SPX",
    "NAS",
    "DAX",
    "DE30",
    "DE40",
    "F40",
    "JP225",
    "UK100",
    "US30",
    "US500",
    "US2000",
    "AUS200",
    "USTEC",
    "HK50",
    "CHINA",
)


# News tokens shown in bot_mode_status.news_mode are currency codes (USD, EUR,
# JPY, ...) plus a few named assets. Currency tokens substring-match the instrument
# symbol; named assets alias to the code that appears in their symbol.
_NEWS_ALIASES = {"GOLD": "XAU"}


def parse_news_symbols(news_mode: str | None) -> frozenset[str]:
    """Parse bot_mode_status.news_mode into a set of news tokens. NULL/empty means
    no news; a single 'ALL' token means every (non-exempt) instrument is gated."""
    if not news_mode:
        return frozenset()
    return frozenset(t.strip().upper() for t in news_mode.split(",") if t.strip())


def instrument_under_news(db_symbol: str, news_symbols: frozenset[str]) -> bool:
    """True if any active news token applies to this DB instrument. A token matches
    when (aliased) it is a substring of the symbol — so 'USD' hits EURUSD, USDJPY,
    XAUUSD, SPX500USD, and 'GOLD' (→XAU) hits XAUUSD only. Oil and US stocks are
    USD-denominated but their symbols (e.g. USOILSPOT, AMD.NAS) have no 'USD'
    substring, so USD news gates them via the asset-class path too."""
    if not news_symbols:
        return False
    if "ALL" in news_symbols:
        return True
    s = db_symbol.upper()
    if any(_NEWS_ALIASES.get(token, token) in s for token in news_symbols):
        return True
    return "USD" in news_symbols and detect_asset_class(db_symbol) in (
        AssetClass.OIL,
        AssetClass.STOCKS,
    )


def _index_lookup(table: dict[str, float], db_symbol: str) -> float | None:
    """Longest matching keyword wins, so an entry can't be claimed by a shorter one it
    contains — 'AUS2000' must take its own value, not 'US2000'. None = no entry."""
    s = db_symbol.upper()
    hits = [key for key in table if key.upper() in s]
    return table[max(hits, key=len)] if hits else None


def pip_size(info: SymbolInfo) -> float:
    """Pip size in price units. 5- and 3-digit instruments count 1 pip = 10
    points (e.g. EURUSD, USDJPY); everything else 1 pip = 1 point."""
    return info.point * (10 if info.digits in (3, 5) else 1)


def detect_asset_class(db_symbol: str) -> AssetClass:
    s = db_symbol.upper()

    if s in _METALS:
        return AssetClass.METALS
    if any(k in s for k in _OIL_KEYWORDS):
        return AssetClass.OIL
    # Stocks MUST be checked before indices (e.g. "AMD.NAS" contains "NAS")
    if s.endswith(".NAS") or s.endswith(".NYSE"):
        return AssetClass.STOCKS
    # Gold and micro-gold futures (e.g. MGCQ6, GCZ6) — must precede the index check
    if s.startswith("MGC") or s.startswith("GC"):
        return AssetClass.METALS
    if any(k in s for k in _INDEX_KEYWORDS):
        return AssetClass.INDICES
    if (s.endswith("USD") or s.endswith("USDT")) and len(s) > 6:
        return AssetClass.CRYPTO
    if "JPY" in s:
        return AssetClass.FOREX_JPY
    return AssetClass.FOREX


def _suffix_for(db_symbol: str, config: Settings) -> str:
    asset = detect_asset_class(db_symbol).value
    for rule in config.symbol_suffixes:
        if asset in rule.asset_classes:
            return rule.suffix
    return ""


def map_symbol(db_symbol: str, config: Settings) -> str:
    if db_symbol in config.symbol_map:
        mt5_symbol = config.symbol_map[db_symbol]
    elif db_symbol.upper().endswith((".NAS", ".NYSE")) and db_symbol not in config.stock_no_suffix:
        mt5_symbol = db_symbol + config.stock_suffix
    else:
        mt5_symbol = db_symbol
    # Don't double up when an explicit symbol_map target already carries the suffix
    # (e.g. mapping SPX500USD -> "SPX500m" while indices also has an "m" rule).
    suffix = _suffix_for(db_symbol, config)
    if suffix and not mt5_symbol.endswith(suffix):
        mt5_symbol += suffix
    return mt5_symbol


def needs_offset(db_symbol: str, config: Settings) -> bool:
    return db_symbol in config.offset_instruments


def proximity_threshold(
    asset_class: AssetClass, info: SymbolInfo, prox: ProximityConfig, db_sym: str
) -> float | None:
    """Distance in price units within which a limit counts as 'close' — the same
    distance the placement gate uses to decide a pending order is worth placing.
    None means this asset class has no configured threshold (no proximity gate)."""
    if asset_class == AssetClass.STOCKS:
        s = db_sym.upper()
        for sym, threshold in prox.stock_overrides.items():
            if sym.upper() in s:
                return threshold
        return prox.stocks

    if asset_class == AssetClass.INDICES:
        return _index_lookup(prox.indices, db_sym)  # None = unrecognized, no filter

    if asset_class in (AssetClass.FOREX, AssetClass.FOREX_JPY):
        pip_sz = pip_size(info)
        if pip_sz <= 0:
            return None
        pips = prox.forex_pips if asset_class == AssetClass.FOREX else prox.forex_jpy_pips
        return pips * pip_sz

    if asset_class == AssetClass.METALS:
        return prox.metals

    if asset_class == AssetClass.CRYPTO:
        s = db_sym.upper()
        for sym, threshold in prox.crypto_overrides.items():
            if sym.upper() in s:
                return threshold
        return prox.crypto

    if asset_class == AssetClass.OIL:
        return prox.oil

    return None  # any other unhandled classes


def offset_drift_threshold(asset_class: AssetClass, drift: OffsetDriftConfig, db_sym: str) -> float:
    """Max offset drift (in the instrument's own price units) tolerated before a
    pending offset order is re-placed. Offset instruments are non-forex, so this is
    always a dollar/point distance, never pips."""
    if asset_class == AssetClass.INDICES:
        match = _index_lookup(drift.indices, db_sym)
        return drift.default if match is None else match
    if asset_class == AssetClass.CRYPTO:
        return drift.crypto
    if asset_class == AssetClass.OIL:
        return drift.oil
    if asset_class == AssetClass.METALS:
        return drift.metals
    return drift.default


def is_symbol_available(db_symbol: str, broker_symbols, config: Settings) -> bool:
    """True if the broker carries the MT5 symbol this DB instrument maps to —
    either directly, or via the bare stock symbol when the suffixed form is
    absent (mirrors the stock-suffix fallback in the placement pre-check)."""
    mt5_symbol = map_symbol(db_symbol, config)
    if mt5_symbol in broker_symbols:
        return True
    if config.stock_suffix and mt5_symbol.endswith(config.stock_suffix):
        return mt5_symbol[: -len(config.stock_suffix)] in broker_symbols
    return False


def _strip_to_base(mt5_symbol: str, config: Settings) -> str:
    for db_sym, mapped in config.symbol_map.items():
        if mapped == mt5_symbol:
            return db_sym
    if config.stock_suffix and mt5_symbol.endswith(config.stock_suffix):
        return mt5_symbol[: -len(config.stock_suffix)]
    return mt5_symbol


def db_symbol_from_mt5(mt5_symbol: str, config: Settings) -> str:
    """Reverse-map an MT5 symbol back to its DB symbol for asset-class detection.
    The broker suffix depends on the (unknown) asset class, so try each configured
    suffix (longest first) and keep the candidate that round-trips through
    map_symbol; fall back to a plain strip when none match."""
    for suffix in sorted(
        {r.suffix for r in config.symbol_suffixes if r.suffix}, key=len, reverse=True
    ):
        if mt5_symbol.endswith(suffix):
            db_sym = _strip_to_base(mt5_symbol[: -len(suffix)], config)
            if map_symbol(db_sym, config) == mt5_symbol:
                return db_sym
    return _strip_to_base(mt5_symbol, config)
