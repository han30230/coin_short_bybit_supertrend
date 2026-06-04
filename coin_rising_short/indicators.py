import logging
import time
from decimal import Decimal
from typing import List, Optional, Tuple

from coin_rising_short import client, config, runtime

logger = logging.getLogger(__name__)

_RSI_PERIOD = 14

_ohlc_cache: dict[str, tuple[float, dict[str, List[Decimal]]]] = {}


def _mean(values: List[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values) / Decimal(len(values))


def _wilder_rsi_series(closes: List[Decimal]) -> List[Optional[Decimal]]:
    n = len(closes)
    out: List[Optional[Decimal]] = [None] * n
    if n < _RSI_PERIOD + 1:
        return out

    gains: List[Decimal] = []
    losses: List[Decimal] = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(ch if ch > 0 else Decimal("0"))
        losses.append(-ch if ch < 0 else Decimal("0"))

    period = Decimal(str(_RSI_PERIOD))
    avg_gain = sum(gains[0:_RSI_PERIOD]) / period
    avg_loss = sum(losses[0:_RSI_PERIOD]) / period

    def rsi_from_avgs(ag: Decimal, al: Decimal) -> Decimal:
        if al == 0:
            return Decimal("100") if ag > 0 else Decimal("50")
        rs = ag / al
        return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

    out[_RSI_PERIOD] = rsi_from_avgs(avg_gain, avg_loss)
    pm1 = period - Decimal("1")

    for j in range(_RSI_PERIOD, n - 1):
        g = gains[j]
        l_ = losses[j]
        avg_gain = (avg_gain * pm1 + g) / period
        avg_loss = (avg_loss * pm1 + l_) / period
        out[j + 1] = rsi_from_avgs(avg_gain, avg_loss)

    return out


def _ma20_gap_pct_series(closes: List[Decimal]) -> List[Optional[Decimal]]:
    n = len(closes)
    out: List[Optional[Decimal]] = [None] * n
    for i in range(19, n):
        window = closes[i - 19 : i + 1]
        ma20 = sum(window) / Decimal("20")
        if ma20 <= 0:
            out[i] = None
        else:
            out[i] = (closes[i] - ma20) / ma20 * Decimal("100")
    return out


def _ma5_slope_turns_down(closes: List[Decimal]) -> bool:
    """최신 닫힌 봉 기준: slope_prev > 0 and slope_now < 0."""
    L = len(closes) - 1
    if L < 14:
        return False
    ma5_now = _mean(closes[L - 4 : L + 1])
    ma5_prev = _mean(closes[L - 9 : L - 4])
    ma5_prev2 = _mean(closes[L - 14 : L - 9])
    slope_prev = ma5_prev - ma5_prev2
    slope_now = ma5_now - ma5_prev
    return slope_prev > 0 and slope_now < 0


def _get_closed_ohlc(
    symbol: str,
    interval: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[Optional[dict[str, List[Decimal]]], str]:
    iv = interval or config.INDICATOR_INTERVAL
    if limit is None:
        limit = (
            config.SUPERTREND_KLINE_LIMIT
            if iv == config.SUPERTREND_INTERVAL
            else config.INDICATOR_KLINE_LIMIT
        )
    cache_key = f"{symbol}:{iv}"
    now = time.time()
    cached = _ohlc_cache.get(cache_key)
    if cached is not None and now - cached[0] < config.INDICATOR_CACHE_TTL_SEC:
        return cached[1], ""

    try:
        data = client.get_klines_binance_shape(symbol, interval=iv, limit=limit)
    except Exception as exc:
        msg = f"kline API/파싱 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_kline_failed", "symbol": symbol})
        return None, msg

    if not isinstance(data, list):
        msg = "kline 응답이 list가 아님"
        logger.warning("%s symbol=%s type=%s", msg, symbol, type(data), extra={"event": "indicator_kline_bad_type", "symbol": symbol})
        return None, msg

    rows = data[:-1]
    highs: List[Decimal] = []
    lows: List[Decimal] = []
    closes: List[Decimal] = []
    try:
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            highs.append(Decimal(str(row[2])))
            lows.append(Decimal(str(row[3])))
            closes.append(Decimal(str(row[4])))
    except Exception as exc:
        msg = f"지표 계산 실패: OHLC 변환 오류: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_ohlc_parse_failed", "symbol": symbol})
        return None, msg

    if not closes:
        msg = "닫힌 캔들이 없음"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_no_closed_candles", "symbol": symbol})
        return None, msg

    ohlc = {"highs": highs, "lows": lows, "closes": closes}
    _ohlc_cache[cache_key] = (now, ohlc)
    return ohlc, ""


