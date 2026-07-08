"""
====================================================================
14-DAY EXIT CRITERIA  (deployed: 2026-05-19, ends: 2026-06-02)
====================================================================

Strategy parameters are LOCKED for 14 days from deploy date.
Bug fixes OK. Strategy changes (TP/SL/sizing/universe/prompt) NOT OK.

SUCCESS CONDITIONS (all four must be met):
  [ ] 14 calendar days complete, bot ran continuously
  [ ] 15+ resolved trades in the trade log
  [ ] Net trading P&L > $45 (covers ~$90/mo API + server prorated)
  [ ] No single position exceeded 5% of balance

DECISION TREE:
  All 4 met               -> fund to $1-2K, run another 14 days
  Some met, near-breakeven -> one more 14-day cycle, NO fund-up
  Two inconclusive cycles  -> KILL
  P&L clearly negative     -> KILL
  Fewer than 15 trades     -> KILL (insufficient data, won't get
                              better by waiting)

The "well maybe with more capital" rationalization is the exact
failure mode this checklist exists to prevent. If P&L net of costs
is negative on day 14, the strategy has not earned more capital.

====================================================================

Kalshi trading bot - full rewrite.

Key changes from prior version:
- Exit orders actually exit (place opposing sell orders, track fills, mark resolved
  only when exit fills are confirmed).
- Take-profit is absolute (+0.20 from entry, capped at 0.97), reachable for any entry.
- Stop-loss is proportional (0.5x entry).
- Re-prices unfilled exit orders each scan until they fill.
- Secrets loaded from environment variables; hard fail if missing.
- get_balance and get_open_positions hard-fail (no fake fallbacks).
- File-based concurrency lock prevents overlapping scans.
- Trade log appends forever; rotates by date instead of truncating.
- Specific exception handling with logged errors instead of bare except.
- Expanded market data sent to Claude: volume, close time, real no_ask.
- Dedup uses series_ticker consistently.
- R/R math in prompt is hold-to-resolution (conservative; stops are pure downside
  protection, not an R/R improvement).
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

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
LOG_FILE = os.path.join(LOG_DIR, "trade_log.json")
ERROR_LOG = os.path.join(LOG_DIR, "bot_errors.log")

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
    """Returns balance in dollars. Raises on failure (no fake fallback)."""
    data = kalshi_request("GET", "/portfolio/balance")
    return float(data.get("balance", 0)) / 100


def get_open_positions():
    """
    Returns list of (ticker, series_ticker, position_count, side) tuples.
    Raises on failure - sizing and dedup depend on this.
    """
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
    """Returns order dict or None if not found."""
    try:
        data = kalshi_request("GET", f"/portfolio/orders/{order_id}")
        return data.get("order")
    except requests.exceptions.RequestException:
        return None


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
    """Look up a specific market by ticker."""
    series = ticker.split("-")[0]
    for m in fetch_series_markets(series):
        if m.get("ticker") == ticker:
            return m
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
            f"https://api.coingecko.com/api/v3/simple/price",
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

    world = ""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": "Search top breaking news today affecting commodities, "
                           "crypto, and macro markets. 3 short bullets."
            }],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        )
        for block in resp.content:
            if hasattr(block, "text") and block.text and len(block.text) > 20:
                world = block.text[:300]
                break
    except anthropic.APIError as e:
        log.warning(f"News summary failed: {e}")
        world = "news unavailable"

    return f"CRYPTO: {crypto_news} | NEWS: {world}"


# ---------- Trade log ----------

def migrate_trade(t):
    """
    Map old-schema trade dicts to new schema in-place.
    Old schema had `resolved` (bool), `final_status`, `price` (entry).
    New schema has `lifecycle`, `exit_reason`, `entry_price`, `exit_order_id`,
    `exit_order_price`, `fill_price`.
    Old trades are marked resolved and excluded from active exit management;
    they ride to settlement on their own.
    """
    if "lifecycle" in t:
        return t  # already new schema

    # Old schema -> new schema
    if "price" in t and "entry_price" not in t:
        t["entry_price"] = t.pop("price")
    t.setdefault("series", t.get("ticker", "").split("-")[0])
    t.setdefault("entry_order_id", t.get("order_id"))
    t.setdefault("entry_status", t.get("status", "unknown"))
    t.setdefault("exit_order_id", None)
    t.setdefault("exit_order_price", None)
    t.setdefault("fill_price", None)

    # Map status to lifecycle. Old trades default to "resolved" so the new
    # exit logic ignores them - they'll close via settlement reconciliation.
    if t.get("resolved"):
        t["lifecycle"] = "resolved"
        t.setdefault("exit_reason", t.get("final_status", "settlement"))
    else:
        # Position from old code: mark resolved so new exit logic skips it.
        # Settlement reconciliation will pick up the real P&L when it expires.
        t["lifecycle"] = "resolved"
        t["exit_reason"] = "legacy_unmigrated"
        log.warning(f"Legacy trade {t.get('ticker')} marked resolved; "
                    f"will close via settlement only.")

    # Clean up old keys we no longer use
    t.pop("resolved", None)
    t.pop("final_status", None)
    t.pop("order_id", None)
    t.pop("status", None)
    return t


def load_trades():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE) as f:
            trades = json.load(f)
        migrated = [migrate_trade(t) for t in trades]
        return migrated
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Trade log read failed: {e}")
        return []


def save_trades(trades):
    """Append-safe write via temp file rename."""
    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, LOG_FILE)


def migrate_log_to_disk():
    """
    Run once at startup. Load the trade log, migrate any old-schema rows,
    and persist the result. After this runs, the on-disk log is normalized
    to the new schema so check_day14.sh sees consistent fields.

    Safe to run repeatedly: migrate_trade is idempotent on already-new rows.
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

    needs_migration = sum(1 for t in trades if "lifecycle" not in t)
    if needs_migration == 0:
        log.info(f"Trade log already in new schema ({len(trades)} rows).")
        return

    log.info(f"Migrating {needs_migration} legacy rows out of {len(trades)} total...")
    migrated = [migrate_trade(t) for t in trades]
    save_trades(migrated)
    log.info(f"Migration persisted to disk.")


