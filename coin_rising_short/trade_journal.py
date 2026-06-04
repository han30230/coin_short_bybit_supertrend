import csv
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Union

from coin_rising_short import config

CSV_HEADERS = [
    "이벤트",
    "코인",
    "방향",
    "진입시간(UTC)",
    "청산시간(UTC)",
    "진입주문ID",
    "청산주문ID",
    "진입가",
    "청산가",
    "수량",
    "명목금액USDT",
    "PNL_USDT",
    "수익률(%)",
    "레버리지적용수익률(%)",
    "비고",
]


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _ms_to_iso_utc(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _append_row(row: dict) -> None:
    path = config.TRADE_JOURNAL_PATH
    _ensure_parent_dir(path)
    file_exists = os.path.isfile(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_entry_filled(
    symbol: str,
    direction: str,
    order_id: int,
    entry_price: Decimal,
    qty: Decimal,
    filled_time_ms: Optional[int],
    note: str = "",
) -> None:
    notional = entry_price * qty
    _append_row(
        {
            "이벤트": "진입체결",
            "코인": symbol,
            "방향": direction,
            "진입시간(UTC)": _ms_to_iso_utc(filled_time_ms),
            "청산시간(UTC)": "",
            "진입주문ID": order_id,
            "청산주문ID": "",
            "진입가": str(entry_price),
            "청산가": "",
            "수량": str(qty),
            "명목금액USDT": str(notional),
            "PNL_USDT": "",
            "수익률(%)": "",
            "레버리지적용수익률(%)": "",
            "비고": note,
        }
    )


def calc_pnl_usdt(
    direction: str, entry_price: Decimal, exit_price: Decimal, qty: Decimal
) -> Decimal:
    if direction.upper() == "SHORT":
        return (entry_price - exit_price) * qty
    return (exit_price - entry_price) * qty


def log_exit_filled(
    symbol: str,
    direction: str,
    entry_order_id: Union[int, str],
    tp_order_id: Union[int, str],
    entry_price: Decimal,
    exit_price: Decimal,
    qty: Decimal,
    entry_time_ms: Optional[int],
    exit_time_ms: Optional[int],
    note: str = "",
) -> None:
    notional = entry_price * qty
    pnl = calc_pnl_usdt(direction, entry_price, exit_price, qty)
    profit_pct = Decimal("0")
    if notional > 0:
        profit_pct = (pnl / notional) * Decimal("100")
    leveraged_pct = profit_pct * Decimal(str(config.LEVERAGE))
    _append_row(
        {
            "이벤트": "TP청산체결",
            "코인": symbol,
            "방향": direction,
            "진입시간(UTC)": _ms_to_iso_utc(entry_time_ms),
            "청산시간(UTC)": _ms_to_iso_utc(exit_time_ms),
            "진입주문ID": entry_order_id,
            "청산주문ID": tp_order_id,
            "진입가": str(entry_price),
            "청산가": str(exit_price),
            "수량": str(qty),
            "명목금액USDT": str(notional),
            "PNL_USDT": str(pnl),
            "수익률(%)": str(profit_pct),
            "레버리지적용수익률(%)": str(leveraged_pct),
            "비고": note,
        }
    )
