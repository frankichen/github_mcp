#!/usr/bin/env python3
"""Fetch A-share data from AKShare and emit CSV for the Go pipeline.

This script is intentionally defensive: AKShare column names may vary across
versions and upstream sources. Missing optional fields are emitted as empty or
zero values so the Go selector/backtest pipeline can continue to run.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from typing import Any, Iterable

try:
    sys.stdout.reconfigure(encoding="utf-8", newline="")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def _import_akshare():
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local env
        raise SystemExit(
            "missing python dependency: please run `pip install akshare pandas`"
        ) from exc
    return ak


def norm_date(value: str | None) -> str:
    if not value:
        return dt.date.today().strftime("%Y%m%d")
    return "".join(ch for ch in str(value) if ch.isdigit())


def market_of(code: str) -> str:
    code = str(code).strip()
    lower = code.lower()
    if lower.startswith("sh"):
        return "SH"
    if lower.startswith("sz"):
        return "SZ"
    if lower.startswith("bj"):
        return "BJ"
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return ""


def tx_symbol(code: str) -> str:
    market = market_of(code)
    if market == "SH":
        return "sh" + code
    if market == "SZ":
        return "sz" + code
    return code


def get_value(row: dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        if name in row and row[name] is not None:
            value = row[name]
            if str(value).lower() != "nan":
                return value
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        if text in ("", "-", "--", "None", "nan"):
            return default
        return float(text)
    except Exception:
        return default


def stock_code(value: Any) -> str:
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    lower = text.lower()
    if lower.startswith(("sh", "sz", "bj")) and lower[2:].isdigit():
        text = lower[2:]
    return text.zfill(6) if text.isdigit() else text


SNAPSHOT_FIELDS = [
    "date",
    "code",
    "name",
    "market",
    "close",
    "prev_close",
    "high",
    "low",
    "change_pct",
    "turnover_rate",
    "volume_ratio",
    "amount",
    "market_cap",
    "pe",
    "pb",
    "roe",
    "revenue_growth",
    "net_profit_growth",
    "debt_asset_ratio",
    "rsi6",
    "ma5",
    "ma20",
    "ma60",
    "five_day_pct",
    "twenty_day_pct",
    "limit_up",
    "limit_down",
    "suspended",
    "st",
]

BAR_FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "change",
    "change_pct",
    "volume",
    "amount",
]


def emit_csv(fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})


def fetch_spot(args: argparse.Namespace) -> None:
    ak = _import_akshare()
    try:
        df = ak.stock_zh_a_spot_em()
    except Exception as primary_exc:
        print(
            "akshare stock_zh_a_spot_em failed, falling back to stock_zh_a_spot: "
            f"{primary_exc}",
            file=sys.stderr,
        )
        try:
            df = ak.stock_zh_a_spot()
        except Exception as fallback_exc:
            raise SystemExit(
                "akshare spot fetch failed: "
                f"stock_zh_a_spot_em={primary_exc}; stock_zh_a_spot={fallback_exc}"
            ) from fallback_exc

    rows: list[dict[str, Any]] = []
    today = norm_date(args.trade_date)
    for raw in df.to_dict(orient="records"):
        raw_code = get_value(raw, ["代码", "code", "证券代码"])
        code = stock_code(raw_code)
        if not code:
            continue
        name = str(get_value(raw, ["名称", "name", "证券简称"], ""))
        close = as_float(get_value(raw, ["最新价", "收盘", "close"]))
        prev_close = as_float(get_value(raw, ["昨收", "prev_close"]))
        high = as_float(get_value(raw, ["最高", "high"]))
        low = as_float(get_value(raw, ["最低", "low"]))
        change_pct = as_float(get_value(raw, ["涨跌幅", "change_pct", "pct_chg"]))
        rows.append(
            {
                "date": today,
                "code": code,
                "name": name,
                "market": market_of(raw_code) or market_of(code),
                "close": close,
                "prev_close": prev_close,
                "high": high,
                "low": low,
                "change_pct": change_pct,
                "turnover_rate": as_float(get_value(raw, ["换手率", "turnover_rate"])),
                "volume_ratio": as_float(get_value(raw, ["量比", "volume_ratio"])),
                "amount": as_float(get_value(raw, ["成交额", "amount"])),
                "market_cap": as_float(get_value(raw, ["总市值", "market_cap"])),
                "pe": as_float(get_value(raw, ["市盈率-动态", "市盈率", "pe"])),
                "pb": as_float(get_value(raw, ["市净率", "pb"])),
                "roe": 0,
                "revenue_growth": 0,
                "net_profit_growth": 0,
                "debt_asset_ratio": 0,
                "rsi6": 0,
                "ma5": 0,
                "ma20": 0,
                "ma60": 0,
                "five_day_pct": 0,
                "twenty_day_pct": 0,
                "limit_up": "false",
                "limit_down": "false",
                "suspended": "false",
                "st": "true" if "ST" in name.upper() else "false",
            }
        )
    emit_csv(SNAPSHOT_FIELDS, rows)


def fetch_bars(args: argparse.Namespace) -> None:
    ak = _import_akshare()
    code = stock_code(args.code)
    if not code:
        raise SystemExit("--code is required for bars")
    start = norm_date(args.start_date)
    end = norm_date(args.end_date)
    adjust = args.adjust or ""
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
            timeout=30,
        )
    except Exception as primary_exc:
        symbol = tx_symbol(code)
        print(
            "akshare stock_zh_a_hist failed, falling back to stock_zh_a_hist_tx: "
            f"{primary_exc}",
            file=sys.stderr,
        )
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start,
                end_date=end,
                adjust=adjust,
                timeout=30,
            )
        except Exception as fallback_exc:
            raise SystemExit(
                "akshare bars fetch failed for "
                f"{code}: stock_zh_a_hist={primary_exc}; "
                f"stock_zh_a_hist_tx={fallback_exc}"
            ) from fallback_exc

    rows: list[dict[str, Any]] = []
    previous_close = 0.0
    for raw in df.to_dict(orient="records"):
        close = as_float(get_value(raw, ["收盘", "close"]))
        change = as_float(get_value(raw, ["涨跌额", "change"]))
        change_pct = as_float(get_value(raw, ["涨跌幅", "change_pct", "pct_chg"]))
        prev_close = previous_close
        if prev_close == 0 and close != 0 and change != 0:
            prev_close = close - change
        if change == 0 and prev_close > 0 and close != 0:
            change = close - prev_close
        if change_pct == 0 and prev_close > 0:
            change_pct = change / prev_close * 100
        rows.append(
            {
                "date": norm_date(str(get_value(raw, ["日期", "date", "trade_date"]))),
                "code": code,
                "open": as_float(get_value(raw, ["开盘", "open"])),
                "high": as_float(get_value(raw, ["最高", "high"])),
                "low": as_float(get_value(raw, ["最低", "low"])),
                "close": close,
                "prev_close": prev_close,
                "change": change,
                "change_pct": change_pct,
                "volume": as_float(get_value(raw, ["成交量", "volume", "vol"])),
                "amount": as_float(get_value(raw, ["成交额", "amount"])),
            }
        )
        if close > 0:
            previous_close = close
    emit_csv(BAR_FIELDS, rows)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AKShare CSV bridge for auto_gupiao")
    parser.add_argument("--type", choices=["spot", "bars"], required=True)
    parser.add_argument("--code", default="")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--adjust", default="qfq", help="AKShare adjust option, e.g. qfq/hfq/empty")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.type == "spot":
        fetch_spot(args)
        return 0
    if args.type == "bars":
        fetch_bars(args)
        return 0
    raise SystemExit(f"unsupported type: {args.type}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
