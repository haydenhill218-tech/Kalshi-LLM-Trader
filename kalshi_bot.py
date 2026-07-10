
"""
====================================================================
CYCLE 2: 14-DAY PAPER EVALUATION (deployed: 2026-07-09, ends: 2026-07-23)
====================================================================
 
PAPER MODE: this cycle runs with PAPER_TRADING=true and a $2,000
simulated bankroll (top of the pre-registered funding range; sized to
respect strategy capacity - see README). The bot reads LIVE market
data and makes REAL Claude decisions, but places NO real orders and
risks NO capital. Fills are simulated (entries at the ask, exits at
the bid, settlements at $1/$0 from actual market results).
Simulated fills are OPTIMISTIC - no queue, no slippage - so a passing
paper cycle is necessary but not sufficient before funding.
 
Strategy parameters are LOCKED for 14 days from deploy date.
Bug fixes OK. Strategy changes (TP/SL/sizing/universe/prompt) NOT OK.
 
SUCCESS CONDITIONS (all four must be met):
  [ ] 14 calendar days complete, bot ran continuously
  [ ] 15+ resolved trades in the (paper) trade log
  [ ] Simulated net P&L > $45 (the bar that would cover ~$90/mo
      API + server prorated if this were real capital)
  [ ] No single position exceeded 5% of (paper) balance
 
DECISION TREE:
  All 4 met               -> fund real capital, run a LIVE 14-day cycle
                             (paper passing does not skip the live test)
  Some met, near-breakeven -> one more 14-day cycle, NO fund-up
  Two inconclusive cycles  -> KILL
  P&L clearly negative     -> KILL
  Fewer than 15 trades     -> KILL (insufficient data, won't get
                              better by waiting)
 
CYCLE 1 (2026-05-19 to 2026-06-02, real capital): KILLED per these
criteria. 29 resolved trades, 7W/22L, ~breakeven P&L. Post-mortem in
the README.
 
The "well maybe with more capital" rationalization is the exact
failure mode this checklist exists to prevent. If P&L net of costs
is negative on day 14, the strategy has not earned more capital.
 
====================================================================
 
Kalshi trading bot.
 
REVISION NOTES (bug-fix pass; strategy parameters unchanged):
 
1. ENTRY FILLS ARE NOW CONFIRMED. New lifecycle state `pending_entry`.
   An entry limit order is not treated as a position until the order
   is confirmed filled (fully or partially). Unfilled entries are
   cancelled after ENTRY_TTL_SEC and closed out with pnl=0 instead of
   becoming phantom positions the exit logic tries to sell.
 
2. FILLED COUNT IS STORED, NOT RECOMPUTED. The actual contract count
   and volume-weighted average entry price come from Kalshi's
   /portfolio/fills endpoint (authoritative), not from
   int(amount / entry_price) guesswork. All exit math uses the stored
   filled_count.
 
3. CANCEL/RE-PLACE RACE FIXED. Before re-pricing an exit, the bot
   cancels, then RE-CHECKS the order. If the order filled during the
   race window, the fill is accounted and no duplicate sell is placed.
   Fills are accumulated per-order exactly once (an order's fills are
   only tallied when it reaches a terminal state), so partial fills
   across multiple re-priced exit orders are summed correctly.
 
4. PARTIAL FILLS HANDLED. Cumulative `exited_count` / `exit_proceeds`
   track how many contracts have actually been sold across all exit
   orders for a trade. Re-priced exit orders are sized to the
   REMAINING count only. A trade resolves when remaining hits zero.
 
5. WRONG-MARKET FALLBACK REMOVED. If the model recommends a ticker
   that can't be found, the trade is skipped. Previously the bot fell
   back to "any market in the same series", which for strike-based
   markets could enter a completely different strike than the one
   analyzed.
 
6. STRUCTURED DECISIONS. The decide() stage now uses a forced
   tool-use call with a JSON schema instead of parsing a free-text
   line, eliminating the malformed-decision failure mode.
 
7. LOG-VS-EXCHANGE RECONCILIATION. Each scan compares the trade log's
   idea of open positions against Kalshi's actual positions and logs
   loud warnings on mismatch (warn-only; no destructive auto-repair).
 
8. Honest docstring: the trade log appends forever; there is no date
   rotation. (The previous header claimed rotation that didn't exist.)
 
9. PAPER MODE (PAPER_TRADING=true). Same code path end to end - live
   market data, real Claude analysis and decisions, same lifecycle
   states, same log schema - but order placement is simulated:
   entries fill instantly at the limit (ask) price, TP/SL exits fill
   instantly at the current bid, and settlements pay $1/$0 based on
   the market's actual result. Paper trades log to a SEPARATE file
   (paper_trade_log.json) and every record carries "paper": true, so
   paper results can never contaminate real trade history. Switching
   to live money later is one environment variable, zero code changes.
 
10. AUDIT TRAIL. Every scan that reaches the decision stage writes one
    JSON file to LOG_DIR/audit/ capturing the model's full context
    (balance, news, history summary, markets analyzed) plus its raw
    analysis and structured decision. Pure logging - it reads no
    state and influences no decisions - so it is lockdown-safe. This
    exists so the day-14 review can answer WHY the bot traded what it
    traded, not just what happened.
 
Carried over from the prior version:
- Exit orders actually exit; re-priced until they fill (rate-limited).
- Take-profit absolute (+0.20, capped 0.97); stop-loss proportional (0.5x).
- Secrets from environment variables; hard fail if missing.
- get_balance / get_open_positions hard-fail (no fake fallbacks).
- File-based concurrency lock prevents overlapping scans.
- Specific exception handling with logged errors.
- Dedup uses series_ticker consistently.
- R/R math in prompt is hold-to-resolution.
 
NOT modeled: Kalshi trading fees. P&L here is gross of fees; judge
the 14-day cycle against the fee-inclusive numbers in the account UI.
"""
 
import os
import sys
import json
import time
import base64
import datetime
import logging
import fcntl
from contextlib import contextmanager
 
import requests
import schedule
import anthropic
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as cp
 
 
# ---------- Configuration ----------
 
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "/root/kalshi_private_key.pem")
LOG_DIR = os.environ.get("KALSHI_LOG_DIR", "/root")
LOCK_PATH = "/tmp/kalshi_bot.lock"
 
if not ANTHROPIC_API_KEY or not KALSHI_API_KEY_ID:
    sys.stderr.write(
        "FATAL: ANTHROPIC_API_KEY and KALSHI_API_KEY_ID environment variables must be set.\n"
    )
    sys.exit(1)
 
if not os.path.exists(KEY_PATH):
    sys.stderr.write(f"FATAL: Kalshi private key not found at {KEY_PATH}\n")
    sys.exit(1)
 
