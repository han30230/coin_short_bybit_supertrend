import unittest
from unittest.mock import patch

from coin_rising_short import config, runtime, state
from coin_rising_short.monitor import _active_st_position_count, _can_open_new_st_slot


class TestConcurrentLimit(unittest.TestCase):
    def setUp(self) -> None:
        state.position_state.clear()

    def test_slot_limit_blocks_new_symbol(self) -> None:
        with patch.object(config, "MAX_CONCURRENT_ST_SYMBOLS", 2):
            state.position_state["A"] = {"st_mode": True, "entries": []}
            state.position_state["B"] = {"st_mode": True, "entries": []}
            self.assertEqual(_active_st_position_count(), 2)
            self.assertFalse(_can_open_new_st_slot("C"))
            self.assertTrue(_can_open_new_st_slot("A"))

    def test_existing_symbol_always_allowed(self) -> None:
        with patch.object(config, "MAX_CONCURRENT_ST_SYMBOLS", 1):
            state.position_state["X"] = {"st_mode": True, "entries": []}
            self.assertTrue(_can_open_new_st_slot("X"))


if __name__ == "__main__":
    unittest.main()
