import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from coin_rising_short import client, config, indicators, orders, runtime, state, symbols, trade_journal

logger = logging.getLogger(__name__)


def get_24h_risers_and_top_movers() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """24h 상승률이 GAINER_THRESHOLD_PCT 이상인 USDT 선물 목록 (전체, 정렬)."""
    data = client.get_linear_tickers()
    if not isinstance(data, list):
        raise RuntimeError(f"24hr ticker 응답 형식 오류: {type(data)}")

    qualified: List[Dict[str, Any]] = []
    all_movers: List[Dict[str, Any]] = []
    for t in data:
        symbol = t.get("symbol")
        if symbol not in symbols.TRADING_SYMBOLS:
            continue
        try:
            # Bybit price24hPcnt: 소수(0.1678 = 16.78%)
            change_pct = Decimal(str(t.get("price24hPcnt", "0"))) * Decimal("100")
            turnover_24h = Decimal(str(t.get("turnover24h", "0")))
            last_price = Decimal(str(t.get("lastPrice", "0")))

            row = {
                "symbol": symbol,
                "change_pct": change_pct,
                "last_price": last_price,
                "turnover_24h": turnover_24h,
            }
            all_movers.append(row)
            passed_basic = change_pct >= config.GAINER_THRESHOLD_PCT
            if config.USE_VOLUME_FILTER:
                passed_basic = passed_basic and turnover_24h >= config.MIN_VOLUME_USDT
            if not passed_basic:
                continue

            qualified.append(row)
        except Exception:
            continue

    qualified.sort(key=lambda x: x["change_pct"], reverse=True)

    all_movers.sort(key=lambda x: x["change_pct"], reverse=True)
    return qualified, all_movers[:3]


def _get_filled_position(st: Dict[str, Any]) -> Tuple[Decimal, Decimal, str]:
    total_qty = Decimal("0")
    weighted_sum = Decimal("0")
    direction = "SHORT"
    for entry in st.get("entries", []):
        if not entry.get("filled"):
            continue
        qty = Decimal(str(entry.get("qty", "0")))
        price = Decimal(str(entry.get("entry_price", "0")))
        if qty <= 0 or price <= 0:
            continue
        total_qty += qty
        weighted_sum += price * qty
        direction = str(entry.get("direction", "SHORT"))
    if total_qty <= 0:
        return Decimal("0"), Decimal("0"), direction
    return weighted_sum / total_qty, total_qty, direction


def _st_target_direction(symbol: str) -> Tuple[Optional[str], str]:
    curr_d, reason = indicators.get_supertrend_direction(symbol)
    if curr_d is None:
        return None, reason
    return indicators.st_int_to_direction(curr_d), reason


def _st_matches_entry(symbol: str, direction: str) -> Tuple[bool, str]:
    target, reason = _st_target_direction(symbol)
    if target is None:
        return False, reason
    if target != direction.upper():
        return False, f"ST={target} vs entry={direction.upper()} ({reason})"
    return True, reason


def _set_last_flat_direction(symbol: str, direction: str) -> None:
    _ensure_st_watch(symbol)
    runtime.QUALIFIED_WATCH[symbol]["last_flat_direction"] = direction.upper()


def _can_st_flat_enter(symbol: str, target_dir: str) -> bool:
    """TP/청산 후 ST 방향이 같으면 재진입하지 않음 (전환 시에만 반대 진입)."""
    watch = runtime.QUALIFIED_WATCH.get(symbol, {})
    last = watch.get("last_flat_direction")
    if not last:
        return True
    return target_dir.upper() != str(last).upper()


def _exchange_position_side(symbol: str) -> Optional[str]:
    try:
        for p in client.get_position_risk():
            if p.get("symbol") != symbol:
                continue
            size = Decimal(str(p.get("size", "0")))
            if size <= 0:
                continue
            side = str(p.get("side", "")).lower()
            if side == "buy":
                return "LONG"
            if side == "sell":
                return "SHORT"
    except Exception as exc:
        logger.warning("포지션 조회 실패 %s: %s", symbol, exc)
    return None


def _has_exchange_entry_order(symbol: str) -> bool:
    try:
        for o in client.get_open_orders():
            if o.get("symbol") != symbol:
                continue
            if o.get("reduceOnly"):
                continue
            if o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                return True
    except Exception as exc:
        logger.warning("미체결 주문 조회 실패 %s: %s", symbol, exc)
    return False


