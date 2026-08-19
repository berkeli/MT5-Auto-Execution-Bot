from enum import Enum

_PRODUCTION_DSN: str = ""
_PRODUCTION_LICENSE_URL: str = ""
_PRODUCTION_UPDATE_MANIFEST_URL: str = ""

MAGIC_NUMBER: int = 20250001
BOT_VERSION: str = "1.6.8"

# order_mappings.order_type values written by an instant-entry market fill. The
# sender's own take profit rides on the broker as a hard TP, so the local TP engine
# leaves these positions alone — this set is how it recognises them.
MARKET_ORDER_TYPES: frozenset[str] = frozenset({"buy_market", "sell_market"})

# Entries an instant-entry signal is sized against. The TM opens one at the market and
# the sender may average a second in later ("add"), at a price and a time nobody can
# know in advance — so risk is budgeted for both up front, treating the second as if it
# came at the first's price. Sizing against the one visible entry instead would double
# the signal's risk the moment the second arrived, since the added entry reuses its
# filled sibling's lot. A signal that never gets its second entry simply runs at half
# budget, which is the safe side of the guess.
INSTANT_ENTRY_LADDER_SIZE: int = 2


class AssetClass(str, Enum):
    FOREX = "forex"
    FOREX_JPY = "forex_jpy"
    METALS = "metals"
    INDICES = "indices"
    STOCKS = "stocks"
    CRYPTO = "crypto"
    OIL = "oil"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    SPREAD_CANCELLED = "spread_cancelled"
    CLOSED = "closed"
    ERROR = "error"


DEFAULT_TP_CONFIG: dict = {
    "forex": {"profit_threshold": 7, "threshold_unit": "pips", "trailing_distance": 3},
    "forex_jpy": {"profit_threshold": 7, "threshold_unit": "pips", "trailing_distance": 3},
    "metals": {"profit_threshold": 4.0, "threshold_unit": "dollars", "trailing_distance": 2.0},
    "indices": {"profit_threshold": 20.0, "threshold_unit": "dollars", "trailing_distance": 5.0},
    "stocks": {"profit_threshold": 1.0, "threshold_unit": "dollars", "trailing_distance": 0.5},
    "crypto": {"profit_threshold": 300.0, "threshold_unit": "dollars", "trailing_distance": 50.0},
    "oil": {"profit_threshold": 0.5, "threshold_unit": "dollars", "trailing_distance": 0.2},
    "scalp_overrides": {
        "forex": {"profit_threshold": 5, "trailing_distance": 2},
        "forex_jpy": {"profit_threshold": 5, "trailing_distance": 3},
        "metals": {"profit_threshold": 2.0, "trailing_distance": 1.0},
        "indices": {"profit_threshold": 10.0, "trailing_distance": 3.0},
        "stocks": {"profit_threshold": 0.5, "trailing_distance": 0.25},
        "crypto": {"profit_threshold": 150.0, "trailing_distance": 25.0},
        "oil": {"profit_threshold": 0.25, "trailing_distance": 0.1},
    },
}
