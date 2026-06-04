import logging
import os
import json
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from coin_rising_short import client, config, monitor, orders, runtime, state, sync, symbols


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


def run() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)
    logger.info(
        "Bybit Linear + Spot 공존 필터 버전 시작 (ENV=%s)",
        config.ENV,
        extra={"event": "startup"},
    )
    client.refresh_time_offset()
    logger.info(
        "서버 시간 오프셋 동기화 완료 (LEVERAGE=%sx)",
        config.LEVERAGE,
        extra={"event": "time_sync_done"},
    )

    symbols.init_trading_symbols(max_retries=3)

    if config.FORCE_HEDGE:
        orders.set_dual_side_position(True)
    runtime.IS_HEDGE = orders.get_dual_side_position()
    logger.info("Hedge mode?: %s", runtime.IS_HEDGE, extra={"event": "hedge_mode_checked"})

    state.load_qualified_watch()
    sync.sync_state_with_exchange()
    monitor.monitor_loop()


if __name__ == "__main__":
    run()
