import unittest
from unittest.mock import patch

from coin_rising_short import config, runtime, state
from coin_rising_short.monitor import _can_add_st_tracked, enforce_st_tracked_limit


class TestStTrackedLimit(unittest.TestCase):
    def setUp(self) -> None:
        runtime.ST_TRACKED_SYMBOLS.clear()
        runtime.QUALIFIED_WATCH.clear()
        state.position_state.clear()

    def test_can_add_when_under_limit(self) -> None:
        with patch.object(config, "MAX_ST_TRACKED_SYMBOLS", 3):
            runtime.ST_TRACKED_SYMBOLS.update({"A", "B"})
            self.assertTrue(_can_add_st_tracked("C"))

    def test_cannot_add_when_at_limit(self) -> None:
        with patch.object(config, "MAX_ST_TRACKED_SYMBOLS", 2):
            runtime.ST_TRACKED_SYMBOLS.update({"A", "B"})
            self.assertFalse(_can_add_st_tracked("C"))

    def test_position_holder_always_allowed(self) -> None:
        with patch.object(config, "MAX_ST_TRACKED_SYMBOLS", 1):
            runtime.ST_TRACKED_SYMBOLS.add("A")
            state.position_state["B"] = {"st_mode": True, "entries": []}
            self.assertTrue(_can_add_st_tracked("B"))

    def test_enforce_trims_oldest_unprotected(self) -> None:
        with patch.object(config, "MAX_ST_TRACKED_SYMBOLS", 2):
            runtime.ST_TRACKED_SYMBOLS.update({"OLD", "MID", "NEW"})
            runtime.QUALIFIED_WATCH["OLD"] = {"added_at": 1.0}
            runtime.QUALIFIED_WATCH["MID"] = {"added_at": 50.0}
            runtime.QUALIFIED_WATCH["NEW"] = {"added_at": 99.0}
            state.position_state["NEW"] = {"st_mode": True, "entries": []}
            changed = enforce_st_tracked_limit({})
            self.assertTrue(changed)
            self.assertNotIn("OLD", runtime.ST_TRACKED_SYMBOLS)
            self.assertIn("NEW", runtime.ST_TRACKED_SYMBOLS)


if __name__ == "__main__":
    unittest.main()
