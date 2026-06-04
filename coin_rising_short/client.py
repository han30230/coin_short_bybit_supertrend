import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from coin_rising_short import config

_time_offset_ms = 0
_position_mode_hedge: Optional[bool] = None
logger = logging.getLogger(__name__)

_ORDER_STATUS_MAP = {
    "New": "NEW",
    "PartiallyFilled": "PARTIALLY_FILLED",
    "Filled": "FILLED",
    "Cancelled": "CANCELED",
    "Rejected": "REJECTED",
    "Deactivated": "EXPIRED",
    "Triggered": "NEW",
    "Untriggered": "NEW",
}

_KLINE_INTERVAL_MAP = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1M": "M",
}


def refresh_time_offset() -> None:
    global _time_offset_ms
    r = _http_get(f"{config.BASE_URL}/v5/market/time", timeout=5)
    r.raise_for_status()
    body = r.json()
    if body.get("retCode") != 0:
        raise RuntimeError(f"서버 시간 조회 실패: {body}")
    server = int(body["result"]["timeSecond"]) * 1000
    local = int(time.time() * 1000)
    _time_offset_ms = server - local


def effective_timestamp_ms() -> int:
    return int(time.time() * 1000) + _time_offset_ms


def kline_interval(interval: Optional[str] = None) -> str:
    iv = (interval or config.INDICATOR_INTERVAL).strip().lower()
    return _KLINE_INTERVAL_MAP.get(iv, iv)


def normalize_order_status(bybit_status: str) -> str:
    return _ORDER_STATUS_MAP.get(bybit_status, bybit_status)


def normalize_order(raw: dict) -> dict:
    """Binance 호환 형태로 주문 필드 정규화."""
    oid = raw.get("orderId")
    return {
        "symbol": raw.get("symbol"),
        "orderId": int(oid) if oid is not None else 0,
        "status": normalize_order_status(str(raw.get("orderStatus", ""))),
        "avgPrice": str(raw.get("avgPrice") or "0"),
        "executedQty": str(raw.get("cumExecQty") or "0"),
        "updateTime": int(raw.get("updatedTime") or raw.get("createdTime") or 0),
    }


def position_idx_for_side(position_side: Optional[str]) -> int:
    if not position_side:
        return 0
    side = position_side.upper()
    if side == "LONG":
        return 1
    if side == "SHORT":
        return 2
    return 0


def _http_get(url: str, **kwargs) -> requests.Response:
    last: Optional[requests.Response] = None
    for attempt in range(config.HTTP_MAX_RETRIES):
        last = requests.get(url, **kwargs)
        if last.status_code != 429:
            return last
        wait = int(last.headers.get("Retry-After", 1 + attempt))
        logger.warning(
            "Rate limit 429, %ss 후 재시도 (GET %s/%s)",
            wait,
            attempt + 1,
            config.HTTP_MAX_RETRIES,
            extra={"event": "http_rate_limit_get", "wait_sec": wait},
        )
        time.sleep(wait)
    return last  # type: ignore


def _http_post(url: str, **kwargs) -> requests.Response:
    last: Optional[requests.Response] = None
    for attempt in range(config.HTTP_MAX_RETRIES):
        last = requests.post(url, **kwargs)
        if last.status_code != 429:
            return last
        wait = int(last.headers.get("Retry-After", 1 + attempt))
        logger.warning(
            "Rate limit 429, %ss 후 재시도 (POST %s/%s)",
            wait,
            attempt + 1,
            config.HTTP_MAX_RETRIES,
            extra={"event": "http_rate_limit_post", "wait_sec": wait},
        )
        time.sleep(wait)
    return last  # type: ignore


def _auth_headers(signature: str, timestamp: str) -> dict:
    return {
        "X-BAPI-API-KEY": config.API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": str(config.RECV_WINDOW_MS),
        "Content-Type": "application/json",
    }


