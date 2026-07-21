#!/usr/bin/env python3
"""Generate and email the local-recalc strategy report."""
from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from market_scan_local import (
    DENSE,
    FORMULA_VERSION,
    INDICATOR_SPEC,
    KDJ_MAX_BONUS,
    PULL20,
    PULL60,
    WEEKLY_J_LT_ZERO,
    WEEKLY_J_LT_ZERO_EXTRA_BONUS,
    Thresholds,
    candidate_to_dict,
    scan,
)

ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / "symbols_watchlist.json"
CRYPTO = ROOT / "symbols_crypto.json"

SECTION_LABELS = {
    WEEKLY_J_LT_ZERO: "周线J<0高权重",
    DENSE: "均线密集",
    PULL20: "回踩20日均线",
    PULL60: "回踩60日均线",
}

MACD_LABELS = {
    "DIF>=DEA": "DIF在DEA上方",
    "DIF<DEA": "DIF在DEA下方",
}

DIV_LABELS = {
    "MACD_BULL_DIV": "MACD底背离",
    "MACD_HIDDEN_BULL_DIV": "MACD隐藏底背离",
    "DIF_BULL_DIV": "DIF底背离",
    "HIST_BULL_DIV": "柱体底背离",
    "MACD_BEAR_DIV": "MACD顶背离压力",
    "DIF_BEAR_DIV": "DIF顶背离压力",
    "HIST_BEAR_DIV": "柱体顶背离压力",
}

KDJ_NOTE_LABELS = {
    "KDJ no data": "KDJ数据不足",
    "J<0": "J<0 极度低位",
    "J<0 hook": "J<0 低位勾头",
    "J<20": "J<20 低位",
    "J<20 hook": "J<20 低位勾头",
    "J hook": "J值勾头",
}


def fmt_float(value: object, digits: int = 2) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "-"


def fmt_pct(value: object) -> str:
    try:
        if value is None:
            return "-"
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def zh_timeframe(report_type: str) -> str:
    return "周线" if report_type == "weekly" else "日线"


def zh_report_name(report_type: str) -> str:
    return "周报" if report_type == "weekly" else "日报"


def zh_kind(kind: object) -> str:
    return SECTION_LABELS.get(str(kind), str(kind or "-"))


def zh_kdj_note(note: object) -> str:
    text = str(note or "").strip()
    if not text:
        return "-"
    if text in KDJ_NOTE_LABELS:
        return KDJ_NOTE_LABELS[text]
    match = re.fullmatch(r"J(-?[\d.]+)->(-?[\d.]+)(?: (.+))?", text)
    if match:
        prev_j, j_value, tag = match.groups()
        if tag:
            suffix = KDJ_NOTE_LABELS.get(tag, tag)
            return f"J值 {prev_j} 变为 {j_value}，{suffix}"
        return f"J值 {prev_j} 变为 {j_value}"
    return text


def zh_macd(macd: object) -> str:
    text = str(macd or "").strip()
    return MACD_LABELS.get(text, text or "-")


def zh_divergence(divergence: object) -> str:
    text = str(divergence or "").strip()
    if not text:
        return "-"
    match = re.fullmatch(r"([A-Z_]+)@(\d+)bars", text)
    if not match:
        return DIV_LABELS.get(text, text)
    key, bars = match.groups()
    label = DIV_LABELS.get(key, key)
    return f"{label}（{bars}根K线前确认）"