# ---- Paper trading mode ----
# PAPER_TRADING=true : live market data, real Claude decisions, but no
# real orders and no real capital. Simulated fills, separate log file.
# Default paper bankroll is $2,000: the top of the pre-registered
# funding range, and roughly the largest size where simulated instant
# fills still resemble what these thin order books would really give
# (see "Strategy capacity" in the README).
PAPER_TRADING = os.environ.get("PAPER_TRADING", "").lower() in ("1", "true", "yes")
PAPER_STARTING_BALANCE = float(os.environ.get("PAPER_STARTING_BALANCE", "2000"))
 
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
if PAPER_TRADING:
    LOG_FILE = os.path.join(LOG_DIR, "paper_trade_log.json")
else:
    LOG_FILE = os.path.join(LOG_DIR, "trade_log.json")
ERROR_LOG = os.path.join(LOG_DIR, "bot_errors.log")
 
# Audit trail: one JSON file per scan capturing exactly what the model
# saw and said. Pure logging - reads no state, changes no decisions.
AUDIT_DIR = os.path.join(LOG_DIR, "audit")
os.makedirs(AUDIT_DIR, exist_ok=True)
 
CRYPTO_15M = []  # Removed - XRP 15-min markets had 1W/7L record, no edge
CRYPTO_DAILY = ["KXXRPD", "KXBTCD", "KXETHD", "KXSOLD"]
COMMODITIES = [
    "KXGOLDD", "KXGOLDW", "KXBRENTD", "KXBRENTW", "KXNATGASD", "KXNATGASW",
    "KXCORNW", "KXWHEATW", "KXCOPPERW", "KXSILVERW",
]
ALL_SERIES = CRYPTO_15M + CRYPTO_DAILY + COMMODITIES
 
# Strategy parameters (do not change for 14 days post-deploy)
TAKE_PROFIT_DELTA = 0.20      # exit when price >= entry + 0.20
TAKE_PROFIT_CAP = 0.97        # never set TP above this
STOP_LOSS_RATIO = 0.50        # exit when price <= entry * 0.50
MAX_POSITION_PCT = 0.03       # never risk more than 3% of balance
PRICE_FLOOR_ENTRY = 0.10      # don't trade contracts below this
PRICE_CEILING_ENTRY = 0.90    # don't trade contracts above this
REPRICE_THRESHOLD = 0.03      # only reprice exit if bid drift > this (3 cents)
REPRICE_MIN_INTERVAL_SEC = 1800  # at least 30 min (2 scan cycles) between reprices
 
# Execution hygiene (not strategy): entry limit orders that haven't
# filled within this window are cancelled. Prevents stale resting
# orders from filling hours later at a price the analysis no longer
# supports, and prevents phantom "open" positions.
ENTRY_TTL_SEC = 1800  # 30 min = 2 scan cycles
 
 
# ---------- Logging ----------
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(ERROR_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("kalshi_bot")
 
 
# ---------- Auth ----------
 
with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)
 
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
 
def sign(method, path):
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = ts + method + path.split("?")[0]
    sig = private_key.sign(
        msg.encode(),
        cp.PSS(mgf=cp.MGF1(hashes.SHA256()), salt_length=cp.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }
 
 
def kalshi_request(method, path, params=None, json_body=None, timeout=15):
    """Wrapped HTTP call with consistent error logging. Raises on failure."""
    url = BASE_URL + path
    headers = sign(method, "/trade-api/v2" + path)
    try:
        r = requests.request(
            method, url, headers=headers, params=params, json=json_body, timeout=timeout
        )
        if r.status_code >= 400:
            log.error(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Kalshi {method} {path} request failed: {e}")
        raise
 
 
# ---------- Concurrency lock ----------
 
@contextmanager
def scan_lock():
    """Prevent overlapping scans. Raises RuntimeError if another scan is running."""
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("Another scan is already running; skipping.")
    try:
        f.write(str(os.getpid()))
        f.flush()
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
 
 
# ---------- Account state ----------
 
def get_balance():
    """Returns balance in dollars. Raises on failure or missing field."""
    if PAPER_TRADING:
        # Simulated balance: starting stake plus all resolved paper P&L,
        # minus cash currently committed to open paper positions.
        bal = PAPER_STARTING_BALANCE
        for t in load_trades():
            if t.get("lifecycle") == "resolved":
                bal += float(t.get("pnl") or 0)
            elif t.get("lifecycle") in ("open", "exiting"):
                entry = float(t.get("avg_entry_price") or t.get("entry_price") or 0)
                held = int(t.get("filled_count", 0)) - int(t.get("exited_count", 0))
                bal -= entry * max(0, held)
        return round(bal, 2)
    data = kalshi_request("GET", "/portfolio/balance")
    # KeyError here is intentional: a response without a balance field
    # should abort the scan, not silently size trades off $0.
    return float(data["balance"]) / 100
 
 
def get_open_positions():
    """
    Returns list of position dicts. Raises on failure - sizing and
    dedup depend on this.
    """
    if PAPER_TRADING:
        # Paper positions live only in the trade log.
        result = []
        for t in load_trades():
            if t.get("lifecycle") in ("open", "exiting"):
                held = int(t.get("filled_count", 0)) - int(t.get("exited_count", 0))
                if held > 0:
                    result.append({
                        "ticker": t["ticker"],
                        "series": t.get("series", t["ticker"].split("-")[0]),
                        "count": held,
                        "side": t["side"],
                    })
        return result
    data = kalshi_request("GET", "/portfolio/positions", params={"limit": 50})
    positions = data.get("market_positions", [])
    result = []
    for p in positions:
        pos = float(p.get("position_fp", 0) or 0)
        if abs(pos) < 0.0001:
            continue
        ticker = p["ticker"]
        parts = ticker.split("-")
        series = parts[0] if parts else ticker
        # Positive position = YES holding, negative = NO holding (Kalshi convention)
        side = "yes" if pos > 0 else "no"
        result.append({
            "ticker": ticker,
            "series": series,
            "count": abs(pos),
            "side": side,
        })
    return result
 
 
def get_order_status(order_id):
    """Returns order dict or None if not found / request failed."""
    try:
        data = kalshi_request("GET", f"/portfolio/orders/{order_id}")
        return data.get("order")
    except requests.exceptions.RequestException:
        return None
 
 
def cancel_order(order_id):
    """
    Attempt to cancel. Returns True if the API accepted the cancel,
    False otherwise (including 'already filled'). Callers MUST NOT
    assume the order is dead on False - re-check with get_order_status.
    """
    try:
        kalshi_request("DELETE", f"/portfolio/orders/{order_id}")
        return True
    except requests.exceptions.RequestException:
        return False
 
 
# ---------- Fills (authoritative execution data) ----------
 
def _fill_side_price(fill, side):
    """
    Extract the side-relative execution price in dollars from a fill
    record. Prefers the *_price_dollars field; falls back to the
    legacy *_price field in cents.
    """
    v = fill.get(f"{side}_price_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = fill.get(f"{side}_price")
    if v not in (None, ""):
        try:
            return float(v) / 100.0
        except (TypeError, ValueError):
            pass
    return None
 
 
def get_order_fills(order_id, side):
    """
    Returns (filled_count, avg_price_dollars) for an order from the
    /portfolio/fills endpoint. avg_price is volume-weighted and
    side-relative (YES price for yes, NO price for no).
    Returns (0, None) if there are no fills or the request fails.
    """
    try:
        data = kalshi_request(
            "GET", "/portfolio/fills",
            params={"order_id": order_id, "limit": 200},
        )
    except requests.exceptions.RequestException:
        return 0, None
 
    total = 0
    notional = 0.0
    for f in data.get("fills", []):
        try:
            c = int(f.get("count", 0))
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        price = _fill_side_price(f, side)
        if price is None:
            continue
        total += c
        notional += c * price
 
    if total <= 0:
        return 0, None
    return total, round(notional / total, 4)
 
 
# ---------- Market data ----------
 
def fetch_series_markets(series_ticker):
    """Fetch open markets for a series with full metadata."""
    try:
        data = kalshi_request(
            "GET", "/markets",
            params={"limit": 10, "status": "open", "series_ticker": series_ticker},
        )
        return data.get("markets", [])
    except requests.exceptions.RequestException:
        return []
 
 
def get_all_markets():
    """Fetch markets across the configured universe with filtering."""
    markets = []
    for series in ALL_SERIES:
        for m in fetch_series_markets(series):
            ask = float(m.get("yes_ask_dollars", 0) or 0)
            if PRICE_FLOOR_ENTRY < ask < PRICE_CEILING_ENTRY:
                markets.append(m)
                if len([x for x in markets if x.get("ticker", "").startswith(series)]) >= 2:
                    break
 
    return markets[:30]
 
 
def find_market(ticker):
    """Look up a specific OPEN market by ticker."""
    series = ticker.split("-")[0]
    for m in fetch_series_markets(series):
        if m.get("ticker") == ticker:
            return m
    return None
 
 
def fetch_market_any_status(ticker):
    """
    Fetch a single market regardless of status (open, closed, settled).
    Used by paper-mode settlement to learn the final result.
    """
    try:
        data = kalshi_request("GET", f"/markets/{ticker}")
        return data.get("market")
    except requests.exceptions.RequestException:
        return None
 
 
# ---------- News ----------
 
def get_news():
    """Crypto prices plus a Claude-summarized world news snippet."""
    crypto_news = ""
    try:
        coins = {"XRP": "ripple", "BTC": "bitcoin", "ETH": "ethereum",
                 "SOL": "solana", "DOGE": "dogecoin", "BNB": "binancecoin"}
        ids = ",".join(coins.values())
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        lines = []
        for symbol, cid in coins.items():
            if cid in data:
                p = data[cid]["usd"]
                c = data[cid].get("usd_24h_change", 0)
                d = "UP" if c > 0 else "DOWN"
                lines.append(f"{symbol}: ${p} {d} {c:.1f}%")
        crypto_news = ", ".join(lines)
    except requests.exceptions.RequestException as e:
        log.warning(f"CoinGecko fetch failed: {e}")
        crypto_news = "crypto unavailable"
 
    world = "news unavailable"
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": "Search top breaking news today affecting commodities, "
                           "crypto, and macro markets. 3 short bullets."
            }],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        )
        # The final text block is the post-search answer; earlier text
        # blocks are usually preamble before the search executes.
        text_blocks = [b.text for b in resp.content
                       if hasattr(b, "text") and b.text and len(b.text) > 20]
        if text_blocks:
            world = text_blocks[-1][:300]
    except anthropic.APIError as e:
        log.warning(f"News summary failed: {e}")
 
    return f"CRYPTO: {crypto_news} | NEWS: {world}"
 
 
