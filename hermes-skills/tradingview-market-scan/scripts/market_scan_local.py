#!/usr/bin/env python3
"""Full OHLCV local recalculation scanner for the TradingView watch universe.

Data source: yfinance OHLCV. Indicators are recalculated locally to match the
user's Pine formulas more closely than TradingView scanner indicator fields.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import contextlib
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
DEFAULT_SYMBOLS = ROOT / "symbols.json"
DEFAULT_STATE_DIR = Path(os.environ.get("TV_SCAN_STATE_DIR", ROOT.parent.parent.parent / "automation" / "state"))
YF_CACHE_DIR = ROOT.parent.parent.parent / "data" / "yfinance-cache"

DENSE = "\u5747\u7ebf\u5bc6\u96c6"
PULL20 = "\u56de\u8e2920"
PULL60 = "\u56de\u8e2960"
WEEKLY_J_LT_ZERO = "\u5468\u7ebfJ<0"

KDJ_MAX_BONUS = 35
WEEKLY_J_LT_ZERO_EXTRA_BONUS = 15

YAHOO_OVERRIDES = {
    "NASDAQ:NDX": "^NDX",
    "BOATS:DRAM": "DRAM",
    "SSE:000001": "000001.SS",
    "SSE:600519": "600519.SS",
    "HKEX:700": "0700.HK",
    "HKEX:1810": "1810.HK",
    "KRX:000660": "000660.KS",
    "COINBASE:BTCUSD": "BTC-USD",
    "BITSTAMP:BTCUSD": "BTC-USD",
    "COINBASE:ETHUSD": "ETH-USD",
    "BINANCE:ETHBTC": "ETH-BTC",
    "BINANCE:SOLUSDT": "SOL-USD",
    "BINANCE:BNBUSDT": "BNB-USD",
    "BITGET:HYPEUSDT": "HYPE32196-USD",
    "BITGET:BGBUSDT": "BGB-USD",
    "BINANCE:DOGEUSDT": "DOGE-USD",
    "BINANCE:LTCUSDT": "LTC-USD",
    "BINANCE:PEPEUSDT": "PEPE24478-USD",
    "BINANCE:AVAXUSDT": "AVAX-USD",
    "BINANCE:ADAUSDT": "ADA-USD",
    "BINANCE:XRPUSDT": "XRP-USD",
    "BINANCE:NEIROUSDT": "NEIRO-USD",
    "CME:BTC1!": "BTC=F",
    "OANDA:XAUUSD": "GC=F",
    "OANDA:XAGUSD": "SI=F",
    "TVC:SILVER": "SI=F",
}


def tv_to_yahoo(symbol: str) -> str | None:
    if symbol in YAHOO_OVERRIDES:
        return YAHOO_OVERRIDES[symbol]
    if ":" not in symbol:
        return symbol
    exchange, ticker = symbol.split(":", 1)
    if exchange in {"NASDAQ", "NYSE", "AMEX"}:
        return ticker
    if exchange == "HKEX" and ticker.isdigit():
        return f"{int(ticker):04d}.HK"
    if exchange == "SSE":
        return f"{ticker}.SS"
    if exchange == "SZSE":
        return f"{ticker}.SZ"
    if exchange == "KRX":
        return f"{ticker}.KS"
    return None


@dataclass
class Thresholds:
    dense_atr: float = 2.05
    dense_price_atr: float = 0.50
    dense_width_pct: float = 0.08
    pullback_approach_atr: float = 0.35
    pullback_approach_pct: float = 0.03
    pullback_close_above_atr: float = 0.00
    pullback_break_low_atr: float = 0.35
    pullback_break_close_atr: float = 0.25
    max_items_per_section: int = 30


@dataclass
class Candidate:
    symbol: str
    yahoo: str
    name: str
    market: str
    close: float
    change: float | None
    density: float | None
    kind: str
    reason: str
    tags: list[str] = field(default_factory=list)
    j: float | None = None
    prev_j: float | None = None
    macd: str | None = None
    macd_divergence: str = ""
    score: int = 0
    kdj_note: str = ""
    fresh_pullback: bool = False
    source: str = "yfinance-local"

    def sort_tuple(self) -> tuple[int, int, float, float]:
        kind_score = {WEEKLY_J_LT_ZERO: -1, DENSE: 0, PULL20: 1, PULL60: 1}.get(self.kind, 9)
        density = self.density if self.density is not None else 999.0
        j_value = self.j if self.j is not None else 999.0
        return (kind_score, -self.score, j_value, density)


def load_symbols(path: Path) -> dict[str, list[str]]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return {str(k): list(dict.fromkeys(v)) for k, v in data.items() if v}


def clean_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def clamp_score(value: float) -> int:
    return max(0, min(100, round(value)))


def kdj_j_bonus(j: float | None, max_bonus: int = KDJ_MAX_BONUS, start: float = 20.0) -> int:
    if j is None or j >= start:
        return 0
    raw = ((start - j) / start) * max_bonus
    return max(0, min(max_bonus, round(raw)))


def rma(series: pd.Series, length: int) -> pd.Series:
    values = series.astype(float).to_list()
    out: list[float] = [math.nan] * len(values)
    alpha = 1.0 / length
    valid_window: list[float] = []
    prev: float | None = None
    for idx, value in enumerate(values):
        if value is None or math.isnan(value):
            continue
        if prev is None:
            valid_window.append(value)
            if len(valid_window) == length:
                prev = sum(valid_window) / length
                out[idx] = prev
        else:
            prev = alpha * value + (1.0 - alpha) * prev
            out[idx] = prev
    return pd.Series(out, index=series.index)


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.astype(float).ewm(span=length, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    parts = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1)
    return parts.max(axis=1)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]).lower().replace(" ", "_") for col in df.columns]
    else:
        df.columns = [str(col).lower().replace(" ", "_") for col in df.columns]
    needed = ["open", "high", "low", "close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    df = df[needed].copy()
    for col in needed:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def fetch_yahoo_chart(yahoo: str, timeframe: str, period: str) -> pd.DataFrame:
    interval = "1d" if timeframe == "daily" else "1wk"
    encoded = urllib.parse.quote(yahoo, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={period}&interval={interval}&events=history"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    chart = data.get("chart", {})
    if chart.get("error"):
        raise ValueError(chart["error"])
    result = chart.get("result") or []
    if not result:
        return pd.DataFrame()
    payload = result[0]
    timestamps = payload.get("timestamp") or []
    quote = ((payload.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not quote:
        return pd.DataFrame()
    df = pd.DataFrame({
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": quote.get("close"),
        "volume": quote.get("volume"),
    }, index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize())
    return normalize_ohlcv(df)


def fetch_ohlcv(yahoo: str, timeframe: str, bars: int) -> pd.DataFrame:
    interval = "1d" if timeframe == "daily" else "1wk"
    periods = ["3y", "2y", "1y", "6mo", "3mo"] if timeframe == "daily" else ["10y", "5y", "3y", "1y", "6mo"]
    last_error: Exception | None = None
    for attempt in range(3):
        for period in periods:
            try:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    df = yf.download(yahoo, period=period, interval=interval, progress=False, auto_adjust=False, threads=False)
                df = normalize_ohlcv(df)
                if not df.empty:
                    if len(df) > bars:
                        df = df.tail(bars).copy()
                    return df
            except Exception as exc:
                last_error = exc
            try:
                df = fetch_yahoo_chart(yahoo, timeframe, period)
                if not df.empty:
                    if len(df) > bars:
                        df = df.tail(bars).copy()
                    return df
            except Exception as exc:
                last_error = exc
        time.sleep(1.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["SMA20"] = close.rolling(20).mean()
    out["SMA60"] = close.rolling(60).mean()
    out["SMA120"] = close.rolling(120).mean()
    out["EMA20"] = ema(close, 20)
    out["EMA60"] = ema(close, 60)
    out["EMA120"] = ema(close, 120)
    out["ATR"] = rma(true_range(out), 14)

    lowest_low = out["low"].rolling(9).min()
    highest_high = out["high"].rolling(9).max()
    denom = highest_high - lowest_low
    rsv = ((close - lowest_low) / denom * 100.0).where(denom != 0, 0.0)
    out["K"] = rma(rsv, 3)
    out["D"] = rma(out["K"], 3)
    out["J"] = 3 * out["K"] - 2 * out["D"]

    out["MACD_DIF"] = ema(close, 12) - ema(close, 26)
    out["MACD_DEA"] = ema(out["MACD_DIF"], 9)
    out["MACD_HIST"] = out["MACD_DIF"] - out["MACD_DEA"]
    return out


def kdj_note(j: float | None, prev_j: float | None) -> tuple[str, bool]:
    if j is None:
        return "KDJ no data", False
    hook = prev_j is not None and j > prev_j
    if prev_j is None:
        return f"J{j:.1f}", False
    if j < 0:
        return f"J{prev_j:.1f}->{j:.1f} J<0", hook
    if j < 20:
        return f"J{prev_j:.1f}->{j:.1f} J<20", hook
    if hook:
        return f"J{prev_j:.1f}->{j:.1f} hook", hook
    return f"J{prev_j:.1f}->{j:.1f}", False


def recent_macd_divergence(df: pd.DataFrame, pivot_left: int = 5, pivot_right: int = 5, min_bars: int = 5, max_bars: int = 80, recent_confirm_bars: int = 15) -> tuple[str, int]:
    n = len(df)
    high_pivots: list[int] = []
    low_pivots: list[int] = []
    events: list[tuple[int, str, int]] = []
    for pivot in range(pivot_left, n - pivot_right):
        left = pivot - pivot_left
        right = pivot + pivot_right + 1
        high = df["high"].iloc[pivot]
        low = df["low"].iloc[pivot]
        if pd.isna(high) or pd.isna(low):
            continue
        is_high = high >= df["high"].iloc[left:right].max()
        is_low = low <= df["low"].iloc[left:right].min()
        confirm = pivot + pivot_right
        if is_high:
            if high_pivots:
                prev = high_pivots[-1]
                distance = pivot - prev
                if min_bars <= distance <= max_bars:
                    curr_price = df["high"].iloc[pivot]
                    prev_price = df["high"].iloc[prev]
                    curr_dif = df["MACD_DIF"].iloc[pivot]
                    prev_dif = df["MACD_DIF"].iloc[prev]
                    curr_hist = df["MACD_HIST"].iloc[pivot]
                    prev_hist = df["MACD_HIST"].iloc[prev]
                    if curr_price > prev_price and (curr_dif < prev_dif or curr_hist < prev_hist):
                        events.append((confirm, "MACD_BEAR_DIV", -8))
                    elif curr_price < prev_price and (curr_dif > prev_dif or curr_hist > prev_hist):
                        events.append((confirm, "MACD_HIDDEN_BEAR_DIV", -5))
            high_pivots.append(pivot)
        if is_low:
            if low_pivots:
                prev = low_pivots[-1]
                distance = pivot - prev
                if min_bars <= distance <= max_bars:
                    curr_price = df["low"].iloc[pivot]
                    prev_price = df["low"].iloc[prev]
                    curr_dif = df["MACD_DIF"].iloc[pivot]
                    prev_dif = df["MACD_DIF"].iloc[prev]
                    curr_hist = df["MACD_HIST"].iloc[pivot]
                    prev_hist = df["MACD_HIST"].iloc[prev]
                    if curr_price < prev_price and (curr_dif > prev_dif or curr_hist > prev_hist):
                        events.append((confirm, "MACD_BULL_DIV", 12))
                    elif curr_price > prev_price and (curr_dif < prev_dif or curr_hist < prev_hist):
                        events.append((confirm, "MACD_HIDDEN_BULL_DIV", 6))
            low_pivots.append(pivot)
    if not events:
        return "", 0
    recent = [(idx, note, score) for idx, note, score in events if n - 1 - idx <= recent_confirm_bars]
    if not recent:
        return "", 0
    idx, note, score = recent[-1]
    age = n - 1 - idx
    return f"{note}@{age}bars", score


def weekly_j_lt_zero_candidate(
    tv_symbol: str,
    yahoo: str,
    market: str,
    df: pd.DataFrame,
) -> Candidate | None:
    """Build the independent weekly J<0 candidate without requiring 120-week MAs."""
    if len(df) < 2:
        return None
    row = df.iloc[-1]
    prev = df.iloc[-2]
    close = clean_float(row.get("close"))
    j = clean_float(row.get("J"))
    if close is None or j is None or j >= 0:
        return None

    prev_j = clean_float(prev.get("J"))
    note, _ = kdj_note(j, prev_j)
    prev_close = clean_float(prev.get("close"))
    change = (close / prev_close - 1.0) * 100.0 if prev_close else None
    atr = clean_float(row.get("ATR"))
    ma_values = [clean_float(row.get(key)) for key in ["SMA20", "EMA20", "SMA60", "EMA60", "SMA120", "EMA120"]]
    density = None
    if atr is not None and atr > 0 and all(value is not None for value in ma_values):
        valid_ma_values = [value for value in ma_values if value is not None]
        density = (max(valid_ma_values) - min(valid_ma_values)) / atr

    dif = clean_float(row.get("MACD_DIF"))
    dea = clean_float(row.get("MACD_DEA"))
    macd = None if dif is None or dea is None else ("DIF>=DEA" if dif >= dea else "DIF<DEA")
    macd_div, _ = recent_macd_divergence(df)
    total_kdj_weight = KDJ_MAX_BONUS + WEEKLY_J_LT_ZERO_EXTRA_BONUS
    return Candidate(
        symbol=tv_symbol,
        yahoo=yahoo,
        name=tv_symbol.split(":")[-1],
        market=market,
        close=close,
        change=change,
        density=density,
        kind=WEEKLY_J_LT_ZERO,
        reason=(
            f"\u5468\u7ebfKDJ J\u503c{j:.1f}<0\uff0cKDJ\u9ad8\u6743\u91cd +{total_kdj_weight}\u5206\uff1b"
            "\u8be5\u540d\u5355\u4e0d\u8981\u6c42\u540c\u65f6\u6ee1\u8db3\u5747\u7ebf\u5bc6\u96c6\u6216\u56de\u8e29\u6761\u4ef6"
        ),
        tags=["J<0"],
        j=j,
        prev_j=prev_j,
        macd=macd,
        macd_divergence=macd_div,
        score=clamp_score(50 + total_kdj_weight),
        kdj_note=note,
    )


def classify_frame(
    tv_symbol: str,
    yahoo: str,
    market: str,
    df: pd.DataFrame,
    th: Thresholds,
    crypto_dense_only: bool = False,
    timeframe: str = "daily",
) -> list[Candidate]:
    if len(df) < 130:
        return []
    row = df.iloc[-1]
    prev = df.iloc[-2]
    close = clean_float(row["close"])
    high = clean_float(row["high"])
    low = clean_float(row["low"])
    prev_low = clean_float(prev["low"])
    atr = clean_float(row["ATR"])
    if close is None or high is None or low is None or atr is None or atr <= 0:
        return []
    ma20 = clean_float(row["SMA20"])
    ema20 = clean_float(row["EMA20"])
    ma60 = clean_float(row["SMA60"])
    ema60 = clean_float(row["EMA60"])
    ma120 = clean_float(row["SMA120"])
    ema120 = clean_float(row["EMA120"])
    if None in (ma20, ema20, ma60, ema60, ma120, ema120):
        return []

    lines = [ma20, ema20, ma60, ema60, ma120, ema120]
    line_high = max(lines)
    line_low = min(lines)
    density = (line_high - line_low) / atr
    width_pct = (line_high - line_low) / close
    price_dist = 0.0 if line_low <= close <= line_high else min(abs(close - line_low), abs(close - line_high)) / atr
    j = clean_float(row["J"])
    prev_j = clean_float(prev["J"])
    note, j_hook = kdj_note(j, prev_j)
    macd = "DIF>=DEA" if clean_float(row["MACD_DIF"]) is not None and clean_float(row["MACD_DEA"]) is not None and row["MACD_DIF"] >= row["MACD_DEA"] else "DIF<DEA"
    macd_div, macd_div_score = recent_macd_divergence(df)
    change = None
    prev_close = clean_float(prev["close"])
    if prev_close:
        change = (close / prev_close - 1.0) * 100.0
    tags: list[str] = []
    if j is not None and j < 0:
        tags.append("J<0")
    elif j is not None and j < 20:
        tags.append("J<20")

    base = dict(
        symbol=tv_symbol,
        yahoo=yahoo,
        name=tv_symbol.split(":")[-1],
        market=market,
        close=close,
        change=change,
        density=density,
        tags=tags,
        j=j,
        prev_j=prev_j,
        macd=macd,
        macd_divergence=macd_div,
    )
    candidates: list[Candidate] = []
    weekly_j_extra_bonus = WEEKLY_J_LT_ZERO_EXTRA_BONUS if timeframe == "weekly" and j is not None and j < 0 else 0

    above_20_group = close > max(ma20, ema20)
    if above_20_group and density <= th.dense_atr and width_pct <= th.dense_width_pct and price_dist <= th.dense_price_atr:
        kdj_bonus = kdj_j_bonus(j) + weekly_j_extra_bonus
        score = clamp_score(100 - (density / th.dense_atr) * 45 - (price_dist / th.dense_price_atr) * 30 + kdj_bonus + (5 if change and change > 0 else 0) + macd_div_score)
        candidates.append(Candidate(
            kind=DENSE,
            reason=f"six-line width {density:.2f}ATR/{width_pct * 100:.1f}%, price distance {price_dist:.2f}ATR, close above 20 group",
            score=score,
            kdj_note=note,
            **base,
        ))

    if crypto_dense_only:
        return candidates

    group20_low, group20_high = min(ma20, ema20), max(ma20, ema20)
    group60_low, group60_high = min(ma60, ema60), max(ma60, ema60)
    group120_low, group120_high = min(ma120, ema120), max(ma120, ema120)
    uptrend = group20_low > group60_high and group60_low > group120_high and close > group120_high

    def approach_band(group_high: float) -> float:
        return min(atr * th.pullback_approach_atr, group_high * th.pullback_approach_pct)

    def zone_distance(value: float, group_low: float, group_high: float) -> float:
        if group_low <= value <= group_high:
            return 0.0
        return min(abs(value - group_low), abs(value - group_high))

    def pullback_candidate(kind: str, period: int, group_low: float, group_high: float) -> dict[str, float | int | str] | None:
        band = approach_band(group_high)
        near_from_above = low <= group_high + band
        not_broken = low >= group_low - atr * th.pullback_break_low_atr and close >= group_low - atr * th.pullback_break_close_atr
        first_near = prev_low is not None and prev_low > group_high + band
        touched_zone = low <= group_high
        close_still_in_zone = close <= group_high + atr * th.pullback_close_above_atr
        if not (uptrend and near_from_above and not_broken and close_still_in_zone and (first_near or touched_zone)):
            return None
        close_distance_atr = zone_distance(close, group_low, group_high) / atr
        low_distance_atr = zone_distance(low, group_low, group_high) / atr
        score = 44 + 15 + max(0, 10 - close_distance_atr * 12)
        score += kdj_j_bonus(j) + weekly_j_extra_bonus
        if j_hook:
            score += 15
        if change and change > 0:
            score += 3
        score += macd_div_score
        return {
            "kind": kind,
            "period": period,
            "group_low": group_low,
            "group_high": group_high,
            "close_distance_atr": close_distance_atr,
            "low_distance_atr": low_distance_atr,
            "score": clamp_score(score),
        }

    pullbacks = [
        item for item in [
            pullback_candidate(PULL20, 20, group20_low, group20_high),
            pullback_candidate(PULL60, 60, group60_low, group60_high),
        ]
        if item is not None
    ]
    if pullbacks:
        nearest = min(pullbacks, key=lambda item: (float(item["close_distance_atr"]), float(item["low_distance_atr"]), int(item["period"])))
        candidates.append(Candidate(
            kind=str(nearest["kind"]),
            reason=(
                f"uptrend, nearest MA/EMA{int(nearest['period'])} zone "
                f"{float(nearest['group_low']):.2f}-{float(nearest['group_high']):.2f}, "
                f"close distance {float(nearest['close_distance_atr']):.2f}ATR, "
                f"low distance {float(nearest['low_distance_atr']):.2f}ATR"
            ),
            score=int(nearest["score"]),
            kdj_note=note,
            fresh_pullback=True,
            **base,
        ))
    return candidates

def scan(symbol_file: Path, timeframe: str, th: Thresholds, crypto_dense_only: bool = False, bars: int = 420) -> dict[str, Any]:
    try:
        YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(YF_CACHE_DIR))
    except Exception:
        pass
    symbols = load_symbols(symbol_file)
    requested = [symbol for tickers in symbols.values() for symbol in tickers]
    rows_count = 0
    missing: list[str] = []
    errors: list[str] = []
    sections: dict[str, list[Candidate]] = {DENSE: [], PULL20: [], PULL60: []}
    if timeframe == "weekly":
        sections[WEEKLY_J_LT_ZERO] = []
    for market, tickers in symbols.items():
        for tv_symbol in tickers:
            yahoo = tv_to_yahoo(tv_symbol)
            if not yahoo:
                missing.append(tv_symbol)
                errors.append(f"no yahoo mapping: {tv_symbol}")
                continue
            try:
                df = fetch_ohlcv(yahoo, timeframe, bars)
                if df.empty:
                    missing.append(tv_symbol)
                    errors.append(f"empty ohlcv: {tv_symbol}->{yahoo}")
                    continue
                ind = add_indicators(df)
                rows_count += 1
                if timeframe == "weekly":
                    weekly_candidate = weekly_j_lt_zero_candidate(tv_symbol, yahoo, market, ind)
                    if weekly_candidate is not None:
                        sections[WEEKLY_J_LT_ZERO].append(weekly_candidate)
                for cand in classify_frame(
                    tv_symbol,
                    yahoo,
                    market,
                    ind,
                    th,
                    crypto_dense_only=crypto_dense_only,
                    timeframe=timeframe,
                ):
                    sections.setdefault(cand.kind, []).append(cand)
            except Exception as exc:
                missing.append(tv_symbol)
                errors.append(f"{tv_symbol}->{yahoo}: {type(exc).__name__}: {exc}")
    allowed_sections = [DENSE] if crypto_dense_only else [DENSE, PULL20, PULL60]
    if timeframe == "weekly":
        allowed_sections.insert(0, WEEKLY_J_LT_ZERO)
    for key in list(sections):
        if key not in allowed_sections:
            sections[key] = []
        else:
            sections[key] = sorted(sections[key], key=lambda c: c.sort_tuple())[:th.max_items_per_section]
    return {
        "timeframe": timeframe,
        "source": "yfinance-local-recalc",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "symbols_count": len(requested),
        "rows_count": rows_count,
        "missing_symbols": list(dict.fromkeys(missing)),
        "sections": sections,
        "errors": errors,
    }


def candidate_to_dict(c: Candidate) -> dict[str, Any]:
    return {
        "symbol": c.symbol,
        "yahoo": c.yahoo,
        "name": c.name,
        "market": c.market,
        "close": c.close,
        "change": c.change,
        "density": c.density,
        "kind": c.kind,
        "reason": c.reason,
        "tags": c.tags,
        "j": c.j,
        "prev_j": c.prev_j,
        "macd": c.macd,
        "macd_divergence": c.macd_divergence,
        "score": c.score,
        "kdj_note": c.kdj_note,
        "fresh_pullback": c.fresh_pullback,
        "source": c.source,
    }


def serializable(result: dict[str, Any]) -> dict[str, Any]:
    copied = dict(result)
    copied["sections"] = {key: [candidate_to_dict(c) for c in values] for key, values in result["sections"].items()}
    return copied


def save_state(result: dict[str, Any], state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"latest_{result['timeframe']}_local.json"
    path.write_text(json.dumps(serializable(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def self_test() -> None:
    close = pd.Series(range(1, 160), dtype=float)
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    ind = add_indicators(df)
    assert not pd.isna(ind["SMA120"].iloc[-1])
    assert not pd.isna(ind["J"].iloc[-1])
    assert not pd.isna(ind["MACD_DIF"].iloc[-1])
    weekly_test = ind.copy()
    weekly_test.loc[weekly_test.index[-2], "J"] = -8.0
    weekly_test.loc[weekly_test.index[-1], "J"] = -5.0
    weekly_candidate = weekly_j_lt_zero_candidate("NASDAQ:TEST", "TEST", "america", weekly_test)
    assert weekly_candidate is not None and weekly_candidate.kind == WEEKLY_J_LT_ZERO
    assert weekly_candidate.score == 100
    assert kdj_j_bonus(-1.0) == KDJ_MAX_BONUS
    assert KDJ_MAX_BONUS + WEEKLY_J_LT_ZERO_EXTRA_BONUS == 50
    print("self-test passed")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan watch universe using full OHLCV local recalculation.")
    parser.add_argument("--timeframe", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--symbols", type=Path, default=DEFAULT_SYMBOLS)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--crypto-dense-only", action="store_true")
    parser.add_argument("--max-items", type=int, default=Thresholds.max_items_per_section)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    th = Thresholds(max_items_per_section=args.max_items)
    started = time.time()
    result = scan(args.symbols, args.timeframe, th, crypto_dense_only=args.crypto_dense_only)
    result["elapsed_seconds"] = round(time.time() - started, 2)
    save_state(result, args.state_dir)
    print(json.dumps(serializable(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
