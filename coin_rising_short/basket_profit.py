"""Restartable, scoped, reduce-only basket profit closing for the ST bot."""
import copy
import hashlib
import json
import logging
import os
import tempfile
import uuid
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from coin_rising_short import client, config, runtime, state, trade_journal

logger = logging.getLogger(__name__)
_controller = None
_process_lock = None
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'DEACTIVATED'}


def number(value, *, signed=False):
    value = Decimal(str(value))
    if not value.is_finite() or (not signed and value < 0):
        raise ValueError('invalid numeric exchange/state field')
    return value


def direction(side):
    if side == 'Buy':
        return 'LONG'
    if side == 'Sell':
        return 'SHORT'
    raise ValueError('invalid position side')


def key(symbol, side):
    if side not in ('LONG', 'SHORT') or not isinstance(symbol, str) or not symbol:
        raise ValueError('invalid owned position identity')
    return symbol + ':' + side


def status(row):
    raw = row.get('orderStatus', row.get('status', ''))
    if raw == 'PartiallyFilledCanceled':
        return 'CANCELED'
    return client.normalize_order_status(str(raw)).upper()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(state._sanitize_for_json(data), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Exchange:
    def _pages(self, path, params, *, missing_ok=False):
        rows, seen = [], set()
        while True:
            result = client.signed_get(path, params)
            if missing_ok and isinstance(result, dict) and result.get('_error') and result.get('retCode') == 110001 and not rows:
                return []
            if not isinstance(result, dict) or result.get('_error') or not isinstance(result.get('list'), list):
                raise RuntimeError('incomplete exchange snapshot')
            if any(not isinstance(row, dict) for row in result['list']):
                raise RuntimeError('malformed exchange snapshot')
            rows.extend(result['list'])
            cursor = result.get('nextPageCursor', '')
            if not cursor:
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                raise RuntimeError('exchange pagination did not progress')
            seen.add(cursor)
            params = dict(params, cursor=cursor)

    def positions(self):
        return self._pages('/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT', 'limit': 200})

    def open_orders(self):
        return self._pages('/v5/order/realtime', {'category': 'linear', 'settleCoin': 'USDT', 'openOnly': 0, 'limit': 50})

    def order(self, symbol, *, order_id=None, link_id=None):
        selector = {'orderId': order_id} if order_id else {'orderLinkId': link_id}
        for endpoint in ('/v5/order/realtime', '/v5/order/history'):
            rows = self._pages(endpoint, {'category': 'linear', 'symbol': symbol, **selector}, missing_ok=True)
            if rows:
                if len(rows) != 1:
                    raise RuntimeError('ambiguous order lookup')
                row = rows[0]
                field, value = ('orderId', order_id) if order_id else ('orderLinkId', link_id)
                if row.get('symbol') != symbol or str(row.get(field)) != str(value):
                    raise RuntimeError('order identity mismatch')
                return row
        return None

    def cancel(self, symbol, oid):
        result = client.signed_post('/v5/order/cancel', {'category': 'linear', 'symbol': symbol, 'orderId': oid})
        if not isinstance(result, dict) or result.get('_error') or not result.get('orderId'):
            raise RuntimeError('cancel not acknowledged; confirmation required')

    def lot(self, symbol):
        result = client.public_get('/v5/market/instruments-info', {'category': 'linear', 'symbol': symbol})
        rows = result.get('list') if isinstance(result, dict) else None
        if not rows or len(rows) != 1 or rows[0].get('symbol') != symbol:
            raise RuntimeError('missing lot limits')
        lot = rows[0]['lotSizeFilter']
        values = tuple(number(lot[name]) for name in ('qtyStep', 'minOrderQty', 'maxMktOrderQty'))
        if any(value <= 0 for value in values):
            raise RuntimeError('invalid lot limits')
        return values

    def close(self, symbol, side, idx, qty, link):
        result = client.signed_post('/v5/order/create', {
            'category': 'linear', 'symbol': symbol, 'side': 'Sell' if side == 'LONG' else 'Buy',
            'orderType': 'Market', 'qty': format(qty, 'f'), 'positionIdx': idx,
            'timeInForce': 'IOC', 'reduceOnly': True, 'closeOnTrigger': True, 'orderLinkId': link,
        })
        if not isinstance(result, dict) or result.get('_error') or not result.get('orderId'):
            raise RuntimeError('close not acknowledged; reconcile by persisted orderLinkId')


