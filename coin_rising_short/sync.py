import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from coin_rising_short import client, config, orders, runtime, state, symbols

logger = logging.getLogger(__name__)


def _position_side_from_raw(side: str) -> Optional[str]:
    s = str(side).lower()
    if s == "buy":
        return "LONG"
    if s == "sell":
        return "SHORT"
    return None


def fetch_exchange_positions() -> Dict[str, Dict[str, Any]]:
    """symbol -> {side: LONG|SHORT, size: Decimal, avg_price: Decimal}"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for p in client.get_position_risk():
            size = Decimal(str(p.get("size", "0")))
            if size <= 0:
                continue
            sym = p.get("symbol")
            if not sym:
                continue
            direction = _position_side_from_raw(p.get("side", ""))
            if not direction:
                continue
            avg = Decimal(str(p.get("avgPrice") or p.get("entryPrice") or p.get("markPrice") or "0"))
            if avg <= 0:
                avg = Decimal(str(p.get("markPrice", "0")))
            out[sym] = {"side": direction, "size": size, "avg_price": avg}
    except Exception as exc:
        logger.warning("포지션 조회 실패: %s", exc)
    return out


def _get_filled_qty_direction(st: Dict[str, Any]) -> Tuple[Decimal, str]:
    total_qty = Decimal("0")
    direction = "SHORT"
    for entry in st.get("entries", []):
        if not entry.get("filled"):
            continue
        qty = Decimal(str(entry.get("qty", "0")))
        if qty <= 0:
            continue
        total_qty += qty
        direction = str(entry.get("direction", "SHORT"))
    return total_qty, direction


def _symbol_managed_by_bot(symbol: str) -> bool:
    if symbol in runtime.ST_TRACKED_SYMBOLS:
        return True
    if symbol in runtime.QUALIFIED_WATCH:
        return True
    if symbol in state.position_state:
        return True
    return any(sym == symbol for sym, _ in runtime.BOT_ENTRY_ORDER_KEYS)


def _cancel_st_tp_if_any(symbol: str, st: Dict[str, Any]) -> bool:
    if not st.get("st_mode"):
        return False
    tp_oid = st.get("tp_order_id")
    if not tp_oid:
        return False
    orders.cancel_order(symbol, tp_oid)
    st["tp_order_id"] = None
    st["tp_exit_logged"] = False
    logger.info(
        "ST 모드 구 TP 취소: %s orderId=%s",
        symbol,
        tp_oid,
        extra={"event": "st_legacy_tp_cancelled", "symbol": symbol, "order_id": str(tp_oid)},
    )
    return True


def _adopt_exchange_position(symbol: str, ep: Dict[str, Any]) -> None:
    direction = ep["side"]
    qty = ep["size"]
    avg = ep["avg_price"]
    if avg <= 0:
        avg = qty  # fallback unlikely; keep non-zero guard below
    runtime.ST_TRACKED_SYMBOLS.add(symbol)
    if symbol not in runtime.QUALIFIED_WATCH:
        import time

        runtime.QUALIFIED_WATCH[symbol] = {
            "added_at": time.time(),
            "last_direction": None,
            "consecutive_losses": 0,
            "halted": False,
        }
    state.position_state[symbol] = {
        "st_mode": True,
        "st_signal_direction": direction,
        "entry_price": avg if avg > 0 else Decimal("0"),
        "reentry_count": 0,
        "last_reentry_price": avg if avg > 0 else Decimal("0"),
        "tp_order_id": None,
        "tp_entry_price": Decimal("0"),
        "tp_qty": Decimal("0"),
        "tp_exit_logged": False,
        "entries": [
            {
                "direction": direction,
                "entry_price": avg if avg > 0 else Decimal("0"),
                "qty": qty,
                "order_id": "EXCHANGE_SYNC",
                "filled": True,
                "closed": False,
                "entry_logged": True,
            }
        ],
    }
    logger.warning(
        "거래소 포지션 → 봇 상태 복원: %s %s qty=%s avg=%s",
        symbol,
        direction,
        qty,
        avg,
        extra={
            "event": "position_adopted_from_exchange",
            "symbol": symbol,
            "direction": direction,
            "qty": str(qty),
        },
    )


def reconcile_positions_with_exchange(ex_positions: Dict[str, Dict[str, Any]]) -> bool:
    """저장 상태 ↔ 거래소 포지션 맞춤. True if state file should be saved."""
    from coin_rising_short.monitor import _set_last_flat_direction, enforce_st_tracked_limit

    dirty = False
    for symbol, st in list(state.position_state.items()):
        if _cancel_st_tp_if_any(symbol, st):
            dirty = True

        if not st.get("st_mode"):
            continue

        local_qty, local_dir = _get_filled_qty_direction(st)
        ex = ex_positions.get(symbol)

        if local_qty > 0 and ex is None:
            _set_last_flat_direction(symbol, local_dir)
            state.position_state.pop(symbol, None)
            dirty = True
            logger.info(
                "거래소 무포지션 — 로컬 ST 상태 제거: %s (was %s)",
                symbol,
                local_dir,
                extra={"event": "position_state_cleared_flat_exchange", "symbol": symbol},
            )
            continue

        if local_qty > 0 and ex is not None:
            if ex["side"] != local_dir:
                logger.warning(
                    "포지션 방향 불일치(로컬=%s 거래소=%s): %s — 거래소 기준 반영",
                    local_dir,
                    ex["side"],
                    symbol,
                    extra={"event": "position_side_reconciled", "symbol": symbol},
                )
                st["st_signal_direction"] = ex["side"]
                dirty = True
            if ex["size"] != local_qty:
                for entry in st.get("entries", []):
                    if entry.get("filled") and not entry.get("closed"):
                        entry["qty"] = ex["size"]
                        if ex["avg_price"] > 0:
                            entry["entry_price"] = ex["avg_price"]
                        dirty = True
                        break

        if local_qty <= 0 and ex is not None:
            has_pending = any(
                not e.get("filled") and not e.get("closed") for e in st.get("entries", [])
            )
            if has_pending:
                for entry in st.get("entries", []):
                    if entry.get("filled") or entry.get("closed"):
                        continue
                    entry["filled"] = True
                    entry["closed"] = False
                    entry["direction"] = ex["side"]
                    entry["qty"] = ex["size"]
                    if ex["avg_price"] > 0:
                        entry["entry_price"] = ex["avg_price"]
                    entry["entry_logged"] = True
                    st["st_signal_direction"] = ex["side"]
                    dirty = True
                    logger.info(
                        "미체결 상태였으나 거래소 포지션 존재 — 체결 반영: %s %s",
                        symbol,
                        ex["side"],
                        extra={"event": "pending_state_marked_filled_from_exchange", "symbol": symbol},
                    )

    for symbol, ep in ex_positions.items():
        if symbol in state.position_state:
            continue
        if symbol not in symbols.TRADING_SYMBOLS:
            logger.info(
                "거래소 포지션 스킵(비거래 심볼): %s",
                symbol,
                extra={"event": "exchange_position_skipped_unlisted", "symbol": symbol},
            )
            continue
        if not _symbol_managed_by_bot(symbol):
            logger.warning(
                "거래소 포지션 미복원(봇 미관리 — 타 전략 가능): %s %s qty=%s",
                symbol,
                ep["side"],
                ep["size"],
                extra={"event": "exchange_position_not_adopted", "symbol": symbol},
            )
            continue
        _adopt_exchange_position(symbol, ep)
        dirty = True

    trimmed = enforce_st_tracked_limit(ex_positions)
    if trimmed:
        dirty = True
    return dirty


def sync_state_with_exchange() -> None:
    state.load_position_state()
    state.load_qualified_watch()

    ex_positions = fetch_exchange_positions()
    for sym, ep in ex_positions.items():
        logger.info(
            "거래소 포지션: %s %s size=%s",
            sym,
            ep["side"],
            ep["size"],
            extra={"event": "exchange_position_seen", "symbol": sym},
        )

    if not state.position_state and not ex_positions:
        logger.info(
            "저장된 상태·거래소 포지션 없음 (%s)",
            config.POSITION_STATE_PATH,
            extra={"event": "state_empty"},
        )
        from coin_rising_short.monitor import enforce_st_tracked_limit

        if enforce_st_tracked_limit(ex_positions):
            state.save_qualified_watch()
        return

    logger.info("거래소와 상태 동기화 중...", extra={"event": "sync_started"})
    try:
        orders_list = client.get_open_orders()
    except Exception as exc:
        logger.warning("openOrders 조회 실패: %s", exc)
        orders_list = []

    open_map = {(o["symbol"], str(o["orderId"])): o for o in orders_list}

    remove_symbols: List[str] = []
    dirty = False
    for symbol, st in list(state.position_state.items()):
        st.setdefault("reentry_count", 0)
        st.setdefault("last_reentry_price", st.get("entry_price", Decimal("0")))
        st.setdefault("tp_order_id", None)
        st.setdefault("tp_entry_price", Decimal("0"))
        st.setdefault("tp_qty", Decimal("0"))
        st.setdefault("tp_exit_logged", False)
        for entry in st.get("entries", []):
            entry.setdefault("filled", False)
            entry.setdefault("closed", False)
            entry.setdefault("entry_logged", False)
            oid = str(entry["order_id"])
            if oid == "EXCHANGE_SYNC":
                continue
            key = (symbol, oid)
            if key in open_map:
                o = open_map[key]
                st_ord = o.get("status", "")
                if st_ord == "PARTIALLY_FILLED":
                    ex = Decimal(str(o.get("executedQty", "0")))
                    if ex > 0:
                        entry["qty"] = ex
                        dirty = True
                entry["filled"] = False
                entry["closed"] = False
                continue

            detail = orders.get_order_detail(symbol, oid)
            if not detail:
                logger.warning("주문 상세 없음(스킵): %s orderId=%s", symbol, oid)
                continue
            st_detail = detail.get("status")
            if st_detail == "FILLED":
                ap = Decimal(str(detail.get("avgPrice", "0")))
                eq = Decimal(str(detail.get("executedQty", "0")))
                if ap > 0:
                    entry["entry_price"] = ap
                if eq > 0:
                    entry["qty"] = eq
                entry["filled"] = True
                entry["closed"] = False
                dirty = True
            elif st_detail in ("CANCELED", "REJECTED", "EXPIRED", "NEW"):
                entry["filled"] = False
                entry["closed"] = True
                dirty = True
            elif st_detail == "PARTIALLY_FILLED":
                ex = Decimal(str(detail.get("executedQty", "0")))
                if ex > 0:
                    entry["qty"] = ex
                    dirty = True

        tp_oid = st.get("tp_order_id")
        if tp_oid and not st.get("st_mode"):
            tp_detail = orders.get_order_detail(symbol, tp_oid)
            if tp_detail:
                tp_status = tp_detail.get("status")
                if tp_status == "FILLED":
                    st["tp_exit_logged"] = True
                    for entry in st.get("entries", []):
                        if entry.get("filled"):
                            entry["filled"] = False
                            entry["closed"] = True
                    remove_symbols.append(symbol)
                    dirty = True
                elif tp_status in ("CANCELED", "REJECTED", "EXPIRED"):
                    st["tp_order_id"] = None
                    dirty = True

    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        dirty = True
        logger.info(
            "동기화 중 종료 심볼 상태 정리: %s",
            symbol,
            extra={"event": "sync_symbol_state_cleared", "symbol": symbol},
        )

    if reconcile_positions_with_exchange(ex_positions):
        dirty = True

    if dirty:
        state.save_position_state()
    state.save_qualified_watch()
    logger.info(
        "동기화 완료, 포지션 상태 %s개 | ST 추적 %s개 -> %s",
        len(state.position_state),
        len(runtime.ST_TRACKED_SYMBOLS),
        config.POSITION_STATE_PATH,
        extra={
            "event": "sync_completed",
            "tracked_symbols": len(state.position_state),
            "st_tracked": len(runtime.ST_TRACKED_SYMBOLS),
        },
    )