# ---------- Trade log ----------
#
# The trade log appends forever. There is no rotation or truncation;
# at this trade volume (a few rows/day) the full-load-full-save cycle
# is fine for years. Revisit if it ever exceeds a few MB.
#
# Trade record schema:
#   time                entry order placement time (ISO)
#   ticker, series, side
#   amount              dollars requested at entry
#   entry_price         limit price requested at entry
#   requested_count     contracts requested at entry
#   entry_order_id
#   filled_count        contracts actually filled (from fills endpoint)
#   avg_entry_price     volume-weighted entry fill price
#   exit_order_id       currently-working exit order (or None)
#   exit_order_price    price of currently-working exit order
#   exited_count        cumulative contracts sold across all exit orders
#   exit_proceeds       cumulative dollars received from exit fills
#   exit_reason         take_profit | stop_loss | settlement_* | entry_unfilled | ...
#   lifecycle           pending_entry | open | exiting | resolved
#   fill_price          avg exit fill price (set at resolution)
#   pnl                 gross P&L, dollars (fees not modeled)
#   last_reprice_time   ISO timestamp of last exit re-price
 
ACTIVE_LIFECYCLES = ("pending_entry", "open", "exiting")
 
 
def migrate_trade(t):
    """
    Normalize a trade dict to the current schema in-place. Handles
    both the original schema (`resolved` bool / `price` / `status`)
    and the intermediate schema (lifecycle present, but no fill
    tracking fields). Idempotent.
    """
    # --- v1 -> v2: original schema -> lifecycle schema ---
    if "lifecycle" not in t:
        if "price" in t and "entry_price" not in t:
            t["entry_price"] = t.pop("price")
        t.setdefault("series", t.get("ticker", "").split("-")[0])
        t.setdefault("entry_order_id", t.get("order_id"))
        t.setdefault("exit_order_id", None)
        t.setdefault("exit_order_price", None)
 
        if t.get("resolved"):
            t["lifecycle"] = "resolved"
            t.setdefault("exit_reason", t.get("final_status", "settlement"))
        else:
            # Position from old code: mark resolved so exit logic skips
            # it. Settlement reconciliation picks up the real P&L.
            t["lifecycle"] = "resolved"
            t["exit_reason"] = "legacy_unmigrated"
            log.warning(f"Legacy trade {t.get('ticker')} marked resolved; "
                        f"will close via settlement only.")
 
        t.pop("resolved", None)
        t.pop("final_status", None)
        t.pop("order_id", None)
        t.pop("status", None)
 
    # --- v2 -> v3: add fill-tracking fields ---
    if "filled_count" not in t:
        entry = float(t.get("entry_price") or 0)
        amount = float(t.get("amount") or 0)
        # Best-effort reconstruction for rows written before fill
        # tracking existed; matches the old implicit count math.
        est = max(1, int(amount / entry)) if entry > 0 else 0
        t["requested_count"] = est
        t["filled_count"] = est
        t["avg_entry_price"] = entry
    t.setdefault("requested_count", t.get("filled_count", 0))
    t.setdefault("avg_entry_price", t.get("entry_price"))
    t.setdefault("exited_count", 0)
    t.setdefault("exit_proceeds", 0.0)
    t.setdefault("fill_price", None)
    t.setdefault("pnl", None)
    t.pop("entry_status", None)  # replaced by lifecycle=pending_entry
    return t
 
 