def _sign_payload(payload: str) -> str:
    ts = str(effective_timestamp_ms())
    raw = ts + config.API_KEY + str(config.RECV_WINDOW_MS) + payload
    return hmac.new(
        config.API_SECRET.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()


def _unwrap_body(body: dict, context: str) -> Any:
    ret_code = body.get("retCode")
    if ret_code != 0:
        ret_msg = body.get("retMsg", "")
        raise RuntimeError(f"{context} Bybit 오류 retCode={ret_code} retMsg={ret_msg}")
    return body.get("result")


def parse_json_response(response: requests.Response, context: str) -> Any:
    if response.status_code >= 400:
        raise RuntimeError(f"{context} HTTP 오류: {response.status_code} / {response.text}")
    if not (response.text or "").strip():
        raise RuntimeError(f"{context} 빈 응답 (HTTP {response.status_code})")
    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError(f"{context} JSON 파싱 실패: {exc}") from exc
    if isinstance(body, dict) and "retCode" in body:
        return _unwrap_body(body, context)
    return body


def public_get(path: str, params: Optional[dict] = None) -> Any:
    qs = urlencode(params or {}, doseq=True)
    url = f"{config.BASE_URL}{path}"
    if qs:
        url = f"{url}?{qs}"
    resp = _http_get(url, timeout=15)
    return parse_json_response(resp, path)


def _signed_request(method: str, path: str, params: Optional[dict] = None) -> Any:
    last_body: Optional[dict] = None
    for attempt in range(config.HTTP_MAX_RETRIES):
        ts = str(effective_timestamp_ms())
        if method.upper() == "GET":
            qs = urlencode(params or {}, doseq=True)
            sign = hmac.new(
                config.API_SECRET.encode(),
                (ts + config.API_KEY + str(config.RECV_WINDOW_MS) + qs).encode(),
                hashlib.sha256,
            ).hexdigest()
            url = f"{config.BASE_URL}{path}"
            if qs:
                url = f"{url}?{qs}"
            resp = _http_get(url, headers=_auth_headers(sign, ts), timeout=15)
        else:
            body_str = json.dumps(params or {}, separators=(",", ":"))
            sign = hmac.new(
                config.API_SECRET.encode(),
                (ts + config.API_KEY + str(config.RECV_WINDOW_MS) + body_str).encode(),
                hashlib.sha256,
            ).hexdigest()
            resp = _http_post(
                f"{config.BASE_URL}{path}",
                headers=_auth_headers(sign, ts),
                data=body_str,
                timeout=15,
            )

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 1 + attempt))
            time.sleep(wait)
            continue

        if not (resp.text or "").strip():
            raise RuntimeError(f"{path} 빈 응답 (HTTP {resp.status_code})")
        try:
            last_body = resp.json()
        except Exception:
            raise RuntimeError(f"{path} JSON 파싱 실패: {resp.text}")

        if not isinstance(last_body, dict):
            raise RuntimeError(f"{path} 응답 형식 오류")

        ret_code = last_body.get("retCode")
        if ret_code == 0:
            return last_body.get("result")

        ret_msg = str(last_body.get("retMsg", ""))
        if ret_code in (10002, 10006) or "timestamp" in ret_msg.lower():
            logger.warning(
                "타임스탬프 오차, 서버 시간 재동기화",
                extra={"event": "timestamp_resync"},
            )
            refresh_time_offset()
            time.sleep(0.25)
            continue

        return {"_error": True, "retCode": ret_code, "retMsg": ret_msg, "raw": last_body}

    if last_body and isinstance(last_body, dict):
        return {
            "_error": True,
            "retCode": last_body.get("retCode"),
            "retMsg": last_body.get("retMsg"),
            "raw": last_body,
        }
    raise RuntimeError(f"{path} 요청 실패")


def signed_get(path: str, params: Optional[dict] = None) -> Any:
    return _signed_request("GET", path, params)


def signed_post(path: str, body: Optional[dict] = None) -> Any:
    return _signed_request("POST", path, body)


