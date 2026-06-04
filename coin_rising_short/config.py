import os
from decimal import Decimal, getcontext
from dotenv import load_dotenv

getcontext().prec = 16

_PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PACKAGE_ROOT)

load_dotenv(dotenv_path=os.path.join(_PROJECT_ROOT, ".env"))

API_KEY = os.getenv("BYBIT_API_KEY") or os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_SECRET") or os.getenv("BYBIT_SECRET")

ENV = (os.getenv("BYBIT_ENV") or os.getenv("BINANCE_ENV") or "mainnet").lower()
BASE_URL = "https://api.bybit.com" if ENV == "mainnet" else "https://api-testnet.bybit.com"

# 하위 호환 (일부 모듈이 참조할 수 있음)
BASE_URL_FUTURES = BASE_URL
BASE_URL_SPOT = BASE_URL

CATEGORY_LINEAR = "linear"
CATEGORY_SPOT = "spot"
SETTLE_COIN = "USDT"

if not API_KEY or not API_SECRET:
    raise Exception("❌ .env에서 Bybit API 키를 불러오지 못했습니다! (BYBIT_API_KEY_SH / BYBIT_SECRET_SH)")

POSITION_USDT = Decimal("50")
PREMIUM_PCT = Decimal("0.01")
DISCOUNT_PCT = Decimal("0.01")
GAINER_THRESHOLD_PCT = Decimal("20")
MIN_VOLUME_USDT = Decimal("1_000_000")
REENTRY_RISE_PCT = Decimal("50")
REENTRY_MAX_COUNT = 4
TAKE_PROFIT_PCT = Decimal("10")
POLL_INTERVAL_SEC = 10

LEVERAGE = int(os.getenv("LEVERAGE") or "5")
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES") or "5")
RECV_WINDOW_MS = int(os.getenv("BYBIT_RECV_WINDOW") or "8000")

POSITION_STATE_PATH = os.getenv("POSITION_STATE_FILE") or os.path.join(
    _PROJECT_ROOT, "position_state.json"
)
TRADE_JOURNAL_PATH = os.getenv("TRADE_JOURNAL_FILE") or os.path.join(
    _PROJECT_ROOT, "logs", "trade_journal.csv"
)
FORCE_HEDGE = (os.getenv("FORCE_HEDGE") or "true").lower() == "true"

FILTER_UPBIT_LISTED = (os.getenv("FILTER_UPBIT_LISTED") or "false").lower() == "true"
FILTER_FUTURES_LISTING_AGE = (os.getenv("FILTER_FUTURES_LISTING_AGE") or "false").lower() == "true"
MIN_FUTURES_LISTING_AGE_DAYS = int(os.getenv("MIN_FUTURES_LISTING_AGE_DAYS") or "365")

MIN_FUNDING_RATE = Decimal(os.getenv("MIN_FUNDING_RATE") or "-0.005")
USE_VOLUME_FILTER = (os.getenv("USE_VOLUME_FILTER") or "false").lower() == "true"

USE_ENTRY_INDICATOR_FILTER = (os.getenv("USE_ENTRY_INDICATOR_FILTER") or "false").lower() == "true"
USE_REENTRY_INDICATOR_FILTER = (os.getenv("USE_REENTRY_INDICATOR_FILTER") or "false").lower() == "true"
INDICATOR_INTERVAL = os.getenv("INDICATOR_INTERVAL") or "5m"
INDICATOR_KLINE_LIMIT = int(os.getenv("INDICATOR_KLINE_LIMIT") or "60")
INDICATOR_CACHE_TTL_SEC = int(os.getenv("INDICATOR_CACHE_TTL_SEC") or "60")

ENTRY_RSI_THRESHOLD = Decimal(os.getenv("ENTRY_RSI_THRESHOLD") or "78")
ENTRY_MA20_GAP_PCT = Decimal(os.getenv("ENTRY_MA20_GAP_PCT") or "1.0")

REENTRY_RSI_THRESHOLD = Decimal(os.getenv("REENTRY_RSI_THRESHOLD") or "80")
REENTRY_MA20_GAP_PCT = Decimal(os.getenv("REENTRY_MA20_GAP_PCT") or "1.0")
REENTRY_RECENT_OVER_BARS = int(os.getenv("REENTRY_RECENT_OVER_BARS") or "5")

# SuperTrend (TradingView 스크립트: 4h / ATR 4 / Factor 7 / hlc3)
USE_SUPERTREND_ENTRY = (os.getenv("USE_SUPERTREND_ENTRY") or "true").lower() == "true"
SUPERTREND_INTERVAL = os.getenv("SUPERTREND_INTERVAL") or "4h"
SUPERTREND_ATR_PERIOD = int(os.getenv("SUPERTREND_ATR_PERIOD") or "4")
SUPERTREND_FACTOR = Decimal(os.getenv("SUPERTREND_FACTOR") or "7")
SUPERTREND_SOURCE = (os.getenv("SUPERTREND_SOURCE") or "hl2").lower()
SUPERTREND_KLINE_LIMIT = int(os.getenv("SUPERTREND_KLINE_LIMIT") or "100")
SUPERTREND_WATCH_STATE_PATH = os.getenv("SUPERTREND_WATCH_STATE_FILE") or os.path.join(
    _PROJECT_ROOT, "supertrend_watch.json"
)
ST_MAX_CONSECUTIVE_LOSSES = int(os.getenv("ST_MAX_CONSECUTIVE_LOSSES") or "2")
MAX_CONCURRENT_ST_SYMBOLS = int(os.getenv("MAX_CONCURRENT_ST_SYMBOLS") or "20")
MAX_ST_TRACKED_SYMBOLS = int(os.getenv("MAX_ST_TRACKED_SYMBOLS") or "60")
USE_MARKET_ENTRY = (os.getenv("USE_MARKET_ENTRY") or "false").lower() == "true"
OPEN_ORDER_MAX_AGE_DAYS = int(os.getenv("OPEN_ORDER_MAX_AGE_DAYS") or "2")

CMC_API_KEY = (os.getenv("CMC_API_KEY") or "").strip()
MCAP_FILTER_ENABLED = (os.getenv("MCAP_FILTER_ENABLED") or "false").lower() == "true"
MIN_MARKET_CAP_USD = Decimal(os.getenv("MIN_MARKET_CAP_USD") or "100000000")
MCAP_CACHE_TTL_SEC = int(os.getenv("MCAP_CACHE_TTL_SEC") or "900")

FILTER_MCAP_FDV = (os.getenv("FILTER_MCAP_FDV") or "false").lower() == "true"
MIN_MCAP_FDV_RATIO = Decimal(os.getenv("MIN_MCAP_FDV_RATIO") or "0.4")
COINGECKO_API_BASE = os.getenv("COINGECKO_API_BASE") or "https://api.coingecko.com/api/v3"