def _refresh_symbol_take_profit(symbol: str, st: Dict[str, Any]) -> bool:
    if st.get("st_mode"):
        existing_tp = st.get("tp_order_id")
        if existing_tp:
            orders.cancel_order(symbol, existing_tp)
            st["tp_order_id"] = None
            logger.info(
                "ST 추종 모드 — 고정 %% TP 취소(청산은 ST 전환): %s",
                symbol,
                extra={"event": "st_fixed_tp_cancelled", "symbol": symbol},
            )
        return False

    avg_entry, total_qty, direction = _get_filled_position(st)
    if total_qty <= 0:
        return False

    need_replace = False
    existing_tp_oid = st.get("tp_order_id")
    target_avg = str(avg_entry)
    target_qty = str(total_qty)

    if existing_tp_oid:
        old_avg = str(st.get("tp_entry_price", ""))
        old_qty = str(st.get("tp_qty", ""))
        tp_status = orders.get_order_status(symbol, existing_tp_oid)
        if tp_status == "FILLED":
            return False
        if tp_status in ("NEW", "PARTIALLY_FILLED") and old_avg == target_avg and old_qty == target_qty:
            return False
        need_replace = True

    if need_replace and existing_tp_oid:
        if not orders.cancel_order(symbol, existing_tp_oid):
            return False

    tp_oid = None
    for attempt in range(3):
        tp_oid = orders.place_take_profit_order(symbol, direction, avg_entry, total_qty)
        if tp_oid is not None:
            break
        logger.warning(
            "TP 재생성 실패 재시도: %s (%s/3)",
            symbol,
            attempt + 1,
            extra={"event": "symbol_tp_refresh_retry", "symbol": symbol},
        )
        time.sleep(0.5)
    if tp_oid is None:
        logger.error(
            "TP 재생성 최종 실패: %s (무보호 구간 가능)",
            symbol,
            extra={"event": "symbol_tp_refresh_failed", "symbol": symbol},
        )
        return False
    st["tp_order_id"] = tp_oid
    st["tp_entry_price"] = avg_entry
    st["tp_qty"] = total_qty
    st["tp_exit_logged"] = False
    logger.info(
        "심볼 TP 갱신: %s avg=%s qty=%s tpOrderId=%s",
        symbol,
        avg_entry,
        total_qty,
        tp_oid,
        extra={"event": "symbol_tp_refreshed", "symbol": symbol, "order_id": tp_oid},
    )
    return True


def check_filled_and_refresh_tp() -> None:
    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in state.position_state.items():
        entries = st.get("entries", [])
        symbol_dirty = False
        for entry in entries:
            if entry.get("filled") or entry.get("closed"):
                continue

            order_id = entry["order_id"]
            direction = entry["direction"]
            entry_price = entry["entry_price"]
            qty = entry["qty"]

            status = orders.get_order_status(symbol, order_id)
            if status is None:
                continue
            if status == "NOT_FOUND":
                logger.warning(
                    "주문 미존재(-2013)로 엔트리 종료 처리: %s orderId=%s",
                    symbol,
                    order_id,
                    extra={"event": "entry_order_not_found", "symbol": symbol, "order_id": order_id},
                )
                entry["filled"] = False
                entry["closed"] = True
                _unregister_bot_entry_order(symbol, str(order_id))
                dirty = True
                symbol_dirty = True
                continue

            if status == "FILLED":
                if st.get("st_mode"):
                    ok_st, st_reason = _st_matches_entry(symbol, direction)
                    if not ok_st:
                        logger.warning(
                            "ST 방향 불일치 — %s %s 체결 후 즉시 전환 처리: %s",
                            symbol,
                            direction,
                            st_reason,
                            extra={"event": "st_fill_direction_mismatch", "symbol": symbol},
                        )
                        entry["filled"] = True
                        entry["closed"] = False
                        dirty = True
                        symbol_dirty = True
                        state.save_position_state()
                        _process_supertrend_symbol(symbol)
                        continue

                logger.info(
                    "진입 체결 확인: %s %s (orderId=%s) -> %s",
                    symbol,
                    direction,
                    order_id,
                    "TP 생략(ST 추종)" if st.get("st_mode") else "TP 주문 생성 시도",
                    extra={
                        "event": "entry_filled",
                        "symbol": symbol,
                        "direction": direction,
                        "order_id": order_id,
                    },
                )
                detail = orders.get_order_detail(symbol, order_id)
                filled_time_ms = None
                if detail and isinstance(detail, dict):
                    ap = Decimal(str(detail.get("avgPrice", "0")))
                    ex = Decimal(str(detail.get("executedQty", "0")))
                    if ap > 0:
                        entry["entry_price"] = ap
                        entry_price = ap
                    if ex > 0:
                        entry["qty"] = ex
                        qty = ex
                    t = detail.get("updateTime")
                    if isinstance(t, int):
                        filled_time_ms = t

                if not entry.get("entry_logged"):
                    trade_journal.log_entry_filled(
                        symbol=symbol,
                        direction=direction,
                        order_id=order_id,
                        entry_price=entry_price,
                        qty=qty,
                        filled_time_ms=filled_time_ms,
                    )
                    entry["entry_logged"] = True
                entry["filled"] = True
                entry["closed"] = False
                _unregister_bot_entry_order(symbol, str(order_id))
                dirty = True
                symbol_dirty = True
            elif status == "PARTIALLY_FILLED":
                d = orders.get_order_detail(symbol, order_id)
                if d:
                    ex = Decimal(str(d.get("executedQty", "0")))
                    ap = Decimal(str(d.get("avgPrice", "0")))
                    if ex > 0:
                        entry["qty"] = ex
                    if ap > 0:
                        entry["entry_price"] = ap
                    dirty = True
                    symbol_dirty = True
            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                logger.warning(
                    "진입 주문 종료 상태(%s): %s (orderId=%s) -> TP 생성 스킵",
                    status,
                    symbol,
                    order_id,
                    extra={
                        "event": "entry_closed_without_tp",
                        "symbol": symbol,
                        "status": status,
                        "order_id": order_id,
                    },
                )
                entry["filled"] = False
                entry["closed"] = True
                _unregister_bot_entry_order(symbol, str(order_id))
                dirty = True
                symbol_dirty = True
        if symbol_dirty and _refresh_symbol_take_profit(symbol, st):
            dirty = True
        # 모든 엔트리가 종료됐고 TP도 없다면, 심볼 상태를 지워서 다음 사이클 진입 가능하게 함.
        entries = st.get("entries", [])
        if entries and all(bool(e.get("closed")) for e in entries) and not st.get("tp_order_id"):
            remove_symbols.append(symbol)
            logger.info(
                "종료된 심볼 상태 정리(수동청산/취소 등): %s",
                symbol,
                extra={"event": "symbol_state_cleared_manual", "symbol": symbol},
            )
            dirty = True
    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
    if dirty:
        state.save_position_state()


