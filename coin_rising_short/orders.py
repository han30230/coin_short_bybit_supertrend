import logging
import time
from decimal import Decimal
from typing import Optional, Tuple

from coin_rising_short import client, config, filters, runtime

_leverage_ready: set = set()
logger = logging.getLogger(__name__)

# Bybit: 주문 수량/리스크 한도 초과 등
_RISK_LIMIT_CODES = {110017, 110090, 110007}


def ensure_leverage(symbol: str) -> bool:
    global _leverage_ready
    if symbol in _leverage_ready:
        return True
    if client.set_leverage(symbol, config.LEVERAGE):
        _leverage_ready.add(symbol)
        logger.info(
            "%s 레버리지 %sx 설정",
            symbol,
            config.LEVERAGE,
            extra={"event": "leverage_set", "symbol": symbol},
        )
        return True
    logger.warning(
        "레버리지 설정 실패 %s",
        symbol,
        extra={"event": "leverage_set_failed", "symbol": symbol},
    )
    return False


def get_dual_side_position() -> bool:
    return client.get_dual_side_position()


def set_dual_side_position(enable: bool) -> bool:
    return client.set_dual_side_position(enable)


def get_order_status(symbol: str, order_id: int) -> Optional[str]:
    try:
        detail = client.get_order_detail(symbol, order_id)
        if not detail:
            return None
        if detail.get("status") == "NOT_FOUND":
            return "NOT_FOUND"
        return detail.get("status")
    except Exception as e:
        logger.exception("주문 조회 예외 발생: %s", e, extra={"symbol": symbol, "order_id": order_id})
        return None


def get_order_detail(symbol: str, order_id: int) -> Optional[dict]:
    try:
        return client.get_order_detail(symbol, order_id)
    except Exception as e:
        logger.exception("주문 상세 조회 예외: %s", e, extra={"symbol": symbol, "order_id": order_id})
        return None


def cancel_order(symbol: str, order_id: int) -> bool:
    try:
        if client.cancel_order(symbol, order_id):
            logger.info(
                "주문 취소 성공: %s / %s",
                symbol,
                order_id,
                extra={"event": "order_canceled", "symbol": symbol, "order_id": order_id},
            )
            return True
        logger.warning(
            "주문 취소 실패: %s / %s",
            symbol,
            order_id,
            extra={"event": "order_cancel_failed", "symbol": symbol, "order_id": order_id},
        )
        return False
    except Exception as e:
        logger.exception("주문 취소 예외: %s", e, extra={"symbol": symbol, "order_id": order_id})
        return False


def place_limit_order(
    symbol: str, side: str, price: Decimal, qty: Decimal, position_side: Optional[str]
) -> Tuple[Optional[int], Optional[dict]]:
    try:
        order_id, err = client.place_limit_order_raw(
            symbol=symbol,
            side=side,
            price=str(price),
            qty=str(qty),
            position_side=position_side,
            reduce_only=False,
        )
        if order_id is not None:
            logger.info(
                "%s 주문 성공: %s @ %s (positionSide=%s, orderId=%s)",
                side,
                symbol,
                price,
                position_side,
                order_id,
                extra={
                    "event": "order_placed",
                    "symbol": symbol,
                    "order_id": order_id,
                    "side": side,
                    "price": str(price),
                    "qty": str(qty),
                    "position_side": position_side,
                },
            )
            return order_id, None

        code = err.get("code") if err else None
        if code in (110100, 110056, 110074):
            runtime.SKIP_UNTIL[symbol] = int(time.time()) + 60 * 30
            logger.warning(
                "오픈 불가 심볼로 판단, 30분 스킵: %s",
                symbol,
                extra={"event": "symbol_skip_set", "symbol": symbol, "wait_sec": 1800},
            )
        logger.warning(
            "주문 실패: %s / %s",
            symbol,
            err,
            extra={"event": "order_place_failed", "symbol": symbol, "side": side},
        )
        return None, err
    except Exception as e:
        logger.exception("주문 예외 발생: %s", e)
        return None, None


