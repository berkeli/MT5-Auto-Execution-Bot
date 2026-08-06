import logging
import math

from bot.config.settings import LotExceptionConfig, Settings
from bot.mt5.client import MT5Client
from bot.mt5.types import SymbolInfo
from bot.trading.symbol_mapper import pip_size

logger = logging.getLogger(__name__)


def _pip_value_per_lot(info: SymbolInfo) -> float:
    pip_sz = pip_size(info)
    return info.trade_tick_value * (pip_sz / info.trade_tick_size)


def price_distance_to_money(info: SymbolInfo, distance: float, volume: float) -> float | None:
    """Convert a price distance into account-currency money for `volume` lots.
    Returns None when the symbol's tick metadata is unusable."""
    if info.trade_tick_size <= 0:
        return None
    return abs(distance) * (info.trade_tick_value / info.trade_tick_size) * volume


def _clamp(lot: float, info: SymbolInfo) -> float:
    step = info.volume_step
    # Floor to nearest step (conservative — never over-leverage)
    lot = math.floor(lot / step) * step
    lot = round(lot, 8)
    return min(max(lot, info.volume_min), info.volume_max)


def resolve_lot_exception(
    config: Settings, mt5_symbol: str, signal_type: str, channel_id: int | None
) -> LotExceptionConfig | None:
    # Most-specific match wins, weighting channel > symbol > signal_type. A rule
    # is a candidate only if every dimension it specifies matches the trade; ties
    # (true duplicates) resolve to the last-defined entry.
    chan = None if channel_id is None else str(channel_id)
    best: LotExceptionConfig | None = None
    best_score = -1
    for ex in config.lot_sizing.exceptions:
        sym_w = not ex.symbol or ex.symbol.lower() == "all"
        chan_w = not ex.channel or ex.channel.lower() == "all"
        type_w = ex.signal_type == "all"
        if not sym_w and ex.symbol != mt5_symbol:
            continue
        if not chan_w and ex.channel != chan:
            continue
        if not type_w and ex.signal_type != signal_type:
            continue
        score = (0 if chan_w else 4) + (0 if sym_w else 2) + (0 if type_w else 1)
        if score >= best_score:
            best, best_score = ex, score
    return best


def _per_symbol(value, mt5_symbol: str, fallback: float) -> float:
    if isinstance(value, dict):
        return value.get(mt5_symbol) or value.get("default") or fallback
    return float(value)


def resolve_lot_mode(
    config: Settings, mt5_symbol: str, signal_type: str, channel_id: int | None
) -> dict:
    """Resolved lot mode/value for this trade, for the tp_outcomes config snapshot.
    Mirrors LotCalculator precedence (exception over global mode) without MT5 access."""
    exception = resolve_lot_exception(config, mt5_symbol, signal_type, channel_id)
    if exception is not None:
        return {"mode": exception.mode, "value": exception.value, "source": "exception"}
    mode = config.lot_sizing.mode
    if mode == "fixed":
        value = _per_symbol(config.lot_sizing.fixed_lot, mt5_symbol, 0.01)
    elif mode == "total_lot":
        value = _per_symbol(config.lot_sizing.total_lot, mt5_symbol, 0.1)
    else:
        value = _per_symbol(config.lot_sizing.risk_percent, mt5_symbol, 1.0)
    return {"mode": mode, "value": value, "source": "global"}


class LotCalculator:
    def __init__(self, mt5_client: MT5Client, config: Settings) -> None:
        self._client = mt5_client
        self._config = config

    def _get_fixed_lot(self, mt5_symbol: str) -> float:
        fl = self._config.lot_sizing.fixed_lot
        if isinstance(fl, dict):
            return fl.get(mt5_symbol) or fl.get("default") or 0.01
        return float(fl)

    def _get_total_lot(self, mt5_symbol: str) -> float:
        tl = self._config.lot_sizing.total_lot
        if isinstance(tl, dict):
            return tl.get(mt5_symbol) or tl.get("default") or 0.1
        return float(tl)

    def _get_risk_percent(self, mt5_symbol: str) -> float:
        rp = self._config.lot_sizing.risk_percent
        if isinstance(rp, dict):
            return rp.get(mt5_symbol) or rp.get("default") or 1.0
        return float(rp)

    def _resolve_exception(
        self, mt5_symbol: str, signal_type: str, channel_id: int | None
    ) -> LotExceptionConfig | None:
        return resolve_lot_exception(self._config, mt5_symbol, signal_type, channel_id)

    def calculate(
        self,
        stop_loss: float,
        limit_prices: list[float],
        mt5_symbol: str,
        signal_type: str = "all",
        channel_id: int | None = None,
        ladder_size: int | None = None,
    ) -> float:
        """`ladder_size` is the signal's declared limit count (Supabase
        signals.total_limits). The ladder-splitting modes divide by it rather than
        by len(limit_prices): the TM inserts a signal's limits in several batches
        (outer levels first, infills seconds later), so the rows visible at
        placement time are a moving subset of the ladder. Dividing by that subset
        sizes the early limits against a shorter ladder than the signal ends up
        with, and the signal overshoots its budget. Falls back to the visible
        count, and never trusts a declared size below it (would over-size)."""
        info = self._client.symbol_info(mt5_symbol)
        if info is None:
            logger.error("symbol_info unavailable for %s, using fixed_lot fallback", mt5_symbol)
            return self._get_fixed_lot(mt5_symbol)

        n_limits = max(ladder_size or 0, len(limit_prices))

        # A matching exception takes precedence over the global mode.
        exception = self._resolve_exception(mt5_symbol, signal_type, channel_id)
        if exception is not None:
            if exception.mode == "fixed":
                return _clamp(exception.value, info)
            if exception.mode == "total_lot":
                return _clamp(exception.value / n_limits, info)
            return self._calc_risk_lot(
                exception.value, info, stop_loss, limit_prices, mt5_symbol, n_limits
            )

        mode = self._config.lot_sizing.mode
        if mode == "fixed":
            return _clamp(self._get_fixed_lot(mt5_symbol), info)
        if mode == "total_lot":
            return _clamp(self._get_total_lot(mt5_symbol) / n_limits, info)

        return self._calc_risk_lot(
            self._get_risk_percent(mt5_symbol), info, stop_loss, limit_prices, mt5_symbol, n_limits
        )

    def _calc_risk_lot(
        self,
        risk_percent: float,
        info: SymbolInfo,
        stop_loss: float,
        limit_prices: list[float],
        mt5_symbol: str,
        n_limits: int,
    ) -> float:
        account = self._client.account_info()
        if account is None:
            logger.error("account_info unavailable, using volume_min for %s", mt5_symbol)
            return _clamp(info.volume_min, info)

        pip_sz = pip_size(info)
        pip_val = _pip_value_per_lot(info)

        if pip_sz <= 0 or pip_val <= 0:
            logger.error("Invalid pip metrics for %s (size=%s val=%s)", mt5_symbol, pip_sz, pip_val)
            return _clamp(info.volume_min, info)

        sl_pips = [abs(price - stop_loss) / pip_sz for price in limit_prices]
        avg_sl_pips = sum(sl_pips) / len(sl_pips)

        if avg_sl_pips <= 0:
            logger.warning("Zero SL distance for %s, using volume_min", mt5_symbol)
            return _clamp(info.volume_min, info)

        raw = (account.balance * risk_percent / 100) / (n_limits * avg_sl_pips * pip_val)
        capped = min(raw, self._config.lot_sizing.max_lot_per_order)
        return _clamp(capped, info)
