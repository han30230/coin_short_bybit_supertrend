import copy
import importlib
import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


def position(symbol='BTCUSDT', direction='LONG', qty='1', pnl='100'):
    return dict(symbol=symbol, side='Buy' if direction == 'LONG' else 'Sell',
                positionIdx=1 if direction == 'LONG' else 2, size=qty,
                unrealisedPnl=pnl, avgPrice='100')


def order(oid, symbol='BTCUSDT', direction='LONG', status='New', filled='0', qty='1', price='100', reduce=False, link=''):
    return dict(orderId=oid, symbol=symbol, side='Buy' if direction == 'LONG' else 'Sell',
                positionIdx=1 if direction == 'LONG' else 2, orderStatus=status,
                cumExecQty=filled, qty=qty, avgPrice=price, reduceOnly=reduce, orderLinkId=link)


class Exchange:
    def __init__(self):
        self.rows = [position()]
        self.orders = {}
        self.actions = []
        self.auto_fill = False
        self.cancel_immediate = True
        self.timeout_after_accept = False
        self.now = 100000

    def positions(self):
        return copy.deepcopy(self.rows)

    def open_orders(self):
        return [copy.deepcopy(o) for o in self.orders.values() if o['orderStatus'] in ('New', 'PartiallyFilled')]

    def order(self, symbol, *, order_id=None, link_id=None):
        for o in self.orders.values():
            if o['symbol'] == symbol and (o['orderId'] == order_id if order_id else o['orderLinkId'] == link_id):
                return copy.deepcopy(o)
        return None

    def cancel(self, symbol, oid):
        self.actions.append(('cancel', symbol, oid))
        if self.cancel_immediate:
            self.orders[oid]['orderStatus'] = 'Cancelled'

    def lot(self, symbol):
        return Decimal('.01'), Decimal('.01'), Decimal('100')

    def close(self, symbol, direction, idx, qty, link):
        if any(o['orderLinkId'] == link for o in self.orders.values()):
            return
        oid = str(len(self.orders) + 100)
        self.actions.append(('close', symbol, direction, str(qty), link))
        opposite = 'SHORT' if direction == 'LONG' else 'LONG'
        o = order(oid, symbol, opposite, qty=str(qty), reduce=True, link=link)
        o['positionIdx'] = idx
        self.orders[oid] = o
        if self.auto_fill:
            self.fill(oid, str(qty))
        if self.timeout_after_accept:
            raise TimeoutError('response lost after exchange acceptance')

    def fill(self, oid, qty, status='Filled'):
        o = self.orders[oid]
        delta = Decimal(qty) - Decimal(o['cumExecQty'])
        o.update(cumExecQty=qty, avgPrice='105', orderStatus=status)
        for p in self.rows:
            if p['symbol'] == o['symbol'] and p['positionIdx'] == o['positionIdx']:
                p['size'] = str(Decimal(p['size']) - delta)


