from enum import Enum

_PRODUCTION_DSN: str = ""
_PRODUCTION_LICENSE_URL: str = ""
_PRODUCTION_UPDATE_MANIFEST_URL: str = ""

MAGIC_NUMBER: int = 20250001
BOT_VERSION: str = "1.6.3"


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
