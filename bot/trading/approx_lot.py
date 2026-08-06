from dataclasses import dataclass

from bot.config.settings import Settings
from bot.mt5.types import SymbolInfo
from bot.trading.lot_calculator import _clamp, _pip_value_per_lot, price_distance_to_money
from bot.trading.symbol_mapper import map_symbol

# Target average risk per signal as a percent of account balance. The approximation
# sizes each instrument so a median-shaped signal risks roughly this much; signals
# with more limits or wider stops risk more, fewer/tighter risk less.
_TARGET_RISK_PCT = 5.0

# Median number of limits per signal, from signal-history analysis. Only used for the
# "total_lot" mode: its recommended total is the per-limit fixed lot scaled up by this,
# since the bot splits a total lot evenly across a signal's limits at placement time.
_AVG_LIMITS = 3

# Median cumulative stop-loss distance per instrument, in the instrument's own price
# units (sum of |limit - SL| across a signal's limits). From signal-history analysis.
# Keyed by DB instrument (e.g. AAPL.NAS); mapped to the broker symbol at compute time.
# Stocks are included where the history has >= 3 signals for that ticker.
_INSTRUMENT_MEDIANS: dict[str, float] = {
    "XAUUSD": 25.00,
    "SPX500USD": 52.50,
    "NAS100USD": 826.00,
    "USOILSPOT": 3.47,
    "BTCUSDT": 4485.00,
    "ETHUSDT": 936.00,
    "JP225": 1565.00,
    "GCZ26_CFD": 59.00,
    "DE30EUR": 1058.50,
    "US30USD": 431.00,
    "UK100GBP": 618.72,
    "AAPL.NAS": 16.38,
    "NVDA.NAS": 24.60,
    "AMZN.NAS": 33.66,
    "AVGO.NAS": 25.25,
    "CTAS.NAS": 12.57,
    "TSLA.NAS": 50.94,
    "ANF.NYSE": 14.49,
    "AMD.NAS": 34.97,
    "CRWD.NAS": 107.52,
    "LRCX.NAS": 87.42,
    "MSFT.NAS": 34.26,
    "NKE.NYSE": 3.74,
    "ACMR.NAS": 17.58,
    "INTC.NAS": 14.85,
    "NFLX.NAS": 20.41,
    "PYPL.NAS": 7.10,
    "SNPS.NAS": 68.14,
    "XOM.NYSE": 14.59,
}

# Forex is aggregated across all pairs as a single median cumulative SL in pips; it's
# applied per common pair using that pair's own pip value, so each pair lands near the
# same money risk despite differing pip values (JPY/cross pairs included).
_FOREX_MEDIAN_PIPS = 90.20
_FOREX_PAIRS = [
    "EURUSD",
    "GBPUSD",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "NZDJPY",
    "EURGBP",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "EURNZD",
    "GBPAUD",
    "GBPCAD",
    "GBPCHF",
    "GBPNZD",
    "AUDCAD",
    "AUDCHF",
    "AUDNZD",
    "CADCHF",
    "NZDCAD",
    "NZDCHF",
]


@dataclass
class LotRecommendation:
    symbol: str  # broker (MT5) symbol
    signal_type: str
    mode: str
    value: float


def target_db_symbols() -> list[str]:
    """DB instruments the approximation needs broker specs for, so the engine can
    pre-cache their SymbolInfo off the request path."""
    return list(_INSTRUMENT_MEDIANS) + _FOREX_PAIRS


def compute_recommendations(
    config: Settings,
    balance: float,
    specs: dict[str, SymbolInfo],
    max_lot: float,
    mode: str = "fixed",
) -> list[LotRecommendation]:
    """Lot values that put a median-shaped signal at ~5% account risk, one per supported
    broker symbol. `specs` maps broker symbol -> SymbolInfo; symbols the broker doesn't
    carry are skipped. Lots are floored to the broker volume step and capped at max_lot.

    `mode="fixed"` returns the per-limit lot directly. `mode="total_lot"` returns the
    total lot (per-limit scaled by `_AVG_LIMITS`), so the same median signal lands at the
    same risk once the bot splits that total across its limits."""
    risk_money = balance * _TARGET_RISK_PCT / 100
    out: list[LotRecommendation] = []

    for db, median in _INSTRUMENT_MEDIANS.items():
        mt5_sym = map_symbol(db, config)
        info = specs.get(mt5_sym)
        if info is None:
            continue
        # Money risked per lot for this signal = money-per-price-unit * cumulative SL.
        money_per_unit = price_distance_to_money(info, 1.0, 1.0)
        if not money_per_unit or median <= 0:
            continue
        lot = _clamp(min(risk_money / (money_per_unit * median), max_lot), info)
        if lot > 0:
            out.append(LotRecommendation(mt5_sym, "all", "fixed", lot))

    for db in _FOREX_PAIRS:
        mt5_sym = map_symbol(db, config)
        info = specs.get(mt5_sym)
        if info is None:
            continue
        pip_val = _pip_value_per_lot(info)
        if pip_val <= 0:
            continue
        lot = _clamp(min(risk_money / (pip_val * _FOREX_MEDIAN_PIPS), max_lot), info)
        if lot > 0:
            out.append(LotRecommendation(mt5_sym, "all", "fixed", lot))

    if mode == "total_lot":
        # Per-limit lots are step-multiples, so scaling by an integer stays on-step;
        # round to shed binary float artifacts (0.3 * 3 -> 0.8999999999999999).
        return [
            LotRecommendation(r.symbol, r.signal_type, "total_lot", round(r.value * _AVG_LIMITS, 8))
            for r in out
        ]

    return out
