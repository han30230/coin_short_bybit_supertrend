import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from coin_rising_short import client, config, indicators, market_cap, market_data, orders, runtime, state, symbols, trade_journal

logger = logging.getLogger(__name__)


def _get_funding_rate_map() -> Dict[str, Decimal]:
    tickers = client.get_linear_tickers()
    out: Dict[str, Decimal] = {}
    for row in tickers:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        try:
            out[symbol] = Decimal(str(row.get("fundingRate", "0")))
        except Exception:
            continue
    return out


def get_futures_gainers_and_top_movers(
    funding_rate_map: Dict[str, Decimal],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    market_cap.log_mcap_filter_status_once()

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
                "funding_rate": funding_rate_map.get(symbol, Decimal("0")),
            }
            all_movers.append(row)
            passed_basic = (
                change_pct >= config.GAINER_THRESHOLD_PCT
                and turnover_24h >= config.MIN_VOLUME_USDT
                and row["funding_rate"] > config.MIN_FUNDING_RATE
            )
            if not passed_basic:
                continue

            if config.MCAP_FILTER_ENABLED:
                mcap_usd = market_cap.get_market_cap_usd(symbol)
                if mcap_usd is None:
                    logger.warning(
                        "시가총액 조회 실패로 진입 후보 제외: symbol=%s",
                        symbol,
                        extra={"event": "mcap_skip_fetch_failed", "symbol": symbol},
                    )
                    continue
                if mcap_usd < config.MIN_MARKET_CAP_USD:
                    logger.info(
                        "최소 시가총액 미달로 진입 후보 제외: symbol=%s cap_usd=%s min_usd=%s",
                        symbol,
                        mcap_usd,
                        config.MIN_MARKET_CAP_USD,
                        extra={
                            "event": "mcap_skip_below_min",
                            "symbol": symbol,
                            "market_cap_usd": str(mcap_usd),
                            "min_market_cap_usd": str(config.MIN_MARKET_CAP_USD),
                        },
                    )
                    continue
                row["market_cap_usd"] = mcap_usd

            qualified.append(row)
        except Exception:
            continue

    qualified.sort(key=lambda x: x["change_pct"], reverse=True)

    qualified = market_data.filter_by_mcap_fdv(qualified)

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


def _refresh_symbol_take_profit(symbol: str, st: Dict[str, Any]) -> bool:
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
        tp_status = orders.get_order_status(symbol, int(existing_tp_oid))
        if tp_status == "FILLED":
            return False
        if tp_status in ("NEW", "PARTIALLY_FILLED") and old_avg == target_avg and old_qty == target_qty:
            return False
        need_replace = True

    if need_replace and existing_tp_oid:
        if not orders.cancel_order(symbol, int(existing_tp_oid)):
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
                dirty = True
                symbol_dirty = True
                continue

            if status == "FILLED":
                logger.info(
                    "진입 체결 확인: %s %s (orderId=%s) -> TP 주문 생성 시도",
                    symbol,
                    direction,
                    order_id,
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
        tp_oid = st.get("tp_order_id")
        if not tp_oid or st.get("tp_exit_logged"):
            continue
        tp_detail = orders.get_order_detail(symbol, int(tp_oid))
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
            tp_order_id=int(tp_oid),
            entry_price=entry_price,
            exit_price=exit_price,
            qty=exit_qty,
            entry_time_ms=None,
            exit_time_ms=tp_detail.get("updateTime") if isinstance(tp_detail.get("updateTime"), int) else None,
            note="평균진입가 기준 TP 청산",
        )
        if config.USE_SUPERTREND_ENTRY and st.get("st_mode"):
            _record_st_trade_pnl(symbol, pnl)
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
            extra={"event": "tp_filled_logged", "symbol": symbol, "order_id": int(tp_oid)},
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


def _passes_entry_prefilters(symbol: str) -> Tuple[bool, str]:
    if config.USE_ENTRY_INDICATOR_FILTER:
        return indicators.allow_initial_short(symbol)
    return True, "ok"


