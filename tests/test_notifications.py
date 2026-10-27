import importlib
import importlib.util
import logging
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


class TestNotifications(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec('coin_rising_short.notifications'),
            'Telegram notification module is missing',
        )
        self.mod = importlib.import_module('coin_rising_short.notifications')
        self.messages = []
        self.lookup = lambda symbol, oid: {
            'status': 'FILLED', 'avgPrice': '101', 'executedQty': '2'
        }

    def notifier(self, **kwargs):
        return self.mod.TelegramNotifier(
            'test-token', '12345', 'test-bot', self.lookup,
            start_worker=False, **kwargs,
        )

    def drain(self, notifier):
        with patch.object(notifier, '_send', side_effect=lambda text: self.messages.append(text) or True):
            while notifier.process_one():
                pass

    def test_confirmed_entry_uses_exchange_fill_not_requested_price(self):
        n = self.notifier()
        self.assertTrue(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '42', Decimal('100')))
        self.drain(n)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('진입 체결', self.messages[0])
        self.assertIn('101', self.messages[0])
        self.assertIn('수량: 2', self.messages[0])
        self.assertIn('test-bot', self.messages[0])

    def test_exit_profit_uses_actual_fill_and_labels_fees_excluded(self):
        n = self.notifier()
        n.enqueue_fill('exit', 'BTCUSDT', 'SHORT', '43', Decimal('100'), 'ST 반전')
        self.drain(n)
        self.assertIn('청산 체결', self.messages[0])
        self.assertIn('-2', self.messages[0])
        self.assertIn('수수료·펀딩 제외', self.messages[0])
        self.assertIn('ST 반전', self.messages[0])

    def test_unconfirmed_order_is_never_reported_as_fill(self):
        self.lookup = lambda *_: {'status': 'NEW'}
        n = self.notifier()
        n.enqueue_fill('exit', 'BTCUSDT', 'SHORT', '44', Decimal('100'))
        with patch.object(self.mod.time, 'sleep'):
            self.drain(n)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('체결 확인 필요', self.messages[0])
        self.assertNotIn('청산 체결', self.messages[0])

    def test_partial_fill_is_labeled_with_executed_quantity(self):
        self.lookup = lambda *_: {'status': 'PARTIALLY_FILLED', 'avgPrice': '100', 'executedQty': '0.5'}
        n = self.notifier()
        n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '45', Decimal('100'))
        with patch.object(self.mod.time, 'sleep'):
            self.drain(n)
        self.assertIn('부분 체결', self.messages[0])
        self.assertIn('0.5', self.messages[0])

    def test_same_order_is_deduplicated_but_opposite_event_is_not(self):
        n = self.notifier()
        self.assertTrue(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '42', Decimal('100')))
        self.assertFalse(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '42', Decimal('100')))
        self.drain(n)
        self.assertFalse(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '42', Decimal('100')))
        self.assertTrue(n.enqueue_fill('exit', 'BTCUSDT', 'LONG', '43', Decimal('100')))

    def test_bounded_queue_drops_without_blocking(self):
        n = self.notifier(queue_size=1)
        self.assertTrue(n.enqueue_text('first'))
        with self.assertLogs(self.mod.__name__, level='WARNING'):
            self.assertFalse(n.enqueue_text('second'))

    def test_worker_does_not_block_trading_caller(self):
        entered, release = threading.Event(), threading.Event()
        def slow_lookup(*_):
            entered.set()
            release.wait(2)
            return {'status': 'FILLED', 'avgPrice': '101', 'executedQty': '2'}
        n = self.mod.TelegramNotifier('token', '123', 'bot', slow_lookup)
        try:
            with patch.object(n, '_send', return_value=True):
                self.assertTrue(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', '42', Decimal('100')))
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                self.assertTrue(n.enqueue_text('trading thread still responsive'))
                release.set()
                n.close(timeout=2)
        finally:
            release.set()
            n.close(timeout=2)

    def test_http_failure_does_not_leak_token_or_raise(self):
        n = self.notifier()
        with patch.object(self.mod.requests, 'post', side_effect=RuntimeError('secret-token-in-url')):
            with self.assertLogs(self.mod.__name__, level='WARNING') as logs:
                self.assertFalse(n._send('hello'))
        self.assertNotIn('secret-token-in-url', '\n'.join(logs.output))

    def test_send_uses_timeout_and_plain_text(self):
        n = self.notifier()
        with patch.object(self.mod.requests, 'post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'ok': True}
            self.assertTrue(n._send('hello'))
            args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.telegram.org/bottest-token/sendMessage')
        self.assertEqual(kwargs['json']['chat_id'], '12345')
        self.assertNotIn('parse_mode', kwargs['json'])
        self.assertIn('timeout', kwargs)

    def test_error_handler_throttles_and_does_not_forward_raw_secrets(self):
        n = self.notifier()
        h = self.mod.TelegramLogHandler(n)
        r = logging.LogRecord('coin_rising_short.monitor', logging.ERROR, '', 1, 'api_secret=SECRET', (), None)
        r.event, r.symbol = 'exit_close_failed', 'BTCUSDT'
        h.handle(r)
        h.handle(r)
        self.drain(n)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('exit_close_failed', self.messages[0])
        self.assertNotIn('SECRET', self.messages[0])

    def test_scan_logs_do_not_send_notifications(self):
        n = self.notifier()
        h = self.mod.TelegramLogHandler(n)
        r = logging.LogRecord('coin_rising_short.monitor', logging.INFO, '', 1, 'scan', (), None)
        r.event = 'gainer_ranked'
        h.handle(r)
        self.drain(n)
        self.assertEqual(self.messages, [])

    def test_disabled_and_incomplete_settings_do_not_start_worker(self):
        with patch.dict('os.environ', {'TELEGRAM_ENABLED': 'false'}, clear=True):
            self.assertIsNone(self.mod.configure('bot', self.lookup))
        with patch.dict('os.environ', {'TELEGRAM_ENABLED': 'true'}, clear=True):
            with self.assertLogs(self.mod.__name__, level='WARNING'):
                self.assertIsNone(self.mod.configure('bot', self.lookup))

    def test_trade_journal_enqueues_fill_after_recording(self):
        from coin_rising_short import config, trade_journal
        n = self.notifier()
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'journal.csv')
            with patch.object(config, 'TRADE_JOURNAL_PATH', path), patch.object(self.mod, '_notifier', n):
                trade_journal.log_entry_filled('BTCUSDT', 'LONG', '42', Decimal('100'), Decimal('2'), None)
                self.assertTrue(Path(path).is_file())
                trade_journal.log_exit_filled('BTCUSDT', 'LONG', '42', '43', Decimal('100'), Decimal('101'), Decimal('2'), None, None)
                self.drain(n)
        self.assertEqual(len(self.messages), 2)

    def test_synthetic_position_recovery_is_not_a_new_fill(self):
        n = self.notifier()
        for oid in ['', '0', 'EXCHANGE_SYNC']:
            self.assertFalse(n.enqueue_fill('entry', 'BTCUSDT', 'LONG', oid, Decimal('100')))


    def test_fatal_alert_has_priority_over_queued_updates(self):
        n = self.notifier()
        n.enqueue_text('ordinary update')
        h = self.mod.TelegramLogHandler(n)
        r = logging.LogRecord('coin_rising_short.main', logging.ERROR, '', 1, 'fatal', (), None)
        r.event = 'bot_fatal'
        h.handle(r)
        self.drain(n)
        self.assertIn('bot_fatal', self.messages[0])

    def test_take_profit_warning_is_an_important_alert(self):
        n = self.notifier()
        h = self.mod.TelegramLogHandler(n)
        r = logging.LogRecord('coin_rising_short.orders', logging.WARNING, '', 1, 'TP failed', (), None)
        r.event, r.symbol = 'tp_place_failed', 'BTCUSDT'
        h.handle(r)
        self.drain(n)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('tp_place_failed', self.messages[0])

    def test_shutdown_allows_slow_delivery_to_finish(self):
        entered, release = threading.Event(), threading.Event()
        delivered = []
        def slow_send(text):
            entered.set()
            release.wait(4)
            delivered.append(text)
            return True
        n = self.mod.TelegramNotifier('token', '123', 'bot', self.lookup, start_worker=False)
        timer = threading.Timer(2.1, release.set)
        try:
            with patch.object(n, '_send', side_effect=slow_send):
                n.worker = threading.Thread(target=n._run, daemon=True)
                n.worker.start()
                n.enqueue_text('fatal')
                self.assertTrue(entered.wait(1))
                timer.start()
                n.close()
                self.assertEqual(delivered, ['fatal'])
        finally:
            release.set()
            timer.cancel()
            n.close(timeout=2)


if __name__ == '__main__':
    unittest.main()