class Cycle:
    def __init__(self, path, exchange, *, clock=None, account):
        self.path, self.exchange = Path(path), exchange
        self.clock = clock or client.effective_timestamp_ms
        self.account = account
        self.fault = None
        self.blocked = True
        self.data = {'version': 1, 'account': account, 'phase': 'idle'}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                self._validate()
            except Exception:
                self.fault = 'cycle state unreadable or account mismatch; manual review required'

    @property
    def active(self):
        return bool(self.fault) or self.data.get('phase') != 'idle'

    def _validate(self):
        d = self.data
        if d.get('version') != 1 or d.get('account') != self.account or d.get('phase') not in ('idle', 'closing', 'resetting'):
            raise ValueError('invalid cycle state')
        if d['phase'] != 'idle':
            if not isinstance(d['cycle_id'], str) or len(d['cycle_id']) != 16:
                raise ValueError('invalid cycle identifier')
            for k, target in d['targets'].items():
                if k != key(target['symbol'], target['direction']) or target['idx'] not in (0, 1, 2):
                    raise ValueError('invalid saved target')
                for field in ('owned_qty', 'entry_price'):
                    number(target[field])
            if not isinstance(d['cancel_orders'], list) or not isinstance(d['jobs'], list):
                raise ValueError('invalid saved intents')
            identities = set()
            for item in d['cancel_orders']:
                identity = (item['symbol'], item['oid'])
                if identity in identities or not isinstance(item['oid'], str) or not item['oid']:
                    raise ValueError('invalid/duplicate saved cancel intent')
                identities.add(identity)
                if not any(t['symbol'] == item['symbol'] for t in d['targets'].values()):
                    raise ValueError('cancel intent outside owned scope')
                if item.get('entry'):
                    if d['targets'][item['key']]['symbol'] != item['symbol']:
                        raise ValueError('cancel/target identity mismatch')
                    number(item['counted'])
            links = set()
            for job in d['jobs']:
                if job['link'] in links or not isinstance(job.get('done'), bool):
                    raise ValueError('invalid/duplicate saved close intent')
                links.add(job['link'])
                if job['key'] not in d['targets'] or not job['link'].startswith('basket-' + d['cycle_id']):
                    raise ValueError('invalid saved close intent')
                qty, filled = number(job['qty']), number(job.get('filled', '0'))
                if qty <= 0 or filled > qty:
                    raise ValueError('invalid close intent quantity')

    def _save(self):
        self._validate()
        atomic_write(self.path, self.data)

    def _replace(self, data):
        previous, self.data = self.data, data
        try:
            self._save()
        except Exception:
            self.data = previous
            raise

    def _positions(self):
        result = {}
        for row in self.exchange.positions():
            qty = number(row['size'])
            if not qty:
                continue
            side = direction(row['side'])
            idx = int(row['positionIdx'])
            if idx not in (0, 1, 2) or (idx == 1 and side != 'LONG') or (idx == 2 and side != 'SHORT'):
                raise ValueError('position mode/side mismatch')
            k = key(row['symbol'], side)
            if k in result:
                raise ValueError('duplicate position in exchange snapshot')
            result[k] = dict(row, size=qty, idx=idx)
        return result

    def _no_foreign_entries(self, targets, open_orders, known):
        for row in open_orders:
            symbol = row['symbol']
            if not isinstance(row.get('reduceOnly'), bool):
                raise RuntimeError('opening order reduceOnly state unknown')
            if (symbol, str(row['orderId'])) in known or row.get('reduceOnly'):
                continue
            side = direction(row['side'])
            if key(symbol, side) in targets or (int(row['positionIdx']) == 0 and any(t['symbol'] == symbol for t in targets.values())):
                raise RuntimeError('foreign opening order overlaps managed position')

    def _plan(self):
        targets, cancels, known_entries = {}, [], set()
        for symbol, st in state.position_state.items():
            if not st.get('st_mode'):
                continue
            for entry in st.get('entries', []):
                oid = str(entry.get('order_id') or '')
                if (symbol, oid) in known_entries:
                    raise RuntimeError('duplicate entry ownership')
                known_entries.add((symbol, oid))
                if entry.get('closed'):
                    continue
                side = entry['direction']
                k = key(symbol, side)
                target = targets.setdefault(k, dict(symbol=symbol, direction=side, idx=None, owned_qty='0', entry_price='0'))
                if entry.get('filled'):
                    if not oid or oid == 'EXCHANGE_SYNC':
                        raise RuntimeError('adopted inventory has no verified bot entry identity')
                    qty = number(entry['qty'])
                    target['owned_qty'] = str(number(target['owned_qty']) + qty)
                else:
                    if not oid or oid == 'EXCHANGE_SYNC':
                        raise RuntimeError('pending entry lacks an exchange order ID')
                    cancels.append(dict(symbol=symbol, oid=oid, key=k, entry=True, counted='0', cost='0'))
            if st.get('tp_order_id'):
                cancels.append(dict(symbol=symbol, oid=str(st['tp_order_id']), entry=False))
        if runtime.BOT_ENTRY_ORDER_KEYS - known_entries:
            raise RuntimeError('unreconciled bot order ownership')
        positions = self._positions()
        open_orders = self.exchange.open_orders()
        for item in cancels:
            row = self.exchange.order(item['symbol'], order_id=item['oid'])
            if not row:
                raise RuntimeError('owned order state unknown')
            self._entry_fill(item, row, targets)
        total = Decimal('0')
        for k, target in targets.items():
            row = positions.get(k)
            owned = number(target['owned_qty'])
            actual = row['size'] if row else Decimal('0')
            if actual > owned:
                raise RuntimeError('managed/exchange quantity mismatch; cannot separate same-side inventory')
            # A manual reduction cannot increase what this bot is allowed to close.
            target['external_reduction'] = str(owned - actual)
            target['owned_qty'] = str(actual)
            if target['idx'] is None:
                target['idx'] = (1 if target['direction'] == 'LONG' else 2) if runtime.IS_HEDGE else 0
            if row:
                target['idx'] = row['idx']
                target['entry_price'] = str(number(row['avgPrice']))
                total += number(row['unrealisedPnl'], signed=True)
        known = {(i['symbol'], i['oid']) for i in cancels}
        self._no_foreign_entries(targets, open_orders, known)
        return total, targets, cancels

    def _entry_fill(self, item, row, targets):
        if not item.get('entry'):
            return
        t = targets[item['key']]
        if row.get('reduceOnly') is not False or direction(row['side']) != t['direction']:
            raise RuntimeError('entry ownership/side mismatch')
        qty = number(row.get('cumExecQty') or '0')
        previous = number(item['counted'])
        if qty < previous:
            raise RuntimeError('entry fill quantity went backwards')
        t['owned_qty'] = str(number(t['owned_qty']) + qty - previous)
        idx = int(row['positionIdx'])
        if idx not in (0, 1, 2) or (t['idx'] is not None and t['idx'] != idx):
            raise RuntimeError('entry mode changed')
        t['idx'] = idx
        item['counted'] = str(qty)

    def tick(self):
        self.blocked = True
        try:
            if self.fault:
                raise RuntimeError(self.fault)
            if self.active:
                self._save()  # Persist any prior interrupted in-memory update before side effects.
                if self.data['phase'] == 'resetting':
                    self._reset()
                else:
                    self._advance()
                return True
            if not config.BASKET_TP_ENABLED:
                self.blocked = False
                return False
            goal = number(config.BASKET_TP_TARGET_USDT)
            if goal <= 0:
                raise ValueError('profit goal must be positive')
            if not any(st.get('st_mode') for st in state.position_state.values()) and not runtime.BOT_ENTRY_ORDER_KEYS:
                self.blocked = False
                return False
            pnl, targets, cancels = self._plan()
            if pnl < goal or not any(number(t['owned_qty']) > 0 for t in targets.values()):
                self.blocked = False
                return False
            self._replace(dict(version=1, account=self.account, phase='closing', cycle_id=uuid.uuid4().hex[:16],
                               trigger_pnl=str(pnl), targets=targets, cancel_orders=cancels, jobs=[]))
            logger.info('Basket profit target reached: %s USDT', pnl,
                        extra={'event': 'basket_started', 'pnl': str(pnl), 'positions': len(targets), 'cycle_id': self.data['cycle_id']})
            return True
        except Exception as exc:
            logger.error('Basket cycle paused (%s): %s', type(exc).__name__, str(exc),
                         extra={'event': 'basket_error'})
            return self.active

    def _advance(self):
        d = self.data
        waiting = False
        for item in d['cancel_orders']:
            row = self.exchange.order(item['symbol'], order_id=item['oid'])
            if not row:
                raise RuntimeError('cancel status cannot be confirmed')
            self._entry_fill(item, row, d['targets'])
            if status(row) not in TERMINAL:
                self._save()
                self.exchange.cancel(item['symbol'], item['oid'])
                waiting = True
        self._save()
        if waiting:
            return
        # Each submission has a stable persisted link ID. An ambiguous timeout never gets a new ID.
        for job in d['jobs']:
            if job.get('done'):
                self._record_job(job)
                continue
            t = d['targets'][job['key']]
            row = self.exchange.order(t['symbol'], link_id=job['link'])
            if row is None:
                if self.clock() - job.get('last_submit_ms', 0) >= 30_000 or not job.get('last_submit_ms'):
                    job['last_submit_ms'] = self.clock()
                    self._save()
                    self.exchange.close(t['symbol'], t['direction'], t['idx'], number(job['qty']), job['link'])
                return
            if row.get('reduceOnly') is not True or int(row['positionIdx']) != t['idx'] or direction(row['side']) == t['direction']:
                raise RuntimeError('close intent identity mismatch')
            if status(row) not in TERMINAL:
                return
            filled = number(row.get('cumExecQty') or '0')
            if filled > number(job['qty']):
                raise RuntimeError('close executed beyond requested quantity')
            price = number(row.get('avgPrice') or '0')
            if filled and price <= 0:
                raise RuntimeError('filled close lacks actual price')
            job.update(done=True, filled=str(filled), price=str(price), oid=str(row['orderId']))
            self._save()
            self._record_job(job)
        positions = self._positions()
        opens = self.exchange.open_orders()
        known = {(i['symbol'], i['oid']) for i in d['cancel_orders']}
        known.update((d['targets'][j['key']]['symbol'], j.get('oid', '')) for j in d['jobs'])
        self._no_foreign_entries(d['targets'], opens, known)
        if any((o['symbol'], str(o['orderId'])) in known for o in opens):
            return
        submitted = False
        for k, target in d['targets'].items():
            filled = sum((number(j['filled']) for j in d['jobs'] if j['key'] == k and j.get('done')), Decimal('0'))
            remaining = number(target['owned_qty']) - filled
            if remaining < 0:
                raise RuntimeError('closed more than owned')
            row = positions.get(k)
            actual = row['size'] if row else Decimal('0')
            if actual != remaining:
                raise RuntimeError('residual quantity differs; wait for exchange or manual review')
            if actual == 0:
                continue
            if row['idx'] != target['idx']:
                raise RuntimeError('position mode changed during cycle')
            if not filled:
                target['entry_price'] = str(number(row['avgPrice']))
            step, minimum, maximum = self.exchange.lot(target['symbol'])
            qty = (min(actual, maximum) / step).to_integral_value(rounding=ROUND_DOWN) * step
            if qty < minimum or qty <= 0:
                raise RuntimeError('residual below market order minimum; cannot safely reset')
            job = dict(key=k, qty=str(qty), link='basket-' + d['cycle_id'] + '-' + str(len(d['jobs'])),
                       filled='0', done=False, last_submit_ms=self.clock())
            d['jobs'].append(job)
            self._save()
            self.exchange.close(target['symbol'], target['direction'], target['idx'], qty, job['link'])
            submitted = True
        if not submitted:
            next_state = copy.deepcopy(d)
            next_state['phase'] = 'resetting'
            self._replace(next_state)
            self._reset()

    def _record_job(self, job):
        if job.get('logged') or not number(job.get('filled', '0')):
            return
        t = self.data['targets'][job['key']]
        trade_journal.log_exit_filled(
            symbol=t['symbol'], direction=t['direction'], entry_order_id='BASKET',
            tp_order_id=job['oid'], entry_price=number(t['entry_price']),
            exit_price=number(job['price']), qty=number(job['filled']),
            entry_time_ms=None, exit_time_ms=None, note='합산 평가손익 목표 전체 청산',
        )
        job['logged'] = True
        self._save()

    def _reset(self):
        # This phase is persisted before touching ordinary state files, so a crash repeats cleanup.
        if any(number(p['size']) for k, p in self._positions().items() if k in self.data['targets']):
            raise RuntimeError('position reappeared during reset')
        known = {(i['symbol'], i['oid']) for i in self.data['cancel_orders']}
        if any((o['symbol'], str(o['orderId'])) in known for o in self.exchange.open_orders()):
            raise RuntimeError('owned order reappeared during reset')
        kept = {s: st for s, st in state.position_state.items() if not st.get('st_mode')}
        atomic_write(config.POSITION_STATE_PATH, kept)
        atomic_write(config.SUPERTREND_WATCH_STATE_PATH, {runtime._BOT_ORDERS_META_KEY: []})
        state.position_state.clear()
        state.position_state.update(kept)
        runtime.QUALIFIED_WATCH.clear()
        runtime.ST_TRACKED_SYMBOLS.clear()
        runtime.ST_HALTED_SYMBOLS.clear()
        runtime.BOT_ENTRY_ORDER_KEYS.clear()
        runtime.SKIP_UNTIL.clear()
        pnl = Decimal('0')
        for job in self.data['jobs']:
            t = self.data['targets'][job['key']]
            price, qty = number(job.get('price', '0')), number(job['filled'])
            entry = number(t['entry_price'])
            pnl += (price - entry if t['direction'] == 'LONG' else entry - price) * qty
        summary = dict(cycle_id=self.data['cycle_id'], trigger_pnl=self.data['trigger_pnl'], price_pnl=str(pnl),
                       completed_ms=self.clock(), positions=len(self.data['targets']))
        self._replace(dict(version=1, account=self.account, phase='idle', last_result=summary))
        logger.info('Basket closing complete; new cycle begins next poll. Price PnL=%s (fees/funding excluded)', pnl,
                    extra={'event': 'basket_completed', 'price_pnl': str(pnl), 'positions': summary['positions'], 'cycle_id': summary['cycle_id']})


