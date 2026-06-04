"""실행 시점에 설정되는 값 (예: Hedge 모드)."""

IS_HEDGE = False

# 심볼별 일시 스킵(거래소 오픈 금지/점검 등)
# key: symbol, value: unix epoch seconds until which to skip
SKIP_UNTIL: dict[str, int] = {}

# 급등+지표 등 1차 진입 조건을 통과한 종목 (SuperTrend 방향 매매)
# symbol -> {"added_at": float, "last_direction": int | None, "consecutive_losses": int, "halted": bool}
QUALIFIED_WATCH: dict[str, dict] = {}

# SuperTrend 연속 손실 한도 초과로 영구 정지된 심볼
ST_HALTED_SYMBOLS: set[str] = set()

# 최초 ST 진입 후 24h +20% 이탈해도 ST 추종 매매 유지 (연속 손실 정지 전까지)
ST_TRACKED_SYMBOLS: set[str] = set()

# 이 봇이 넣은 진입 주문만 (symbol, order_id) — 타 전략 미체결 주문 취소 방지
BOT_ENTRY_ORDER_KEYS: set[tuple[str, str]] = set()

_BOT_ORDERS_META_KEY = "__bot_entry_orders__"