class TestBasketProfit(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('coin_rising_short.basket_profit'), 'Basket profit controller missing')
        self.mod = importlib.import_module('coin_rising_short.basket_profit')
        from coin_rising_short import config, runtime, state
        self.config, self.runtime, self.state = config, runtime, state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'cycle.json'
        self.ex = Exchange()
        for p in [patch.object(config, 'BASKET_TP_ENABLED', True),
                  patch.object(config, 'BASKET_TP_TARGET_USDT', Decimal('100')),
                  patch.object(config, 'POSITION_STATE_PATH', str(Path(self.tmp.name) / 'positions.json')),
                  patch.object(config, 'SUPERTREND_WATCH_STATE_PATH', str(Path(self.tmp.name) / 'watch.json')),
                  patch.object(config, 'TRADE_JOURNAL_PATH', str(Path(self.tmp.name) / 'trades.csv'))]:
            p.start()
            self.addCleanup(p.stop)
        state.position_state.clear()
        for data in (runtime.QUALIFIED_WATCH, runtime.ST_TRACKED_SYMBOLS, runtime.ST_HALTED_SYMBOLS, runtime.BOT_ENTRY_ORDER_KEYS):
            data.clear()
        self.own('BTCUSDT')

    def own(self, symbol, direction='LONG', qty='1', pending=None):
        entries = [dict(direction=direction, qty=Decimal(qty), entry_price=Decimal('100'),
                        order_id='entry-' + symbol, filled=True, closed=False)] if Decimal(qty) else []
        if pending:
            entries.append(dict(direction=direction, qty=Decimal('1'), entry_price=Decimal('100'),
                                order_id=pending, filled=False, closed=False))
            self.runtime.BOT_ENTRY_ORDER_KEYS.add((symbol, pending))
        self.state.position_state[symbol] = dict(st_mode=True, entries=entries, tp_order_id=None)
        self.runtime.ST_TRACKED_SYMBOLS.add(symbol)
        self.runtime.QUALIFIED_WATCH[symbol] = {'consecutive_losses': 1}

    def cycle(self):
        return self.mod.Cycle(self.path, self.ex, clock=lambda: self.ex.now, account='test')

    def run_until_idle(self, cycle, count=12):
        for _ in range(count):
            cycle.tick()
            self.ex.now += 10000
            if not cycle.active:
                break
        self.assertFalse(cycle.active)

    def test_threshold_includes_losses_and_excludes_other_strategies(self):
        self.own('ETHUSDT', 'SHORT')
        self.ex.rows = [position(pnl='150'), position('ETHUSDT', 'SHORT', pnl='-51'), position('FOREIGN', pnl='10000')]
        c = self.cycle()
        self.assertFalse(c.tick())
        self.assertEqual(self.ex.actions, [])
        self.ex.rows[1]['unrealisedPnl'] = '-50'
        self.assertTrue(c.tick())
        self.assertTrue(c.active)
        self.assertTrue(self.path.is_file())

    def test_cancel_ack_must_be_confirmed_before_any_close(self):
        self.own('BTCUSDT', pending='pending')
        self.ex.orders['pending'] = order('pending')
        self.ex.cancel_immediate = False
        c = self.cycle()
        c.tick()
        c.tick()
        self.assertFalse(any(a[0] == 'close' for a in self.ex.actions))
        self.assertTrue(c.active)
        self.ex.orders['pending']['orderStatus'] = 'Cancelled'
        c.tick()
        self.assertTrue(any(a[0] == 'close' for a in self.ex.actions))

    def test_fill_during_cancel_is_adopted_and_closed(self):
        self.own('BTCUSDT', pending='pending')
        self.ex.orders['pending'] = order('pending')
        c = self.cycle()
        c.tick()
        self.ex.orders['pending'].update(orderStatus='Filled', cumExecQty='1')
        self.ex.rows[0]['size'] = '2'
        c.tick()
        closes = [a for a in self.ex.actions if a[0] == 'close']
        self.assertEqual(closes[0][3], '2')

    def test_submission_timeout_reuses_intent_and_never_duplicates(self):
        self.ex.timeout_after_accept = True
        c = self.cycle()
        c.tick()
        c.tick()
        restarted = self.cycle()
        for _ in range(3):
            restarted.tick()
            self.ex.now += 40000
        self.assertEqual(len([a for a in self.ex.actions if a[0] == 'close']), 1)
        self.assertTrue(restarted.active)
        self.ex.fill(next(iter(self.ex.orders)), '1')
        self.run_until_idle(restarted)

    def test_partial_close_only_retries_remaining_quantity(self):
        c = self.cycle()
        c.tick()
        c.tick()
        self.ex.fill(next(iter(self.ex.orders)), '.4', status='Cancelled')
        c.tick()
        self.assertEqual([a[3] for a in self.ex.actions if a[0] == 'close'], ['1', '0.6'])
        self.assertTrue(c.active)

    def test_only_managed_side_is_closed_and_reset_waits_for_flat(self):
        self.ex.rows.append(position(direction='SHORT', pnl='500'))
        c = self.cycle()
        c.tick()
        c.tick()
        self.assertTrue(c.active)
        self.assertIn('BTCUSDT', self.state.position_state)
        self.ex.fill(next(iter(self.ex.orders)), '1')
        self.run_until_idle(c)
        self.assertEqual(self.ex.rows[1]['size'], '1')
        self.assertEqual(self.state.position_state, {})
        self.assertEqual(self.runtime.ST_TRACKED_SYMBOLS, set())
        self.assertEqual(self.runtime.QUALIFIED_WATCH, {})

    def test_same_side_quantity_mismatch_and_foreign_orders_pause(self):
        self.ex.rows[0]['size'] = '2'
        c = self.cycle()
        self.assertFalse(c.tick())
        self.assertTrue(c.blocked)
        self.assertEqual(self.ex.actions, [])
        self.ex.rows[0]['size'] = '1'
        self.ex.orders['foreign'] = order('foreign')
        self.assertFalse(c.tick())
        self.assertTrue(c.blocked)
        self.assertEqual(self.ex.actions, [])

    def test_api_error_is_not_treated_as_flat(self):
        c = self.cycle()
        c.tick()
        with patch.object(self.ex, 'positions', side_effect=RuntimeError('API failed')):
            self.assertTrue(c.tick())
        self.assertTrue(c.active)
        self.assertIn('BTCUSDT', self.state.position_state)

    def test_corrupt_persistent_state_blocks_all_trading(self):
        self.path.write_text('{broken')
        c = self.cycle()
        self.assertTrue(c.tick())
        self.assertTrue(c.active)
        self.assertEqual(self.ex.actions, [])

    def test_failed_intent_save_prevents_order_side_effect(self):
        c = self.cycle()
        c.tick()
        with patch.object(c, '_save', side_effect=OSError('disk full')):
            self.assertTrue(c.tick())
        self.assertFalse(any(a[0] == 'close' for a in self.ex.actions))

    def test_complete_reset_and_report_are_persistent(self):
        self.ex.auto_fill = True
        c = self.cycle()
        self.run_until_idle(c)
        restarted = self.cycle()
        self.assertFalse(restarted.active)
        self.assertEqual(restarted.data['last_result']['price_pnl'], '5')
        self.assertEqual(self.ex.rows[0]['size'], '0')

    def test_disabled_config_does_not_abandon_in_progress_close(self):
        c = self.cycle()
        c.tick()
        self.ex.auto_fill = True
        with patch.object(self.config, 'BASKET_TP_ENABLED', False):
            self.run_until_idle(self.cycle())


    def test_strict_adapter_paginates_positions_and_orders(self):
        from coin_rising_short import client
        ex = self.mod.Exchange()
        for method in (ex.positions, ex.open_orders):
            with self.subTest(method=method.__name__), patch.object(client, 'signed_get', side_effect=[{'list': [{'symbol': 'A'}], 'nextPageCursor': 'page2'}, {'list': [{'symbol': 'B'}], 'nextPageCursor': ''}]) as get:
                self.assertEqual(method(), [{'symbol': 'A'}, {'symbol': 'B'}])
                self.assertEqual(get.call_args_list[1].args[1]['cursor'], 'page2')
            with patch.object(client, 'signed_get', return_value={'_error': True, 'retCode': 10000}):
                with self.assertRaises(RuntimeError):
                    method()

    def test_strict_adapter_closes_reduce_only_with_persisted_link(self):
        from coin_rising_short import client
        with patch.object(client, 'signed_post', return_value={'orderId': '42'}) as post:
            self.mod.Exchange().close('BTCUSDT', 'SHORT', 2, Decimal('1'), 'basket-link')
        body = post.call_args.args[1]
        self.assertEqual(body['side'], 'Buy')
        self.assertEqual(body['positionIdx'], 2)
        self.assertTrue(body['reduceOnly'])
        self.assertTrue(body['closeOnTrigger'])
        self.assertEqual(body['orderLinkId'], 'basket-link')

    def test_restart_does_not_run_legacy_sync_or_inflate_ownership(self):
        from coin_rising_short import main
        self.ex.rows[0]['size'] = '2'
        with patch.object(main, '_configure_logging'), patch.object(main.client, 'refresh_time_offset'), patch.object(main.symbols, 'init_trading_symbols'), patch.object(main.orders, 'get_dual_side_position', return_value=True), patch.object(self.config, 'FORCE_HEDGE', False), patch.object(main.basket_profit, 'is_active', return_value=False), patch.object(main.state, 'load_position_state'), patch.object(main.state, 'load_qualified_watch'), patch.object(main.sync, 'sync_state_with_exchange') as legacy, patch.object(main.monitor, 'monitor_loop'):
            main._run_bot()
        legacy.assert_not_called()
        self.assertEqual(self.state.position_state['BTCUSDT']['entries'][0]['qty'], Decimal('1'))
        c = self.cycle()
        self.assertFalse(c.tick())
        self.assertTrue(c.blocked)
        self.assertEqual(self.ex.actions, [])

    def test_watch_only_foreign_position_is_not_owned(self):
        self.state.position_state.clear()
        self.runtime.ST_TRACKED_SYMBOLS.clear()
        self.runtime.QUALIFIED_WATCH['BTCUSDT'] = {'last_direction': 1}
        c = self.cycle()
        self.assertFalse(c.tick())
        self.assertEqual(self.state.position_state, {})
        self.assertEqual(self.ex.actions, [])

    def test_ambiguous_adopted_position_is_not_closed(self):
        self.state.position_state['BTCUSDT']['entries'][0]['order_id'] = 'EXCHANGE_SYNC'
        c = self.cycle()
        self.assertFalse(c.tick())
        self.assertTrue(c.blocked)
        self.assertEqual(self.ex.actions, [])

    def test_idle_validation_error_keeps_exit_and_fill_maintenance_running(self):
        from coin_rising_short import monitor
        self.ex.rows[0]['size'] = '2'
        c = self.cycle()
        with patch.object(monitor.basket_profit, 'tick', side_effect=c.tick), patch.object(monitor, 'get_24h_risers_and_top_movers', return_value=([], [])), patch.object(monitor, '_sync_qualified_watch'), patch.object(monitor.symbols, 'TRADING_SYMBOLS', {'BTCUSDT': {}}), patch.object(monitor.orders, 'cancel_stale_open_entry_orders', return_value=0), patch.object(monitor, 'check_filled_and_refresh_tp') as fills, patch.object(monitor, 'check_tp_filled_and_log') as exits, patch.object(monitor, '_process_supertrend_symbol', side_effect=KeyboardInterrupt) as manage:
            monitor.monitor_loop()
        fills.assert_called_once()
        exits.assert_called_once()
        manage.assert_called_once_with('BTCUSDT')
        self.assertTrue(c.blocked)

    def test_normal_st_exit_never_closes_foreign_same_side_inventory(self):
        c = self.cycle()
        self.ex.rows[0]['size'] = '2'
        with patch.object(self.mod, '_controller', c):
            self.assertIsNone(self.mod.managed_close_qty('BTCUSDT', 'LONG', Decimal('1')))
            self.ex.rows[0]['size'] = '.5'
            self.assertEqual(self.mod.managed_close_qty('BTCUSDT', 'LONG', Decimal('1')), Decimal('.5'))

    def test_reset_interruption_resumes_before_reentry(self):
        self.ex.auto_fill = True
        c = self.cycle()
        c.tick()
        c.tick()
        original = self.mod.atomic_write
        def fail_watch(path, data):
            if str(path) == self.config.SUPERTREND_WATCH_STATE_PATH:
                raise OSError('disk full while resetting')
            original(path, data)
        with patch.object(self.mod, 'atomic_write', side_effect=fail_watch):
            self.assertTrue(c.tick())
        self.assertEqual(c.data['phase'], 'resetting')
        restarted = self.cycle()
        self.assertTrue(restarted.active)
        self.run_until_idle(restarted)
        self.assertEqual(self.state.position_state, {})

    def test_entry_wrappers_are_gated_during_close(self):
        from coin_rising_short import orders
        with patch.object(self.mod, 'entries_allowed', return_value=False), patch.object(orders, 'ensure_leverage') as leverage, patch.object(orders, 'place_limit_order') as send:
            self.assertIsNone(orders.place_short_order('BTCUSDT'))
            self.assertIsNone(orders.place_long_order('BTCUSDT'))
        leverage.assert_not_called()
        send.assert_not_called()

    def test_exchange_delay_reuses_same_close_link(self):
        self.ex.timeout_after_accept = True
        c = self.cycle()
        c.tick()
        c.tick()
        with patch.object(self.ex, 'order', return_value=None):
            for _ in range(2):
                self.ex.now += 40000
                c.tick()
        self.assertEqual(len([a for a in self.ex.actions if a[0] == 'close']), 1)
        self.assertEqual(len(c.data['jobs']), 1)

    def test_market_quantity_limit_closes_in_chunks(self):
        self.ex.auto_fill = True
        with patch.object(self.ex, 'lot', return_value=(Decimal('.01'), Decimal('.01'), Decimal('.4'))):
            self.run_until_idle(self.cycle())
        self.assertEqual([Decimal(a[3]) for a in self.ex.actions if a[0] == 'close'], [Decimal('.4'), Decimal('.4'), Decimal('.2')])

    def test_process_lock_rejects_second_local_bot(self):
        lock = self.mod.ProcessLock(str(self.path) + '.lock')
        try:
            with self.assertRaises(RuntimeError):
                self.mod.ProcessLock(str(self.path) + '.lock')
        finally:
            lock.close()
        self.mod.ProcessLock(str(self.path) + '.lock').close()


    def test_cancelled_entry_partial_fill_stays_managed_below_goal(self):
        from coin_rising_short import monitor
        self.own('BTCUSDT', qty='0', pending='pending')
        self.ex.orders['pending'] = order('pending', status='Cancelled', filled='.4')
        self.ex.rows[0].update(size='.4', unrealisedPnl='0')
        c = self.cycle()
        self.assertFalse(c.tick())
        with patch.object(monitor.orders, 'get_order_status', return_value='CANCELED'), patch.object(monitor.orders, 'get_order_detail', return_value={'status': 'CANCELED', 'executedQty': '.4', 'avgPrice': '100'}), patch.object(monitor.state, 'save_position_state'):
            monitor.check_filled_and_refresh_tp()
        self.assertIn('BTCUSDT', self.state.position_state)
        entry = self.state.position_state['BTCUSDT']['entries'][0]
        self.assertTrue(entry['filled'])
        self.assertFalse(entry['closed'])
        self.assertEqual(entry['qty'], Decimal('.4'))


if __name__ == '__main__':
    unittest.main()
