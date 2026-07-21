# GitHub Email Report Setup

This repo contains a GitHub Actions workflow for the TradingView strategy report.

## Schedule

- Daily report: Beijing time 09:10 every day.
- Weekly report: Beijing time 09:25, Monday.

## Required GitHub Secrets

Add these in GitHub: Settings -> Secrets and variables -> Actions -> New repository secret.

- SMTP_HOST
- SMTP_PORT
- SMTP_USER
- SMTP_PASSWORD
- SMTP_TLS optional, default true
- MAIL_FROM
- MAIL_TO optional, default zyf18236610022@qq.com

For QQ Mail, use the SMTP authorization code, not the normal login password.
Common QQ settings:

- SMTP_HOST: smtp.qq.com
- SMTP_PORT: 587
- SMTP_TLS: true
- SMTP_USER: your QQ email address
- SMTP_PASSWORD: your QQ mail SMTP authorization code
- MAIL_FROM: your QQ email address

## Manual test

After pushing this repo to GitHub, open Actions -> TradingView Strategy Email Report -> Run workflow.
Choose dry_run=true first. If logs look good, run again with dry_run=false after SMTP secrets are configured.