def place_take_profit_order(
    symbol: str, direction: str, entry_price: Decimal, qty: Decimal
) -> Optional[int]:
    try:
        price_step, qty_step, min_qty, min_notional = filters.get_price_step_and_qty_step(symbol)

        if direction.upper() == "SHORT":
            raw_tp_price = entry_price * (Decimal("1") - config.TAKE_PROFIT_PCT / Decimal("100"))
            side = "BUY"
            pos_side = "SHORT" if runtime.IS_HEDGE else None
        else:
            raw_tp_price = entry_price * (Decimal("1") + config.TAKE_PROFIT_PCT / Decimal("100"))
            side = "SELL"
            pos_side = "LONG" if runtime.IS_HEDGE else None

        tp_price = filters.round_step_floor(raw_tp_price, price_step)
        eff_qty = filters.round_step_floor(qty, qty_step)

        if eff_qty < min_qty:
            logger.warning("TP 최소 수량 미달: %s < %s", eff_qty, min_qty)
            return None

        if min_notional > 0 and tp_price * eff_qty < min_notional:
            eff_qty = filters.adjust_qty_for_min_notional(
                tp_price, eff_qty, qty_step, min_qty, min_notional
            )
            if eff_qty is None or eff_qty > qty:
                logger.warning("TP MIN_NOTIONAL 불가: tp=%s qty=%s", tp_price, qty)
                return None

        order_id, err = client.place_limit_order_raw(
            symbol=symbol,
            side=side,
            price=str(tp_price),
            qty=str(eff_qty),
            position_side=pos_side,
            reduce_only=True,
        )
        if order_id is not None:
            logger.info(
                "TP 주문 성공: %s %s 익절 @ %s (qty=%s, tpOrderId=%s)",
                symbol,
                direction,
                tp_price,
                eff_qty,
                order_id,
                extra={
                    "event": "tp_order_placed",
                    "symbol": symbol,
                    "direction": direction,
                    "order_id": order_id,
                    "tp_price": str(tp_price),
                    "qty": str(eff_qty),
                },
            )
            return order_id
        logger.warning("TP 주문 실패: %s / %s", symbol, err)
        return None
    except Exception as e:
        logger.exception("TP 주문 예외 발생: %s", e)
        return None


def _is_risk_limit_error(err: Optional[dict]) -> bool:
    if not err:
        return False
    code = err.get("code")
    try:
        return int(code) in _RISK_LIMIT_CODES
    except (TypeError, ValueError):
        return False


def close_position_market(
    symbol: str, direction: str, qty: Decimal
) -> Optional[Tuple[Decimal, int]]:
    """보유 포지션(direction) 시장가 청산. (체결가 추정용 현재가, orderId) 반환."""
    if qty <= 0:
        return None
    try:
        from coin_rising_short import filters

        price_step, qty_step, min_qty, _ = filters.get_price_step_and_qty_step(symbol)
        eff_qty = filters.round_step_floor(qty, qty_step)
        if eff_qty < min_qty:
            logger.warning("청산 수량 최소 미달: %s qty=%s", symbol, qty)
            return None

        if direction.upper() == "SHORT":
            side = "BUY"
            pos_side = "SHORT" if runtime.IS_HEDGE else None
        else:
            side = "SELL"
            pos_side = "LONG" if runtime.IS_HEDGE else None

        price = client.get_ticker_price(symbol)
        order_id, err = client.place_market_order_raw(
            symbol=symbol,
            side=side,
            qty=str(eff_qty),
            position_side=pos_side,
            reduce_only=True,
        )
        if order_id is None:
            logger.warning(
                "시장가 청산 실패: %s %s / %s",
                symbol,
                direction,
                err,
                extra={"event": "market_close_failed", "symbol": symbol},
            )
            return None
        logger.info(
            "시장가 청산 주문: %s %s qty=%s orderId=%s",
            symbol,
            direction,
            eff_qty,
            order_id,
            extra={
                "event": "market_close_placed",
                "symbol": symbol,
                "direction": direction,
                "order_id": order_id,
            },
        )
        return price, order_id
    except Exception as e:
        logger.exception("시장가 청산 예외: %s", e)
        return None


