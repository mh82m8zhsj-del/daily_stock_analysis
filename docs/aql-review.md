# عقل الأسهم: implementation review

Baseline: `78a9736d2ae98d10aed2cefeae3e7e6e37cc4f4d` on `main`.
Branch: `feat/aql-alashum`.

## Repository assessment

The checkout contains 1,216 tracked files. An AST inventory parsed all 655 tracked Python files without syntax errors. Review covered the entry point, configuration bootstrap, daily workflow and backend CI gate, existing daily provider normalization, trading calendars, portfolio risk service, analysis pipeline, report renderer/localization, notification dispatch/senders, broker portfolio boundary, and their relevant tests. The Web/Desktop and bot/API surfaces were inventoried for compatibility and left unchanged. This is an implementation review and static inventory, not a claim of an exhaustive security audit of every dependency or every frontend line.

The original product is a multi-market daily analysis platform. Its report languages are Chinese, English and Korean; its daily technical analysis and generated advice cannot certify a closed 15-minute US breakout. Existing portfolio risk reporting also has different thresholds and is not a source of a verified current personal-account snapshot. Simply translating a prompt or changing default stock symbols would leave those requirements unenforced.

The new optional profile enters through `main.py` before legacy generation/config validation. It reuses yfinance (already required), exchange-calendars (already required), and existing notification transports. Its calculations are deterministic and never depend on generated model text. It does not instantiate an order client, read broker positions, submit orders, or require an LLM key. Unsupported CLI combinations, including broker portfolio imports, are rejected before fetching prices.

## Findings addressed

| Finding | Implementation |
| --- | --- |
| Daily/partial bars cannot confirm intraday breakouts | Dedicated 15-minute read adapter, regular-session calendar, closed-bar selection and contiguous history checks; no daily fallback |
| Volume ratios across arbitrary periods are misleading | Same slot in five prior trading sessions, including DST-aware session opens; missing history blocks entry |
| Missing account data could create invented sizes | Verified explicit snapshot schema and five-minute freshness requirement; no fabricated balance |
| Per-symbol 2% allocation can exceed the daily budget | Shared basket risk/cash reservations, current equity cap, starting-equity daily cap, existing risk and gross daily losses deducted |
| Profits could hide an earlier daily-loss breach | Mandatory `daily_loss`, without profit netting, with documented non-reset policy after a daily stop |
| Relaxed mode could silently weaken capital protection | Modes differ only in RVOL/momentum; confirmation and capital protection are shared |
| Legacy sender titles could remain Chinese | Scoped context-local Arabic sender text; legacy text preserved outside personal notifications; RTL email HTML |
| Scheduled analysis is before the US open | Preserve 10:00 UTC cron; explicitly block entry outside a verified regular session; document manual/external scheduling |

## Boundaries and operational requirements

- This profile supplies analysis and hypothetical entry plans, not executable orders or broker-side stops. Risk limits constrain calculated plans; gaps, fees and slippage can exceed planned losses.
- The account snapshot is supplied by the operator. It must reflect all current positions/pending orders, losses and cash. It is not independently reconciled against a broker. Old/missing snapshots block plans.
- Basket reservations apply within one report. A new run requires a refreshed account snapshot; reports are not a cross-process order reservation ledger.
- The reviewed technology/AI universe is intentionally explicit. Other symbols are rejected. Listing changes require maintenance.
- Legacy Web/Desktop/API/chat reports keep their existing languages and behavior. Arabic output covers the new personal CLI/Actions profile and its notifications. Existing user-defined webhook body text is not rewritten.
- `AQL_ENABLED=false` remains the default to preserve existing workflows. Four optional environment bindings were added to the daily job; existing secrets, cron, inputs, concurrency and commands remain intact. No account-level GitHub settings or secrets were changed.
- The Arabic guide is task-specific; there is no existing English counterpart to synchronize. This review provides English implementation and compatibility notes.

## Validation

- `python -m pytest tests/test_aql_analysis.py -q`: 59 passed (closed/in-progress bars, gaps, invalid prices/volumes, stale/future account data, holidays/early close, sizing, daily loss, gross-loss stop, basket budget, mode guards, CLI routing, adapter and Arabic notification payload).
- Existing affected regression tests: 290 passed across `test_notification_sender.py`, `test_formatters.py`, `test_daily_analysis_workflow_notification_env.py`, `test_main_schedule_mode.py`, `test_main_portfolio.py`.
- `scripts/ci_gate.sh syntax`, `flake8`, `deterministic`: passed. New Python modules were also compiled and linted.
- Full `scripts/ci_gate.sh` was attempted. The offline suite stopped at `test_convergence_operation_qualification_reaches_real_api` in `tests/test_agent_active_stock_integration.py` after the gate's 120-second timeout. An isolated worktree of the untouched baseline reproduced a timeout on the same test family with a 40-second watchdog. The full offline suite is therefore **not verified green** in this Python 3.12 environment; CI uses Python 3.11. No tests or workflow gates were disabled.
- Read-only online smoke: `python main.py --aql --stocks NVDA --no-notify` fetched provider data successfully and produced an Arabic market-closed report on 2026-11-08, without sending notifications. Live intraday entry behavior was verified with deterministic fixtures, not a real account.
- Report evidence uses synthetic NVDA entry at 102, stop 99, targets 108/111, 66 shares and $198 planned risk on a $10,000 account, plus an AMD case blocked for missing account data. Evidence is kept outside the repository; the PR includes reproducible report text.

## Rollback

Set `AQL_ENABLED=false` and omit `--aql` to restore the original execution path immediately. Revert this feature commit for a code rollback. No database migration, secret rewrite, or broker-account change is involved.