def _is_st_halted(symbol: str) -> bool:
    return symbol in runtime.ST_HALTED_SYMBOLS


def _record_st_trade_pnl(symbol: str, pnl: Decimal) -> None:
    watch = runtime.QUALIFIED_WATCH.get(symbol, {})
    if pnl < 0:
        losses = int(watch.get("consecutive_losses", 0)) + 1
    else:
        losses = 0
    if symbol in runtime.QUALIFIED_WATCH:
        runtime.QUALIFIED_WATCH[symbol]["consecutive_losses"] = losses
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
    runtime.QUALIFIED_WATCH.pop(symbol, None)
    state.save_qualified_watch()
    logger.warning(
        "SuperTrend 연속 %s회 손실 — %s 매매 정지",
        losses,
        symbol,
        extra={"event": "st_symbol_halted", "symbol": symbol, "consecutive_losses": losses},
    )


def _sync_qualified_watch(gainers: List[Dict[str, Any]]) -> set[str]:
    """급등 후보(상위) 중 1차 지표 통과 종목을 SuperTrend 방향 매매 감시에 반영."""
    now = time.time()
    active: set[str] = set()
    for g in gainers[:10]:
        symbol = g["symbol"]
        if _is_st_halted(symbol):
            continue
        until = runtime.SKIP_UNTIL.get(symbol, 0)
        if until and int(now) < until:
            continue
        if symbol in state.position_state:
            active.add(symbol)
            continue
        ok, reason = _passes_entry_prefilters(symbol)
        if not ok:
            continue
        active.add(symbol)
        if symbol not in runtime.QUALIFIED_WATCH:
            runtime.QUALIFIED_WATCH[symbol] = {
                "added_at": now,
                "last_direction": None,
                "consecutive_losses": 0,
                "halted": False,
            }
            logger.info(
                "SuperTrend 방향 매매 감시 등록: %s",
                symbol,
                extra={"event": "supertrend_watch_added", "symbol": symbol},
            )

    for symbol in list(runtime.QUALIFIED_WATCH.keys()):
        if symbol not in active:
            runtime.QUALIFIED_WATCH.pop(symbol, None)
            logger.info(
                "SuperTrend 감시 해제(조건 이탈): %s",
                symbol,
                extra={"event": "supertrend_watch_removed", "symbol": symbol},
            )
    if config.USE_SUPERTREND_ENTRY:
        state.save_qualified_watch()
    return active