def record_entry(ticker, side, price, amount, order_id, status):
    trades = load_trades()
    trades.append({
        "time": datetime.datetime.now().isoformat(),
        "ticker": ticker,
        "series": ticker.split("-")[0],
        "side": side,
        "entry_price": float(price),
        "amount": float(amount),
        "entry_order_id": order_id,
        "entry_status": status,
        "exit_order_id": None,
        "exit_order_price": None,
        "exit_reason": None,           # take_profit | stop_loss | None
        "lifecycle": "open",           # open | exiting | resolved
        "fill_price": None,            # actual fill from settlement or exit
        "pnl": None,
    })
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
    Returns API response dict. Caller is responsible for handling errors and
    checking that the response indicates a real close (not an opening short).
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


def cancel_order(order_id):
    try:
        return kalshi_request("DELETE", f"/portfolio/orders/{order_id}")
    except requests.exceptions.RequestException:
        return None


def manage_exits():
    """
    Two-phase exit management:
    1. For 'open' trades: check if TP or SL triggered; if so, place exit order.
    2. For 'exiting' trades: check if exit order filled; if so, mark resolved.
       If still unfilled, cancel and re-price at current bid (Option 2).
    """
    trades = load_trades()
    changed = False

    for t in trades:
        if t.get("lifecycle") == "resolved":
            continue

        ticker = t["ticker"]
        side = t["side"]
        entry = float(t["entry_price"])
        market = find_market(ticker)

        if not market:
            # Market may have resolved already; settlements job will catch it
            continue

        # Phase 2: exit order already placed - check status
        if t.get("lifecycle") == "exiting" and t.get("exit_order_id"):
            order = get_order_status(t["exit_order_id"])
            if order and order.get("status") == "filled":
                # Sell-side fills are returned under the side-relative price key.
                price_key = f"{side}_price_dollars"
                raw_fill = order.get(price_key)
                if raw_fill is None or raw_fill == "":
                    # Some Kalshi endpoints use other field names for executed price.
                    # Try generic fallbacks then fall through to exit_order_price.
                    raw_fill = (order.get("fill_price")
                                or order.get("avg_fill_price")
                                or order.get("executed_price"))
                if raw_fill is None or raw_fill == "":
                    fill_price = float(t.get("exit_order_price") or 0)
                    log.warning(f"Exit fill price missing for {ticker} "
                                f"(order keys: {list(order.keys()) if order else 'none'}); "
                                f"using exit_order_price {fill_price}")
                else:
                    try:
                        fill_price = float(raw_fill)
                    except (TypeError, ValueError):
                        fill_price = float(t.get("exit_order_price") or 0)
                        log.warning(f"Could not parse fill price {raw_fill!r} "
                                    f"for {ticker}; using {fill_price}")

                count = int(t["amount"] / entry) if entry > 0 else 0
                t["fill_price"] = fill_price
                t["pnl"] = round((fill_price - entry) * count, 2)
                t["lifecycle"] = "resolved"
                log.info(f"EXIT FILLED: {ticker} {side} entry={entry} "
                         f"fill={fill_price} pnl={t['pnl']}")
                changed = True
                continue

            # Re-price: cancel and re-issue at current bid (Option 2)
            # Rate-limited and wider threshold to prevent fee thrash.
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

            price_drift = abs(current_bid - float(t.get("exit_order_price", 0)))
            if price_drift > REPRICE_THRESHOLD:
                log.info(f"Re-pricing exit for {ticker}: "
                         f"{t['exit_order_price']} -> {current_bid} (drift {price_drift:.3f})")
                cancel_order(t["exit_order_id"])
                count = int(t["amount"] / entry) if entry > 0 else 0
                if count <= 0:
                    log.error(f"Bad count for {ticker}; skipping re-price")
                    continue
                try:
                    result = place_exit_order(ticker, side, current_bid, count)
                    order = result.get("order", {})
                    t["exit_order_id"] = order.get("order_id")
                    t["exit_order_price"] = current_bid
                    t["last_reprice_time"] = now.isoformat()
                    changed = True
                except requests.exceptions.RequestException as e:
                    log.error(f"Re-price failed for {ticker}: {e}")
            continue

        # Phase 1: still open - check TP/SL triggers
        current_bid = current_sell_price(market, side)
        tp = compute_take_profit(entry)
        sl = compute_stop_loss(entry)

        if current_bid >= tp:
            reason = "take_profit"
        elif current_bid <= sl and current_bid > 0:
            reason = "stop_loss"
        else:
            continue

        count = int(t["amount"] / entry) if entry > 0 else 0
        if count <= 0:
            log.error(f"Bad count for {ticker}; cannot exit")
            continue

        log.info(f"{reason.upper()}: {ticker} {side} entry={entry} bid={current_bid} -> placing exit")
        try:
            result = place_exit_order(ticker, side, current_bid, count)
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
    Settlement reconciliation handles two cases:

    1. `open` trades: position rode to expiration without TP/SL trigger.
       Record settlement P&L normally.

    2. `exiting` trades whose market settled before the exit order filled.
       Kalshi auto-cancels unfilled orders at settlement, so the exit order
       is dead and the position has settled at $0 or $1. Without this fallback,
       these trades would stay in `exiting` lifecycle forever.

    `manage_exits` is responsible for `exiting` trades whose exit DID fill -
    we never touch those here because lifecycle becomes `resolved` on fill.
    """
    trades = load_trades()
    settlements = get_settlements()
    changed = False
    for t in trades:
        lifecycle = t.get("lifecycle")
        if lifecycle == "resolved":
            continue
        if lifecycle not in ("open", "exiting"):
            continue

        s = settlements.get(t["ticker"])
        if not s:
            continue

        result = s.get("market_result", "")
        side = t["side"]
        won = (side == "yes" and result == "yes") or (side == "no" and result == "no")
        revenue = float(s.get("revenue", 0)) / 100
        cost_key = "yes_total_cost_dollars" if side == "yes" else "no_total_cost_dollars"
        cost = float(s.get(cost_key, t["amount"]) or t["amount"])

        t["lifecycle"] = "resolved"
        if lifecycle == "exiting":
            # Exit order didn't fill before settlement; clean up the stuck state.
            t["exit_reason"] = "settled_during_exit_win" if won else "settled_during_exit_loss"
            log.warning(f"STUCK EXIT SETTLED: {t['ticker']} {side} - exit order "
                        f"{t.get('exit_order_id')} did not fill before settlement; "
                        f"recording as settled. result={result} pnl=${round(revenue-cost,2)}")
        else:
            t["exit_reason"] = "settlement_win" if won else "settlement_loss"
            log.info(f"SETTLED: {t['ticker']} {side} result={result} "
                     f"pnl=${round(revenue-cost,2)}")
        t["pnl"] = round(revenue - cost, 2)
        changed = True

    if changed:
        save_trades(trades)


# ---------- Learning summary ----------

def summarize_history():
    trades = load_trades()
    resolved = [t for t in trades if t.get("lifecycle") == "resolved"]
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
        f"{t.get('series','?')} {t['side']}@{t['entry_price']} {t.get('exit_reason','?')} pnl=${t.get('pnl', 0)}"
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
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        system=(
            "Trading bot. Output EXACTLY ONE LINE in one of these formats:\n"
            "TRADE: TICKER yes/no PRICE AMOUNT\n"
            "NO_TRADE\n"
            "PRICE = contract price between 0.01 and 0.99 (NOT the underlying asset price).\n"
            "AMOUNT = dollars, plain number no dollar sign."
        ),
        messages=[{
            "role": "user",
            "content": f"Max: ${max_trade:.2f}. Analysis: {analysis}. Decision?",
        }],
    )
    return resp.content[0].text.strip()


# ---------- Entry placement ----------

def place_entry_order(ticker, side, price_dollars, amount_dollars):
    """Place a buy order. Returns API response dict."""
    count = max(1, int(amount_dollars / float(price_dollars)))
    side_price_key = f"{side}_price_dollars"
    payload = {
        "ticker": ticker,
        "side": side,
        "type": "limit",
        "count": count,
        "action": "buy",
        side_price_key: f"{float(price_dollars):.2f}",
    }
    return kalshi_request("POST", "/portfolio/orders", json_body=payload)


# ---------- Main scan ----------

def scan():
    log.info("=== Scan start ===")

    # Always run exit management and settlement reconciliation first
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

    held_series = {p["series"] for p in open_positions}
    held_tickers = {p["ticker"] for p in open_positions}

    # Also block tickers with trades still in open/exiting state in the log.
    # Prevents re-entering a position whose exit filled but the bot hasn't
    # processed the fill confirmation yet.
    for t in load_trades():
        if t.get("lifecycle") in ("open", "exiting"):
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

    log.info(f"Decision: {decision[:120]}")

    if not decision.startswith("TRADE:"):
        log.info("No trade this scan.")
        return

    parts = decision.replace("TRADE:", "").strip().split()
    if len(parts) != 4:
        log.error(f"Malformed decision: {decision}")
        return

    raw_ticker, side, _suggested_price, raw_amount = parts
    side = side.lower()
    if side not in ("yes", "no"):
        log.error(f"Invalid side: {side}")
        return

    # Look up real current market to get authoritative price
    target_ticker = raw_ticker
    target_series = target_ticker.split("-")[0]

    if target_series in held_series:
        log.warning(f"Skipping {target_ticker} - already hold {target_series}")
        return

    market = find_market(target_ticker)
    if not market:
        # Fall back to first eligible market in the suggested series
        market = next((m for m in markets if m["ticker"].startswith(target_series)), None)
    if not market:
        log.error(f"Cannot find market for {target_ticker}")
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
    except ValueError:
        log.error(f"Bad amount in decision: {raw_amount}")
        return

    if amount < 1.0:
        log.info(f"Amount ${amount} too small; skipping")
        return

    log.info(f"Placing entry: {real_ticker} {side} ${amount} @ {price}")
    try:
        result = place_entry_order(real_ticker, side, price, amount)
    except requests.exceptions.RequestException as e:
        log.error(f"Entry order failed: {e}")
        return

    order = result.get("order", {})
    record_entry(
        real_ticker, side, price, amount,
        order.get("order_id", ""), order.get("status", "unknown"),
    )
    log.info(f"Entry recorded: {order.get('order_id')} status={order.get('status')}")
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
    log.info("Bot starting up.")
    migrate_log_to_disk()  # One-shot migration of old-schema rows
    schedule.every(15).minutes.do(safe_scan)
    safe_scan()  # Run once immediately
    while True:
        schedule.run_pending()
        time.sleep(1)
