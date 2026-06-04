import logging
import time
from typing import Dict

from coin_rising_short import client, config

logger = logging.getLogger(__name__)


def _linear_to_binance_shape(row: dict) -> dict:
    """filters.parse_filters 호환용 Binance exchangeInfo 심볼 형태."""
    symbol = row["symbol"]
    base = row.get("baseCoin") or symbol.replace("USDT", "")
    price_filter = row.get("priceFilter") or {}
    lot_filter = row.get("lotSizeFilter") or {}
    launch_ms = int(row.get("launchTime") or 0)
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": row.get("quoteCoin", "USDT"),
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "closeOnly": False,
        "orderTypes": ["LIMIT"],
        "onboardDate": launch_ms,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "tickSize": str(price_filter.get("tickSize", "0.01")),
            },
            {
                "filterType": "LOT_SIZE",
                "stepSize": str(lot_filter.get("qtyStep", "0.001")),
                "minQty": str(lot_filter.get("minOrderQty", "0.001")),
            },
            {
                "filterType": "MIN_NOTIONAL",
                "notional": str(lot_filter.get("minNotionalValue", "0")),
            },
        ],
    }


def get_trading_symbols() -> Dict[str, dict]:
    """Bybit USDT 무기한 선물(Trading) 전체. 스팟/업비트/상장일 필터 없음."""
    logger.info("심볼 정보 로딩 중...")

    fut_rows = client.fetch_instruments_paginated(config.CATEGORY_LINEAR)
    futures_symbols: Dict[str, dict] = {}
    for row in fut_rows:
        if row.get("status") != "Trading":
            continue
        if row.get("quoteCoin") != "USDT":
            continue
        shaped = _linear_to_binance_shape(row)
        futures_symbols[shaped["symbol"]] = shaped

    logger.info("거래 가능 USDT 선물 심볼: %s개", len(futures_symbols))
    return futures_symbols


TRADING_SYMBOLS: Dict[str, dict] = {}


def init_trading_symbols(max_retries: int = 3) -> None:
    global TRADING_SYMBOLS
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            TRADING_SYMBOLS = get_trading_symbols()
            if not TRADING_SYMBOLS:
                raise RuntimeError("로딩된 거래 심볼이 없습니다.")
            return
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                wait_sec = min(2**attempt, 8)
                logger.warning(
                    "심볼 로딩 실패 (%s/%s): %s. %ss 후 재시도",
                    attempt,
                    max_retries,
                    exc,
                    wait_sec,
                )
                time.sleep(wait_sec)
            else:
                logger.exception("심볼 로딩 최종 실패")
    raise RuntimeError(f"심볼 초기화 실패: {last_error}")
