# TradingView Strategy Report

Automated daily and weekly TradingView strategy reports.

- Daily: Beijing time 09:10 every day (completed UTC crypto day and latest completed stock sessions).
- Weekly: Beijing time 09:25, Monday (completed equity and UTC crypto weeks).
- Signals use confirmed bars only.
- KDJ is the custom Pine formula KDJ(9,3,3,RMA), not standard Stochastic.
- Regular equities/indices use repaired Yahoo data; SSE ETFs use Eastmoney/Sina via AKShare.
- Crypto uses the official public API of the exact TradingView exchange prefix.
- Cross-venue proxy mappings are excluded from strict reports.
- Each candidate includes its bar date, source, and quality status.
- Default recipient: zyf18236610022@qq.com.

See GITHUB_EMAIL_SETUP.md for SMTP setup.