def fetch_instruments_paginated(category: str) -> List[dict]:
    out: List[dict] = []
    cursor: Optional[str] = None
    while True:
        params: dict = {"category": category, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = public_get("/v5/market/instruments-info", params)
        if not isinstance(result, dict):
            break
        rows = result.get("list") or []
        out.extend(rows)
        cursor = result.get("nextPageCursor")
        if not cursor:
            break
    return out


def get_linear_tickers() -> List[dict]:
    result = public_get(
        "/v5/market/tickers", {"category": config.CATEGORY_LINEAR}
    )
    if isinstance(result, dict):
        return result.get("list") or []
    return []


def get_ticker_price(symbol: str) -> Decimal:
    result = public_get(
        "/v5/market/tickers",
        {"category": config.CATEGORY_LINEAR, "symbol": symbol},
    )
    rows = result.get("list") if isinstance(result, dict) else None
    if not rows:
        raise RuntimeError(f"{symbol} 티커 없음")
    return Decimal(str(rows[0]["lastPrice"]))


def get_klines_binance_shape(
    symbol: str,
    interval: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[list]:
    """Binance klines 배열 형태 [openTime, o, h, l, close, ...] 로 변환."""
    req_limit = limit if limit is not None else config.INDICATOR_KLINE_LIMIT

    result = public_get(
        "/v5/market/kline",
        {
            "category": config.CATEGORY_LINEAR,
            "symbol": symbol,
            "interval": kline_interval(interval),
            "limit": req_limit,
        },
    )
    rows = result.get("list") if isinstance(result, dict) else None
    if not rows:
        return []
    # Bybit kline: [start, open, high, low, close, volume, turnover] newest first
    out: List[list] = []
    for row in reversed(rows):
        out.append(
            [
                int(row[0]),
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6] if len(row) > 6 else "0",
            ]
        )
    return out


def get_dual_side_position() -> bool:
    """Bybit v5는 포지션 모드 조회 GET이 없어, switch 성공 시 캐시한 값을 사용."""
    global _position_mode_hedge
    if _position_mode_hedge is not None:
        return _position_mode_hedge
    return config.FORCE_HEDGE


def set_dual_side_position(enable: bool) -> bool:
    global _position_mode_hedge
    mode = 3 if enable else 0
    result = signed_post(
        "/v5/position/switch-mode",
        {
            "category": config.CATEGORY_LINEAR,
            "coin": config.SETTLE_COIN,
            "mode": mode,
        },
    )
    if isinstance(result, dict) and result.get("_error"):
        ret_code = result.get("retCode")
        if ret_code in (110025, 110024):
            _position_mode_hedge = enable
            return True
        logger.info(
            "set_dual_side_position(%s) -> retCode=%s %s",
            enable,
            ret_code,
            result.get("retMsg"),
        )
        return False
    _position_mode_hedge = enable
    logger.info("set_dual_side_position(%s) -> OK", enable)
    return True


def set_leverage(symbol: str, leverage: int) -> bool:
    lev = str(leverage)
    result = signed_post(
        "/v5/position/set-leverage",
        {
            "category": config.CATEGORY_LINEAR,
            "symbol": symbol,
            "buyLeverage": lev,
            "sellLeverage": lev,
        },
    )
    if isinstance(result, dict) and result.get("_error"):
        ret_code = result.get("retCode")
        if ret_code == 110043:
            return True
        return False
    return True


def get_open_orders() -> List[dict]:
    result = signed_get(
        "/v5/order/realtime",
        {"category": config.CATEGORY_LINEAR, "settleCoin": config.SETTLE_COIN},
    )
    if isinstance(result, dict):
        return [normalize_order(o) for o in (result.get("list") or [])]
    return []


def get_order_detail(symbol: str, order_id: int) -> Optional[dict]:
    for open_only in (True, False):
        params: dict = {
            "category": config.CATEGORY_LINEAR,
            "symbol": symbol,
            "orderId": str(order_id),
        }
        path = "/v5/order/realtime" if open_only else "/v5/order/history"
        result = signed_get(path, params)
        if isinstance(result, dict) and result.get("_error"):
            ret_code = result.get("retCode")
            if ret_code == 110001:
                continue
            return None
        rows = result.get("list") if isinstance(result, dict) else None
        if rows:
            return normalize_order(rows[0])
    return {"status": "NOT_FOUND"}


def cancel_order(symbol: str, order_id: int) -> bool:
    result = signed_post(
        "/v5/order/cancel",
        {
            "category": config.CATEGORY_LINEAR,
            "symbol": symbol,
            "orderId": str(order_id),
        },
    )
    if isinstance(result, dict) and result.get("_error"):
        return False
    return True


def place_market_order_raw(
    symbol: str,
    side: str,
    qty: str,
    position_side: Optional[str],
    reduce_only: bool = False,
) -> Tuple[Optional[int], Optional[dict]]:
    body: dict = {
        "category": config.CATEGORY_LINEAR,
        "symbol": symbol,
        "side": side.capitalize(),
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC",
        "positionIdx": position_idx_for_side(position_side),
    }
    if reduce_only:
        body["reduceOnly"] = True

    result = signed_post("/v5/order/create", body)
    if isinstance(result, dict) and result.get("_error"):
        return None, {
            "code": result.get("retCode"),
            "msg": result.get("retMsg"),
        }
    if isinstance(result, dict) and result.get("orderId"):
        return int(result["orderId"]), None
    return None, {"msg": str(result)}


def place_limit_order_raw(
    symbol: str,
    side: str,
    price: str,
    qty: str,
    position_side: Optional[str],
    reduce_only: bool = False,
) -> Tuple[Optional[int], Optional[dict]]:
    body: dict = {
        "category": config.CATEGORY_LINEAR,
        "symbol": symbol,
        "side": side.capitalize(),
        "orderType": "Limit",
        "qty": qty,
        "price": price,
        "timeInForce": "GTC",
        "positionIdx": position_idx_for_side(position_side),
    }
    if reduce_only:
        body["reduceOnly"] = True

    result = signed_post("/v5/order/create", body)
    if isinstance(result, dict) and result.get("_error"):
        return None, {
            "code": result.get("retCode"),
            "msg": result.get("retMsg"),
        }
    if isinstance(result, dict) and result.get("orderId"):
        return int(result["orderId"]), None
    return None, {"msg": str(result)}


def get_position_risk() -> List[dict]:
    result = signed_get(
        "/v5/position/list",
        {"category": config.CATEGORY_LINEAR, "settleCoin": config.SETTLE_COIN},
    )
    if isinstance(result, dict):
        return result.get("list") or []
    return []


# 하위 호환: 기존 signed_request / sign_hmac_sha256 호출 제거용 스텁
def signed_request(method: str, path: str, params: Optional[dict] = None) -> requests.Response:
    raise NotImplementedError("Binance signed_request는 Bybit 포팅 후 사용되지 않습니다. client.signed_get/post를 사용하세요.")
