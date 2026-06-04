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