def _new_st_position_state(
    symbol: str, direction: str, entry: Tuple[Decimal, Decimal, int]
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


def _record_initial_short_entry(symbol: str, entry: Tuple[Decimal, Decimal, int]) -> None:
    state.position_state[symbol] = _new_st_position_state(symbol, "SHORT", entry)
    state.position_state[symbol].pop("st_mode", None)
    state.position_state[symbol].pop("st_signal_direction", None)
    logger.info(
        "%s 첫 진입 기록: entry_price=%s, orderId=%s, qty=%s",
        symbol,
        entry[0],
        entry[2],
        entry[1],
        extra={
            "event": "entry_recorded",
            "symbol": symbol,
            "entry_price": str(entry[0]),
            "order_id": entry[2],
            "qty": str(entry[1]),
        },
    )
    state.save_position_state()


def _record_st_entry(symbol: str, direction: str, entry: Tuple[Decimal, Decimal, int]) -> None:
    state.position_state[symbol] = _new_st_position_state(symbol, direction, entry)
    watch = runtime.QUALIFIED_WATCH.get(symbol)
    if watch is not None:
        st_dir = 1 if direction == "LONG" else -1
        watch["last_direction"] = st_dir
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
        orders.cancel_order(symbol, int(tp_oid))
        st["tp_order_id"] = None

    avg_entry, total_qty, direction = _get_filled_position(st)
    if total_qty <= 0:
        return None

    result = orders.close_position_market(symbol, direction, total_qty)
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
    return pnl


def _try_st_entry(symbol: str, direction: str) -> None:
    if direction == "LONG":
        entry = orders.place_long_order(symbol)
    else:
        entry = orders.place_short_order(symbol)
    if entry is not None:
        _record_st_entry(symbol, direction, entry)


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
                    orders.cancel_order(symbol, int(entry["order_id"]))
                    entry["closed"] = True
                state.position_state.pop(symbol, None)
                state.save_position_state()
                if symbol in runtime.QUALIFIED_WATCH:
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
            state.position_state.pop(symbol, None)
            state.save_position_state()
            if pnl is not None:
                _record_st_trade_pnl(symbol, pnl)
            if _is_st_halted(symbol):
                return
            if symbol in runtime.QUALIFIED_WATCH:
                _try_st_entry(symbol, target_dir)
        return

    if symbol not in runtime.QUALIFIED_WATCH:
        return

    logger.info(
        "SuperTrend %s 진입 시도: %s (%s)",
        target_dir,
        symbol,
        reason,
        extra={"event": "st_entry_attempt", "symbol": symbol, "direction": target_dir},
    )
    _try_st_entry(symbol, target_dir)


def _try_initial_short_entry(symbol: str) -> None:
    ok, reason = _passes_entry_prefilters(symbol)
    if not ok:
        logger.info(
            "지표 필터로 신규 진입 스킵: %s reason=%s",
            symbol,
            reason,
            extra={
                "event": "initial_entry_indicator_skipped",
                "symbol": symbol,
                "reason": reason,
            },
        )
        return

    entry = orders.place_short_order(symbol)
    if entry is not None:
        _record_initial_short_entry(symbol, entry)


def monitor_loop() -> None:
    st_mode = (
        f"ON (롱/숏 추종, 연속손실정지={config.ST_MAX_CONSECUTIVE_LOSSES})"
        if config.USE_SUPERTREND_ENTRY
        else "OFF"
    )
    logger.info(
        "Bybit 선물 급등 종목 감시 시작 (스팟+선물 공존, SuperTrend=%s)...",
        st_mode,
    )
    while True:
        try:
            funding_rate_map = _get_funding_rate_map()
            gainers, top3 = get_futures_gainers_and_top_movers(funding_rate_map)
            now_str = time.strftime("%H:%M:%S")

            logger.info("%s [%s] 감시 중 %s", "-" * 20, now_str, "-" * 20)
            if not gainers:
                logger.info(
                    "조건에 맞는 종목 없음 -> 전체 상승률 TOP 3 표시",
                    extra={"event": "no_qualified_symbols_fallback"},
                )
                for i, g in enumerate(top3, start=1):
                    symbol = g["symbol"]
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    current_price = g["last_price"]
                    change_pct = g["change_pct"]
                    turnover_24h = g.get("turnover_24h", Decimal("0"))
                    funding_rate = g.get("funding_rate", Decimal("0"))
                    logger.info(
                        "TOP%s. %s | price: %.4f | change: %.2f%% | volume(24h): %s | funding: %s",
                        i,
                        symbol,
                        current_price,
                        change_pct,
                        turnover_24h,
                        funding_rate,
                        extra={
                            "event": "top_movers_fallback",
                            "symbol": symbol,
                            "rank": i,
                            "last_price": str(current_price),
                            "change_pct": str(change_pct),
                            "turnover_24h": str(turnover_24h),
                            "funding_rate": str(funding_rate),
                        },
                    )
            else:
                _sync_qualified_watch(gainers)

                for i, g in enumerate(gainers[:10], start=1):
                    symbol = g["symbol"]
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    current_price = g["last_price"]
                    change_pct = g["change_pct"]
                    funding_rate = g.get("funding_rate", Decimal("0"))
                    watch_tag = ""
                    if _is_st_halted(symbol):
                        watch_tag = " [ST정지]"
                    elif symbol in runtime.QUALIFIED_WATCH:
                        watch_tag = " [ST감시]"

                    logger.info(
                        "%s. %s%s | price: %.4f | change: %.2f%% | funding: %s",
                        i,
                        symbol,
                        watch_tag,
                        current_price,
                        change_pct,
                        funding_rate,
                        extra={
                            "event": "gainer_ranked",
                            "symbol": symbol,
                            "rank": i,
                            "last_price": str(current_price),
                            "change_pct": str(change_pct),
                            "funding_rate": str(funding_rate),
                            "supertrend_watch": symbol in runtime.QUALIFIED_WATCH,
                        },
                    )

                    if _is_st_halted(symbol):
                        continue

                    if config.USE_SUPERTREND_ENTRY:
                        _process_supertrend_symbol(symbol)
                        if symbol in state.position_state and state.position_state[symbol].get(
                            "st_mode"
                        ):
                            continue

                    if symbol not in state.position_state:
                        if not config.USE_SUPERTREND_ENTRY:
                            _try_initial_short_entry(symbol)
                        continue

                    if config.USE_SUPERTREND_ENTRY:
                        continue

                    st = state.position_state[symbol]
                    reentry_count = int(st.get("reentry_count", 0))
                    if reentry_count < config.REENTRY_MAX_COUNT:
                        base_reentry_price = Decimal(str(st.get("last_reentry_price", st["entry_price"])))
                        target_price = base_reentry_price * (
                            Decimal("1") + config.REENTRY_RISE_PCT / Decimal("100")
                        )
                        if current_price >= target_price:
                            logger.warning(
                                "%s 직전 재진입가 대비 +%s%% 이상! 추가 숏 재진입 시도... (%s/%s)",
                                symbol,
                                config.REENTRY_RISE_PCT,
                                reentry_count + 1,
                                config.REENTRY_MAX_COUNT,
                            )
                            if config.USE_REENTRY_INDICATOR_FILTER:
                                ok, reason = indicators.allow_reentry_short(symbol)
                                if not ok:
                                    logger.info(
                                        "지표 필터로 재진입 보류: %s reason=%s",
                                        symbol,
                                        reason,
                                        extra={
                                            "event": "reentry_indicator_skipped",
                                            "symbol": symbol,
                                            "reason": reason,
                                        },
                                    )
                                    continue
                            short_entry = orders.place_short_order(symbol)
                            if short_entry:
                                se_price, se_qty, se_id = short_entry
                                st.setdefault("entries", []).append(
                                    {
                                        "direction": "SHORT",
                                        "entry_price": se_price,
                                        "qty": se_qty,
                                        "order_id": se_id,
                                        "filled": False,
                                    }
                                )
                                st["reentry_count"] = reentry_count + 1
                                st["last_reentry_price"] = se_price
                                logger.info(
                                    "%s 재진입 숏 기록: price=%s, orderId=%s, qty=%s",
                                    symbol,
                                    se_price,
                                    se_id,
                                    se_qty,
                                    extra={
                                        "event": "reentry_recorded",
                                        "symbol": symbol,
                                        "entry_price": str(se_price),
                                        "order_id": se_id,
                                        "qty": str(se_qty),
                                    },
                                )
                                state.save_position_state()

            if config.USE_SUPERTREND_ENTRY and gainers:
                ranked = {g["symbol"] for g in gainers[:10]}
                for symbol, st in list(state.position_state.items()):
                    if symbol in ranked or not st.get("st_mode") or _is_st_halted(symbol):
                        continue
                    _process_supertrend_symbol(symbol)

            check_filled_and_refresh_tp()
            check_tp_filled_and_log()

        except KeyboardInterrupt:
            logger.info("사용자 중단 (Ctrl+C). 종료.")
            break
        except Exception as e:
            logger.exception("루프 오류: %s", e)
        time.sleep(config.POLL_INTERVAL_SEC)