def check_tp_filled_and_log() -> None:
    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in state.position_state.items():
        if st.get("st_mode"):
            continue
        tp_oid = st.get("tp_order_id")
        if not tp_oid or st.get("tp_exit_logged"):
            continue
        tp_detail = orders.get_order_detail(symbol, tp_oid)
        if not tp_detail or tp_detail.get("status") != "FILLED":
            continue
        exit_price = Decimal(str(tp_detail.get("avgPrice", "0")))
        exit_qty = Decimal(str(tp_detail.get("executedQty", "0")))
        if exit_price <= 0 or exit_qty <= 0:
            continue

        avg_entry = Decimal(str(st.get("tp_entry_price", "0")))
        direction = "SHORT"
        if st.get("entries"):
            direction = str(st["entries"][0].get("direction", "SHORT"))

        entry_price = avg_entry if avg_entry > 0 else exit_price
        pnl = trade_journal.calc_pnl_usdt(direction, entry_price, exit_price, exit_qty)

        trade_journal.log_exit_filled(
            symbol=symbol,
            direction=direction,
            entry_order_id="MULTI",
            tp_order_id=tp_oid,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=exit_qty,
            entry_time_ms=None,
            exit_time_ms=tp_detail.get("updateTime") if isinstance(tp_detail.get("updateTime"), int) else None,
            note="평균진입가 기준 TP 청산",
        )
        if config.USE_SUPERTREND_ENTRY and st.get("st_mode"):
            _record_st_trade_pnl(symbol, pnl)
            if st.get("entries"):
                _set_last_flat_direction(
                    symbol, str(st["entries"][0].get("direction", "SHORT"))
                )
        st["tp_exit_logged"] = True
        for entry in st.get("entries", []):
            if entry.get("filled"):
                entry["closed"] = True
                entry["filled"] = False
        remove_symbols.append(symbol)
        dirty = True
        logger.info(
            "TP 체결 기록 완료: %s tpOrderId=%s pnl=%s",
            symbol,
            tp_oid,
            pnl,
            extra={"event": "tp_filled_logged", "symbol": symbol, "order_id": tp_oid},
        )
    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        logger.info(
            "심볼 상태 정리 완료(신규 사이클 허용): %s",
            symbol,
            extra={"event": "symbol_state_cleared", "symbol": symbol},
        )
    if dirty:
        state.save_position_state()