def _get_closed_closes(symbol: str) -> Tuple[Optional[List[Decimal]], str]:
    ohlc, err = _get_closed_ohlc(symbol)
    if ohlc is None:
        return None, err
    return ohlc["closes"], ""


def _true_range(
    high: Decimal, low: Decimal, prev_close: Decimal
) -> Decimal:
    a = high - low
    b = abs(high - prev_close)
    c = abs(low - prev_close)
    return max(a, b, c)


def _supertrend_src(
    highs: List[Decimal], lows: List[Decimal], closes: List[Decimal], i: int
) -> Decimal:
    src = config.SUPERTREND_SOURCE
    if src == "hl2":
        return (highs[i] + lows[i]) / Decimal("2")
    if src == "close":
        return closes[i]
    return (highs[i] + lows[i] + closes[i]) / Decimal("3")


def _pine_atr_early(
    highs: List[Decimal], lows: List[Decimal], closes: List[Decimal], period: int
) -> List[Optional[Decimal]]:
    """TV Pine: barCount < period 이면 누적평균, 이후 (prev*(period-1)+tr)/period."""
    n = len(closes)
    out: List[Optional[Decimal]] = [None] * n
    prev_atr: Optional[Decimal] = None
    p = Decimal(str(period))

    for i in range(1, n):
        tr = _true_range(highs[i], lows[i], closes[i - 1])
        bar_count = i
        if prev_atr is None:
            prev_atr = tr
        elif bar_count < period:
            prev_atr = (prev_atr * Decimal(bar_count - 1) + tr) / Decimal(bar_count)
        else:
            prev_atr = (prev_atr * (p - Decimal("1")) + tr) / p
        out[i] = prev_atr
    return out


def _supertrend_directions(
    highs: List[Decimal],
    lows: List[Decimal],
    closes: List[Decimal],
    period: int,
    factor: Decimal,
) -> List[int]:
    """
    TradingView Supertrend Only Strategy Pine 로직.
    1=상승, -1=하락(숏). 닫힌 봉 기준 시계열.
    """
    n = len(closes)
    atr_s = _pine_atr_early(highs, lows, closes, period)
    up_band: List[Decimal] = [Decimal("0")] * n
    dn_band: List[Decimal] = [Decimal("0")] * n
    direction: List[int] = [1] * n

    for i in range(n):
        atr = atr_s[i]
        if atr is None:
            continue
        src = _supertrend_src(highs, lows, closes, i)
        basic_up = src - factor * atr
        basic_dn = src + factor * atr

        if i == 0:
            up_band[i] = basic_up
            dn_band[i] = basic_dn
            direction[i] = 1
            continue

        up1 = up_band[i - 1]
        dn1 = dn_band[i - 1]
        up_band[i] = max(basic_up, up1) if closes[i - 1] > up1 else basic_up
        dn_band[i] = min(basic_dn, dn1) if closes[i - 1] < dn1 else basic_dn

        prev_trend = direction[i - 1]
        if prev_trend == -1 and closes[i] > dn1:
            direction[i] = 1
        elif prev_trend == 1 and closes[i] < up1:
            direction[i] = -1
        else:
            direction[i] = prev_trend

    return direction


def st_int_to_direction(st_dir: int) -> str:
    return "LONG" if st_dir == 1 else "SHORT"


def get_supertrend_direction(symbol: str) -> Tuple[Optional[int], str]:
    """최근 닫힌 봉 SuperTrend 방향. 1=상승(롱), -1=하락(숏)."""
    try:
        ohlc, err = _get_closed_ohlc(
            symbol,
            interval=config.SUPERTREND_INTERVAL,
            limit=config.SUPERTREND_KLINE_LIMIT,
        )
        if ohlc is None:
            return None, err or "OHLC 없음"

        highs = ohlc["highs"]
        lows = ohlc["lows"]
        closes = ohlc["closes"]
        period = config.SUPERTREND_ATR_PERIOD
        min_len = period + 5
        if len(closes) < min_len:
            return None, f"캔들 부족: {len(closes)} < {min_len}"

        directions = _supertrend_directions(
            highs, lows, closes, period, config.SUPERTREND_FACTOR
        )
        curr_d = directions[len(directions) - 1]
        return curr_d, (
            f"supertrend_{config.SUPERTREND_INTERVAL} dir={curr_d} "
            f"p={period} f={config.SUPERTREND_FACTOR}"
        )
    except Exception as exc:
        msg = f"SuperTrend 계산 실패: {exc}"
        logger.warning(
            "%s symbol=%s",
            msg,
            symbol,
            extra={"event": "supertrend_exception", "symbol": symbol},
        )
        return None, msg


