import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from coin_rising_short import config, monitor
from coin_rising_short.market_cap import (
    clear_mcap_cache,
    get_market_cap_usd,
    normalize_binance_symbol,
)


class TestNormalizeBinanceSymbol(unittest.TestCase):
    def test_btc_usdt(self) -> None:
        self.assertEqual(normalize_binance_symbol("BTCUSDT"), "BTC")

    def test_1000_pepe_usdt(self) -> None:
        self.assertEqual(normalize_binance_symbol("1000PEPEUSDT"), "PEPE")

    def test_eth(self) -> None:
        self.assertEqual(normalize_binance_symbol("ETHUSDT"), "ETH")


class TestMcapCache(unittest.TestCase):
    def setUp(self) -> None:
        clear_mcap_cache()

    def tearDown(self) -> None:
        clear_mcap_cache()

    @patch("coin_rising_short.market_cap._fetch_market_cap_usd_from_cmc")
    @patch("coin_rising_short.market_cap.time.time", side_effect=[1000.0, 1005.0])
    def test_cache_avoids_second_api_call_within_ttl(self, mock_time: MagicMock, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = Decimal("250000000")
        with patch.object(config, "MCAP_FILTER_ENABLED", True), patch.object(config, "CMC_API_KEY", "fake-key"):
            a = get_market_cap_usd("BTCUSDT")
            b = get_market_cap_usd("BTCUSDT")
        self.assertEqual(a, Decimal("250000000"))
        self.assertEqual(b, Decimal("250000000"))
        self.assertEqual(mock_fetch.call_count, 1)


class TestMonitorQualifiedGainers(unittest.TestCase):
    def _ticker_row(self, change: str = "0.25", turnover: str = "100") -> dict:
        return {
            "symbol": "BTCUSDT",
            "price24hPcnt": change,
            "turnover24h": turnover,
            "lastPrice": "50000",
        }

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_24h_rise_qualifies_without_funding_or_volume(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row()]
        with patch.object(config, "USE_VOLUME_FILTER", False), patch.object(
            config, "GAINER_THRESHOLD_PCT", Decimal("20")
        ):
            qualified, _top = monitor.get_24h_risers_and_top_movers()

        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["symbol"], "BTCUSDT")

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_low_change_excluded(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row(change="0.05")]
        qualified, _top = monitor.get_24h_risers_and_top_movers()
        self.assertEqual(qualified, [])


if __name__ == "__main__":
    unittest.main()