def _is_st_halted(symbol: str) -> bool:
    return symbol in runtime.ST_HALTED_SYMBOLS


def _register_bot_entry_order(symbol: str, order_id: str) -> None:
    runtime.BOT_ENTRY_ORDER_KEYS.add((symbol, str(order_id)))


def _unregister_bot_entry_order(symbol: str, order_id: str) -> None:
    runtime.BOT_ENTRY_ORDER_KEYS.discard((symbol, str(order_id)))


def _sync_bot_entry_orders_from_state() -> None:
    """position_state 미체결 진입 주문 ID를 봇 관리 목록에 반영."""
    for symbol, st in state.position_state.items():
        if not st.get("st_mode"):
            continue
        for entry in st.get("entries", []):
            if entry.get("filled") or entry.get("closed"):
                continue
            oid = entry.get("order_id")
            if oid:
                _register_bot_entry_order(symbol, str(oid))


def _bot_managed_stale_cancel_keys() -> set[tuple[str, str]]:
    _sync_bot_entry_orders_from_state()
    return set(runtime.BOT_ENTRY_ORDER_KEYS)


def _active_st_position_count() -> int:
    return sum(1 for st in state.position_state.values() if st.get("st_mode"))


def _can_open_new_st_slot(symbol: str) -> bool:
    if symbol in state.position_state:
        return True
    return _active_st_position_count() < config.MAX_CONCURRENT_ST_SYMBOLS


def _reconcile_stale_entries_in_state() -> None:
    """거래소에서 취소된 오래된 진입 주문에 맞춰 로컬 상태 정리."""
    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in list(state.position_state.items()):
        if not st.get("st_mode"):
            continue
        for entry in st.get("entries", []):
            if entry.get("filled") or entry.get("closed"):
                continue
            status = orders.get_order_status(symbol, entry["order_id"])
            if status not in ("CANCELED", "REJECTED", "EXPIRED", "NOT_FOUND"):
                continue
            entry["closed"] = True
            entry["filled"] = False
            _unregister_bot_entry_order(symbol, str(entry["order_id"]))
            dirty = True
            logger.info(
                "로컬 진입 상태 정리(%s): %s orderId=%s",
                status,
                symbol,
                entry["order_id"],
                extra={"event": "stale_entry_state_cleared", "symbol": symbol},
            )
        entries = st.get("entries", [])
        if entries and all(bool(e.get("closed")) for e in entries) and not st.get("tp_order_id"):
            remove_symbols.append(symbol)
            dirty = True
    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
    if dirty:
        state.save_position_state()


def _ensure_st_watch(symbol: str) -> None:
    if symbol not in runtime.QUALIFIED_WATCH:
        runtime.QUALIFIED_WATCH[symbol] = {
            "added_at": time.time(),
            "last_direction": None,
            "consecutive_losses": 0,
            "halted": False,
        }


def _get_st_losses(symbol: str) -> int:
    return int(runtime.QUALIFIED_WATCH.get(symbol, {}).get("consecutive_losses", 0))


def _set_st_losses(symbol: str, losses: int) -> None:
    _ensure_st_watch(symbol)
    runtime.QUALIFIED_WATCH[symbol]["consecutive_losses"] = losses


def _can_add_st_tracked(symbol: str) -> bool:
    if symbol in runtime.ST_TRACKED_SYMBOLS:
        return True
    if symbol in state.position_state:
        return True
    if _exchange_position_side(symbol):
        return True
    return len(runtime.ST_TRACKED_SYMBOLS) < config.MAX_ST_TRACKED_SYMBOLS


