# Coin Auto Trading (Bybit)

Bybit USDT 무기한 선물(Linear) 급등 숏 전략 봇입니다. 업비트 상장·선물+스팟 공존·상장 경과일·펀딩비·지표·시가총액 필터를 지원합니다.

## Environment Variables

`.env` 파일을 프로젝트 루트에 두고 아래 키를 설정합니다.

- 필수
  - `BYBIT_API_KEY_SH` / `BYBIT_SECRET_SH` (또는 `BYBIT_API_KEY` / `BYBIT_SECRET`)
- 선택
  - `BYBIT_ENV` (`mainnet` 또는 `testnet`, 기본값: `mainnet`)
  - `BYBIT_RECV_WINDOW` (기본값: `8000`)
  - `LEVERAGE` (기본값: `5`)
  - `FORCE_HEDGE` (기본값: `true`, Bybit 양방향(헷지) 모드)
  - `FILTER_UPBIT_LISTED`, `MIN_FUTURES_LISTING_AGE_DAYS`, `MIN_FUNDING_RATE` 등 — `.env.example` 참고

예시는 `.env.example`을 참고하세요.

## Run

```bash
python Bybit_SH_1.py
```

(호환) `python Binance_SH_1.py` — 동일 진입점이 있으면 같은 봇을 실행합니다.

## Logging

- 로그 이벤트 규칙: `docs/logging_events.md`

## Trade Journal Migration

```bash
python migrate_trade_journal.py
```

## Telegram 알림

선택형 체결·청산·오류 알림과 설정 방법은 [Telegram 알림 설정](docs/telegram_notifications.md)을 참고하세요. 기본은 OFF입니다.