def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE) as f:
            trades = json.load(f)
        return [migrate_trade(t) for t in trades]
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Trade log read failed: {e}")
        return []
 
 
def save_trades(trades):
    """Atomic write via temp file rename."""
    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, LOG_FILE)
 
 
def migrate_log_to_disk():
    """
    Run once at startup. Load the trade log, migrate any old-schema
    rows, and persist the result so external tooling (check_day14.sh)
    sees consistent fields. Safe to run repeatedly.
    """
    if not os.path.exists(LOG_FILE):
        log.info("No existing trade log to migrate.")
        return
    try:
        with open(LOG_FILE) as f:
            trades = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Cannot read trade log for migration: {e}")
        return
 
    needs = sum(1 for t in trades
                if "lifecycle" not in t or "filled_count" not in t)
    if needs == 0:
        log.info(f"Trade log already in current schema ({len(trades)} rows).")
        return
 
    log.info(f"Migrating {needs} rows out of {len(trades)} total...")
    migrated = [migrate_trade(t) for t in trades]
    save_trades(migrated)
    log.info("Migration persisted to disk.")
 
 
def record_entry(ticker, side, price, amount, count, order_id):
    trades = load_trades()
    trades.append({
        "time": datetime.datetime.now().isoformat(),
        "ticker": ticker,
        "series": ticker.split("-")[0],
        "side": side,
        "entry_price": float(price),
        "amount": float(amount),
        "requested_count": int(count),
        "entry_order_id": order_id,
        "filled_count": 0,
        "avg_entry_price": None,
        "exit_order_id": None,
        "exit_order_price": None,
        "exited_count": 0,
        "exit_proceeds": 0.0,
        "exit_reason": None,
        "lifecycle": "pending_entry",   # pending_entry | open | exiting | resolved
        "fill_price": None,
        "pnl": None,
        "paper": PAPER_TRADING,
    })
    save_trades(trades)
 
 
# ---------- Entry lifecycle ----------
 
def manage_entries():
    """
    Confirm entry fills before treating anything as a position.
 
    pending_entry -> open       when the entry order has fills
    pending_entry -> resolved   (exit_reason=entry_unfilled, pnl=0)
                                when the order dies with zero fills
    Resting orders older than ENTRY_TTL_SEC are cancelled; the
    terminal-state branch classifies them on the next pass.
    """
    trades = load_trades()
    changed = False
    now = datetime.datetime.now()
 
    for t in trades:
        if t.get("lifecycle") != "pending_entry":
            continue
 
        ticker = t["ticker"]
        oid = t.get("entry_order_id")
        if not oid:
            t["lifecycle"] = "resolved"
            t["exit_reason"] = "entry_error_no_order_id"
            t["pnl"] = 0.0
            log.error(f"Entry for {ticker} has no order id; closing record.")
            changed = True
            continue
 
        if str(oid).startswith("PAPER-"):
            # Paper entries fill instantly at the limit (ask) price.
            t["filled_count"] = t["requested_count"]
            t["avg_entry_price"] = t["entry_price"]
            t["lifecycle"] = "open"
            log.info(f"[PAPER] ENTRY FILLED: {ticker} {t['side']} "
                     f"{t['filled_count']} @ {t['avg_entry_price']}")
            changed = True
            continue
 
        order = get_order_status(oid)
        status = (order or {}).get("status", "")
 
        if status == "filled":
            fc, avg = get_order_fills(oid, t["side"])
            if fc <= 0:
                # Fills endpoint unavailable; fall back to requested
                # values and say so loudly.
                fc = t["requested_count"]
                avg = t["entry_price"]
                log.warning(f"Fills lookup failed for entry {oid} ({ticker}); "
                            f"assuming requested count {fc} @ {avg}")
            t["filled_count"] = fc
            t["avg_entry_price"] = avg
            t["lifecycle"] = "open"
            log.info(f"ENTRY FILLED: {ticker} {t['side']} "
                     f"{fc} contracts @ {avg}")
            changed = True
            continue
 
        if status in ("canceled", "cancelled", "expired"):
            fc, avg = get_order_fills(oid, t["side"])
            if fc > 0:
                t["filled_count"] = fc
                t["avg_entry_price"] = avg
                t["lifecycle"] = "open"
                log.info(f"ENTRY PARTIALLY FILLED then {status}: {ticker} "
                         f"{fc}/{t['requested_count']} @ {avg}; managing as open.")
            else:
                t["lifecycle"] = "resolved"
                t["exit_reason"] = "entry_unfilled"
                t["pnl"] = 0.0
                log.info(f"ENTRY UNFILLED: {ticker} order {status}; "
                         f"no position taken.")
            changed = True
            continue
 
        # Still resting - enforce TTL
        try:
            placed = datetime.datetime.fromisoformat(t["time"])
        except (TypeError, ValueError):
            placed = now
        if (now - placed).total_seconds() > ENTRY_TTL_SEC:
            log.info(f"Entry order for {ticker} unfilled after "
                     f"{ENTRY_TTL_SEC}s; cancelling.")
            cancel_order(oid)
            # Next pass hits the terminal-state branch and classifies
            # zero-fill vs partial-fill correctly.
 
    if changed:
        save_trades(trades)
 
 
# ---------- Exit logic ----------
 
def compute_take_profit(entry_price):
    return min(entry_price + TAKE_PROFIT_DELTA, TAKE_PROFIT_CAP)
 
 
def compute_stop_loss(entry_price):
    return entry_price * STOP_LOSS_RATIO
 
 
def current_sell_price(market, side):
    """Price at which we can currently sell our position (the bid we'd hit)."""
    if side == "yes":
        return float(market.get("yes_bid_dollars", 0) or 0)
    # NO position - we sell NO at the NO bid, which Kalshi exposes as no_bid_dollars
    # If not present, derive from yes_ask: no_bid = 1 - yes_ask
    no_bid = market.get("no_bid_dollars")
    if no_bid is not None:
        return float(no_bid)
    yes_ask = float(market.get("yes_ask_dollars", 1) or 1)
    return round(1 - yes_ask, 4)
 
 
def place_exit_order(ticker, side, sell_price, count):
    """
    Close a position using Kalshi's standard sell action.
    - YES position: sell YES at yes_price_dollars (the YES bid we want to hit)
    - NO position: sell NO at no_price_dollars (the NO bid we want to hit)
 
    sell_price is side-relative (YES bid for yes, NO bid for no).
    Returns API response dict.
    """
    count = int(count)
    if count <= 0:
        raise ValueError(f"Cannot place exit with count {count}")
    if side not in ("yes", "no"):
        raise ValueError(f"Invalid side: {side}")
 
    price_key = f"{side}_price_dollars"
    payload = {
        "ticker": ticker,
        "side": side,
        "type": "limit",
        "count": count,
        "action": "sell",
        price_key: f"{float(sell_price):.2f}",
    }
    return kalshi_request("POST", "/portfolio/orders", json_body=payload)
 
 
