import logging
import os
import json
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from coin_rising_short import basket_profit, client, config, monitor, notifications, orders, runtime, state, sync, symbols


class JsonLineFormatter(logging.Formatter):
    _BASE_FIELDS = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "event": getattr(record, "event", "log"),
            "env": getattr(record, "env", config.ENV),
            "strategy": getattr(record, "strategy", "coin_rising_short"),
            "exchange": getattr(record, "exchange", "bybit_linear"),
        }
        for key, value in record.__dict__.items():
            if key not in self._BASE_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "env"):
            record.env = config.ENV
        if not hasattr(record, "strategy"):
            record.strategy = "coin_rising_short"
        if not hasattr(record, "exchange"):
            record.exchange = "bybit_linear"
        return True


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    formatter = logging.Formatter(log_format)
    root.setLevel(logging.INFO)
    context_filter = ContextFilter()
    root.addFilter(context_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(project_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "bot.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonLineFormatter())
    file_handler.addFilter(context_filter)
    root.addHandler(file_handler)


def _run_bot() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)
    logger.info(
        "Bybit USDT 선물 SuperTrend 봇 시작 (ENV=%s)",
        config.ENV,
        extra={"event": "startup"},
    )
    client.refresh_time_offset()
    logger.info(
        "서버 시간 오프셋 동기화 완료 (LEVERAGE=%sx)",
        config.LEVERAGE,
        extra={"event": "time_sync_done"},
    )

    try:
        symbols.init_trading_symbols(max_retries=3)
    except Exception:
        if not basket_profit.is_active():
            raise
        logger.error("심볼 조회 실패 — 진행 중 전체 청산을 먼저 재개", extra={"event": "basket_error"})

    if config.FORCE_HEDGE and not basket_profit.is_active():
        orders.set_dual_side_position(True)
    runtime.IS_HEDGE = orders.get_dual_side_position()
    logger.info("Hedge mode?: %s", runtime.IS_HEDGE, extra={"event": "hedge_mode_checked"})

    if config.BASKET_TP_ENABLED or basket_profit.is_active():
        # Do not let legacy sync inflate ownership or adopt watch-only foreign inventory.
        state.load_position_state()
        state.load_qualified_watch()
    else:
        sync.sync_state_with_exchange()
    logger.info("봇 초기화 완료", extra={"event": "bot_ready"})
    monitor.monitor_loop()


def run() -> None:
    _configure_logging()
    notifications.configure("Bybit ST 롱숏", orders.get_order_detail)
    try:
        basket_profit.initialize()
        _run_bot()
    except Exception:
        logging.getLogger(__name__).exception("봇 초기화/실행 실패", extra={"event": "bot_fatal"})
        raise
    finally:
        basket_profit.shutdown()
        notifications.shutdown()


if __name__ == "__main__":
    run()

