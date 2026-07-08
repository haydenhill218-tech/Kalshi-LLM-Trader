# kalshi-llm-trader

An autonomous trading system for Kalshi prediction markets, built through AI-assisted development. I designed the system and directed the build; Claude (Anthropic) wrote the Python under my direction across 15+ iteration cycles. I'm sharing it — including the results and the post-mortem — because the discipline around the experiment matters more to me than how it ended.

## What it does

Every 15 minutes, the bot:

1. Pulls open markets across a configured universe (daily crypto, daily/weekly commodities) from the Kalshi API, filtered to a tradeable price band
2. Gathers context: live crypto prices (CoinGecko) and a Claude-generated news summary via web search
3. Runs a two-stage LLM decision pipeline — an analysis call with full market/position/history context, then a separately constrained decision call that must emit exactly one parseable line (`TRADE: TICKER side PRICE AMOUNT` or `NO_TRADE`)
4. Places limit orders with risk rules enforced in code, not just in the prompt: 3% max position size, price-band entry filters, dedup against held series
5. Manages exits through a two-phase lifecycle (`open → exiting → resolved`): absolute take-profit, proportional stop-loss, rate-limited order repricing, and settlement reconciliation to recover exit orders orphaned when a market settles before the exit fills

Engineering details I'm proud of for a first system: RSA-PSS request signing for Kalshi auth, file-locked concurrency control to prevent overlapping scans, atomic trade-log writes via temp-file rename, and idempotent schema migration for the trade log.

## The experiment

Before deploying real capital, I pre-registered a 14-day evaluation in the file's docstring: strategy parameters locked, four explicit success conditions, and a decision tree ending in KILL. One line from it:

> The "well maybe with more capital" rationalization is the exact failure mode this checklist exists to prevent.

## Results

**29 resolved trades · 7W / 22L · gross P&L ≈ breakeven · negative net of API and server costs.**

Applying my own pre-committed criteria: the strategy had not earned additional capital. I killed it.

## Why it failed (post-mortem)

- **No informational edge.** The bot received prices, 24h moves, and a headline summary — a strict subset of what the market makers setting those prices already had. No prompt fixes that.
- **The price filter bought lottery tickets.** Rejecting contracts above $0.33 steered the bot exclusively into longshots — the segment where favorite-longshot bias makes prediction-market prices worst. 7W/22L at breakeven is that strategy's textbook signature.
- **The exits fought the strategy.** A longshot portfolio pays through its right tail; a +$0.20 take-profit cap truncated exactly that tail, while a 0.5× stop-loss realized losses on binary contracts whose prices whipsaw as noise.
- **The economics were unpassable by design.** The success threshold (net +$45 in 14 days) was mathematically incompatible with 3% sizing on a small bankroll once API costs were included.
- **Small-sample "learning."** Cutting a market segment after a 1W/7L record felt data-driven; over 8 longshot trades it's indistinguishable from chance.

## Operations lesson

The bot ran in an unsupervised `screen` session. It crashed silently ~36 hours into the evaluation window, and I didn't discover this for weeks. The next system runs under process supervision (systemd with restart policy) with a healthcheck/alerting layer — and gets paper-traded against historical settlement data before it touches live capital.

## Repo contents

- `kalshi_bot.py` — the full system (~980 lines), including the pre-registered evaluation criteria in the module docstring

Secrets are loaded from environment variables (`ANTHROPIC_API_KEY`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`); the bot hard-fails if they're missing.

## Status

Killed per pre-registered criteria, capital withdrawn, infrastructure decommissioned. The most valuable output was the post-mortem above.