def _absorb_exit_order_fills(t, order_id):
    """
    Tally the fills of a terminal exit order into the trade's
    cumulative exit accounting, exactly once, then detach the order.
    Returns the number of contracts still remaining to sell.
    """
    fc, avg = get_order_fills(order_id, t["side"])
    if fc > 0 and avg is not None:
        t["exited_count"] = int(t.get("exited_count", 0)) + fc
        t["exit_proceeds"] = round(
            float(t.get("exit_proceeds", 0.0)) + fc * avg, 4
        )
    t["exit_order_id"] = None
    t["exit_order_price"] = None
    return max(0, int(t["filled_count"]) - int(t["exited_count"]))
 
 
def _resolve_via_exit(t):
    """Mark a trade resolved from cumulative exit fills."""
    exited = int(t.get("exited_count", 0))
    proceeds = float(t.get("exit_proceeds", 0.0))
    entry = float(t.get("avg_entry_price") or t.get("entry_price") or 0)
    t["fill_price"] = round(proceeds / exited, 4) if exited > 0 else None
    t["pnl"] = round(proceeds - entry * exited, 2)
    t["lifecycle"] = "resolved"
    log.info(f"EXIT COMPLETE: {t['ticker']} {t['side']} entry={entry} "
             f"avg_exit={t['fill_price']} count={exited} pnl={t['pnl']}")
 
 
def manage_exits():
    """
    Two-phase exit management:
    1. For 'open' trades: check if TP or SL triggered; if so, place an
       exit order for the actual filled count.
    2. For 'exiting' trades: if the working exit order filled, absorb
       its fills and resolve (or re-place for any remainder). If still
       unfilled and the bid drifted, cancel-then-RE-CHECK before
       re-pricing, so a fill during the race window is never doubled.
    """
    trades = load_trades()
    changed = False
 
    for t in trades:
        if t.get("lifecycle") not in ("open", "exiting"):
            continue
 
        ticker = t["ticker"]
        side = t["side"]
        entry = float(t.get("avg_entry_price") or t.get("entry_price") or 0)
        filled_count = int(t.get("filled_count", 0))
        if filled_count <= 0:
            log.error(f"{ticker} is {t['lifecycle']} with filled_count=0; "
                      f"skipping (reconciliation should flag this).")
            continue
 
        market = find_market(ticker)
        if not market:
            # Market may have resolved already; settlements job will catch it
            continue
 
        # ---- Phase 2: exit order already placed ----
        if t.get("lifecycle") == "exiting" and t.get("exit_order_id"):
            oid = t["exit_order_id"]
            order = get_order_status(oid)
            status = (order or {}).get("status", "")
 
            if status == "filled":
                remaining = _absorb_exit_order_fills(t, oid)
                if remaining > 0:
                    # Shouldn't happen for a 'filled' order, but handle it.
                    bid = current_sell_price(market, side)
                    if bid > 0:
                        try:
                            result = place_exit_order(ticker, side, bid, remaining)
                            o = result.get("order", {})
                            t["exit_order_id"] = o.get("order_id")
                            t["exit_order_price"] = bid
                        except requests.exceptions.RequestException as e:
                            log.error(f"Remainder exit failed for {ticker}: {e}")
                else:
                    _resolve_via_exit(t)
                changed = True
                continue
 
            if status in ("canceled", "cancelled", "expired"):
                # Order died outside our control (e.g. market halted).
                remaining = _absorb_exit_order_fills(t, oid)
                changed = True
                if remaining <= 0:
                    _resolve_via_exit(t)
                    continue
                # Fall through to re-place below via the reprice path:
                # treat as if no working order exists.
                bid = current_sell_price(market, side)
                if bid > 0:
                    try:
                        result = place_exit_order(ticker, side, bid, remaining)
                        o = result.get("order", {})
                        t["exit_order_id"] = o.get("order_id")
                        t["exit_order_price"] = bid
                        t["last_reprice_time"] = datetime.datetime.now().isoformat()
                        log.info(f"Re-placed dead exit for {ticker}: "
                                 f"{remaining} @ {bid}")
                    except requests.exceptions.RequestException as e:
                        log.error(f"Exit re-place failed for {ticker}: {e}")
                continue
 
            # Order still working. Consider a re-price.
            current_bid = current_sell_price(market, side)
            if current_bid <= 0:
                log.warning(f"No bid available for {ticker}; leaving exit order in place")
                continue
 
            last_reprice = t.get("last_reprice_time")
            now = datetime.datetime.now()
            if last_reprice:
                elapsed = (now - datetime.datetime.fromisoformat(last_reprice)).total_seconds()
                if elapsed < REPRICE_MIN_INTERVAL_SEC:
                    continue  # too soon since last reprice
 
            price_drift = abs(current_bid - float(t.get("exit_order_price") or 0))
            if price_drift <= REPRICE_THRESHOLD:
                continue
 
            log.info(f"Re-pricing exit for {ticker}: "
                     f"{t['exit_order_price']} -> {current_bid} (drift {price_drift:.3f})")
            cancel_order(oid)
 
            # RACE FIX: the order may have filled between the status
            # check and the cancel. Re-check before placing anything.
            order_after = get_order_status(oid)
            status_after = (order_after or {}).get("status", "")
            if status_after not in ("canceled", "cancelled", "expired", "filled"):
                # Cancel didn't land and order isn't terminal - leave
                # it alone this scan rather than risk a duplicate sell.
                log.warning(f"Cancel for {ticker} exit {oid} not confirmed "
                            f"(status={status_after or 'unknown'}); "
                            f"deferring re-price.")
                continue
 
            remaining = _absorb_exit_order_fills(t, oid)
            changed = True
            if remaining <= 0:
                _resolve_via_exit(t)
                continue
 
            try:
                result = place_exit_order(ticker, side, current_bid, remaining)
                o = result.get("order", {})
                t["exit_order_id"] = o.get("order_id")
                t["exit_order_price"] = current_bid
                t["last_reprice_time"] = now.isoformat()
            except requests.exceptions.RequestException as e:
                log.error(f"Re-price failed for {ticker}: {e}")
                # exited_count/exit_proceeds already saved; next scan
                # re-places for the remainder via the dead-order path.
            continue
 
        # ---- Phase 1: still open - check TP/SL triggers ----
        current_bid = current_sell_price(market, side)
        tp = compute_take_profit(entry)
        sl = compute_stop_loss(entry)
 
        if current_bid >= tp:
            reason = "take_profit"
        elif current_bid <= sl and current_bid > 0:
            reason = "stop_loss"
        else:
            continue
 
        remaining = filled_count - int(t.get("exited_count", 0))
        if remaining <= 0:
            log.error(f"{ticker} open with nothing left to sell; skipping.")
            continue
 
        if PAPER_TRADING:
            # Paper exits fill instantly at the current bid. Optimistic
            # (no queue, no partial), but the bid is the honest price.
            t["exited_count"] = int(t.get("exited_count", 0)) + remaining
            t["exit_proceeds"] = round(
                float(t.get("exit_proceeds", 0.0)) + remaining * current_bid, 4)
            t["exit_reason"] = reason
            log.info(f"[PAPER] {reason.upper()}: {ticker} {side} "
                     f"entry={entry} exit={current_bid} x{remaining}")
            _resolve_via_exit(t)
            changed = True
            continue
 
        log.info(f"{reason.upper()}: {ticker} {side} entry={entry} "
                 f"bid={current_bid} -> placing exit for {remaining}")
        try:
            result = place_exit_order(ticker, side, current_bid, remaining)
            order = result.get("order", {})
            t["exit_order_id"] = order.get("order_id")
            t["exit_order_price"] = current_bid
            t["exit_reason"] = reason
            t["lifecycle"] = "exiting"
            changed = True
        except requests.exceptions.RequestException as e:
            log.error(f"Exit order placement failed for {ticker}: {e}")
 
    if changed:
        save_trades(trades)
 
 