def zh_reason(reason: object) -> str:
    text = str(reason or "").strip()
    if not text:
        return "-"
    dense_pct = re.fullmatch(
        r"six-line width ([\d.]+)ATR/([\d.]+)%, price distance ([\d.]+)ATR, close above 20 group",
        text,
    )
    if dense_pct:
        width, width_pct, distance = dense_pct.groups()
        return f"六线跨度 {width}ATR / {width_pct}%，价格距密集区 {distance}ATR，收盘价站上20日均线组"
    dense = re.fullmatch(
        r"six-line width ([\d.]+)ATR, price distance ([\d.]+)ATR, close above 20 group",
        text,
    )
    if dense:
        width, distance = dense.groups()
        return f"六线跨度 {width}ATR，价格距密集区 {distance}ATR，收盘价站上20日均线组"
    pull = re.fullmatch(r"uptrend, first near MA/EMA(20|60) zone ([\d.]+)-([\d.]+)", text)
    if pull:
        period, low, high = pull.groups()
        return f"上涨趋势中，首次接近 MA/EMA{period} 区间 {low}-{high}"
    nearest = re.fullmatch(
        r"uptrend, nearest MA/EMA(20|60) zone ([\d.]+)-([\d.]+), close distance ([\d.]+)ATR, low distance ([\d.]+)ATR",
        text,
    )
    if nearest:
        period, low, high, close_distance, low_distance = nearest.groups()
        return f"上涨趋势中，当前价格最近的是 MA/EMA{period} 区间 {low}-{high}；收盘距离 {close_distance}ATR，最低价距离 {low_distance}ATR"
    return text.replace("uptrend", "上涨趋势").replace("first near", "首次接近").replace("nearest", "最近").replace("zone", "区间")


