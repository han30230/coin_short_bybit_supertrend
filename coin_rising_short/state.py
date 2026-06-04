import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict

from coin_rising_short import config, runtime

position_state: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    return obj


def _convert_loaded_state(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k in ("entry_price", "qty") and isinstance(v, (str, int, float)):
                out[k] = Decimal(str(v))
            else:
                out[k] = _convert_loaded_state(v)
        return out
    if isinstance(obj, list):
        return [_convert_loaded_state(x) for x in obj]
    return obj


def load_position_state() -> None:
    global position_state
    if not os.path.isfile(config.POSITION_STATE_PATH):
        position_state = {}
        return
    try:
        with open(config.POSITION_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            position_state = {}
            return
        position_state = _convert_loaded_state(raw)
    except Exception as e:
        logger.warning("상태 파일 로드 실패, 빈 상태로 시작: %s", e)
        position_state = {}


def save_position_state() -> None:
    try:
        path = config.POSITION_STATE_PATH
        tmp = path + ".tmp"
        data = _sanitize_for_json(position_state)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("상태 파일 저장 실패: %s", e)


def load_qualified_watch() -> None:
    """재시작 후에도 ST 감시 목록 복원. last_direction은 None으로 리셋해 4h ST 재평가."""
    path = config.SUPERTREND_WATCH_STATE_PATH
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return
        restored = 0
        bot_orders = raw.get(runtime._BOT_ORDERS_META_KEY)
        if isinstance(bot_orders, list):
            for pair in bot_orders:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    runtime.BOT_ENTRY_ORDER_KEYS.add((str(pair[0]), str(pair[1])))
        for symbol, entry in raw.items():
            if symbol == runtime._BOT_ORDERS_META_KEY:
                continue
            if not isinstance(entry, dict):
                continue
            if bool(entry.get("halted", False)):
                runtime.ST_HALTED_SYMBOLS.add(symbol)
                continue
            if bool(entry.get("st_tracked", False)):
                runtime.ST_TRACKED_SYMBOLS.add(symbol)
            runtime.QUALIFIED_WATCH[symbol] = {
                "added_at": float(entry.get("added_at", 0)),
                "last_direction": None,
                "consecutive_losses": int(entry.get("consecutive_losses", 0)),
                "halted": False,
                "last_flat_direction": entry.get("last_flat_direction"),
            }
            restored += 1
        if restored:
            logger.info(
                "SuperTrend 감시 목록 복원: %s개 (%s)",
                restored,
                path,
                extra={"event": "supertrend_watch_restored", "count": restored},
            )
    except Exception as e:
        logger.warning("ST 감시 파일 로드 실패: %s", e)


def save_qualified_watch() -> None:
    if not config.USE_SUPERTREND_ENTRY:
        return
    try:
        path = config.SUPERTREND_WATCH_STATE_PATH
        tmp = path + ".tmp"
        payload: Dict[str, Any] = {}
        for sym, entry in runtime.QUALIFIED_WATCH.items():
            if not isinstance(entry, dict):
                continue
            payload[sym] = {
                "added_at": entry.get("added_at", 0),
                "consecutive_losses": int(entry.get("consecutive_losses", 0)),
                "halted": False,
                "st_tracked": sym in runtime.ST_TRACKED_SYMBOLS,
                "last_flat_direction": entry.get("last_flat_direction"),
            }
        for sym in runtime.ST_HALTED_SYMBOLS:
            payload[sym] = {"halted": True, "consecutive_losses": config.ST_MAX_CONSECUTIVE_LOSSES}
        payload[runtime._BOT_ORDERS_META_KEY] = [
            [sym, oid] for sym, oid in sorted(runtime.BOT_ENTRY_ORDER_KEYS)
        ]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("ST 감시 파일 저장 실패: %s", e)