# ---------- Settlement reconciliation ----------
 
def get_settlements():
    try:
        data = kalshi_request("GET", "/portfolio/settlements", params={"limit": 50})
        return {s["ticker"]: s for s in data.get("settlements", [])}
    except requests.exceptions.RequestException:
        return {}
 
 
def reconcile_settlements():
    """
    Settlement reconciliation handles:
 
    1. `open` trades: position rode to expiration without TP/SL trigger.
    2. `exiting` trades whose market settled before the exit order
       filled (Kalshi auto-cancels unfilled orders at settlement).
    3. `pending_entry` trades whose market settled - the entry order
       was auto-cancelled; if it had partial fills, those settled too.
 
    If a trade was PARTIALLY exited before settlement, total P&L is
    the sum of realized exit P&L plus the settlement P&L reported by
    the API for the remaining contracts.
 
    PAPER MODE: there are no real settlements to query. Instead, each
    active paper trade's market is fetched directly; once the market
    reports a final result, the position pays $1 (win) or $0 (loss)
    per contract.
    """
    trades = load_trades()
 
    if PAPER_TRADING:
        changed = False
        for t in trades:
            if t.get("lifecycle") not in ACTIVE_LIFECYCLES:
                continue
            m = fetch_market_any_status(t["ticker"])
            if not m:
                continue
            status = str(m.get("status", "")).lower()
            result = str(m.get("result", "")).lower()
            if status not in ("settled", "finalized", "determined") \
                    or result not in ("yes", "no"):
                continue
 
            side = t["side"]
            won = (side == result)
            entry = float(t.get("avg_entry_price") or t.get("entry_price") or 0)
            held = int(t.get("filled_count", 0)) - int(t.get("exited_count", 0))
            payout = (1.0 if won else 0.0) * max(0, held)
            exited = int(t.get("exited_count", 0))
            exit_pnl = round(float(t.get("exit_proceeds", 0.0)) - entry * exited, 2)
            settle_pnl = round(payout - entry * max(0, held), 2)
 
            t["lifecycle"] = "resolved"
            t["exit_reason"] = "settlement_win" if won else "settlement_loss"
            t["fill_price"] = 1.0 if won else 0.0
            t["pnl"] = round(settle_pnl + exit_pnl, 2)
            log.info(f"[PAPER] SETTLED: {t['ticker']} {side} result={result} "
                     f"pnl=${t['pnl']}")
            changed = True
 
        if changed:
            save_trades(trades)
        return
 
    settlements = get_settlements()
    changed = False
    for t in trades:
        lifecycle = t.get("lifecycle")
        if lifecycle not in ACTIVE_LIFECYCLES:
            continue
 
        s = settlements.get(t["ticker"])
        if not s:
            continue
 
        side = t["side"]
 
        # Absorb any fills on a still-attached exit order before
        # computing final numbers (it was auto-cancelled at settlement
        # and may have partially filled first).
        if t.get("exit_order_id"):
            _absorb_exit_order_fills(t, t["exit_order_id"])
 
        result = s.get("market_result", "")
        won = (side == "yes" and result == "yes") or (side == "no" and result == "no")
        revenue = float(s.get("revenue", 0)) / 100
        cost_key = "yes_total_cost_dollars" if side == "yes" else "no_total_cost_dollars"
        cost = float(s.get(cost_key, t["amount"]) or t["amount"])
        settlement_pnl = round(revenue - cost, 2)
 
        exited = int(t.get("exited_count", 0))
        entry = float(t.get("avg_entry_price") or t.get("entry_price") or 0)
        exit_pnl = round(float(t.get("exit_proceeds", 0.0)) - entry * exited, 2)
 
        t["lifecycle"] = "resolved"
        t["pnl"] = round(settlement_pnl + exit_pnl, 2)
 
        if lifecycle == "pending_entry":
            t["exit_reason"] = "settled_pending_entry"
            log.warning(f"PENDING ENTRY SETTLED: {t['ticker']} {side} - entry "
                        f"order died at settlement. pnl=${t['pnl']}")
        elif lifecycle == "exiting":
            t["exit_reason"] = "settled_during_exit_win" if won else "settled_during_exit_loss"
            log.warning(f"STUCK EXIT SETTLED: {t['ticker']} {side} - exit did "
                        f"not fully fill before settlement. result={result} "
                        f"exit_pnl=${exit_pnl} settle_pnl=${settlement_pnl} "
                        f"total=${t['pnl']}")
        else:
            t["exit_reason"] = "settlement_win" if won else "settlement_loss"
            log.info(f"SETTLED: {t['ticker']} {side} result={result} pnl=${t['pnl']}")
        changed = True
 
    if changed:
        save_trades(trades)
 
 
# ---------- Log vs exchange reconciliation ----------
 
def reconcile_position_state(open_positions):
    """
    Compare the trade log's view of held contracts against Kalshi's
    actual positions. WARN-ONLY by design: automatic repair of money
    state is more dangerous than a loud log line. If these warnings
    fire, investigate manually before trusting the bot further.
    """
    if PAPER_TRADING:
        return  # No exchange positions exist to reconcile against.
    actual = {p["ticker"]: p for p in open_positions}
    log_holdings = {}
    for t in load_trades():
        if t.get("lifecycle") in ("open", "exiting"):
            held = int(t.get("filled_count", 0)) - int(t.get("exited_count", 0))
            if held > 0:
                log_holdings[t["ticker"]] = log_holdings.get(t["ticker"], 0) + held
 
    for ticker, held in log_holdings.items():
        a = actual.get(ticker)
        if not a:
            log.warning(f"RECONCILE: log says we hold {held}x {ticker} "
                        f"but exchange shows no position "
                        f"(possibly just settled - watch next scan).")
        elif int(a["count"]) != held:
            log.warning(f"RECONCILE: count mismatch on {ticker}: "
                        f"log={held} exchange={int(a['count'])}")
 
    for ticker, a in actual.items():
        if ticker not in log_holdings:
            log.warning(f"RECONCILE: exchange shows {int(a['count'])}x {ticker} "
                        f"({a['side']}) not tracked in trade log.")
 
 
