 kalshi-llm-trader

An autonomous trading system for Kalshi prediction markets, built through AI-assisted development. I designed the system and directed the build; Claude (Anthropic) wrote the Python under my direction across 15+ iteration cycles. I'm sharing it — including the results and the post-mortem — because the discipline around the experiment matters more to me than how it ended.

**Status:** Cycle 1 (real capital) was killed on schedule per the pre-registered criteria — post-mortem below. A post-kill code review surfaced real bugs in how the bot handled orders; v2 fixes them. Cycle 2 runs July 9–23, 2026 as a **paper evaluation**: live market data, real model decisions, simulated fills, zero capital at risk. Real money returns only if cycle 2 passes the same kill criteria.

## What it does

Every 15 minutes, the bot:

1. Pulls open markets across a configured universe (daily crypto, daily/weekly commodities) from the Kalshi API, filtered to a tradeable price band
2. Gathers context: live crypto prices (CoinGecko) and a Claude-generated news summary via web search
3. Runs a two-stage LLM decision pipeline — an analysis call with full market/position/history context, then a final decision made through a structured tool call that fills out a fixed schema (trade or no_trade, ticker, side, dollar amount). v1 had the decision stage emit a parseable line of text; malformed output was a real failure mode, and the structured call eliminates it
4. Places limit orders with risk rules enforced in code, not just in the prompt: 3% max position size, price-band entry filters, dedup against held series
5. Manages exits through a staged lifecycle (`pending_entry → open → exiting → resolved`): entries are only treated as positions once fills are confirmed, then absolute take-profit, proportional stop-loss, rate-limited order repricing, and settlement reconciliation to recover exit orders orphaned when a market settles before the exit fills
6. Writes a per-scan audit trail: one JSON file per decision capturing exactly what the model saw (balance, news, history, markets) and what it said (raw analysis plus structured decision), so the day-14 review can answer *why* it traded, not just what happened

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

The bot ran in an unsupervised `screen` session. It crashed silently ~36 hours into the evaluation window, and I didn't discover this for weeks. Cycle 2 fixes this directly: the bot now runs under systemd with a restart policy, plus an external heartbeat script that alerts me if the log goes silent — and it runs on paper before it touches live capital again.

## Post-kill engineering review (v2)

After cycle 1 ended, I put the entire codebase through a line-by-line AI-assisted code review with one instruction: fresh eyes, no context, tell me what's actually wrong. The review found bugs the 14-day run had masked — all in how the bot handled the lifecycle of orders, separate from whether the strategy had edge:

- **Phantom positions.** The bot placed entry limit orders and immediately treated them as filled positions. It never checked. An unfilled entry would sit in the log as a "position," and the exit logic would later try to sell contracts I didn't own.
- **A race condition that could double-sell.** When re-pricing an exit order, the bot cancelled and re-placed without checking whether the order had filled in the gap between those two actions.
- **No partial-fill handling.** If an order filled 3 of 5 contracts, the bot's accounting didn't know. Trade sizes were re-derived from arithmetic instead of recorded from what actually executed.
- **Guessed data fields.** P&L was computed from order fields the code guessed at, instead of from Kalshi's authoritative fills data.

v2 fixes all four: entries pass through a pending state and only become positions once fills are confirmed; exit re-pricing re-checks order status after every cancel; filled counts and prices come from the fills endpoint; and partial exits are tracked so the bot always knows exactly how many contracts remain. v2 also adds a per-scan check comparing the trade log against the exchange's actual positions.

None of this changes the cycle 1 verdict — the strategy failed for the structural reasons in the post-mortem, not because of these bugs. But I couldn't have fully trusted the P&L accounting either way, and now I can.

**Cycle 2 is a paper evaluation.** v2 runs the same code path end to end — live market data, real Claude analysis — but order placement is simulated: entries fill at the ask, exits at the bid, settlements pay $1/$0 from actual market results. Paper trades log to a separate file so they can never contaminate real trade history. Paper fills are optimistic — no queue, no slippage — so a passing paper cycle earns a live test, not scaled capital. The strategy parameters are unchanged and locked, and the same pre-registered kill criteria apply: if cycle 2 doesn't clear the bar by July 23, it dies the same way cycle 1 did.

## Strategy capacity (the ceiling if it works)

There's a structural problem with this bot that exists even in the scenario where everything goes right: the markets it trades are thin. Daily crypto and weekly commodity contracts on Kalshi often have modest resting liquidity, which means order size works against you. Small orders fill cleanly at quoted prices; larger orders eat through the book, move the price, or sit partially filled. Every strategy has a maximum amount of capital it can absorb before the act of trading it erodes the edge — and this one's ceiling is low.

Rough math: if these markets absorb perhaps $50–100 per order before fill quality degrades, then at 3% position sizing the effective bankroll caps out around $2–3K. Beyond that, added capital doesn't add returns — it adds slippage. And that connects back to the cycle 1 post-mortem: at the scale these markets allow, even a genuinely working strategy may struggle to clear its own ~$90/month API costs. The edge doesn't have to be fake to be uneconomical.

This is why the cycle 2 paper balance is $2,000 — the top of the pre-registered funding range, and roughly the largest bankroll where simulated instant fills still resemble what real order books would give. Testing at a size the markets can't actually support would produce results from a market that doesn't exist.

If the strategy ever earns a scale-up, the levers are known and non-trivial: widening the market universe (capacity is roughly additive across uncorrelated markets), switching from spread-crossing to passive resting orders, or slicing orders — all cycle 3+ work, and any scale-up test must model slippage rather than assume clean fills. Alternatively: accept the ceiling and treat it as a small machine with a known maximum output. Being small is retail's one structural advantage — you can harvest edges too tiny for serious capital to bother with.

## Repo contents

- `kalshi_bot.py` — the full system (~1,601 lines), including the pre-registered evaluation criteria in the module docstring. v1 is preserved in the commit history.

Secrets are loaded from environment variables (`ANTHROPIC_API_KEY`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`); the bot hard-fails if they're missing. Paper mode is a single environment variable (`PAPER_TRADING=true`), with the simulated bankroll set by `PAPER_STARTING_BALANCE`.