def is_supertrend_short_signal(symbol: str) -> Tuple[bool, str]:
    """
    최근 닫힌 4h 봉 기준 SuperTrend 하락(-1)이면 True.
    - 1→-1 전환 시 진입
    - 이미 -1(하락 추세)인 상태에서 감시 등록 직후에도 1회 진입
    동일 -1이 연속이면 중복 진입하지 않음 (watch last_direction).
    """
    try:
        ohlc, err = _get_closed_ohlc(
            symbol,
            interval=config.SUPERTREND_INTERVAL,
            limit=config.SUPERTREND_KLINE_LIMIT,
        )
        if ohlc is None:
            return False, err or "OHLC 없음"

        highs = ohlc["highs"]
        lows = ohlc["lows"]
        closes = ohlc["closes"]
        period = config.SUPERTREND_ATR_PERIOD
        min_len = period + 5
        if len(closes) < min_len:
            return False, f"캔들 부족: {len(closes)} < {min_len}"

        directions = _supertrend_directions(
            highs, lows, closes, period, config.SUPERTREND_FACTOR
        )
        L = len(directions) - 1
        curr_d = directions[L]

        watch = runtime.QUALIFIED_WATCH.get(symbol)
        last_seen = watch.get("last_direction") if watch else None
        if watch is not None:
            watch["last_direction"] = curr_d

        if curr_d != -1:
            return False, (
                f"ST 대기 {config.SUPERTREND_INTERVAL} (curr={curr_d}, need=-1)"
            )

        if last_seen == -1:
            return False, (
                f"ST 하락 유지 {config.SUPERTREND_INTERVAL} (이미 처리됨)"
            )

        tag = "flip" if last_seen == 1 else "downtrend"
        return True, (
            f"supertrend_short_{tag}({config.SUPERTREND_INTERVAL} "
            f"p={period} f={config.SUPERTREND_FACTOR} src={config.SUPERTREND_SOURCE})"
        )
    except Exception as exc:
        msg = f"SuperTrend 계산 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "supertrend_exception", "symbol": symbol})
        return False, msg


def allow_initial_short(symbol: str) -> Tuple[bool, str]:
    try:
        closes, err = _get_closed_closes(symbol)
        if closes is None:
            return False, err or "지표 계산 실패: kline 없음"

        if len(closes) < 20:
            msg = f"캔들 개수 부족: {len(closes)} < 20"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_insufficient_bars", "symbol": symbol})
            return False, msg

        rsi_s = _wilder_rsi_series(closes)
        gap_s = _ma20_gap_pct_series(closes)
        L = len(closes) - 1
        rsi = rsi_s[L]
        gap = gap_s[L]
        if rsi is None or gap is None:
            msg = "RSI 또는 MA20 이격률 계산 불가"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_series_none", "symbol": symbol})
            return False, msg

        if rsi < config.ENTRY_RSI_THRESHOLD:
            return False, f"RSI 미달: {rsi} < {config.ENTRY_RSI_THRESHOLD}"
        if gap < config.ENTRY_MA20_GAP_PCT:
            return False, f"MA20 이격률 미달: {gap}% < {config.ENTRY_MA20_GAP_PCT}%"

        return True, "ok"
    except Exception as exc:
        msg = f"지표 계산 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_initial_exception", "symbol": symbol})
        return False, msg


def allow_reentry_short(symbol: str) -> Tuple[bool, str]:
    try:
        closes, err = _get_closed_closes(symbol)
        if closes is None:
            return False, err or "지표 계산 실패: kline 없음"

        n_bars = config.REENTRY_RECENT_OVER_BARS
        min_len = 19 + n_bars
        if len(closes) < min_len:
            msg = f"캔들 개수 부족: {len(closes)} < {min_len}"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_insufficient_bars_reentry", "symbol": symbol})
            return False, msg

        rsi_s = _wilder_rsi_series(closes)
        gap_s = _ma20_gap_pct_series(closes)
        L = len(closes) - 1
        start = L - (n_bars - 1)
        overheated = False
        for idx in range(start, L + 1):
            r = rsi_s[idx]
            g = gap_s[idx]
            if r is None or g is None:
                continue
            if r >= config.REENTRY_RSI_THRESHOLD and g >= config.REENTRY_MA20_GAP_PCT:
                overheated = True
                break

        if not overheated:
            return False, "최근 과열 구간 없음(RSI/MA20 이격)"

        if not _ma5_slope_turns_down(closes):
            return False, "MA5 기울기 하락 전환 없음"

        return True, "ok"
    except Exception as exc:
        msg = f"지표 계산 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_reentry_exception", "symbol": symbol})
        return False, msg
