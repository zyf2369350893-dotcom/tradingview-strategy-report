# TradingView Strategy Report

Automated daily and weekly TradingView strategy reports.

- Daily: Beijing time 09:10 every day (completed UTC crypto day and latest completed stock sessions).
- Weekly: Beijing time 09:25, Monday (completed equity and UTC crypto weeks).
- Signals use confirmed bars only.
- KDJ is the custom Pine formula KDJ(9,3,3,RMA), not standard Stochastic.
- Regular equities/indices use repaired Yahoo data; SSE ETFs use qfq/split-adjusted history plus the latest completed Sina close quote.
- Crypto uses the official public API of the exact TradingView exchange prefix.
- Cross-venue proxy mappings are excluded from strict reports.
- KDJ scoring uses the custom RMA formula recalculated from validated OHLC; TradingView is a marked fallback only and is capped at 15 points.
- Weekly priority score: 30 setup + dynamic KDJ up to 50 + MACD divergence up to +/-20, with separate bullish/bearish divergence badges.
- Each candidate includes its bar date, source, and quality status.
- Default recipient: zyf18236610022@qq.com.

See GITHUB_EMAIL_SETUP.md for SMTP setup.
