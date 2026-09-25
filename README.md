# TradingView Strategy Report

Automated daily and weekly TradingView strategy reports.

- Daily: Beijing time 09:10 every day (completed UTC crypto day and latest completed stock sessions).
- Daily crypto report also lists symbols above the Vegas third channel (EMA576/EMA676); a fresh close above its upper edge within 1% is highlighted. `NYSE:CRCL` (Circle) is already included in the U.S. watchlist and daily scan.
- Weekly: Beijing time 09:25, Monday (completed equity and UTC crypto weeks).
- Monthly: Beijing time 10:10 on the first day of each month (completed prior calendar month).
- Signals use confirmed bars only.
- Monthly keeps the same two setup families (moving-average density and trend pullbacks) but uses only SMA/EMA 20-month and 60-month lines. With 20-59 completed monthly bars, it uses the 20-month group for pullback analysis plus KDJ/MACD; four-line density and 60-month pullbacks require 60 bars. Fewer than 20 bars means no monthly analysis for that symbol.
- Monthly ranking emphasizes trend (up to 40) and setup (up to 35), with a smaller KDJ contribution (up to 10, plus a 5-point hook bonus on pullbacks) and MACD divergence/confirmation adjustment (up to +/-15). Scores are rankings within the monthly report, not probabilities or directly comparable with daily/weekly scores.
- Crypto uses the same regular scan as equities: six-line MA density plus trend pullbacks to the MA/EMA20 or MA/EMA60 groups.
- Crypto daily KDJ J<0 is listed independently in the daily email; weekly KDJ J<0 remains independently listed in the weekly email.
- KDJ is the custom Pine formula KDJ(9,3,3,RMA), not standard Stochastic.
- Regular equities/indices use repaired Yahoo data; SSE ETFs use qfq/split-adjusted history plus the latest completed Sina close quote.
- Crypto uses the official public API of the exact TradingView exchange prefix.
- Cross-venue proxy mappings are excluded from strict reports.
- KDJ scoring uses the custom RMA formula recalculated from validated OHLC; TradingView is a marked fallback only and is capped at 15 points.
- Weekly priority score: 30 setup + dynamic KDJ up to 50 + MACD divergence up to +/-20, with separate bullish/bearish divergence badges.
- Each candidate includes its bar date, source, and quality status.

See GITHUB_EMAIL_SETUP.md for SMTP setup.
