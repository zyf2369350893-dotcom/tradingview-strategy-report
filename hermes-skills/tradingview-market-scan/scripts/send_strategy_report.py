
#!/usr/bin/env python3
"""Generate and email the local-recalc strategy report."""
from __future__ import annotations

import argparse
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from market_scan_local import DENSE, PULL20, PULL60, Thresholds, candidate_to_dict, scan

ROOT = Path(__file__).resolve().parent
WATCHLIST = ROOT / "symbols_watchlist.json"
CRYPTO = ROOT / "symbols_crypto.json"

SECTION_LABELS = {
    DENSE: "MA Dense",
    PULL20: "Pullback 20",
    PULL60: "Pullback 60",
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


def section_table(title: str, rows: list[dict[str, object]]) -> list[str]:
    lines = [f"### {title}"]
    if not rows:
        lines.append("None")
        lines.append("")
        return lines
    lines.append("| Rank | Symbol | Type | Close | Change | Score | J | MACD | Notes |")
    lines.append("|---:|---|---|---:|---:|---:|---:|---|---|")
    for idx, row in enumerate(rows, 1):
        note = str(row.get("kdj_note") or "")
        macd_div = str(row.get("macd_divergence") or "")
        reason = str(row.get("reason") or "")
        desc = "; ".join(part for part in [note, macd_div, reason] if part)
        kind = SECTION_LABELS.get(str(row.get("kind")), str(row.get("kind") or "-"))
        lines.append(
            f"| {idx} | {row.get('symbol')} | {kind} | {fmt_float(row.get('close'))} | "
            f"{fmt_pct(row.get('change'))} | {row.get('score')} | {fmt_float(row.get('j'), 1)} | "
            f"{row.get('macd') or '-'} | {desc} |"
        )
    lines.append("")
    return lines


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


def build_report(report_type: str, max_items: int) -> tuple[str, str]:
    timeframe = "weekly" if report_type == "weekly" else "daily"
    th = Thresholds(max_items_per_section=max_items)
    watch = scan(WATCHLIST, timeframe, th, crypto_dense_only=False)
    crypto = scan(CRYPTO, timeframe, th, crypto_dense_only=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    label = "Weekly" if report_type == "weekly" else "Daily"
    subject = f"TradingView Strategy {label} Report | {now} CST"

    watch_rows = result_rows(watch, [DENSE, PULL20, PULL60])
    crypto_rows = result_rows(crypto, [DENSE])

    watch_missing = ", ".join(watch.get("missing_symbols") or []) or "None"
    crypto_missing = ", ".join(crypto.get("missing_symbols") or []) or "None"

    lines = [
        f"# TradingView Strategy {label} Report",
        "",
        f"Generated: {now} Beijing time",
        f"Timeframe: {'weekly' if timeframe == 'weekly' else 'daily'}",
        "Source: yfinance OHLCV; local recalculation for SMA/EMA, KDJ and MACD",
        "",
        "Priority rules:",
        "1. Watchlist: MA dense first; lower J is better.",
        "2. Pullback 20 and Pullback 60 are tied second; lower J has high weight.",
        "3. MACD divergence is only an auxiliary score.",
        "4. Crypto: MA dense only; J<0 is a bonus.",
        "",
        "## Watchlist",
        f"Data returned: {watch.get('rows_count')}/{watch.get('symbols_count')}",
        f"Missing / insufficient data: {watch_missing}",
        "",
    ]
    lines.extend(section_table("Watchlist Candidates", watch_rows))
    lines.extend([
        "## Crypto",
        f"Data returned: {crypto.get('rows_count')}/{crypto.get('symbols_count')}",
        f"Missing / insufficient data: {crypto_missing}",
        "",
    ])
    lines.extend(section_table("Crypto MA Dense", crypto_rows))

    errors = list(watch.get("errors") or []) + list(crypto.get("errors") or [])
    if errors:
        lines.append("## Data Notes")
        for err in errors[:20]:
            lines.append(f"- {err}")
        if len(errors) > 20:
            lines.append(f"- {len(errors) - 20} more notes")
        lines.append("")

    lines.extend([
        "## Risk Notice",
        "This is a technical screening report only, not investment advice. Check live broker/exchange data before any trade.",
        "",
    ])
    return subject, "\n".join(lines)


def send_email(subject: str, body: str, dry_run: bool = False) -> bool:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    mail_from = os.environ.get("MAIL_FROM") or user
    mail_to = os.environ.get("MAIL_TO") or "zyf18236610022@qq.com"
    use_tls = os.environ.get("SMTP_TLS", "true").lower() != "false"

    if dry_run or not all([host, user, password, mail_from, mail_to]):
        print("[DRY-RUN] Email not sent. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO to send.")
        print("Subject:", subject)
        print(body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body, subtype="plain", charset="utf-8")

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
    subject, body = build_report(args.report_type, args.max_items)
    send_email(subject, body, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
