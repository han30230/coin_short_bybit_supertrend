import unittest
from decimal import Decimal
from unittest.mock import patch

from coin_rising_short import config, runtime
from coin_rising_short.monitor import _halt_st_symbol, _is_st_halted, _record_st_trade_pnl
from coin_rising_short.indicators import get_supertrend_direction, st_int_to_direction


class TestStDirection(unittest.TestCase):
    def test_st_int_to_direction(self) -> None:
        self.assertEqual(st_int_to_direction(1), "LONG")
        self.assertEqual(st_int_to_direction(-1), "SHORT")

    def test_get_supertrend_direction_bull(self) -> None:
        directions = [1] * 20
        n = len(directions)
        with patch(
            "coin_rising_short.indicators._supertrend_directions",
            return_value=directions,
        ), patch(
            "coin_rising_short.indicators._get_closed_ohlc",
            return_value=(
                {
                    "highs": [Decimal("1")] * n,
                    "lows": [Decimal("1")] * n,
                    "closes": [Decimal("1")] * n,
                },
                "",
            ),
        ):
            curr, _ = get_supertrend_direction("BTCUSDT")
        self.assertEqual(curr, 1)


class TestStLossHalt(unittest.TestCase):
    def setUp(self) -> None:
        runtime.QUALIFIED_WATCH.clear()
        runtime.ST_HALTED_SYMBOLS.clear()

    def test_two_losses_halt_symbol(self) -> None:
        runtime.QUALIFIED_WATCH["ETHUSDT"] = {
            "added_at": 0,
            "consecutive_losses": 0,
            "halted": False,
        }
        with patch.object(config, "ST_MAX_CONSECUTIVE_LOSSES", 2), patch(
            "coin_rising_short.monitor.state.save_qualified_watch"
        ):
            _record_st_trade_pnl("ETHUSDT", Decimal("-1"))
            self.assertFalse(_is_st_halted("ETHUSDT"))
            self.assertEqual(runtime.QUALIFIED_WATCH["ETHUSDT"]["consecutive_losses"], 1)

            _record_st_trade_pnl("ETHUSDT", Decimal("-0.5"))
        self.assertTrue(_is_st_halted("ETHUSDT"))
        self.assertNotIn("ETHUSDT", runtime.QUALIFIED_WATCH)

    def test_profit_resets_loss_streak(self) -> None:
        runtime.QUALIFIED_WATCH["XRPUSDT"] = {"consecutive_losses": 1, "halted": False}
        with patch("coin_rising_short.monitor.state.save_qualified_watch"):
            _record_st_trade_pnl("XRPUSDT", Decimal("3"))
        self.assertEqual(runtime.QUALIFIED_WATCH["XRPUSDT"]["consecutive_losses"], 0)

    def test_halt_removes_from_watch(self) -> None:
        runtime.QUALIFIED_WATCH["SOLUSDT"] = {"consecutive_losses": 1, "halted": False}
        with patch("coin_rising_short.monitor.state.save_qualified_watch"):
            _halt_st_symbol("SOLUSDT", 2)
        self.assertIn("SOLUSDT", runtime.ST_HALTED_SYMBOLS)
        self.assertNotIn("SOLUSDT", runtime.QUALIFIED_WATCH)


if __name__ == "__main__":
    unittest.main()