# ---------- Learning summary ----------
 
def summarize_history():
    trades = load_trades()
    # entry_unfilled rows are non-trades; exclude from the record.
    resolved = [t for t in trades
                if t.get("lifecycle") == "resolved"
                and t.get("exit_reason") not in ("entry_unfilled",
                                                 "entry_error_no_order_id")]
    if not resolved:
        return "No resolved trades yet."
 
    by_series = {}
    total = 0
    for t in resolved:
        s = t.get("series", "?")
        by_series.setdefault(s, {"w": 0, "l": 0, "pnl": 0.0})
        pnl = t.get("pnl") or 0
        total += pnl
        if pnl > 0:
            by_series[s]["w"] += 1
        else:
            by_series[s]["l"] += 1
        by_series[s]["pnl"] += pnl
 
    parts = [
        f"{k} {v['w']}W/{v['l']}L pnl=${round(v['pnl'], 2)}"
        for k, v in by_series.items()
    ]
    recent = " | ".join(
        f"{t.get('series','?')} {t['side']}@{t.get('avg_entry_price', t['entry_price'])} "
        f"{t.get('exit_reason','?')} pnl=${t.get('pnl', 0)}"
        for t in resolved[-5:]
    )
    return f"{' | '.join(parts)} | Total=${round(total, 2)} | Recent: {recent}"
 
 
# ---------- Decision making ----------
 
SYSTEM_PROMPT = """You are a disciplined Kalshi trader using strict reward-to-risk rules.
 
R/R MATH (hold to resolution):
- YES at price P: risk = P, reward = (1 - P)
- NO at price P: risk = P, reward = (1 - P)
Stops exist for downside protection, NOT to improve R/R.
 
HARD REJECT:
- YES price above 0.33 unless thesis is overwhelming (3:1+ confidence)
- NO price above 0.33 unless thesis is overwhelming
- Anything with thin volume (<50 trades today) unless catalyst is imminent
- Anything resolving in under 1 hour
 
PRIORITIES:
1. Commodities daily/weekly with low-priced contracts and clear catalyst
2. Daily crypto with clear momentum direction
 
AVOID:
- Series with more losses than wins in recent history
- Tickers already in current open positions list
 
SIZING (% of balance):
- 2:1 R/R: 1%
- 3:1+ R/R: 2%
- 5:1+ R/R: 3%
- NEVER more than 3%
 
If no trade meets 2:1 minimum, output NO_TRADE.
 
Calculate R/R explicitly before each recommendation."""
 
 
DECISION_TOOL = {
    "name": "submit_decision",
    "description": "Submit the final trading decision. Use action='no_trade' "
                   "if nothing meets the criteria.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["trade", "no_trade"],
            },
            "ticker": {
                "type": "string",
                "description": "Exact ticker from the Markets list. "
                               "Required when action=trade.",
            },
            "side": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Required when action=trade.",
            },
            "amount_dollars": {
                "type": "number",
                "description": "Dollars to commit, <= the stated max. "
                               "Required when action=trade.",
            },
        },
        "required": ["action"],
    },
}
 
 
def analyze(markets, news, learning, balance, max_trade, open_positions_str):
    parts = []
    for m in markets:
        ticker = m["ticker"][:35]
        title = str(m.get("title", m.get("event_title", "")))[:40]
        ask = m.get("yes_ask_dollars")
        bid = m.get("yes_bid_dollars")
        volume = m.get("volume", "?")
        close_time = m.get("close_time", "?")
        no_ask = m.get("no_ask_dollars", "?")
        parts.append(
            f"{ticker} ({title}) yes_ask={ask} yes_bid={bid} no_ask={no_ask} "
            f"vol={volume} closes={close_time}"
        )
    summary = " | ".join(parts)
 
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Balance ${balance:.2f}. Max trade ${max_trade:.2f}. "
                f"History: {learning}. "
                f"News: {news}. "
                f"Currently holding (DO NOT recommend these): {open_positions_str}. "
                f"Markets: {summary}. "
                f"Find the best trade or output NO_TRADE. "
                f"ONLY recommend tickers from the Markets list."
            ),
        }],
    )
    return resp.content[0].text
 
 
def decide(analysis, max_trade):
    """
    Structured final decision via forced tool use. Returns a dict:
      {"action": "no_trade"}  or
      {"action": "trade", "ticker": ..., "side": ..., "amount_dollars": ...}
    Falls back to no_trade on anything unexpected.
    """
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=(
                "You are the final decision stage of a trading bot. Read the "
                "analysis and submit exactly one decision with the "
                "submit_decision tool. Only recommend tickers that appear in "
                "the analysis. amount_dollars must not exceed the stated max."
            ),
            messages=[{
                "role": "user",
                "content": f"Max: ${max_trade:.2f}. Analysis: {analysis}. Decision?",
            }],
            tools=[DECISION_TOOL],
            tool_choice={"type": "tool", "name": "submit_decision"},
        )
    except anthropic.APIError:
        raise
 
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "submit_decision":
            return dict(block.input)
    log.error("decide(): no tool_use block in response; defaulting to no_trade")
    return {"action": "no_trade"}
 
 
# ---------- Audit trail ----------
 
