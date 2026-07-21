#!/usr/bin/env python3
"""Strict completed-bar scanner for the TradingView watch universe.

Equities use repaired and validated Yahoo OHLCV. Crypto uses the public market
API of the exact exchange named by the TradingView symbol. The scanner fails
closed for venue/proxy substitutions: a missing exact-enough data source is
reported instead of silently producing an approximate signal.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any
from threading import Lock
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
KDJ_N = 9
KDJ_M1 = 3
KDJ_M2 = 3
FORMULA_VERSION = "2026-07-21-v3"
INDICATOR_SPEC = "KDJ(9,3,3,RMA); SMA/EMA(20,60,120); ATR(14,RMA); MACD(12,26,9,EMA)"

YAHOO_OVERRIDES = {
    "NASDAQ:NDX": "^NDX",
    "NASDAQ:DRAM": "DRAM",
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

# These mappings point to a different venue, quote currency, session, contract,
# or aggregation. They remain documented for optional diagnostics, but strict
# reports must never treat them as the requested TradingView instrument.
APPROXIMATE_MAPPINGS = {
    "BOATS:DRAM": "BOATS overnight session is not the NASDAQ regular listing",
    "COINBASE:BTCUSD": "Yahoo BTC-USD is not Coinbase BTCUSD",
    "BITSTAMP:BTCUSD": "Yahoo BTC-USD is not Bitstamp BTCUSD",
    "COINBASE:ETHUSD": "Yahoo ETH-USD is not Coinbase ETHUSD",
    "BINANCE:ETHBTC": "Yahoo ETH-BTC is not Binance ETHBTC",
    "BINANCE:SOLUSDT": "Yahoo SOL-USD is not Binance SOLUSDT",
    "BINANCE:BNBUSDT": "Yahoo BNB-USD is not Binance BNBUSDT",
    "BITGET:HYPEUSDT": "Yahoo HYPE-USD is not Bitget HYPEUSDT",
    "BITGET:BGBUSDT": "Yahoo BGB-USD is not Bitget BGBUSDT",
    "BINANCE:DOGEUSDT": "Yahoo DOGE-USD is not Binance DOGEUSDT",
    "BINANCE:LTCUSDT": "Yahoo LTC-USD is not Binance LTCUSDT",
    "BINANCE:PEPEUSDT": "Yahoo PEPE-USD is not Binance PEPEUSDT",
    "BINANCE:AVAXUSDT": "Yahoo AVAX-USD is not Binance AVAXUSDT",
    "BINANCE:ADAUSDT": "Yahoo ADA-USD is not Binance ADAUSDT",
    "BINANCE:XRPUSDT": "Yahoo XRP-USD is not Binance XRPUSDT",
    "BINANCE:NEIROUSDT": "Yahoo NEIRO-USD is not Binance NEIROUSDT",
    "CME:BTC1!": "Yahoo BTC=F continuous contract may use a different roll rule",
    "OANDA:XAUUSD": "COMEX gold futures are not OANDA spot XAUUSD",
    "OANDA:XAGUSD": "COMEX silver futures are not OANDA spot XAGUSD",
    "TVC:SILVER": "COMEX silver futures are not the TVC spot composite",
}

OFFICIAL_CRYPTO_VENUES = {"BINANCE", "BITGET", "COINBASE", "BITSTAMP", "OKX"}
CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "USD", "BTC", "ETH", "EUR", "GBP")
SSE_ETF_LOCK = Lock()
SSE_ETF_SPLITS: dict[str, tuple[tuple[str, float], ...]] = {
    # Effective dates and ratios verified against Shanghai Stock Exchange notices.
    "560780": (("2026-06-26", 3.0),),
    "561980": (("2026-06-26", 5.0),),
    "588170": (("2026-07-06", 3.0),),
}


def is_sse_etf(symbol: str) -> bool:
    return symbol.startswith("SSE:5") and symbol.split(":", 1)[1].isdigit()


def exchange_of(symbol: str) -> str:
    return symbol.split(":", 1)[0].upper() if ":" in symbol else ""


def has_exact_source(symbol: str) -> bool:
    return exchange_of(symbol) in OFFICIAL_CRYPTO_VENUES


def split_crypto_ticker(ticker: str) -> tuple[str, str]:
    ticker = ticker.upper().replace("-", "").replace("/", "")
    for quote in CRYPTO_QUOTES:
        if ticker.endswith(quote) and len(ticker) > len(quote):
            return ticker[:-len(quote)], quote
    raise ValueError(f"unsupported crypto ticker format: {ticker}")


def official_crypto_id(symbol: str) -> str:
    exchange, ticker = symbol.split(":", 1)
    base, quote = split_crypto_ticker(ticker)
    exchange = exchange.upper()
    if exchange in {"COINBASE", "OKX"}:
        return f"{base}-{quote}"
    if exchange == "BITSTAMP":
        return f"{base}{quote}".lower()
    return f"{base}{quote}"


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
    source: str = "yfinance-repaired"
    bar_date: str = ""
    bar_status: str = "confirmed"
    data_quality: str = "OHLC validation passed"

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
    calendar_index = pd.to_datetime(df.index)
    if calendar_index.tz is not None:
        calendar_index = calendar_index.tz_localize(None)
    df.index = calendar_index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["volume"] = df["volume"].fillna(0.0)
    return df


def validate_ohlcv(df: pd.DataFrame, allow_extreme_wicks: bool = False) -> None:
    """Reject malformed or obviously corrupt bars before they affect indicators."""
    if df.empty:
        raise ValueError("empty OHLCV")
    prices = df[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        raise ValueError("non-positive OHLC price")
    if (df["volume"] < 0).any():
        raise ValueError("negative volume")
    envelope_bad = (df["high"] < prices[["open", "low", "close"]].max(axis=1)) | (
        df["low"] > prices[["open", "high", "close"]].min(axis=1)
    )
    if envelope_bad.any():
        dates = ",".join(str(value.date()) for value in df.index[envelope_bad][:3])
        raise ValueError(f"invalid OHLC envelope at {dates}")

    # A single 3x wick relative to the other prices in the same candle is far
    # outside the intended universe and previously corrupted a weekly KDJ value.
    if not allow_extreme_wicks:
        high_reference = prices[["open", "low", "close"]].max(axis=1)
        low_reference = prices[["open", "high", "close"]].min(axis=1)
        extreme_wick = (df["high"] > high_reference * 3.0) | (df["low"] * 3.0 < low_reference)
        if extreme_wick.any():
            dates = ",".join(str(value.date()) for value in df.index[extreme_wick][:3])
            raise ValueError(f"extreme isolated wick at {dates}")


MARKET_SESSIONS = {
    "america": ("America/New_York", clock_time(18, 0)),
    "china": ("Asia/Shanghai", clock_time(16, 30)),
    "hongkong": ("Asia/Hong_Kong", clock_time(17, 0)),
    "korea": ("Asia/Seoul", clock_time(16, 30)),
}


def drop_unconfirmed_rows(
    df: pd.DataFrame,
    market: str,
    now_utc: datetime | None = None,
) -> pd.DataFrame:
    """Keep only bars whose market session is safely past its close buffer."""
    if df.empty:
        return df
    current = now_utc or datetime.now(timezone.utc)
    if market == "crypto":
        cutoff = pd.Timestamp(current.astimezone(timezone.utc).date())
    elif market in MARKET_SESSIONS:
        timezone_name, safe_close = MARKET_SESSIONS[market]
        local = current.astimezone(ZoneInfo(timezone_name))
        cutoff_date = local.date() + (timedelta(days=1) if local.time() >= safe_close else timedelta())
        cutoff = pd.Timestamp(cutoff_date)
    else:
        return df
    return df.loc[df.index < cutoff].copy()


def aggregate_weekly(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """Build only fully completed weekly bars using explicit market boundaries."""
    rule = "W-SUN" if market == "crypto" else "W-FRI"
    weekly = df.resample(rule, label="right", closed="right").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    weekly["_bar_end_date"] = df["close"].resample(rule, label="right", closed="right").apply(
        lambda values: values.index.max() if not values.empty else pd.NaT
    )
    weekly = weekly.dropna(subset=["open", "high", "low", "close"])

    current = datetime.now(timezone.utc)
    if market == "crypto":
        # A UTC crypto week is Monday 00:00 through Sunday 23:59:59.
        current_date = current.date()
        last_completed_end = current_date - timedelta(days=current_date.weekday() + 1)
    else:
        timezone_name, safe_close = MARKET_SESSIONS.get(
            market,
            ("America/New_York", clock_time(18, 0)),
        )
        local = current.astimezone(ZoneInfo(timezone_name))
        days_since_friday = (local.date().weekday() - 4) % 7
        if days_since_friday == 0 and local.time() < safe_close:
            days_since_friday = 7
        last_completed_end = local.date() - timedelta(days=days_since_friday)

    return weekly.loc[weekly.index <= pd.Timestamp(last_completed_end)].copy()


def latest_bar_date(df: pd.DataFrame) -> str:
    value = df["_bar_end_date"].iloc[-1] if "_bar_end_date" in df.columns else df.index[-1]
    return pd.Timestamp(value).date().isoformat()


def fetch_yahoo_chart(yahoo: str, period: str) -> pd.DataFrame:
    interval = "1d"
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


def fetch_json(url: str, attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "tradingview-strategy-report/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1))
    assert last_error is not None
    raise last_error


def crypto_frame_from_rows(
    rows: list[Any],
    positions: tuple[int, int, int, int, int, int],
    timestamp_unit: str = "ms",
) -> pd.DataFrame:
    timestamp_pos, open_pos, high_pos, low_pos, close_pos, volume_pos = positions
    records: list[dict[str, Any]] = []
    timestamps: list[Any] = []
    minimum_width = max(positions) + 1
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < minimum_width:
            continue
        timestamps.append(row[timestamp_pos])
        records.append({
            "open": row[open_pos],
            "high": row[high_pos],
            "low": row[low_pos],
            "close": row[close_pos],
            "volume": row[volume_pos],
        })
    if not records:
        return pd.DataFrame()
    index = pd.to_datetime(pd.Series(timestamps, dtype="float64"), unit=timestamp_unit, utc=True)
    frame = pd.DataFrame(records, index=pd.DatetimeIndex(index).tz_convert(None))
    return normalize_ohlcv(frame)


def finalize_crypto_frame(
    df: pd.DataFrame,
    timeframe: str,
    bars: int,
    source: str,
    confirmation: str,
) -> pd.DataFrame:
    validate_ohlcv(df, allow_extreme_wicks=True)
    if len(df) > bars:
        df = df.tail(bars).copy()
    if timeframe == "weekly" and "_bar_end_date" not in df.columns:
        df["_bar_end_date"] = df.index + pd.Timedelta(days=6)
    df.attrs["source"] = source
    df.attrs["data_quality"] = f"official venue OHLCV; {confirmation}; OHLC envelope passed; venue wicks preserved"
    return df


def fetch_binance_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    instrument = official_crypto_id(symbol)
    interval = "1d" if timeframe == "daily" else "1w"
    params = urllib.parse.urlencode({
        "symbol": instrument,
        "interval": interval,
        "timeZone": "0",
        "limit": min(1000, max(200, bars + 3)),
    })
    rows = fetch_json(f"https://data-api.binance.vision/api/v3/klines?{params}")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    confirmed = [row for row in rows if isinstance(row, list) and len(row) >= 7 and int(row[6]) < now_ms]
    df = crypto_frame_from_rows(confirmed, (0, 1, 2, 3, 4, 5))
    return finalize_crypto_frame(
        df,
        timeframe,
        bars,
        f"Binance Spot official API ({instrument}, UTC {interval})",
        "exchange closeTime passed",
    )


def fetch_bitget_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    instrument = official_crypto_id(symbol)
    interval = "1Dutc" if timeframe == "daily" else "1Wutc"
    params = urllib.parse.urlencode({
        "symbol": instrument,
        "granularity": interval,
        "limit": min(1000, max(200, bars + 3)),
    })
    payload = fetch_json(f"https://api.bitget.com/api/v2/spot/market/candles?{params}")
    if str(payload.get("code")) != "00000":
        raise ValueError(f"Bitget API error {payload.get('code')}: {payload.get('msg')}")
    duration_ms = (86400 if timeframe == "daily" else 604800) * 1000
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = payload.get("data") or []
    confirmed = [row for row in rows if isinstance(row, list) and len(row) >= 6 and int(row[0]) + duration_ms <= now_ms]
    df = crypto_frame_from_rows(confirmed, (0, 1, 2, 3, 4, 5))
    return finalize_crypto_frame(
        df,
        timeframe,
        bars,
        f"Bitget Spot official API ({instrument}, {interval})",
        "UTC interval fully elapsed",
    )


def fetch_okx_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    instrument = official_crypto_id(symbol)
    interval = "1Dutc" if timeframe == "daily" else "1Wutc"
    requested = bars + 3
    rows_by_time: dict[int, list[Any]] = {}
    cursor: int | None = None
    for _ in range(max(2, math.ceil(requested / 300) + 1)):
        params: dict[str, Any] = {
            "instId": instrument,
            "bar": interval,
            "limit": min(300, requested),
        }
        if cursor is not None:
            params["after"] = cursor
        payload = fetch_json(
            "https://www.okx.com/api/v5/market/history-candles?"
            + urllib.parse.urlencode(params)
        )
        if str(payload.get("code")) != "0":
            raise ValueError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
        batch = payload.get("data") or []
        if not batch:
            break
        for row in batch:
            if isinstance(row, list) and len(row) >= 9 and str(row[8]) == "1":
                rows_by_time[int(row[0])] = row
        oldest = min(int(row[0]) for row in batch)
        if cursor == oldest or len(rows_by_time) >= requested:
            break
        cursor = oldest
    df = crypto_frame_from_rows(list(rows_by_time.values()), (0, 1, 2, 3, 4, 5))
    return finalize_crypto_frame(
        df,
        timeframe,
        bars,
        f"OKX Spot official API ({instrument}, {interval})",
        "exchange confirm=1",
    )


def fetch_coinbase_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    instrument = official_crypto_id(symbol)
    needed_days = bars + 10 if timeframe == "daily" else bars * 7 + 14
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows_by_time: dict[int, list[Any]] = {}
    max_pages = math.ceil(needed_days / 299) + 2
    for _ in range(max_pages):
        start = end - timedelta(days=299)
        params = urllib.parse.urlencode({
            "granularity": 86400,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        })
        product = urllib.parse.quote(instrument, safe="-")
        batch = fetch_json(f"https://api.exchange.coinbase.com/products/{product}/candles?{params}")
        if not isinstance(batch, list) or not batch:
            break
        for row in batch:
            if isinstance(row, list) and len(row) >= 6:
                rows_by_time[int(row[0])] = row
        if len(rows_by_time) >= needed_days:
            break
        end = start
        time.sleep(0.12)
    daily = crypto_frame_from_rows(list(rows_by_time.values()), (0, 3, 2, 1, 4, 5), "s")
    daily = drop_unconfirmed_rows(daily, "crypto")
    df = aggregate_weekly(daily, "crypto") if timeframe == "weekly" else daily
    return finalize_crypto_frame(
        df,
        timeframe,
        bars,
        f"Coinbase Exchange official API ({instrument}, UTC daily)"
        + ("; weekly locally aggregated Mon-Sun" if timeframe == "weekly" else ""),
        "current UTC candle removed",
    )


def fetch_bitstamp_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    instrument = official_crypto_id(symbol)
    needed_days = bars + 10 if timeframe == "daily" else bars * 7 + 14
    rows_by_time: dict[int, dict[str, Any]] = {}
    cursor_end: int | None = None
    max_pages = math.ceil(needed_days / 1000) + 2
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "step": 86400,
            "limit": min(1000, max(200, needed_days - len(rows_by_time) + 10)),
            "exclude_current_candle": "true",
        }
        if cursor_end is not None:
            params["end"] = cursor_end
        payload = fetch_json(
            f"https://www.bitstamp.net/api/v2/ohlc/{instrument}/?"
            + urllib.parse.urlencode(params)
        )
        batch = ((payload.get("data") or {}).get("ohlc") or [])
        if not batch:
            break
        for row in batch:
            rows_by_time[int(row["timestamp"])] = row
        oldest = min(int(row["timestamp"]) for row in batch)
        if cursor_end == oldest - 1 or len(rows_by_time) >= needed_days:
            break
        cursor_end = oldest - 1
        time.sleep(0.12)
    if not rows_by_time:
        return pd.DataFrame()
    ordered = list(rows_by_time.values())
    index = pd.to_datetime([int(row["timestamp"]) for row in ordered], unit="s", utc=True).tz_convert(None)
    daily = normalize_ohlcv(pd.DataFrame({
        "open": [row["open"] for row in ordered],
        "high": [row["high"] for row in ordered],
        "low": [row["low"] for row in ordered],
        "close": [row["close"] for row in ordered],
        "volume": [row["volume"] for row in ordered],
    }, index=index))
    daily = drop_unconfirmed_rows(daily, "crypto")
    df = aggregate_weekly(daily, "crypto") if timeframe == "weekly" else daily
    return finalize_crypto_frame(
        df,
        timeframe,
        bars,
        f"Bitstamp official API ({instrument}, UTC daily)"
        + ("; weekly locally aggregated Mon-Sun" if timeframe == "weekly" else ""),
        "exclude_current_candle=true",
    )


def fetch_official_crypto(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    venue = exchange_of(symbol)
    fetchers = {
        "BINANCE": fetch_binance_crypto,
        "BITGET": fetch_bitget_crypto,
        "COINBASE": fetch_coinbase_crypto,
        "BITSTAMP": fetch_bitstamp_crypto,
        "OKX": fetch_okx_crypto,
    }
    try:
        fetcher = fetchers[venue]
    except KeyError as exc:
        raise ValueError(f"no official crypto adapter for {venue}") from exc
    return fetcher(symbol, timeframe, bars)


def parse_sina_sse_quote(payload: str, ticker: str) -> pd.DataFrame:
    marker = f'var hq_str_sh{ticker}="'
    start = payload.find(marker)
    if start < 0:
        raise ValueError(f"Sina quote missing for sh{ticker}")
    start += len(marker)
    end = payload.find('";', start)
    if end < 0:
        raise ValueError(f"malformed Sina quote for sh{ticker}")
    fields = payload[start:end].split(",")
    if len(fields) < 32 or not fields[30]:
        raise ValueError(f"incomplete Sina quote for sh{ticker}")

    quote_date = pd.Timestamp(fields[30])
    values = {
        "open": float(fields[1]),
        "high": float(fields[4]),
        "low": float(fields[5]),
        "close": float(fields[3]),
        "volume": float(fields[8]),
    }
    if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
        raise ValueError(f"non-positive Sina quote for sh{ticker}")
    return pd.DataFrame([values], index=pd.DatetimeIndex([quote_date], name="date"))


def fetch_sina_sse_quote(ticker: str) -> pd.DataFrame:
    url = f"https://hq.sinajs.cn/list=sh{ticker}"
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read().decode("gb18030", errors="replace")
    return parse_sina_sse_quote(payload, ticker)


def merge_latest_quote(frame: pd.DataFrame, quote: pd.DataFrame) -> pd.DataFrame:
    combined = frame.copy()
    for quote_date, row in quote.iterrows():
        combined.loc[pd.Timestamp(quote_date), ["open", "high", "low", "close", "volume"]] = row[
            ["open", "high", "low", "close", "volume"]
        ]
    return combined.sort_index()


def apply_sse_split_adjustments(frame: pd.DataFrame, ticker: str) -> tuple[pd.DataFrame, list[str]]:
    out = frame.copy()
    adjustments: list[str] = []
    price_columns = ["open", "high", "low", "close"]
    for effective_date, factor in SSE_ETF_SPLITS.get(ticker, ()):
        effective = pd.Timestamp(effective_date)
        before = out.index < effective
        after = out.index >= effective
        if not before.any() or not after.any():
            continue
        before_close = float(out.loc[before, "close"].iloc[-1])
        after_close = float(out.loc[after, "close"].iloc[0])
        observed_ratio = before_close / after_close
        if 0.70 <= observed_ratio <= 1.30:
            continue
        if not 0.70 <= observed_ratio / factor <= 1.30:
            raise ValueError(
                f"split ratio check failed for {ticker} at {effective_date}: "
                f"observed={observed_ratio:.4f}, expected={factor:g}"
            )
        out.loc[before, price_columns] = out.loc[before, price_columns] / factor
        out.loc[before, "volume"] = out.loc[before, "volume"] * factor
        adjustments.append(f"{effective_date} 1:{factor:g}")
    return out, adjustments


def reject_unadjusted_split_gaps(frame: pd.DataFrame, ticker: str) -> None:
    ratios = frame["close"] / frame["close"].shift(1)
    for date_value, ratio_value in ratios.dropna().items():
        ratio = float(ratio_value)
        for factor in (2.0, 3.0, 4.0, 5.0, 10.0):
            normalized = ratio * factor if ratio < 1.0 else ratio / factor
            if (ratio < 0.60 or ratio > 1.70) and 0.75 <= normalized <= 1.25:
                raise ValueError(
                    f"possible unadjusted ETF split for {ticker} at "
                    f"{pd.Timestamp(date_value).date().isoformat()}; factor~{factor:g}"
                )


def fetch_sse_etf(symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("AKShare is required for exact SSE ETF data") from exc

    ticker = symbol.split(":", 1)[1]
    local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    history_days = 365 * (3 if timeframe == "daily" else 12)
    start_date = (local_now.date() - timedelta(days=history_days)).strftime("%Y%m%d")
    end_date = local_now.date().strftime("%Y%m%d")
    raw: pd.DataFrame | None = None
    last_error: Exception | None = None
    source_name = "Eastmoney qfq"
    with SSE_ETF_LOCK:
        for attempt in range(2):
            try:
                raw = ak.fund_etf_hist_em(
                    symbol=ticker,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
                if raw is not None and not raw.empty:
                    break
            except Exception as exc:
                last_error = exc
            if attempt < 1:
                time.sleep(1.5 * (attempt + 1))
        if raw is None or raw.empty:
            try:
                raw = ak.fund_etf_hist_sina(symbol=f"sh{ticker}")
                source_name = "Sina Finance raw"
            except Exception as exc:
                last_error = exc
    if raw is None or raw.empty:
        raise RuntimeError(f"all SSE ETF sources failed: {ticker}") from last_error
    renamed = raw.rename(columns={
        "\u65e5\u671f": "date",
        "\u5f00\u76d8": "open",
        "\u6700\u9ad8": "high",
        "\u6700\u4f4e": "low",
        "\u6536\u76d8": "close",
        "\u6210\u4ea4\u91cf": "volume",
    })
    needed = {"date", "open", "high", "low", "close", "volume"}
    if not needed.issubset(renamed.columns):
        raise ValueError(f"unexpected SSE ETF columns: {list(raw.columns)}")
    frame = renamed.set_index("date")[["open", "high", "low", "close", "volume"]]
    frame.index = pd.to_datetime(frame.index)
    frame = normalize_ohlcv(frame)

    split_adjustments: list[str] = []
    if source_name == "Sina Finance raw":
        frame, split_adjustments = apply_sse_split_adjustments(frame, ticker)
    reject_unadjusted_split_gaps(frame, ticker)

    quote_note = "Sina close quote unavailable"
    try:
        quote = fetch_sina_sse_quote(ticker)
        frame = merge_latest_quote(frame, quote)
        quote_note = f"Sina close quote merged through {quote.index[-1].date().isoformat()}"
    except Exception as exc:
        quote_note = f"Sina close quote unavailable ({type(exc).__name__})"

    frame = normalize_ohlcv(frame)
    frame = drop_unconfirmed_rows(frame, "china")
    validate_ohlcv(frame)
    if frame.empty:
        raise ValueError(f"no completed SSE ETF bars: {ticker}")
    latest_age = (local_now.date() - frame.index[-1].date()).days
    if latest_age > 4:
        raise ValueError(
            f"stale SSE ETF data for {ticker}: latest={frame.index[-1].date().isoformat()}"
        )
    if timeframe == "weekly":
        frame = aggregate_weekly(frame, "china")
        validate_ohlcv(frame)
    if len(frame) > bars:
        frame = frame.tail(bars).copy()

    adjustment_note = (
        "Eastmoney qfq"
        if source_name == "Eastmoney qfq"
        else "Sina split-adjusted"
        + (f" ({', '.join(split_adjustments)})" if split_adjustments else "")
    )
    frame.attrs["source"] = (
        f"{source_name} via AKShare + Sina close quote ({ticker}, {adjustment_note})"
    )
    frame.attrs["data_quality"] = (
        f"exact SSE ETF symbol; {adjustment_note}; {quote_note}; "
        "completed bars; recency and OHLC validation passed"
    )
    return frame


def fetch_ohlcv(yahoo: str, timeframe: str, bars: int, market: str) -> pd.DataFrame:
    # Weekly bars are deliberately aggregated from repaired daily bars. This
    # makes the week boundary explicit and avoids opaque provider aggregation.
    interval = "1d"
    periods = ["3y", "2y", "1y", "6mo", "3mo"] if timeframe == "daily" else ["10y", "5y", "3y", "1y", "6mo"]
    last_error: Exception | None = None
    for attempt in range(3):
        for period in periods:
            try:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    df = yf.download(
                        yahoo,
                        period=period,
                        interval=interval,
                        progress=False,
                        auto_adjust=False,
                        repair=True,
                        keepna=False,
                        threads=False,
                    )
                df = normalize_ohlcv(df)
                df = drop_unconfirmed_rows(df, market)
                validate_ohlcv(df)
                if timeframe == "weekly":
                    df = aggregate_weekly(df, market)
                    validate_ohlcv(df)
                if not df.empty:
                    if len(df) > bars:
                        df = df.tail(bars).copy()
                    df.attrs["source"] = "Yahoo Finance via yfinance (repair=True)"
                    df.attrs["data_quality"] = "repair=True; OHLC validation passed"
                    return df
            except Exception as exc:
                last_error = exc
            try:
                df = fetch_yahoo_chart(yahoo, period)
                df = drop_unconfirmed_rows(df, market)
                validate_ohlcv(df)
                if timeframe == "weekly":
                    df = aggregate_weekly(df, market)
                    validate_ohlcv(df)
                if not df.empty:
                    if len(df) > bars:
                        df = df.tail(bars).copy()
                    df.attrs["source"] = "Yahoo Finance chart fallback (validated, unrepaired)"
                    df.attrs["data_quality"] = "OHLC validation passed; repair unavailable"
                    return df
            except Exception as exc:
                last_error = exc
        time.sleep(1.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def fetch_instrument_ohlcv(
    tv_symbol: str,
    provider_symbol: str,
    timeframe: str,
    bars: int,
    market: str,
) -> pd.DataFrame:
    if is_sse_etf(tv_symbol):
        return fetch_sse_etf(tv_symbol, timeframe, bars)
    if has_exact_source(tv_symbol):
        return fetch_official_crypto(tv_symbol, timeframe, bars)
    return fetch_ohlcv(provider_symbol, timeframe, bars, market)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.attrs.update(df.attrs)
    close = out["close"]
    out["SMA20"] = close.rolling(20).mean()
    out["SMA60"] = close.rolling(60).mean()
    out["SMA120"] = close.rolling(120).mean()
    out["EMA20"] = ema(close, 20)
    out["EMA60"] = ema(close, 60)
    out["EMA120"] = ema(close, 120)
    out["ATR"] = rma(true_range(out), 14)

    lowest_low = out["low"].rolling(KDJ_N).min()
    highest_high = out["high"].rolling(KDJ_N).max()
    denom = highest_high - lowest_low
    rsv = ((close - lowest_low) / denom * 100.0).where(denom != 0, 0.0)
    out["K"] = rma(rsv, KDJ_M1)
    out["D"] = rma(out["K"], KDJ_M2)
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
        source=str(df.attrs.get("source", "unknown")),
        bar_date=latest_bar_date(df),
        data_quality=str(df.attrs.get("data_quality", "OHLC validation passed")),
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
        source=str(df.attrs.get("source", "unknown")),
        bar_date=latest_bar_date(df),
        data_quality=str(df.attrs.get("data_quality", "OHLC validation passed")),
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

def scan(
    symbol_file: Path,
    timeframe: str,
    th: Thresholds,
    crypto_dense_only: bool = False,
    bars: int = 420,
    allow_approximate_mappings: bool = False,
) -> dict[str, Any]:
    try:
        YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(YF_CACHE_DIR))
    except Exception:
        pass
    symbols = load_symbols(symbol_file)
    requested = [symbol for tickers in symbols.values() for symbol in tickers]
    rows_count = 0
    missing: list[str] = []
    excluded: list[str] = []
    errors: list[str] = []
    sections: dict[str, list[Candidate]] = {DENSE: [], PULL20: [], PULL60: []}
    if timeframe == "weekly":
        sections[WEEKLY_J_LT_ZERO] = []
    tasks: list[tuple[str, str, str]] = []
    for market, tickers in symbols.items():
        for tv_symbol in tickers:
            exact_source = has_exact_source(tv_symbol)
            if not allow_approximate_mappings and tv_symbol in APPROXIMATE_MAPPINGS and not exact_source:
                reason = APPROXIMATE_MAPPINGS[tv_symbol]
                excluded.append(f"{tv_symbol}: {reason}")
                continue
            if exact_source:
                provider_symbol = official_crypto_id(tv_symbol)
            elif is_sse_etf(tv_symbol):
                provider_symbol = tv_symbol.split(":", 1)[1]
            else:
                provider_symbol = tv_to_yahoo(tv_symbol)
            if not provider_symbol:
                missing.append(tv_symbol)
                errors.append(f"no exact data mapping: {tv_symbol}")
                continue
            tasks.append((market, tv_symbol, provider_symbol))

    workers = max(1, min(8, int(os.environ.get("TV_SCAN_WORKERS", "4"))))
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(tasks)))) as executor:
        futures = {
            executor.submit(fetch_instrument_ohlcv, tv_symbol, provider_symbol, timeframe, bars, market):
            (market, tv_symbol, provider_symbol)
            for market, tv_symbol, provider_symbol in tasks
        }
        for future in as_completed(futures):
            market, tv_symbol, provider_symbol = futures[future]
            try:
                df = future.result()
                if df.empty:
                    raise ValueError("empty OHLCV")
                ind = add_indicators(df)
                rows_count += 1
                if timeframe == "weekly":
                    weekly_candidate = weekly_j_lt_zero_candidate(tv_symbol, provider_symbol, market, ind)
                    if weekly_candidate is not None:
                        sections[WEEKLY_J_LT_ZERO].append(weekly_candidate)
                for cand in classify_frame(tv_symbol, provider_symbol, market, ind, th, crypto_dense_only, timeframe):
                    sections.setdefault(cand.kind, []).append(cand)
            except Exception as exc:
                missing.append(tv_symbol)
                errors.append(f"{tv_symbol}->{provider_symbol}: {type(exc).__name__}: {exc}")
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
        "source": "Equities/indices: Yahoo repaired; SSE ETFs: qfq/split-adjusted + Sina close; crypto: exact-venue official APIs",
        "formula_version": FORMULA_VERSION,
        "indicator_spec": INDICATOR_SPEC,
        "bar_policy": "confirmed bars only",
        "strict_source_policy": not allow_approximate_mappings,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "symbols_count": len(requested),
        "rows_count": rows_count,
        "missing_symbols": list(dict.fromkeys(missing)),
        "excluded_symbols": excluded,
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
        "bar_date": c.bar_date,
        "bar_status": c.bar_status,
        "data_quality": c.data_quality,
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
    dates = pd.date_range("2026-01-01", periods=160, freq="D")
    close = pd.Series(range(10, 170), index=dates, dtype=float)
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000})
    validate_ohlcv(df)
    tz_frame = df.tail(2).copy()
    tz_frame.index = pd.date_range("2026-07-20", periods=2, tz="Asia/Shanghai")
    normalized_tz = normalize_ohlcv(tz_frame)
    assert normalized_tz.index[0].date().isoformat() == "2026-07-20"
    assert official_crypto_id("COINBASE:BTCUSD") == "BTC-USD"
    assert official_crypto_id("BINANCE:ETHBTC") == "ETHBTC"
    assert is_sse_etf("SSE:561980") and not is_sse_etf("SSE:600519")
    quote_fields = [
        "ETF", "0.979", "0.966", "1.063", "1.063", "0.930", "0", "0", "12345", "6789",
        *(["0"] * 20),
        "2026-07-21", "15:34:59",
    ]
    parsed_quote = parse_sina_sse_quote(
        f'var hq_str_sh560780="{",".join(quote_fields)}";',
        "560780",
    )
    assert parsed_quote.index[-1].date().isoformat() == "2026-07-21"
    assert abs(float(parsed_quote["close"].iloc[-1]) - 1.063) < 1e-12

    split_dates = pd.to_datetime(["2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29"])
    split_close = pd.Series([3.0, 3.06, 1.02, 1.05], index=split_dates)
    split_frame = pd.DataFrame({
        "open": split_close,
        "high": split_close * 1.01,
        "low": split_close * 0.99,
        "close": split_close,
        "volume": 1000.0,
    })
    adjusted_split, split_events = apply_sse_split_adjustments(split_frame, "560780")
    assert split_events == ["2026-06-26 1:3"]
    assert abs(float(adjusted_split["close"].iloc[1]) - 1.02) < 1e-12
    reject_unadjusted_split_gaps(adjusted_split, "560780")
    try:
        reject_unadjusted_split_gaps(split_frame, "TEST")
        raise AssertionError("unadjusted split gap was not rejected")
    except ValueError as exc:
        assert "possible unadjusted ETF split" in str(exc)




    ind = add_indicators(df)
    assert not pd.isna(ind["SMA120"].iloc[-1])
    assert abs(float(ind["J"].iloc[-1]) - 90.0) < 1e-9
    assert not pd.isna(ind["MACD_DIF"].iloc[-1])

    partial = df.tail(3).copy()
    partial.index = pd.to_datetime(["2026-07-19", "2026-07-20", "2026-07-21"])
    confirmed = drop_unconfirmed_rows(
        partial,
        "crypto",
        datetime(2026, 7, 21, 0, 23, tzinfo=timezone.utc),
    )
    assert confirmed.index[-1].date().isoformat() == "2026-07-20"
    china_before_close = drop_unconfirmed_rows(
        partial,
        "china",
        datetime(2026, 7, 21, 0, 23, tzinfo=timezone.utc),
    )
    assert china_before_close.index[-1].date().isoformat() == "2026-07-20"
    china_after_close = drop_unconfirmed_rows(
        partial,
        "china",
        datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
    )
    assert china_after_close.index[-1].date().isoformat() == "2026-07-21"
    assert len(aggregate_weekly(df, "america")) >= 20
    holiday_week = df.head(4).copy()
    holiday_week.index = pd.date_range("2026-04-06", periods=4, freq="D")
    assert latest_bar_date(aggregate_weekly(holiday_week, "america")) == "2026-04-09"
    today_utc = datetime.now(timezone.utc).date()
    monday = today_utc - timedelta(days=today_utc.weekday())
    current_week = df.head((today_utc - monday).days + 1).copy()
    current_week.index = pd.date_range(monday, today_utc, freq="D")
    assert aggregate_weekly(current_week, "crypto").empty

    corrupt = df.copy()
    corrupt.loc[corrupt.index[-1], "high"] = corrupt["close"].iloc[-1] * 4
    try:
        validate_ohlcv(corrupt)
        raise AssertionError("extreme wick was not rejected")
    except ValueError as exc:
        assert "extreme isolated wick" in str(exc)
    validate_ohlcv(corrupt, allow_extreme_wicks=True)

    weekly_test = ind.copy()
    weekly_test.loc[weekly_test.index[-2], "J"] = -8.0
    weekly_test.loc[weekly_test.index[-1], "J"] = -5.0
    weekly_candidate = weekly_j_lt_zero_candidate("NASDAQ:TEST", "TEST", "america", weekly_test)
    assert weekly_candidate is not None and weekly_candidate.kind == WEEKLY_J_LT_ZERO
    assert weekly_candidate.score == 100
    assert weekly_candidate.bar_status == "confirmed"
    assert weekly_candidate.bar_date == dates[-1].date().isoformat()
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
    parser.add_argument("--allow-approximate-mappings", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    th = Thresholds(max_items_per_section=args.max_items)
    started = time.time()
    result = scan(
        args.symbols,
        args.timeframe,
        th,
        crypto_dense_only=args.crypto_dense_only,
        allow_approximate_mappings=args.allow_approximate_mappings,
    )
    result["elapsed_seconds"] = round(time.time() - started, 2)
    save_state(result, args.state_dir)
    print(json.dumps(serializable(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
