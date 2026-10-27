"""Optional, best-effort Telegram alerts. Never used to place trading orders."""

import argparse
import logging
import itertools
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)
_notifier = None
_handler = None
_KST = timezone(timedelta(hours=9))


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, bot_name: str,
                 fill_lookup: Optional[Callable] = None, *,
                 start_worker: bool = True, queue_size: int = 200):
        self.token = token
        self.chat_id = chat_id
        self.bot_name = bot_name[:100]
        self.fill_lookup = fill_lookup
        self.jobs = queue.PriorityQueue(maxsize=queue_size)
        self.sequence = itertools.count()
        self.seen = OrderedDict()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self._run, name='telegram-alerts', daemon=True)
            self.worker.start()

    def _header(self, title: str) -> str:
        stamp = datetime.now(_KST).strftime('%Y-%m-%d %H:%M:%S KST')
        return f'[{self.bot_name}] {title}\n{stamp}'

    def _put(self, job: dict, priority: int = 0) -> bool:
        if self.stopping.is_set():
            return False
        try:
            self.jobs.put_nowait((priority, next(self.sequence), job))
            return True
        except queue.Full:
            logger.warning('Telegram queue full; alert dropped (trading continues)')
            return False

    def enqueue_text(self, text: str, priority: int = 0) -> bool:
        return self._put({'text': text}, priority)

    def enqueue_fill(self, kind: str, symbol: str, direction: str, order_id,
                     entry_price: Decimal, note: str = '') -> bool:
        oid = str(order_id or '')
        if not oid or oid in ('0', 'EXCHANGE_SYNC'):
            return False
        key = (kind, symbol, oid)
        with self.lock:
            if key in self.seen:
                return False
            accepted = self._put(dict(kind=kind, symbol=symbol, direction=direction,
                                      order_id=oid, entry_price=entry_price, note=note))
            if accepted:
                self.seen[key] = None
                if len(self.seen) > 2048:
                    self.seen.popitem(last=False)
            return accepted

    def _fill_message(self, job: dict) -> str:
        detail = None
        for attempt in range(3):
            try:
                detail = self.fill_lookup(job['symbol'], job['order_id']) if self.fill_lookup else None
            except Exception:
                detail = None
            if detail and detail.get('status') == 'FILLED':
                break
            if attempt < 2:
                time.sleep(0.5)
        detail = detail or {}
        status = detail.get('status', 'UNKNOWN')
        base = f"\n종목: {job['symbol']} | {job['direction']}\n주문 ID: {job['order_id']}"
        try:
            price = Decimal(str(detail.get('avgPrice') or '0'))
            qty = Decimal(str(detail.get('executedQty') or '0'))
            valid = price.is_finite() and qty.is_finite() and price > 0 and qty > 0
        except Exception:
            valid = False
        if not valid or status not in ('FILLED', 'PARTIALLY_FILLED'):
            return self._header('체결 확인 필요') + base + f'\n주문 상태: {status}\n거래소에서 직접 확인하세요.'
        title = '진입 체결' if job['kind'] == 'entry' else '청산 체결'
        if status == 'PARTIALLY_FILLED':
            title = '진입 부분 체결' if job['kind'] == 'entry' else '청산 부분 체결'
        message = self._header(title) + base + f'\n체결가: {price}\n체결 수량: {qty}\n명목금액: {price * qty} USDT'
        if job['kind'] == 'exit':
            entry = Decimal(str(job['entry_price']))
            if entry.is_finite() and entry > 0:
                pnl = (entry - price) * qty if job['direction'].upper() == 'SHORT' else (price - entry) * qty
                pct = pnl / (entry * qty) * 100
                message += f'\n가격 손익: {pnl:+.4f} USDT ({pct:+.2f}%, 수수료·펀딩 제외)'
        if job.get('note'):
            message += f"\n사유: {job['note']}"
        return message

    def _send(self, text: str) -> bool:
        try:
            # Never log the response body or exception: the URL contains the bot token.
            response = requests.post(
                f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat_id, 'text': text[:4000]},
                timeout=(3.05, 5),
            )
            if response.status_code == 200 and response.json().get('ok') is True:
                return True
            logger.warning('Telegram delivery failed (HTTP %s); trading continues', response.status_code)
        except Exception:
            logger.warning('Telegram delivery failed; trading continues')
        return False

    def process_one(self) -> bool:
        try:
            _, _, job = self.jobs.get_nowait()
        except queue.Empty:
            return False
        try:
            text = job['text'] if 'text' in job else self._fill_message(job)
            self._send(text)
        except Exception:
            logger.warning('Telegram alert processing failed; trading continues')
        finally:
            self.jobs.task_done()
        return True

    def _run(self) -> None:
        while not self.stopping.is_set() or not self.jobs.empty():
            if not self.process_one():
                self.stopping.wait(0.1)

    def close(self, timeout: float = 20) -> None:
        self.stopping.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=timeout)