def write_audit(record):
    """
    Persist one scan's full decision context to a dated JSON file:
    what the model was shown, what it said, and what was decided.
    Failure here must NEVER break a scan - log and move on.
    """
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(AUDIT_DIR, f"scan_{ts}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)
    except OSError as e:
        log.warning(f"Audit write failed (non-fatal): {e}")
 
 
# ---------- Entry placement ----------
 
def place_entry_order(ticker, side, price_dollars, amount_dollars):
    """Place a buy limit order. Returns (api_response_dict, count).
 
    In paper mode: no API call. Returns a synthetic order marked with a
    PAPER- id; manage_entries treats it as instantly filled at the
    limit price (the ask - optimistic but reasonable for a marketable
    limit order).
    """
    count = max(1, int(amount_dollars / float(price_dollars)))
    if PAPER_TRADING:
        fake = {
            "order": {
                "order_id": f"PAPER-{int(time.time() * 1000)}",
                "status": "executed",
            }
        }
        log.info(f"[PAPER] simulated entry order: {ticker} {side} "
                 f"{count} @ {float(price_dollars):.2f}")
        return fake, count
    side_price_key = f"{side}_price_dollars"
    payload = {
        "ticker": ticker,
        "side": side,
        "type": "limit",
        "count": count,
        "action": "buy",
        side_price_key: f"{float(price_dollars):.2f}",
    }
    return kalshi_request("POST", "/portfolio/orders", json_body=payload), count
 
 
# ---------- Main scan ----------
 
def scan():
    log.info("=== Scan start ===")
 
    # Lifecycle management first: confirm entries, manage exits,
    # reconcile settlements. Each is independent; log and continue.
    try:
        manage_entries()
    except Exception as e:
        log.error(f"manage_entries failed: {e}")
 
    try:
        manage_exits()
    except Exception as e:
        log.error(f"manage_exits failed: {e}")
 
    try:
        reconcile_settlements()
    except Exception as e:
        log.error(f"reconcile_settlements failed: {e}")
 
    # Hard-fail on balance/positions - sizing depends on them
    try:
        balance = get_balance()
    except Exception as e:
        log.error(f"Cannot fetch balance, aborting scan: {e}")
        return
 
    try:
        open_positions = get_open_positions()
    except Exception as e:
        log.error(f"Cannot fetch positions, aborting scan: {e}")
        return
 
    try:
        reconcile_position_state(open_positions)
    except Exception as e:
        log.error(f"reconcile_position_state failed: {e}")
 
    held_series = {p["series"] for p in open_positions}
    held_tickers = {p["ticker"] for p in open_positions}
 
    # Also block tickers with trades still active in the log,
    # including pending entries whose orders are still resting.
    for t in load_trades():
        if t.get("lifecycle") in ACTIVE_LIFECYCLES:
            held_tickers.add(t["ticker"])
            held_series.add(t.get("series", t["ticker"].split("-")[0]))
 
    open_pos_str = ", ".join(sorted(held_tickers)[:15]) if held_tickers else "none"
 
    max_trade = round(balance * MAX_POSITION_PCT, 2)
    log.info(f"Balance ${balance:.2f} | Max per trade ${max_trade:.2f} | "
             f"Open positions: {len(open_positions)} ({open_pos_str[:80]})")
 
    if max_trade < 1.0:
        log.warning("Max trade below $1; skipping entry consideration this scan.")
        return
 
    try:
        news = get_news()
    except Exception as e:
        log.warning(f"News fetch failed, continuing: {e}")
        news = "news unavailable"
 
    learning = summarize_history()
 
    try:
        markets = get_all_markets()
    except Exception as e:
        log.error(f"Market fetch failed, aborting scan: {e}")
        return
 
    # Pre-filter markets to exclude anything we already hold
    markets = [m for m in markets if m["ticker"] not in held_tickers]
 
    if not markets:
        log.info("No eligible markets after filtering.")
        return
 
    log.info(f"Analyzing {len(markets)} markets")
 
    try:
        analysis = analyze(markets, news, learning, balance, max_trade, open_pos_str)
        decision = decide(analysis, max_trade)
    except anthropic.APIError as e:
        log.error(f"Claude call failed: {e}")
        return
 
    # Audit: capture exactly what the model saw and said, every scan.
    write_audit({
        "time": datetime.datetime.now().isoformat(),
        "paper": PAPER_TRADING,
        "balance": balance,
        "max_trade": max_trade,
        "open_positions": open_pos_str,
        "news": news,
        "learning": learning,
        "markets_analyzed": [m.get("ticker") for m in markets],
        "analysis_raw": analysis,
        "decision": decision,
    })
 
    log.info(f"Decision: {json.dumps(decision)[:160]}")
 
    if decision.get("action") != "trade":
        log.info("No trade this scan.")
        return
 
    raw_ticker = str(decision.get("ticker") or "").strip()
    side = str(decision.get("side") or "").lower()
    raw_amount = decision.get("amount_dollars")
 
    if not raw_ticker or side not in ("yes", "no") or raw_amount is None:
        log.error(f"Incomplete trade decision: {decision}")
        return
 
    target_series = raw_ticker.split("-")[0]
    if target_series in held_series:
        log.warning(f"Skipping {raw_ticker} - already hold {target_series}")
        return
 
    # Look up the EXACT market the model recommended. No same-series
    # fallback: for strike-based markets, a sibling ticker is a
    # different trade than the one analyzed.
    market = find_market(raw_ticker)
    if not market:
        log.warning(f"Recommended ticker {raw_ticker} not found; skipping trade.")
        return
 
    real_ticker = market["ticker"]
    if real_ticker in held_tickers:
        log.warning(f"Skipping {real_ticker} - already held")
        return
 
    if side == "yes":
        price = float(market.get("yes_ask_dollars", 0) or 0)
    else:
        no_ask = market.get("no_ask_dollars")
        if no_ask is not None:
            price = float(no_ask)
        else:
            yes_bid = float(market.get("yes_bid_dollars", 0.5) or 0.5)
            price = round(1 - yes_bid, 4)
 
    if not (PRICE_FLOOR_ENTRY <= price <= PRICE_CEILING_ENTRY):
        log.warning(f"Price {price} out of range for {real_ticker}; skipping")
        return
 
    try:
        amount = min(float(raw_amount), max_trade)
    except (TypeError, ValueError):
        log.error(f"Bad amount in decision: {raw_amount}")
        return
 
    if amount < 1.0:
        log.info(f"Amount ${amount} too small; skipping")
        return
 
    log.info(f"Placing entry: {real_ticker} {side} ${amount} @ {price}")
    try:
        result, count = place_entry_order(real_ticker, side, price, amount)
    except requests.exceptions.RequestException as e:
        log.error(f"Entry order failed: {e}")
        return
 
    order = result.get("order", {})
    order_id = order.get("order_id", "")
    record_entry(real_ticker, side, price, amount, count, order_id)
    log.info(f"Entry order placed (pending fill confirmation): "
             f"{order_id} status={order.get('status')}")
    log.info("=== Scan end ===")
 
 
def safe_scan():
    try:
        with scan_lock():
            scan()
    except RuntimeError as e:
        log.warning(str(e))
    except Exception as e:
        log.exception(f"Unhandled scan error: {e}")
 
 
# ---------- Entry point ----------
 
if __name__ == "__main__":
    if PAPER_TRADING:
        log.info("=" * 60)
        log.info("PAPER TRADING MODE - no real orders, no real capital.")
        log.info(f"Simulated starting balance: ${PAPER_STARTING_BALANCE:.2f}")
        log.info(f"Paper log: {LOG_FILE}")
        log.info("=" * 60)
    else:
        log.info("LIVE TRADING MODE - real orders, real capital.")
    log.info("Bot starting up.")
    migrate_log_to_disk()  # One-shot migration of old-schema rows
    schedule.every(15).minutes.do(safe_scan)
    safe_scan()  # Run once immediately
    while True:
        schedule.run_pending()
        time.sleep(1)

