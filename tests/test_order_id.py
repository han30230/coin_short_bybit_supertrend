import unittest

from coin_rising_short.client import normalize_order, normalize_order_id


class TestOrderId(unittest.TestCase):
    def test_uuid_preserved(self) -> None:
        uid = "381aa29a-4740-427f-9b29-3f05111f7a80"
        self.assertEqual(normalize_order_id(uid), uid)

    def test_numeric_string(self) -> None:
        self.assertEqual(normalize_order_id("12345678"), "12345678")

    def test_normalize_order_uses_string_id(self) -> None:
        uid = "381aa29a-4740-427f-9b29-3f05111f7a80"
        row = normalize_order(
            {
                "symbol": "BTCUSDT",
                "orderId": uid,
                "orderStatus": "New",
                "avgPrice": "0",
                "cumExecQty": "0",
            }
        )
        self.assertEqual(row["orderId"], uid)


if __name__ == "__main__":
    unittest.main()