def result_rows(result: dict[str, object], sections: list[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    raw_sections = result.get("sections", {})
    if not isinstance(raw_sections, dict):
        return out
    for section in sections:
        values = raw_sections.get(section, [])
        for cand in values:
            out.append(candidate_to_dict(cand))
    return out

def weekly_j_lt_zero_rows(
    watch: dict[str, object],
    crypto: dict[str, object],
    max_items: int,
) -> list[dict[str, object]]:
    combined = result_rows(watch, [WEEKLY_J_LT_ZERO]) + result_rows(crypto, [WEEKLY_J_LT_ZERO])
    unique: dict[str, dict[str, object]] = {}
    for row in combined:
        symbol = str(row.get("symbol") or "")
        if symbol and symbol not in unique:
            unique[symbol] = row

    def sort_key(row: dict[str, object]) -> tuple[float, str]:
        try:
            j_value = float(row.get("j"))
        except (TypeError, ValueError):
            j_value = float("inf")
        return (j_value, str(row.get("symbol") or ""))

    return sorted(unique.values(), key=sort_key)[:max_items]


def missing_text(result: dict[str, object]) -> str:
    missing = result.get("missing_symbols") or []
    if not missing:
        return "无"
    return "、".join(str(item) for item in missing)


def excluded_text(result: dict[str, object]) -> str:
    excluded = result.get("excluded_symbols") or []
    if not excluded:
        return "\u65e0"
    return "; ".join(str(item) for item in excluded)


def plain_candidates(title: str, rows: list[dict[str, object]]) -> list[str]:
    lines = [title]
    if not rows:
        return lines + ["暂无符合条件标的", ""]
    for idx, row in enumerate(rows, 1):
        parts = [
            f"{idx}. {row.get('symbol')}｜{zh_kind(row.get('kind'))}｜评分 {row.get('score')}",
            f"收盘 {fmt_float(row.get('close'))}｜涨跌 {fmt_pct(row.get('change'))}｜J {fmt_float(row.get('j'), 1)}",
            f"KDJ：{zh_kdj_note(row.get('kdj_note'))}",
            f"MACD：{zh_macd(row.get('macd'))}；{zh_divergence(row.get('macd_divergence'))}",
            f"原因：{zh_reason(row.get('reason'))}",
        ]
        parts.insert(-1, f"K\u7ebf\u65e5\u671f\uff1a{row.get('bar_date') or '-'}\uff5c\u72b6\u6001\uff1a\u5df2\u6536\u76d8\u786e\u8ba4")
        parts.insert(-1, f"\u6570\u636e\u6e90\uff1a{row.get('source') or '-'}")
        parts.insert(-1, f"\u8d28\u68c0\uff1a{row.get('data_quality') or '-'}")
        lines.extend(parts)
        lines.append("")
    return lines


def card_html(row: dict[str, object], idx: int) -> str:
    score = esc(row.get("score"))
    symbol = esc(row.get("symbol"))
    name = esc(row.get("name") or "")
    kind = esc(zh_kind(row.get("kind")))
    close = esc(fmt_float(row.get("close")))
    change = esc(fmt_pct(row.get("change")))
    j_value = esc(fmt_float(row.get("j"), 1))
    kdj = esc(zh_kdj_note(row.get("kdj_note")))
    macd = esc(zh_macd(row.get("macd")))
    div = esc(zh_divergence(row.get("macd_divergence")))
    reason = esc(zh_reason(row.get("reason")))
    bar_date = esc(row.get("bar_date") or "-")
    source = esc(row.get("source") or "-")
    quality = esc(row.get("data_quality") or "-")
    change_color = "#b42318" if str(change).startswith("-") else "#067647"
    return f"""
      <div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px 14px 12px;margin:10px 0;background:#ffffff;">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;">
          <div>
            <div style="font-size:17px;font-weight:700;color:#111827;">#{idx} {symbol}</div>
            <div style="font-size:12px;color:#6b7280;margin-top:2px;">{name}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:12px;color:#6b7280;">评分</div>
            <div style="font-size:22px;font-weight:800;color:#111827;line-height:1;">{score}</div>
          </div>
        </div>
        <div style="margin-top:10px;">
          <span style="display:inline-block;background:#eef2ff;color:#3730a3;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;">{kind}</span>
          <span style="display:inline-block;background:#f3f4f6;color:#374151;border-radius:999px;padding:4px 9px;font-size:12px;margin-left:4px;">KDJ：{kdj}</span>
        </div>
        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;margin-top:12px;border-collapse:collapse;font-size:13px;color:#374151;">
          <tr>
            <td style="padding:5px 0;color:#6b7280;">收盘价</td>
            <td style="padding:5px 0;text-align:right;font-weight:700;color:#111827;">{close}</td>
          </tr>
          <tr>
            <td style="padding:5px 0;color:#6b7280;">涨跌幅</td>
            <td style="padding:5px 0;text-align:right;font-weight:700;color:{change_color};">{change}</td>
          </tr>
          <tr>
            <td style="padding:5px 0;color:#6b7280;">J值</td>
            <td style="padding:5px 0;text-align:right;font-weight:700;color:#111827;">{j_value}</td>
          </tr>
          <tr>
            <td style="padding:5px 0;color:#6b7280;">MACD辅助</td>
            <td style="padding:5px 0;text-align:right;color:#111827;">{macd}；{div}</td>
          </tr>
          <tr>
            <td style="padding:5px 0;color:#6b7280;">K\u7ebf\u65e5\u671f</td>
            <td style="padding:5px 0;text-align:right;color:#111827;">{bar_date}\uff08\u5df2\u6536\u76d8\uff09</td>
          </tr>
        </table>
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid #f3f4f6;font-size:13px;line-height:1.55;color:#374151;">
          <strong style="color:#111827;">入选原因：</strong>{reason}
        </div>
        <div style="margin-top:6px;color:#6b7280;font-size:12px;line-height:1.5;">
          \u6570\u636e\u6e90\uff1a{source}<br>\u8d28\u68c0\uff1a{quality}
        </div>
      </div>
    """


def cards_html(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div style="border:1px dashed #d1d5db;border-radius:8px;padding:14px;color:#6b7280;background:#f9fafb;">暂无符合条件标的</div>'
    return "\n".join(card_html(row, idx) for idx, row in enumerate(rows, 1))


def section_html(title: str, result: dict[str, object], rows: list[dict[str, object]]) -> str:
    returned = esc(result.get("rows_count"))
    total = esc(result.get("symbols_count"))
    missing = esc(missing_text(result))
    excluded = esc(excluded_text(result))
    return f"""
      <section style="margin-top:22px;">
        <h2 style="font-size:18px;margin:0 0 10px;color:#111827;">{esc(title)}</h2>
        <div style="font-size:13px;color:#4b5563;margin-bottom:10px;">
          数据返回：<strong>{returned}/{total}</strong>　未返回/数据不足：<strong>{missing}</strong>
        </div>
        <div style="font-size:12px;color:#6b7280;margin:-4px 0 10px;">
          \u4e25\u683c\u6a21\u5f0f\u6392\u9664\u7684\u4ee3\u7406\u54c1\u79cd\uff1a{excluded}
        </div>
        {cards_html(rows)}
      </section>
    """


def weekly_priority_html(rows: list[dict[str, object]]) -> str:
    total_weight = KDJ_MAX_BONUS + WEEKLY_J_LT_ZERO_EXTRA_BONUS
    return f"""
      <section style="margin-top:22px;background:#fffbeb;border:2px solid #f59e0b;border-radius:10px;padding:14px;">
        <h2 style="font-size:18px;margin:0 0 8px;color:#92400e;">周线 KDJ J&lt;0 高权重关注</h2>
        <div style="font-size:13px;color:#92400e;line-height:1.55;margin-bottom:10px;">
          独立筛选全股票池，KDJ总权重 +{total_weight} 分，按J值从低到高排列；可能与下方常规候选重复。
        </div>
        {cards_html(rows)}
      </section>
    """

def build_report(report_type: str, max_items: int) -> tuple[str, str, str]:
    timeframe = "weekly" if report_type == "weekly" else "daily"
    th = Thresholds(max_items_per_section=max_items)
    watch = scan(WATCHLIST, timeframe, th, crypto_dense_only=False)
    crypto = scan(CRYPTO, timeframe, th, crypto_dense_only=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    report_name = zh_report_name(report_type)
    timeframe_name = zh_timeframe(report_type)
    subject = f"TradingView策略{report_name}｜{timeframe_name}｜{now} 北京时间"

    watch_rows = result_rows(watch, [DENSE, PULL20, PULL60])
    crypto_rows = result_rows(crypto, [DENSE])
    weekly_priority_rows = weekly_j_lt_zero_rows(watch, crypto, max_items) if report_type == "weekly" else []
    total_weekly_kdj_weight = KDJ_MAX_BONUS + WEEKLY_J_LT_ZERO_EXTRA_BONUS

    if report_type == "weekly":
        priority_lines = [
            f"1. 周线J<0：独立筛选全股票池，KDJ总权重 +{total_weekly_kdj_weight} 分，并在邮件置顶单列。",
            "2. 自选列表：均线密集需同时满足 ATR 压缩和六线跨度占比，且J值越小越加分。",
            "3. 回踩20周与60周均线并列，J值越小权重越高。",
            "4. MACD背离仅作为辅助评分，不做硬筛选。",
            "5. 加密列表的常规候选目前只看均线密集；周线J<0仍会进入置顶名单。",
        ]
    else:
        priority_lines = [
            f"1. 自选列表：均线密集需同时满足 ATR 压缩和六线跨度占比；J<20按深度加分，J<0时KDJ最高 +{KDJ_MAX_BONUS} 分。",
            "2. 回踩20日与60日均线并列；J值权重同上，若J值向上勾头再加15分。",
            "3. MACD背离仅作为辅助评分，不做硬筛选。",
            "4. 加密列表：目前只看均线密集；密集后J<0作为加分项。",
        ]

    precision_header = [
        f"\u7cbe\u5ea6\u7248\u672c\uff1a{FORMULA_VERSION}",
        f"\u6307\u6807\u516c\u5f0f\uff1a{INDICATOR_SPEC}",
        "\u53ea\u4f7f\u7528\u5df2\u6536\u76d8K\u7ebf\uff1b\u666e\u901a\u80a1\u7968/\u6307\u6570Yahoo repair=True\uff1bA\u80a1ETF\u524d\u590d\u6743/\u62c6\u5206\u6821\u6b63+\u65b0\u6d6a\u6536\u76d8\u8865\u9f50\uff1b\u52a0\u5bc6\u8d27\u5e01\u6307\u5b9a\u4ea4\u6613\u6240\u5b98\u65b9API\uff1b\u5f02\u5e38OHLC\u4e0d\u53c2\u4e0e\u7b5b\u9009",
        "\u8bc4\u5206\u662f\u7b56\u7565\u6392\u5e8f\uff0c\u4e0d\u662f\u884c\u60c5\u6570\u636e\u7cbe\u5ea6",
    ]
    plain_lines = [*precision_header,
        f"TradingView策略{report_name}",
        f"生成时间：{now} 北京时间",
        f"周期：{timeframe_name}",
        "\u6570\u636e\u6e90\uff1a\u666e\u901a\u80a1\u7968/\u6307\u6570Yahoo\u4fee\u590dK\u7ebf\uff1bA\u80a1ETF\u524d\u590d\u6743/\u62c6\u5206\u6821\u6b63+\u65b0\u6d6a\u6536\u76d8\u8865\u9f50\uff1b\u52a0\u5bc6\u8d27\u5e01\u7b26\u53f7\u5bf9\u5e94\u4ea4\u6613\u6240\u5b98\u65b9API\uff1b\u6307\u6807\u672c\u5730\u91cd\u7b97",
        "",
        "筛选优先级：",
        *priority_lines,
        "",
        f"自选列表：数据返回 {watch.get('rows_count')}/{watch.get('symbols_count')}；未返回/数据不足：{missing_text(watch)}",
    ]
    if report_type == "weekly":
        plain_lines.extend(plain_candidates(
            f"周线 KDJ J<0 高权重关注（KDJ总权重 +{total_weekly_kdj_weight}，按J值从低到高）",
            weekly_priority_rows,
        ))
    plain_lines.extend(plain_candidates("自选列表候选", watch_rows))
    plain_lines.append(f"加密列表：数据返回 {crypto.get('rows_count')}/{crypto.get('symbols_count')}；未返回/数据不足：{missing_text(crypto)}")
    plain_lines.extend(plain_candidates("加密列表均线密集", crypto_rows))

    plain_lines.append(f"\u4e25\u683c\u6a21\u5f0f\u6392\u9664\uff08\u81ea\u9009\uff09\uff1a{excluded_text(watch)}")
    plain_lines.append(f"\u4e25\u683c\u6a21\u5f0f\u6392\u9664\uff08\u52a0\u5bc6\uff09\uff1a{excluded_text(crypto)}")
    errors = list(watch.get("errors") or []) + list(crypto.get("errors") or [])
    if errors:
        plain_lines.append("数据备注：")
        for err in errors[:20]:
            plain_lines.append(f"- {err}")
        if len(errors) > 20:
            plain_lines.append(f"- 另有 {len(errors) - 20} 条备注")
        plain_lines.append("")

    plain_lines.extend([
        "风险提醒：本报告只是技术筛选和复盘参考，不构成买卖建议。实际交易前请再核对券商/交易所实时行情、流动性和自身风险承受能力。",
        "",
    ])
    plain_body = "\n".join(plain_lines)

    error_html = ""
    if errors:
        items = "".join(f"<li>{esc(err)}</li>" for err in errors[:20])
        more = f"<li>另有 {len(errors) - 20} 条备注</li>" if len(errors) > 20 else ""
        error_html = f"""
          <section style="margin-top:22px;">
            <h2 style="font-size:18px;margin:0 0 10px;color:#111827;">数据备注</h2>
            <ul style="margin:0;padding-left:18px;color:#4b5563;font-size:13px;line-height:1.55;">{items}{more}</ul>
          </section>
        """

    priority_html_items = "".join(f"<li>{esc(item[3:])}</li>" for item in priority_lines)
    weekly_priority_section = weekly_priority_html(weekly_priority_rows) if report_type == "weekly" else ""

    precision_html = f'<div style="background:#ecfdf3;border:1px solid #abefc6;border-radius:10px;margin-top:12px;padding:12px 16px;color:#05603a;font-size:13px;line-height:1.6;"><strong>\u7cbe\u5ea6\u7248\u672c {FORMULA_VERSION}</strong><br>{esc(INDICATOR_SPEC)}<br>\u4ec5\u5df2\u6536\u76d8K\u7ebf\uff5c\u666e\u901a\u80a1\u7968/\u6307\u6570Yahoo repair=True\uff5cA\u80a1ETF\u524d\u590d\u6743/\u62c6\u5206\u6821\u6b63+\u65b0\u6d6a\u6536\u76d8\u8865\u9f50\uff5c\u52a0\u5bc6\u8d27\u5e01\u6307\u5b9a\u4ea4\u6613\u6240\u5b98\u65b9API</div>'
    html_body = f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'Microsoft YaHei',sans-serif;color:#111827;">
    <div style="max-width:680px;margin:0 auto;padding:18px 12px;">
      <div style="background:#111827;color:#ffffff;border-radius:10px;padding:18px 16px;">
        <div style="font-size:13px;opacity:.78;">{esc(now)} 北京时间</div>
        <h1 style="font-size:22px;line-height:1.25;margin:6px 0 0;">TradingView策略{esc(report_name)}</h1>
        <div style="font-size:14px;margin-top:8px;opacity:.9;">周期：{esc(timeframe_name)}｜完整K线本地重算</div>
      </div>

      <div style="background:#ffffff;border-radius:10px;margin-top:12px;padding:14px 16px;border:1px solid #e5e7eb;">
        <div style="font-size:15px;font-weight:700;margin-bottom:8px;">筛选优先级</div>
        <ol style="margin:0;padding-left:20px;color:#374151;font-size:13px;line-height:1.65;">
          {priority_html_items}
        </ol>
      </div>

      {weekly_priority_section}
      {precision_html}
      {section_html("自选列表候选", watch, watch_rows)}
      {section_html("加密列表均线密集", crypto, crypto_rows)}
      {error_html}

      <div style="margin-top:22px;background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:13px 15px;color:#9a3412;font-size:13px;line-height:1.6;">
        <strong>风险提醒：</strong>本报告只是技术筛选和复盘参考，不构成买卖建议。实际交易前请再核对券商/交易所实时行情、流动性和自身风险承受能力。
      </div>
      <div style="margin:12px 2px 0;color:#6b7280;font-size:12px;line-height:1.5;">
        \u6570\u636e\u6e90\uff1a\u666e\u901a\u80a1\u7968/\u6307\u6570Yahoo\u4fee\u590dK\u7ebf\uff1bA\u80a1ETF\u524d\u590d\u6743/\u62c6\u5206\u6821\u6b63+\u65b0\u6d6a\u6536\u76d8\u8865\u9f50\uff1b\u52a0\u5bc6\u8d27\u5e01\u7b26\u53f7\u5bf9\u5e94\u4ea4\u6613\u6240\u5b98\u65b9API\uff1b\u6307\u6807\u672c\u5730\u91cd\u7b97\u3002
      </div>
    </div>
  </body>
</html>"""

    return subject, plain_body, html_body


def send_email(subject: str, plain_body: str, html_body: str, dry_run: bool = False) -> bool:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    mail_from = os.environ.get("MAIL_FROM") or user
    mail_to = os.environ.get("MAIL_TO") or "zyf18236610022@qq.com"
    use_tls = os.environ.get("SMTP_TLS", "true").lower() != "false"

    if dry_run:
        print("[DRY-RUN] 邮件未发送；已生成中文 HTML 邮件。")
        print("Subject:", subject)
        print(plain_body)
        print(f"[DRY-RUN] HTML length: {len(html_body)} bytes")
        return False

    missing = [
        name
        for name, value in {
            "SMTP_HOST": host,
            "SMTP_USER": user,
            "SMTP_PASSWORD": password,
            "MAIL_FROM": mail_from,
            "MAIL_TO": mail_to,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"邮件未发送，缺少配置：{', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(plain_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Email sent to {mail_to}: {subject}")
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and email strategy report.")
    parser.add_argument("--report-type", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--max-items", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    subject, plain_body, html_body = build_report(args.report_type, args.max_items)
    send_email(subject, plain_body, html_body, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