class TelegramLogHandler(logging.Handler):
    _IMPORTANT = {
        'startup': '봇 시작 (초기화 중)',
        'bot_ready': '봇 초기화 완료',
        'order_place_failed': '주문 접수 실패',
        'market_close_failed': '청산 주문 실패',
        'tp_place_failed': '익절 주문 실패 — 포지션 확인 필요',
        'st_symbol_halted': '종목 매매 중단 (연속 손실)',
    }

    def __init__(self, notifier: TelegramNotifier):
        super().__init__(logging.INFO)
        self.notifier = notifier
        self.last_sent = OrderedDict()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == __name__ or not record.name.startswith('coin_rising_short'):
                return
            event = str(getattr(record, 'event', 'log'))
            if event not in self._IMPORTANT and record.levelno < logging.ERROR:
                return
            symbol = str(getattr(record, 'symbol', ''))
            key = (record.name, event, symbol)
            now = time.monotonic()
            if now - self.last_sent.get(key, float('-inf')) < 300:
                return
            title = self._IMPORTANT.get(event, '봇 오류 — 로그 확인 필요')
            text = self.notifier._header(title)
            # Only structured identifiers are forwarded; raw messages/tracebacks may contain keys.
            safe_event = re.sub(r'[^a-zA-Z0-9_.-]', '', event)[:100]
            text += f'\n이벤트: {safe_event}'
            if symbol:
                text += '\n종목: ' + re.sub(r'[^a-zA-Z0-9_.-]', '', symbol)[:40]
            if self.notifier.enqueue_text(text, priority=-1 if record.levelno >= logging.ERROR else 0):
                self.last_sent[key] = now
                if len(self.last_sent) > 256:
                    self.last_sent.popitem(last=False)
        except Exception:
            # Logging must never break trading or recursively log notification failures.
            pass


def configure(default_name: str, fill_lookup: Optional[Callable] = None):
    global _notifier, _handler
    if _notifier is not None:
        return _notifier
    if os.getenv('TELEGRAM_ENABLED', 'false').lower() != 'true':
        return None
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        logger.warning('Telegram disabled: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing')
        return None
    name = os.getenv('TELEGRAM_BOT_NAME', '').strip() or default_name
    account = os.getenv('BOT_ACCOUNT', '').strip()
    if account:
        name += f'/{account}'
    _notifier = TelegramNotifier(token, chat_id, name, fill_lookup)
    _handler = TelegramLogHandler(_notifier)
    logging.getLogger().addHandler(_handler)
    return _notifier


def notify_fill(kind: str, symbol: str, direction: str, order_id,
                entry_price: Decimal, note: str = '') -> None:
    try:
        if _notifier is not None:
            _notifier.enqueue_fill(kind, symbol, direction, order_id, entry_price, note)
    except Exception:
        logger.warning('Telegram alert enqueue failed; trading continues')


def shutdown(timeout: float = 20) -> None:
    global _notifier, _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler = None
    if _notifier is not None:
        _notifier.close(timeout)
        _notifier = None


def main() -> int:
    """Setup helpers; no exchange keys required and no trading APIs called."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    parser = argparse.ArgumentParser(description='Telegram setup (does not run trading)')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--test', action='store_true')
    group.add_argument('--chat-id', action='store_true')
    args = parser.parse_args()
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token:
        print('Set TELEGRAM_BOT_TOKEN in .env first.')
        return 1
    if args.chat_id:
        try:
            response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=(3.05, 5))
            data = response.json()
            if response.status_code != 200 or not data.get('ok'):
                print('Telegram lookup failed. Check token and network.')
                return 1
            ids = {str(item['message']['chat']['id']) for item in data.get('result', []) if 'message' in item}
            print('\n'.join(sorted(ids)) if ids else 'Send /start to your bot, then retry. Existing webhooks/pollers may consume updates.')
            return 0
        except Exception:
            print('Telegram lookup failed. Check token and network.')
            return 1
    if not chat_id:
        print('Set TELEGRAM_CHAT_ID in .env first.')
        return 1
    notifier = TelegramNotifier(token, chat_id, os.getenv('TELEGRAM_BOT_NAME') or 'Trading bot', start_worker=False)
    ok = notifier._send(notifier._header('테스트 알림 — 매매 주문 없음'))
    print('Telegram test sent.' if ok else 'Telegram test failed. Check token, chat ID and network.')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