class ProcessLock:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.path.getsize(path) == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.file.close()
            raise RuntimeError('another bot owns the cycle state lock')

    def close(self):
        self.file.close()


def initialize():
    global _controller, _process_lock
    if _controller is None:
        _process_lock = ProcessLock(config.BASKET_TP_STATE_FILE + '.lock')
        account = config.ENV + ':' + hashlib.sha256(config.API_KEY.encode()).hexdigest()
        _controller = Cycle(config.BASKET_TP_STATE_FILE, Exchange(), account=account)
    return _controller


def tick():
    return initialize().tick()


def is_active():
    return initialize().active


def entries_allowed():
    c = initialize()
    return not c.active and (not config.BASKET_TP_ENABLED or not c.blocked)


def managed_close_qty(symbol, side, local_qty):
    """Normal ST exits may reduce only independently tracked inventory, never an entire foreign side."""
    try:
        st = state.position_state.get(symbol, {})
        if any(e.get('filled') and not e.get('closed') and str(e.get('order_id', '')) in ('', 'EXCHANGE_SYNC')
               for e in st.get('entries', [])):
            raise RuntimeError('adopted inventory needs manual ownership confirmation')
        c = initialize()
        row = c._positions().get(key(symbol, side))
        qty = row['size'] if row else Decimal('0')
        if qty > local_qty:
            raise RuntimeError('foreign same-side inventory exceeds tracked quantity')
        return qty
    except Exception as exc:
        logger.error('ST close ownership unverified: %s', str(exc), extra={'event': 'basket_error', 'symbol': symbol})
        return None


def shutdown():
    global _controller, _process_lock
    if _process_lock is not None:
        _process_lock.close()
    _controller, _process_lock = None, None