def place_short_order(
    symbol: str, notional_usdt: Optional[Decimal] = None
) -> Optional[Tuple[Decimal, Decimal, int]]:
    logger.info("숏 주문 시도: %s", symbol)
    try:
        ensure_leverage(symbol)
        price = client.get_ticker_price(symbol)
        price_step, qty_step, min_qty, min_notional = filters.get_price_step_and_qty_step(symbol)

        raw_price = price * (Decimal("1") + config.PREMIUM_PCT)
        limit_price = filters.round_step_floor(raw_price, price_step)

        target_notional = notional_usdt if notional_usdt is not None else config.POSITION_USDT

        for attempt in range(10):
            qty = filters.round_step_floor(target_notional / limit_price, qty_step)
            adj = filters.adjust_qty_for_min_notional(
                limit_price, qty, qty_step, min_qty, min_notional
            )
            if adj is None:
                logger.warning("MIN_NOTIONAL 충족 불가: %s", symbol)
                return None
            qty = adj

            pos_side = "SHORT" if runtime.IS_HEDGE else None
            order_id, err = place_limit_order(symbol, "SELL", limit_price, qty, pos_side)
            if order_id is not None:
                return limit_price, qty, order_id

            if _is_risk_limit_error(err):
                logger.warning(
                    "%s 포지션/리스크 한도 초과, 명목 %s USDT → 50%% 축소 후 재시도 (%s/10)",
                    symbol,
                    target_notional,
                    attempt + 1,
                )
                target_notional = target_notional / Decimal("2")
                q_try = filters.round_step_floor(target_notional / limit_price, qty_step)
                if q_try < min_qty:
                    logger.warning("명목 축소 후 최소 수량 미만: %s", symbol)
                    return None
                continue
            return None

        logger.warning("%s 숏 주문 리스크 한도 재시도 한도 초과", symbol)
        return None
    except Exception as e:
        logger.exception("숏 주문 예외 발생: %s", e)
        return None


def place_long_order(
    symbol: str, notional_usdt: Optional[Decimal] = None
) -> Optional[Tuple[Decimal, Decimal, int]]:
    logger.info("롱 주문 시도: %s", symbol)
    try:
        ensure_leverage(symbol)
        price = client.get_ticker_price(symbol)
        price_step, qty_step, min_qty, min_notional = filters.get_price_step_and_qty_step(symbol)

        raw_price = price * (Decimal("1") - config.DISCOUNT_PCT)
        limit_price = filters.round_step_floor(raw_price, price_step)

        target_notional = notional_usdt if notional_usdt is not None else config.POSITION_USDT

        for attempt in range(10):
            qty = filters.round_step_floor(target_notional / limit_price, qty_step)
            adj = filters.adjust_qty_for_min_notional(
                limit_price, qty, qty_step, min_qty, min_notional
            )
            if adj is None:
                logger.warning("MIN_NOTIONAL 충족 불가: %s", symbol)
                return None
            qty = adj

            pos_side = "LONG" if runtime.IS_HEDGE else None
            order_id, err = place_limit_order(symbol, "BUY", limit_price, qty, pos_side)
            if order_id is not None:
                return limit_price, qty, order_id

            if _is_risk_limit_error(err):
                logger.warning(
                    "%s 포지션/리스크 한도 초과, 명목 %s USDT → 50%% 축소 (%s/10)",
                    symbol,
                    target_notional,
                    attempt + 1,
                )
                target_notional = target_notional / Decimal("2")
                q_try = filters.round_step_floor(target_notional / limit_price, qty_step)
                if q_try < min_qty:
                    logger.warning("명목 축소 후 최소 수량 미만: %s", symbol)
                    return None
                continue
            return None

        logger.warning("%s 롱 주문 리스크 한도 재시도 한도 초과", symbol)
        return None
    except Exception as e:
        logger.exception("롱 주문 예외 발생: %s", e)
        return None