def enforce_st_tracked_limit(
    exchange_positions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> bool:
    """ST 추적 심볼 상한 — 포지션 보유 종목은 유지."""
    max_n = config.MAX_ST_TRACKED_SYMBOLS
    if len(runtime.ST_TRACKED_SYMBOLS) <= max_n:
        return False
    protected: Set[str] = set(state.position_state.keys())
    if exchange_positions:
        protected.update(exchange_positions.keys())
    else:
        try:
            for p in client.get_position_risk():
                if Decimal(str(p.get("size", "0"))) > 0 and p.get("symbol"):
                    protected.add(p["symbol"])
        except Exception:
            pass
    removable = [
        s
        for s in runtime.ST_TRACKED_SYMBOLS
        if s not in protected and not _is_st_halted(s)
    ]
    removable.sort(
        key=lambda s: float(runtime.QUALIFIED_WATCH.get(s, {}).get("added_at", 0))
    )
    changed = False
    while len(runtime.ST_TRACKED_SYMBOLS) > max_n and removable:
        sym = removable.pop(0)
        runtime.ST_TRACKED_SYMBOLS.discard(sym)
        if sym in runtime.QUALIFIED_WATCH and sym not in protected:
            runtime.QUALIFIED_WATCH.pop(sym, None)
        changed = True
        logger.info(
            "ST 추적 상한(%s) — 추적 해제: %s",
            max_n,
            sym,
            extra={"event": "st_tracked_trimmed", "symbol": sym, "max_tracked": max_n},
        )
    return changed


def _mark_st_tracked(symbol: str) -> None:
    if not _can_add_st_tracked(symbol):
        logger.warning(
            "ST 추적 상한(%s개) — 추적 등록 스킵: %s",
            config.MAX_ST_TRACKED_SYMBOLS,
            symbol,
            extra={"event": "st_tracked_limit_reached", "symbol": symbol},
        )
        return
    runtime.ST_TRACKED_SYMBOLS.add(symbol)
    _ensure_st_watch(symbol)
    state.save_qualified_watch()


def _record_st_trade_pnl(symbol: str, pnl: Decimal) -> None:
    if pnl < 0:
        losses = _get_st_losses(symbol) + 1
    else:
        losses = 0
    _set_st_losses(symbol, losses)
    logger.info(
        "SuperTrend 손익 기록: %s pnl=%s 연속손실=%s/%s",
        symbol,
        pnl,
        losses,
        config.ST_MAX_CONSECUTIVE_LOSSES,
        extra={
            "event": "st_trade_pnl",
            "symbol": symbol,
            "pnl": str(pnl),
            "consecutive_losses": losses,
        },
    )
    if losses >= config.ST_MAX_CONSECUTIVE_LOSSES:
        _halt_st_symbol(symbol, losses)
    elif config.USE_SUPERTREND_ENTRY:
        state.save_qualified_watch()


def _halt_st_symbol(symbol: str, losses: int) -> None:
    runtime.ST_HALTED_SYMBOLS.add(symbol)
    runtime.ST_TRACKED_SYMBOLS.discard(symbol)
    runtime.QUALIFIED_WATCH.pop(symbol, None)
    state.save_qualified_watch()
    logger.warning(
        "SuperTrend 연속 %s회 손실 — %s 매매 정지",
        losses,
        symbol,
        extra={"event": "st_symbol_halted", "symbol": symbol, "consecutive_losses": losses},
    )


def _sync_qualified_watch(risers: List[Dict[str, Any]]) -> set[str]:
    """24h +20% 신규 후보 등록. 이미 ST 진입한 종목은 +20% 이탈해도 추적 유지."""
    now = time.time()
    active: set[str] = set()

    for symbol in runtime.ST_TRACKED_SYMBOLS:
        if _is_st_halted(symbol):
            continue
        active.add(symbol)
        _ensure_st_watch(symbol)

    for g in risers:
        symbol = g["symbol"]
        if _is_st_halted(symbol):
            continue
        until = runtime.SKIP_UNTIL.get(symbol, 0)
        if until and int(now) < until:
            continue
        active.add(symbol)
        if symbol not in runtime.ST_TRACKED_SYMBOLS and symbol not in runtime.QUALIFIED_WATCH:
            runtime.QUALIFIED_WATCH[symbol] = {
                "added_at": now,
                "last_direction": None,
                "consecutive_losses": 0,
                "halted": False,
            }
            logger.info(
                "SuperTrend 후보 등록 (24h +%s%%): %s",
                g["change_pct"],
                symbol,
                extra={
                    "event": "supertrend_watch_added",
                    "symbol": symbol,
                    "change_pct": str(g["change_pct"]),
                },
            )

    for symbol in list(runtime.QUALIFIED_WATCH.keys()):
        if symbol not in active:
            runtime.QUALIFIED_WATCH.pop(symbol, None)
            logger.info(
                "SuperTrend 후보 해제(미진입·조건 이탈): %s",
                symbol,
                extra={"event": "supertrend_watch_removed", "symbol": symbol},
            )
    if config.USE_SUPERTREND_ENTRY:
        state.save_qualified_watch()
    return active


def _new_st_position_state(
    symbol: str, direction: str, entry: Tuple[Decimal, Decimal, str]
) -> Dict[str, Any]:
    entry_price, qty, order_id = entry
    return {
        "st_mode": True,
        "st_signal_direction": direction,
        "entry_price": entry_price,
        "reentry_count": 0,
        "last_reentry_price": entry_price,
        "tp_order_id": None,
        "tp_entry_price": Decimal("0"),
        "tp_qty": Decimal("0"),
        "tp_exit_logged": False,
        "entries": [
            {
                "direction": direction,
                "entry_price": entry_price,
                "qty": qty,
                "order_id": order_id,
                "filled": False,
            }
        ],
    }


def _record_st_entry(
    symbol: str, direction: str, entry: Tuple[Decimal, Decimal, str], *, market_filled: bool = False
) -> None:
    _mark_st_tracked(symbol)
    _register_bot_entry_order(symbol, entry[2])
    state.position_state[symbol] = _new_st_position_state(symbol, direction, entry)
    if market_filled and state.position_state[symbol].get("entries"):
        ent = state.position_state[symbol]["entries"][0]
        ent["filled"] = True
        ent["closed"] = False
    watch = runtime.QUALIFIED_WATCH.get(symbol)
    if watch is not None:
        st_dir = 1 if direction == "LONG" else -1
        watch["last_direction"] = st_dir
        watch.pop("last_flat_direction", None)
    logger.info(
        "%s SuperTrend %s 진입 기록: price=%s orderId=%s qty=%s",
        symbol,
        direction,
        entry[0],
        entry[2],
        entry[1],
        extra={
            "event": "st_entry_recorded",
            "symbol": symbol,
            "direction": direction,
            "entry_price": str(entry[0]),
            "order_id": entry[2],
        },
    )
    state.save_position_state()
    state.save_qualified_watch()


def _has_pending_entry(st: Dict[str, Any]) -> bool:
    for entry in st.get("entries", []):
        if not entry.get("filled") and not entry.get("closed"):
            return True
    return False


def _close_st_position_for_flip(symbol: str, st: Dict[str, Any]) -> Optional[Decimal]:
    tp_oid = st.get("tp_order_id")
    if tp_oid:
        orders.cancel_order(symbol, tp_oid)
        st["tp_order_id"] = None

    avg_entry, total_qty, direction = _get_filled_position(st)
    if total_qty <= 0:
        return None

    ex_side = _exchange_position_side(symbol)
    if ex_side and ex_side != direction.upper():
        logger.warning(
            "청산 방향 불일치(거래소=%s 상태=%s): %s — 거래소 수량 기준 청산",
            ex_side,
            direction,
            symbol,
        )
    close_qty = total_qty
    if ex_side == direction.upper():
        try:
            for p in client.get_position_risk():
                if p.get("symbol") == symbol:
                    ex_size = Decimal(str(p.get("size", "0")))
                    if ex_size > 0:
                        close_qty = ex_size
                    break
        except Exception:
            pass

    result = orders.close_position_market(symbol, direction, close_qty)
    if not result:
        return None
    est_price, close_oid = result
    time.sleep(0.5)
    exit_price = est_price
    detail = orders.get_order_detail(symbol, close_oid)
    if detail:
        ap = Decimal(str(detail.get("avgPrice", "0")))
        if ap > 0:
            exit_price = ap

    pnl = trade_journal.calc_pnl_usdt(direction, avg_entry, exit_price, total_qty)
    trade_journal.log_exit_filled(
        symbol=symbol,
        direction=direction,
        entry_order_id="MULTI",
        tp_order_id=close_oid,
        entry_price=avg_entry,
        exit_price=exit_price,
        qty=total_qty,
        entry_time_ms=None,
        exit_time_ms=detail.get("updateTime") if detail and isinstance(detail.get("updateTime"), int) else None,
        note="SuperTrend 방향 전환 청산",
    )
    for entry in st.get("entries", []):
        entry["closed"] = True
        entry["filled"] = False
    _set_last_flat_direction(symbol, direction)
    return pnl


def _try_st_entry(symbol: str, direction: str) -> None:
    if symbol in state.position_state:
        logger.info(
            "이미 상태 존재 — 중복 진입 스킵: %s",
            symbol,
            extra={"event": "st_entry_skip_has_state", "symbol": symbol},
        )
        return

    ex_side = _exchange_position_side(symbol)
    if ex_side:
        logger.warning(
            "거래소 포지션 존재 — 중복 진입 스킵: %s side=%s",
            symbol,
            ex_side,
            extra={"event": "st_entry_skip_exchange_position", "symbol": symbol},
        )
        return

    if _has_exchange_entry_order(symbol):
        logger.info(
            "거래소 미체결 진입 주문 존재 — 중복 스킵: %s",
            symbol,
            extra={"event": "st_entry_skip_open_order", "symbol": symbol},
        )
        return

    ok_st, reason = _st_matches_entry(symbol, direction)
    if not ok_st:
        logger.info(
            "ST 방향 불일치 — 진입 스킵: %s want=%s (%s)",
            symbol,
            direction,
            reason,
            extra={"event": "st_entry_skip_direction", "symbol": symbol},
        )
        return

    if not _can_st_flat_enter(symbol, direction):
        logger.info(
            "ST 동일방향 재진입 스킵: %s %s (전환 대기)",
            symbol,
            direction,
            extra={"event": "st_entry_skip_same_after_flat", "symbol": symbol},
        )
        return

    if not _can_open_new_st_slot(symbol):
        logger.info(
            "동시 매매 한도(%s개) — 신규 진입 스킵: %s",
            config.MAX_CONCURRENT_ST_SYMBOLS,
            symbol,
            extra={
                "event": "st_entry_skip_max_concurrent",
                "symbol": symbol,
                "active": _active_st_position_count(),
            },
        )
        return

    if symbol not in runtime.ST_TRACKED_SYMBOLS and not _can_add_st_tracked(symbol):
        logger.info(
            "ST 추적 상한(%s개) — 신규 진입 스킵: %s",
            config.MAX_ST_TRACKED_SYMBOLS,
            symbol,
            extra={
                "event": "st_entry_skip_max_tracked",
                "symbol": symbol,
                "tracked": len(runtime.ST_TRACKED_SYMBOLS),
            },
        )
        return

    if direction == "LONG":
        entry = orders.place_long_order(symbol)
    else:
        entry = orders.place_short_order(symbol)
    if entry is not None:
        _record_st_entry(symbol, direction, entry, market_filled=config.USE_MARKET_ENTRY)


def _process_supertrend_symbol(symbol: str) -> None:
    if _is_st_halted(symbol):
        return

    curr_d, reason = indicators.get_supertrend_direction(symbol)
    if curr_d is None:
        if symbol in runtime.QUALIFIED_WATCH:
            logger.info(
                "SuperTrend 방향 조회 실패: %s (%s)",
                symbol,
                reason,
                extra={"event": "supertrend_direction_failed", "symbol": symbol},
            )
        return

    target_dir = indicators.st_int_to_direction(curr_d)
    st = state.position_state.get(symbol)

    if st is not None:
        if _has_pending_entry(st) and not any(e.get("filled") for e in st.get("entries", [])):
            held_dir = str(st.get("st_signal_direction", ""))
            if held_dir and held_dir != target_dir:
                logger.info(
                    "SuperTrend 전환 — 미체결 %s 주문 취소 후 %s 재시도: %s",
                    held_dir,
                    target_dir,
                    symbol,
                    extra={"event": "st_cancel_pending_flip", "symbol": symbol},
                )
                for entry in st.get("entries", []):
                    if entry.get("filled") or entry.get("closed"):
                        continue
                    orders.cancel_order(symbol, entry["order_id"])
                    entry["closed"] = True
                state.position_state.pop(symbol, None)
                state.save_position_state()
                if symbol in runtime.ST_TRACKED_SYMBOLS:
                    _try_st_entry(symbol, target_dir)
            return

        avg_entry, total_qty, held_dir = _get_filled_position(st)
        if total_qty > 0 and held_dir != target_dir:
            logger.warning(
                "SuperTrend %s→%s 전환 청산 후 반대 진입: %s (%s)",
                held_dir,
                target_dir,
                symbol,
                reason,
                extra={
                    "event": "st_flip",
                    "symbol": symbol,
                    "from": held_dir,
                    "to": target_dir,
                },
            )
            pnl = _close_st_position_for_flip(symbol, st)
            if pnl is None:
                logger.error(
                    "ST 전환 청산 실패 — 상태 유지(재시도): %s",
                    symbol,
                    extra={"event": "st_flip_close_failed", "symbol": symbol},
                )
                return
            state.position_state.pop(symbol, None)
            state.save_position_state()
            _record_st_trade_pnl(symbol, pnl)
            if _is_st_halted(symbol):
                return
            if symbol in runtime.ST_TRACKED_SYMBOLS:
                _try_st_entry(symbol, target_dir)
        return

    if symbol not in runtime.ST_TRACKED_SYMBOLS and symbol not in runtime.QUALIFIED_WATCH:
        return

    logger.info(
        "SuperTrend %s 진입 시도: %s (%s)",
        target_dir,
        symbol,
        reason,
        extra={"event": "st_entry_attempt", "symbol": symbol, "direction": target_dir},
    )
    _try_st_entry(symbol, target_dir)


def monitor_loop() -> None:
    st_mode = (
        f"ON (롱/숏 추종, 연속손실정지={config.ST_MAX_CONSECUTIVE_LOSSES})"
        if config.USE_SUPERTREND_ENTRY
        else "OFF"
    )
    entry_mode = "시장가" if config.USE_MARKET_ENTRY else "지정가"
    logger.info(
        "Bybit USDT 선물 (24h +%s%% → ST, %s 진입, 동시최대 %s개, ST추적최대 %s개, 미체결 %s일 취소, 연속손실 %s회 정지) %s",
        config.GAINER_THRESHOLD_PCT,
        entry_mode,
        config.MAX_CONCURRENT_ST_SYMBOLS,
        config.MAX_ST_TRACKED_SYMBOLS,
        config.OPEN_ORDER_MAX_AGE_DAYS,
        config.ST_MAX_CONSECUTIVE_LOSSES,
        st_mode,
    )
    while True:
        try:
            risers, top3 = get_24h_risers_and_top_movers()
            now_str = time.strftime("%H:%M:%S")
            riser_symbols = {g["symbol"] for g in risers}
            _sync_qualified_watch(risers)

            trade_symbols: set[str] = set(runtime.ST_TRACKED_SYMBOLS)
            trade_symbols.update(riser_symbols)
            for symbol in state.position_state:
                trade_symbols.add(symbol)

            stale_n = orders.cancel_stale_open_entry_orders(_bot_managed_stale_cancel_keys())
            if stale_n:
                _reconcile_stale_entries_in_state()
                state.save_qualified_watch()

            check_filled_and_refresh_tp()
            check_tp_filled_and_log()

            logger.info("%s [%s] 감시 중 %s", "-" * 20, now_str, "-" * 20)
            logger.info(
                "ST 대상: 24h+%s%% %s개 | ST 추적중 %s개 | 합계 %s개",
                config.GAINER_THRESHOLD_PCT,
                len(risers),
                len(runtime.ST_TRACKED_SYMBOLS),
                len(trade_symbols),
                extra={
                    "event": "st_universe",
                    "riser_count": len(risers),
                    "tracked_count": len(runtime.ST_TRACKED_SYMBOLS),
                },
            )

            if not trade_symbols:
                logger.info(
                    "24h +%s%%·ST 추적 종목 없음 -> 상승률 TOP 3 표시",
                    config.GAINER_THRESHOLD_PCT,
                    extra={"event": "no_qualified_symbols_fallback"},
                )
                for i, g in enumerate(top3, start=1):
                    symbol = g["symbol"]
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    logger.info(
                        "TOP%s. %s | price: %.4f | change: %.2f%%",
                        i,
                        symbol,
                        g["last_price"],
                        g["change_pct"],
                        extra={
                            "event": "top_movers_fallback",
                            "symbol": symbol,
                            "rank": i,
                            "change_pct": str(g["change_pct"]),
                        },
                    )
            else:
                riser_rank = {g["symbol"]: i for i, g in enumerate(risers, start=1)}
                for symbol in sorted(trade_symbols):
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    watch_tag = ""
                    if _is_st_halted(symbol):
                        watch_tag = " [ST정지]"
                    elif symbol in runtime.ST_TRACKED_SYMBOLS:
                        watch_tag = " [ST추적]"
                    elif symbol in runtime.QUALIFIED_WATCH:
                        watch_tag = " [ST후보]"

                    g = next((x for x in risers if x["symbol"] == symbol), None)
                    if g is not None:
                        logger.info(
                            "%s. %s%s | price: %.4f | change: %.2f%%",
                            riser_rank.get(symbol, "?"),
                            symbol,
                            watch_tag,
                            g["last_price"],
                            g["change_pct"],
                            extra={
                                "event": "st_symbol_riser",
                                "symbol": symbol,
                                "change_pct": str(g["change_pct"]),
                            },
                        )
                    else:
                        logger.info(
                            "—. %s%s | (24h +%s%% 이탈, ST 추적 유지)",
                            symbol,
                            watch_tag,
                            config.GAINER_THRESHOLD_PCT,
                            extra={"event": "st_symbol_tracked_only", "symbol": symbol},
                        )

                    if _is_st_halted(symbol):
                        continue

                    _process_supertrend_symbol(symbol)

        except KeyboardInterrupt:
            logger.info("사용자 중단 (Ctrl+C). 종료.")
            break
        except Exception as e:
            logger.exception("루프 오류: %s", e)
        time.sleep(config.POLL_INTERVAL_SEC)
