#!/usr/bin/env python3
"""
Telegram DEMO bot - Predict.fun paper trading (NO REAL MONEY)
================================================================
Demo first: virtual fund, NO real trades. Use it to see if the fund
gets eaten (balance drops on loss). Messages arrive on Telegram
even when the phone is off, as long as the server is running.

LIVE MODE (same bot, same Railway project):
  Paper is the default. Live real-money orders ONLY start with
  /livestart, and ONLY if Railway has LIVE_TRADING=1 plus
  PREDICT_API_KEY and PREDICT_WALLET_PRIVATE_KEY set.
  With LIVE_TRADING=0 (default) /livestart only does a CHECK:
  auth + market + book + signed order in memory, NO order sent.
  Live stakes are still capped $1/$2. There is NO safety system -
  no daily loss limit, no consecutive-loss stop, no unusual-move
  stop, no API-fail stop, no $0 stop (removed at the user's request,
  2026-11-16). The bot keeps trading until stopped by hand.

Fund can be EDITED from Telegram:
  /setfund 20      -> sets starting fund to $20 (balance reset)
  /setbalance 14.84 -> sets the balance to any amount (saved)
  /setstake 1 2    -> bet 1 = $1, bet 2 = $2
  /fund            -> shows current fund / balance

Other commands:
  /start   - bot is on, shows fund
  /status  - is there a trigger now, last candle
  /startbot - demo bot start
  /stopbot  - demo bot stop
  /pnl     - ajker win/loss, balance
  /help    - shob command

Strategy: 4 red -> UP $1, if lost $2, if lost STOP. 4 green -> DOWN $1,
if lost $2, if lost STOP. Max loss $3/round.

Price feed: Predict.fun's own chart series - Binance global top-of-book
mid-price, 60s TWAP - via WebSocket push (~0.1-0.3s) with 2s REST
polling as fallback, built into 5m candles by a background thread.
Falls back to Binance US klines if the feed goes stale. See the feed
section below BINANCE.

SETUP - first steps:
  1. Telegram kholo, @BotFather khujo
  2. /newbot -> give the bot a name -> give a username (must end with _bot)
  3. Copy the TOKEN BotFather gives you - do not give it to anyone,
     do not send it in chat. The token goes only on the server in .env / environment.
  4. Find the bot on Telegram, send /start - if the bot replies, it is running.
  5. It must run 24h on a server: Railway / Render / PythonAnywhere
     (free) or a cheap VPS. Put this file + token there and run:
         TELEGRAM_BOT_TOKEN=xxxx python3 telegram-demo-bot.py
     The server keeps running even when the phone is off.

Requires: Python 3.9+ only, no extra package needed.
Token: taken from the TELEGRAM_BOT_TOKEN environment variable.
         Never write the token in code / chat.
"""

import collections
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PREDICT_API_KEY = os.environ.get("PREDICT_API_KEY", "")
PREDICT_PRIV_KEY = os.environ.get("PREDICT_WALLET_PRIVATE_KEY", "")
PREDICT_BASE = os.environ.get("PREDICT_BASE_URL", "https://api.predict.fun").rstrip("/")
LIVE_TRADING = os.environ.get("LIVE_TRADING", "0") == "1"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.json")
DEFAULT_CONFIG = {
    "starting_fund": 20.0,   # <-- demo fund, change with /setfund
    "stake1": 1.0,
    "stake2": 2.0,
    # No safety system - removed at the user's request (2026-11-16).
    # No daily loss limit, no consecutive-loss stop, no unusual-move stop,
    # no API-fail stop, no $0 stop. The bot keeps trading until stopped.
}
BINANCE = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12"

# ---- Predict.fun chart feed: Binance global top-of-book mid-price + 60s TWAP ----
# What Predict.fun's chart actually is: its BTC 5m markets settle on
# Chainlink Data Streams - the BTC/USDT TopOfBook stream (mid-price =
# (best bid + best ask) / 2 from Binance's top of book), and current
# markets name the BTC/USD TWAP 60s stream (60-second average of that
# stream). This is NOT the classic Chainlink price feed (0x2649..., one
# update ~60s) the backtests used, and NOT Binance US spot klines, which
# is what the bot read until now (REST, every 10s).
#
# So the bot now samples the same raw input Predict.fun settles on:
# Binance GLOBAL top of book, mid-price, via WebSocket push (~0.1-0.3s)
# with 2s REST polling as fallback, keeps a rolling 60s TWAP, and builds
# its own 5m candles from the TWAP - the same series the chart shows.
# If the feed ever goes stale (>15s without a sample) fetch_closed falls
# back to the old Binance US klines so the bot keeps running.
FEED_WS_HOSTS = [
    ("data-stream.binance.vision", 9443, "/ws/btcusdt@bookTicker"),
    ("stream.binance.com", 9443, "/ws/btcusdt@bookTicker"),
    ("stream.binance.us", 9443, "/ws/btcusdt@bookTicker"),
]
FEED_REST_BOOK = [
    "https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
    "https://api.binance.us/api/v3/ticker/bookTicker?symbol=BTCUSDT",
]
FEED_SEED_KLINES = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12"
FEED_STALE_AFTER = 15.0  # seconds without a sample -> fall back to klines

_feed_lock = threading.RLock()
_feed_samples = collections.deque(maxlen=240)  # (ts, mid) - last ~8 min at 2s
_feed_twap_series = collections.deque(maxlen=240)  # (ts, twap) - sampled with each mid
_feed_candles = []  # kline-format 5m candles built from TWAP: [open_ms, o, h, l, c, 0, close_ms]
_feed_last_ts = 0.0
_feed_mode = "starting"  # ws | rest | seeded | starting
_feed_thread_started = False


def _feed_twap(now=None):
    """60s time-weighted average of the mid-price. Returns None if no samples yet."""
    now = now if now is not None else time.time()
    with _feed_lock:
        pts = [(ts, mid) for ts, mid in _feed_samples if ts >= now - 60]
        before = [(ts, mid) for ts, mid in _feed_samples if ts < now - 60]
    if not pts:
        return None
    # Value at the window start: last sample before the window, else the first in it.
    start_val = before[-1][1] if before else pts[0][1]
    total = 0.0
    prev_ts, prev_val = now - 60, start_val
    for ts, val in pts:
        total += prev_val * (ts - prev_ts)
        prev_ts, prev_val = ts, val
    total += prev_val * (now - prev_ts)
    return total / 60.0


def _feed_update_candle_locked(ts, twap):
    """Update the current 5m candle with a TWAP value; close it on a boundary.
    A whole 5m window with no samples becomes a flat (doji) candle at the
    last TWAP - a doji breaks runs, so a gap can never fake a trigger."""
    open_ms = int(ts // 300) * 300000
    if _feed_candles and _feed_candles[-1][0] == open_ms:
        c = _feed_candles[-1]
        c[4] = twap
        if twap > c[2]:
            c[2] = twap
        if twap < c[3]:
            c[3] = twap
        return
    last_close = _feed_candles[-1][4] if _feed_candles else twap
    nxt = (_feed_candles[-1][0] + 300000) if _feed_candles else open_ms
    while nxt < open_ms:
        _feed_candles.append([nxt, last_close, last_close, last_close, last_close, 0, nxt + 299999])
        nxt += 300000
    _feed_candles.append([open_ms, twap, twap, twap, twap, 0, open_ms + 299999])
    del _feed_candles[:-14]


def _feed_on_mid(mid, ts=None):
    """One mid-price sample in: store it, recompute the TWAP, update candles."""
    global _feed_last_ts
    ts = ts if ts is not None else time.time()
    # Sanity: a mid-price must be a positive, finite number, and samples
    # must arrive in time order. A bad tick (0, NaN, infinity, or a
    # timestamp older than the last sample, e.g. after a clock step) is
    # dropped instead of corrupting the TWAP and the candle list - an
    # out-of-order sample would otherwise insert a candle with an earlier
    # open time AFTER later candles and scramble the colour sequence.
    try:
        mid = float(mid)
    except Exception:
        return
    if not (math.isfinite(mid) and mid > 0):
        return
    with _feed_lock:
        if _feed_samples and ts < _feed_samples[-1][0]:
            return
        _feed_samples.append((ts, mid))
        _feed_last_ts = ts
        twap = _feed_twap(ts)
        if twap is not None:
            _feed_twap_series.append((ts, twap))
            _feed_update_candle_locked(ts, twap)


def _feed_seed():
    """Seed candle history from Binance GLOBAL 5m klines so the bot can see
    the last 4 candles (and trigger) right after a restart instead of
    waiting ~20 min for 4 live TWAP candles. Spot closes are only an
    approximation of TWAP values, but only the run-up uses them - every bet
    candle is judged on live TWAP candles."""
    global _feed_mode
    try:
        req = urllib.request.Request(FEED_SEED_KLINES, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.load(r)
        now_ms = int(time.time() * 1000)
        with _feed_lock:
            _feed_candles.clear()
            for k in raw:
                if int(k[6]) <= now_ms:
                    _feed_candles.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), 0, int(k[6])])
            del _feed_candles[:-14]
        _feed_mode = "seeded"
        print(f"[feed] seeded {len(_feed_candles)} candles from Binance global klines", flush=True)
        return True
    except Exception as e:
        print("[feed] seed failed:", e, flush=True)
        return False


def _feed_rest_poll_once():
    """One bookTicker read (global first, Binance US second). Returns the mid-price or None."""
    for url in FEED_REST_BOOK:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.load(r)
            return (float(d["bidPrice"]) + float(d["askPrice"])) / 2.0
        except Exception:
            continue
    return None


def _feed_ws_run(host, port, path):
    """One WebSocket connection: stream bookTicker mids until it drops.
    Minimal stdlib client (no extra package): handshake, text frames,
    ping->pong, close frames, fragmented messages. Raises on any failure -
    the caller reconnects or falls back to REST polling."""
    global _feed_mode
    import base64
    import os as _os
    import socket
    import ssl
    import struct
    ctx = ssl.create_default_context()
    sock = socket.create_connection((host, port), timeout=10)
    ss = ctx.wrap_socket(sock, server_hostname=host)
    ss.settimeout(10)
    try:
        key = base64.b64encode(_os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        ss.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = ss.recv(4096)
            if not chunk:
                raise RuntimeError("WS handshake: connection closed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError("WS handshake failed: " + resp.split(b"\r\n")[0].decode(errors="replace"))
        _feed_mode = "ws"
        print(f"[feed] WS live: {host}", flush=True)
        # The handshake read can already contain the start of the first
        # frame (the server sends it at once) - keep those bytes, or the
        # first bookTicker update is silently lost.
        buf = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else b""
        frag = b""
        while True:
            data = ss.recv(65536)
            if not data:
                raise RuntimeError("WS closed by server")
            buf += data
            while True:
                if len(buf) < 2:
                    break
                b1, b2 = buf[0], buf[1]
                fin = b1 & 0x80
                op = b1 & 0x0F
                ln = b2 & 0x7F
                idx = 2
                if ln == 126:
                    if len(buf) < 4:
                        break
                    ln = struct.unpack(">H", buf[2:4])[0]
                    idx = 4
                elif ln == 127:
                    if len(buf) < 10:
                        break
                    ln = struct.unpack(">Q", buf[2:10])[0]
                    idx = 10
                if b2 & 0x80:
                    idx += 4
                if len(buf) < idx + ln:
                    break
                payload = buf[idx:idx + ln]
                if b2 & 0x80:
                    mask = buf[idx - 4:idx]
                    payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                buf = buf[idx + ln:]
                if op == 9:  # ping -> pong with the same payload
                    mask = _os.urandom(4)
                    mp = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                    if len(payload) < 126:
                        hdr = bytes([0x8A, 0x80 | len(payload)])
                    elif len(payload) < 65536:
                        hdr = bytes([0x8A, 0xFE]) + struct.pack(">H", len(payload))
                    else:
                        hdr = bytes([0x8A, 0xFF]) + struct.pack(">Q", len(payload))
                    ss.sendall(hdr + mask + mp)
                elif op == 8:
                    raise RuntimeError("WS close frame")
                elif op == 1 or op == 0:  # text / continuation
                    frag += payload
                    if fin:
                        try:
                            d = json.loads(frag)
                            _feed_on_mid((float(d["b"]) + float(d["a"])) / 2.0)
                        except Exception:
                            pass
                        frag = b""
                # op 2 (binary) and others: ignored
    finally:
        try:
            ss.close()
        except Exception:
            pass


def _feed_loop():
    """Background feed thread: WS first, REST polling while WS is down.
    Never raises - a dead feed only makes fetch_closed fall back to klines."""
    global _feed_mode
    _feed_seed()
    while True:
        try:
            for host, port, path in FEED_WS_HOSTS:
                try:
                    _feed_ws_run(host, port, path)
                except Exception as e:
                    print(f"[feed] WS {host} failed: {e}", flush=True)
                    continue
                break  # _feed_ws_run only returns via exception; kept for safety
            _feed_mode = "rest"
            end = time.time() + 60
            while time.time() < end:
                mid = _feed_rest_poll_once()
                if mid is not None:
                    _feed_on_mid(mid)
                time.sleep(2)
        except Exception as e:
            print("[feed] loop error:", e, flush=True)
            time.sleep(5)


def start_feed():
    """Start the chart-feed thread once (called from main, not from bot_loop,
    so tests that run bot_loop directly are unaffected)."""
    global _feed_thread_started
    if _feed_thread_started:
        return
    _feed_thread_started = True
    threading.Thread(target=_feed_loop, daemon=True).start()


def feed_status_line():
    """One line for /status: which feed is live, the TWAP price, how fresh it is."""
    with _feed_lock:
        mode = _feed_mode
        last = _feed_last_ts
        twap = _feed_twap_series[-1][1] if _feed_twap_series else None
    if not last:
        return "Feed: starting..."
    age = time.time() - last
    if twap is None:
        return f"Feed: {mode} (no TWAP yet)"
    if age < FEED_STALE_AFTER:
        return f"Feed: Predict.fun chart (TWAP-{mode}) ${twap:,.2f} - {age:.0f}s ago"
    return f"Feed: STALE ({age:.0f}s) - using Binance US klines fallback"

# ---- state (kept in memory, saved in the config file) ----
config = dict(DEFAULT_CONFIG)
balance = DEFAULT_CONFIG["starting_fund"]
running = False
round_no = 0
wins = 0
losses = 0
day_start_balance = DEFAULT_CONFIG["starting_fund"]
owner_chat_id = None
live_running = False
bet_active = False  # True while a round has an open bet (set by bot_loop)
_live_jwt = None
_live_jwt_at = 0


def load_config():
    global config, balance, day_start_balance
    if os.path.exists(CONFIG_FILE):
        try:
            config.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    balance = config.get("starting_fund", 20.0)
    day_start_balance = balance


def save_config():
    json.dump(config, open(CONFIG_FILE, "w"), indent=2)


def tg(method, data=None):
    if not TG_API:
        print("[demo] No TG token, message (not sent):", data)
        return {}
    url = TG_API + "/" + method
    body = urllib.parse.urlencode(data or {}).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def send(chat_id, text):
    try:
        tg("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print("[tg send failed]", e)
    print("[tg]", text)



# ---- LIVE (Predict.fun, real money) - only used by /livestart ----
def _pf_rest(method, path, data=None, jwt=None):
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    if PREDICT_API_KEY:
        headers["x-api-key"] = PREDICT_API_KEY
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(PREDICT_BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = ""
        raise RuntimeError(f"{method} {path} -> HTTP {e.code} {detail}") from e

def _pf_jwt():
    global _live_jwt, _live_jwt_at
    if _live_jwt and time.time() - _live_jwt_at < 20 * 3600:
        return _live_jwt
    from eth_account import Account
    from eth_account.messages import encode_defunct
    acct = Account.from_key(PREDICT_PRIV_KEY)
    msg_res = _pf_rest("GET", "/v1/auth/message")
    message = msg_res.get("data", {}).get("message") or msg_res.get("message")
    signed = acct.sign_message(encode_defunct(text=message))
    auth = _pf_rest("POST", "/v1/auth", {
        "message": message, "signature": signed.signature.hex(), "signer": acct.address})
    token = auth.get("data", {}).get("token") or auth.get("token")
    if not token:
        raise RuntimeError("Predict.fun auth gave no token")
    _live_jwt, _live_jwt_at = token, time.time()
    return token

def _pf_place_order(direction, stake, market_start=None):
    """direction UP -> buy Yes, DOWN -> buy No. Returns order info or None in CHECK mode.
    Raises on failure - caller must handle it and NOT count a failed order as placed.
    market_start: epoch seconds of the market to bet on. MUST be the start of the
    candle the bot judges the bet on (bet_open // 1000). The old code used the
    NEXT 5-min boundary, which put the order on the FOLLOWING market while the
    bot judged WIN/LOSS on the current candle - paper and real results diverged
    (e.g. 2026-11-15 round 2: bot said WIN, real 2:40-2:45 PM market candle was
    green so the No bet actually lost)."""
    from predict_sdk import Book, BuildOrderInput, ChainId, MarketHelperValueInput, OrderBuilder, Side
    jwt = _pf_jwt()
    nxt = int(market_start) if market_start else (int(time.time()) // 300 + 1) * 300
    market = None
    # The category slug IS the market we want. Retry it briefly (a market
    # that just opened may take a second to appear), but never fall back to
    # "first market of the rounds list" - that could be a DIFFERENT market
    # (past or future) and the order would silently bet on the wrong candle.
    for _attempt in range(3):
        try:
            res = _pf_rest("GET", f"/v1/categories/btc-updown-5m-{nxt}", jwt=jwt)
        except Exception:
            res = None
        if res:
            data = res.get("data", res)
            markets = data.get("markets") or []
            if markets:
                market = markets[0]
                break
        time.sleep(2)
    if not market:
        raise RuntimeError(f"No Predict.fun market for round {nxt} (tried 3x)")
    outcome_name = "Yes" if direction == "UP" else "No"
    outcome = None
    for oc in market.get("outcomes", []):
        if oc.get("name", "").lower() == outcome_name.lower() or oc.get("indexSet") == (1 if outcome_name == "Yes" else 2):
            outcome = oc
            break
    if not outcome:
        raise RuntimeError(f"Outcome {outcome_name} not in market {market.get('id')}")
    res = _pf_rest("GET", f"/v1/markets/{market['id']}/orderbook", jwt=jwt)
    data = res.get("data", res)
    def levels(key):
        out = []
        for lv in data.get(key, []) or []:
            if isinstance(lv, (list, tuple)):
                out.append((str(lv[0]), str(lv[1])))
            else:
                out.append((str(lv.get("price")), str(lv.get("size"))))
        return out
    book = Book(market_id=int(market["id"]), update_timestamp_ms=int(time.time() * 1000),
                asks=levels("asks"), bids=levels("bids"))
    builder = OrderBuilder.make(ChainId.BNB_MAINNET, signer=PREDICT_PRIV_KEY)
    amounts = builder.get_market_order_amounts(
        MarketHelperValueInput(side=Side.BUY, value_wei=int(stake * 10**18),
                               slippage_bps=200, is_min_amount_out=True), book)
    order_input = BuildOrderInput(side=Side.BUY, token_id=str(outcome["onChainId"]),
        maker_amount=str(amounts.maker_amount), taker_amount=str(amounts.taker_amount),
        fee_rate_bps=str(market.get("feeRateBps", 0)))
    order = builder.build_order("MARKET", order_input)
    typed = builder.build_typed_data(order, is_neg_risk=bool(market.get("isNegRisk")),
                                     is_yield_bearing=bool(market.get("isYieldBearing")))
    signed = builder.sign_typed_data_order(typed)
    order_hash = builder.build_typed_data_hash(typed)
    import datetime as _dt
    _mkt_time = _dt.datetime.fromtimestamp(nxt, _dt.timezone.utc).strftime("%H:%M UTC")
    logline = (f"LIVE order {outcome_name} ${stake} market {market['id']} ({_mkt_time}) "
               f"price ~{amounts.price_per_share/1e18:.3f} hash {order_hash[:18]}...")
    print("[live]", logline, flush=True)
    if not LIVE_TRADING:
        return {"check_only": True, "info": logline, "order_hash": order_hash,
                "price": amounts.price_per_share / 1e18}
    payload = {"data": {"order": {
        "hash": order_hash, "salt": str(signed.salt), "maker": signed.maker,
        "signer": signed.signer, "taker": signed.taker, "tokenId": str(signed.token_id),
        "makerAmount": str(signed.maker_amount), "takerAmount": str(signed.taker_amount),
        "expiration": str(signed.expiration), "nonce": str(signed.nonce),
        "feeRateBps": str(signed.fee_rate_bps), "side": int(signed.side),
        "signatureType": int(signed.signature_type), "signature": signed.signature},
        "pricePerShare": str(amounts.price_per_share), "strategy": "MARKET",
        "slippageBps": str(amounts.slippage_bps), "isMinAmountOut": bool(amounts.is_min_amount_out)}}
    res = _pf_rest("POST", "/v1/orders", payload, jwt=jwt)
    return {"check_only": False, "info": logline, "response": res, "order_hash": order_hash,
            "price": amounts.price_per_share / 1e18}

def _pf_order_status(order_hash):
    """GET /v1/orders/{hash} -> (status, amount_filled_wei, reason).
    status: OPEN / FILLED / CANCELLED / EXPIRED / INVALIDATED, or None if the
    order is not known yet. Per the docs a hash can briefly 404 right after
    placing, so None means 'check again on the next poll', not 'failed'.
    amount_filled_wei is the filled size in wei (shares for a buy order);
    None when unknown. A market order can fill PARTIALLY - FILLED after any
    fill, with the rest cancelled - so the filled amount is what tells a
    $0.62 fill apart from a full $1 fill. reason is the cancel reason when
    the API reports one (field name varies, so several keys are tried)."""
    jwt = _pf_jwt()
    try:
        res = _pf_rest("GET", f"/v1/orders/{order_hash}", jwt=jwt)
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None, None, ""
        raise
    data = res.get("data", res) or {}
    status = (data.get("status") or "").upper() or None
    try:
        filled = int(data.get("amountFilled") or 0) or None
    except Exception:
        filled = None
    reason = ""
    for key in ("reason", "cancelReason", "statusReason", "cancelledReason"):
        if data.get(key):
            reason = str(data[key])
            break
    return status, filled, reason

def _pf_remove_order(order_hash):
    """Try to take an OPEN order off the book (POST /v1/orders/remove-by-hash).
    Returns True only if a follow-up status read shows CANCELLED. Up/down
    markets lock removal until removalLockedUntil, so a removal can be
    rejected - never assume it worked; the caller must re-check the status
    before placing a replacement, or the same step could fill twice."""
    jwt = _pf_jwt()
    try:
        _pf_rest("POST", "/v1/orders/remove-by-hash", {"data": {"hashes": [order_hash]}}, jwt=jwt)
    except Exception as e:
        print("[remove order failed]", e, flush=True)
        return False
    time.sleep(5)
    try:
        st, _filled, _reason = _pf_order_status(order_hash)
        return st == "CANCELLED"
    except Exception:
        return False

def fetch_closed(n=10):
    """Closed 5m candles, newest last. Prefers the Predict.fun-chart feed
    (TWAP candles built in the background thread); falls back to Binance
    US klines only if the feed is stale, so the bot keeps running on the
    old feed instead of going blind."""
    now_ms = int(time.time() * 1000)
    with _feed_lock:
        fresh = _feed_last_ts and (time.time() - _feed_last_ts) < FEED_STALE_AFTER
        if fresh and _feed_candles:
            closed = [list(k) for k in _feed_candles if int(k[6]) <= now_ms]
            if len(closed) >= 4:
                return closed[-n:]
    req = urllib.request.Request(BINANCE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.load(r)
    closed = [k for k in raw if int(k[6]) <= now_ms]
    return closed[-n:]


def colour(k):
    o, c = float(k[1]), float(k[4])
    return "G" if c > o else ("R" if c < o else "D")


def status_text():
    try:
        closed = fetch_closed(6)
        cols = [colour(k) for k in closed]
        price = float(closed[-1][4])
        if cols[-4:] == ["R"] * 4:
            trig = "🎯 TRIGGER! Next candle: $1 bet on UP"
        elif cols[-4:] == ["G"] * 4:
            trig = "🎯 TRIGGER! Next candle: $1 bet on DOWN"
        else:
            trig = "⏳ No trigger — please wait"
        return (f"BTC: ${price:,.2f}\nLast 4: {''.join(cols[-4:])}\n{trig}\n"
                f"Fund: ${balance:.2f} (start ${config['starting_fund']:.2f})\n"
                f"{feed_status_line()}")
    except Exception as e:
        return f"Status check failed: {e}"


def handle_command(chat_id, text):
    global balance, running, wins, losses, round_no, day_start_balance
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "/start":
        return (f"🤖 Predict.fun DEMO bot\n"
                f"Fund: ${balance:.2f} (demo)\n"
                f"4 red → UP, 4 green → DOWN, $1 then $2, then STOP\n"
                f"/help")
    if cmd == "/mode":
        if live_running:
            return "Mode: LIVE - real money orders (LIVE_TRADING=1)" if LIVE_TRADING else "Mode: LIVE (CHECK only - no orders sent, LIVE_TRADING=0)"
        if running:
            return "Mode: PAPER demo - virtual fund, no real trades"
        return "Mode: stopped. Paper: /startbot | Live: /livestart"
    if cmd == "/livestart":
        if not PREDICT_API_KEY or not PREDICT_PRIV_KEY:
            return "Live off: PREDICT_API_KEY + PREDICT_WALLET_PRIVATE_KEY are not set in Railway Variables."
        if live_running:
            return "Live is already running."
        try:
            _pf_jwt()
        except Exception as e:
            return f"Live failed to start - Predict.fun auth failed: {e}"
        globals()["live_running"] = True
        return ("LIVE started. " + ("REAL ORDERS - real money!" if LIVE_TRADING else "CHECK mode - no orders sent."))
    if cmd == "/livestop":
        globals()["live_running"] = False
        return f"Live stopped. PnL: ${balance - day_start_balance:+.2f}"
    if cmd == "/help":
        return ("/start, /status, /fund, /pnl, /mode\n"
                "/livestart - live start (real money, only if LIVE_TRADING=1)\n"
                "/livestop - live stop\n"
                "/setfund 20 — fund change\n"
                "/setbalance 14.84 — set the balance to any amount\n"
                "/setstake 1 2 — bet change\n"
                "/startbot — demo bot start\n/stopbot — demo bot stop")
    if cmd == "/status":
        return status_text()
    if cmd == "/fund":
        return (f"Starting fund: ${config['starting_fund']:.2f}\n"
                f"Balance now: ${balance:.2f}\n"
                f"PnL: ${balance - config['starting_fund']:+.2f}\n"
                f"Stake: ${config['stake1']:.0f} / ${config['stake2']:.0f}")
    if cmd == "/pnl":
        return (f"Today's PnL: ${balance - day_start_balance:+.2f}\n"
                f"Win: {wins} | Loss: {losses} | Round: {round_no}\n"
                f"Balance: ${balance:.2f}")
    if cmd == "/setfund":
        try:
            v = float(args[0])
            if v <= 0:
                return "Fund must be more than $0. Example: /setfund 20"
            if bet_active:
                return "A round is running right now - wait for it to finish, or /stopbot first, then /setfund."
            config["starting_fund"] = v
            balance = v
            day_start_balance = v
            wins = losses = round_no = 0
            save_config()
            return f"✅ Fund set: ${v:.2f} (balance reset, demo money)"
        except Exception:
            return "Usage: /setfund 20"
    if cmd == "/setbalance":
        try:
            v = float(args[0])
            if v <= 0:
                return "Balance must be more than $0. Example: /setbalance 14.84"
            balance = v
            day_start_balance = v
            config["starting_fund"] = v
            save_config()
            return (f"✅ Balance set: ${v:.2f} (saved - survives a redeploy). "
                    f"An open round still settles on top of this balance.")
        except Exception:
            return "Usage: /setbalance 14.84"
    if cmd == "/setstake":
        try:
            s1 = float(args[0])
            s2 = float(args[1])
            if not (0 < s1 <= 5 and 0 < s2 <= 10 and s2 >= s1):
                return "Stake must be $0-5 / $0-10, second >= first. Example: /setstake 1 2"
            config["stake1"] = s1
            config["stake2"] = s2
            save_config()
            return f"✅ Stake set: ${config['stake1']:.0f} / ${config['stake2']:.0f}"
        except Exception:
            return "Usage: /setstake 1 2"
    if cmd == "/startbot":
        if running:
            return "Bot is already running."
        running = True
        return "▶️ Demo bot started."
    if cmd == "/stopbot":
        running = False
        return f"⏹️ Bot stopped. Balance: ${balance:.2f}"
    return "I didn't understand. Type /help."


def bot_loop(chat_id):
    """Paper bot loop: checks candles every 10 sec, notifies on Telegram."""
    global balance, running, round_no, wins, losses, day_start_balance, bet_active
    api_errors = 0
    state = "idle"
    bet_open = None
    bet_stake = 0.0
    bet_dir = None
    win_col = None
    current_day = time.strftime("%Y-%m-%d", time.gmtime())  # UTC day, for /pnl + daily limit
    # Colour of the run that just finished ("R" for a red run, "G" for a green
    # run). After a STOP the bot stays in cooldown until a candle of the
    # OPPOSITE colour closes, i.e. the run really breaks. Without this the
    # last 4 candles are still the same run and the bot re-enters it at once.
    cooldown_run_col = None
    # Live orders waiting for a fill check: {"hash","round","step","dir",
    # "stake","market_start","placed","warned","retried","done"}. A 201 from
    # POST /v1/orders only means "accepted", NOT filled - so every live order
    # is checked with GET /v1/orders/{hash} until it is FILLED or final.
    pending = []
    # (round, step) -> True if that step's live order really filled, False
    # if it ended unfilled. The paper balance is adjusted from this, NOT
    # from candle colour alone: a step whose order never filled moves $0,
    # so a "LOSS -$3" can really be -$1 (or $0). Without this the paper
    # balance drifts away from the wallet and scares everyone.
    filled = {}

    def _step_filled(rnd, step):
        """Did this round-step's live order actually fill? True/False, or
        None if not known yet. Paper mode / CHECK mode (LIVE_TRADING=0):
        True - no real orders exist, paper counts as before."""
        if not LIVE_TRADING:
            return True
        key = (rnd, step)
        if key in filled:
            return filled[key]
        for p in pending:
            if p["round"] == rnd and p["step"] == step and not p["done"]:
                try:
                    st, _f, _r = _pf_order_status(p["hash"])
                except Exception:
                    return None
                if st is None:
                    return None
                if st == "FILLED":
                    filled[key] = True
                    return True
                if st in ("CANCELLED", "EXPIRED", "INVALIDATED"):
                    filled[key] = False
                    return False
                return None  # still OPEN
        return False  # no order was ever tracked for this step

    def _retry_pending(p):
        """One retry for a pending order that did not fill. Only while its
        round is still active and its market is still open."""
        if p["retried"] or state not in ("bet1", "bet2"):
            return False
        if time.time() >= p["market_start"] + 300:
            return False
        p["retried"] = True
        try:
            r2 = _pf_place_order(p["dir"], p["stake"], market_start=p["market_start"])
            send(chat_id, "🔄 Retrying order round %d: %s" % (p["round"], r2.get("info", "")))
            if not r2.get("check_only"):
                pending.append({"hash": r2["order_hash"], "round": p["round"], "step": p["step"],
                                "dir": p["dir"], "stake": p["stake"], "market_start": p["market_start"],
                                "price": p.get("price"), "placed": time.time(), "warned": False, "retried": True, "done": False})
            return True
        except Exception as e:
            send(chat_id, f"⚠️ Retry FAILED round {p['round']}: {e}")
            return False

    def _track_order(r, step):
        """Remember a just-sent live order so the loop checks its fill."""
        if live_running and r and not r.get("check_only") and r.get("order_hash"):
            pending.append({"hash": r["order_hash"], "round": round_no, "step": step,
                            "dir": bet_dir, "stake": bet_stake, "market_start": bet_open // 1000,
                            "price": r.get("price"), "placed": time.time(), "warned": False, "retried": False, "done": False})

    while True:
        if not running and not live_running:
            # Stopped: drop any half-open paper bet so a later restart does
            # not resolve a stale round against an old candle. (Cooldown is
            # left alone - it must survive a restart or the bot re-enters
            # the same run.) Pending fill checks are kept; they resume on
            # restart.
            if state in ("bet1", "bet2"):
                state, bet_open = "idle", None
                bet_active = False
            time.sleep(5)
            continue
        try:
            # New UTC day: /pnl measures "today", not "since the bot
            # last restarted".
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if today != current_day:
                current_day = today
                day_start_balance = balance
                wins = 0
                losses = 0
            closed = fetch_closed(10)
            api_errors = 0
            cols = [colour(k) for k in closed]
            last = closed[-1]
            # No safety stops - removed at the user's request (2026-11-16).
            # The bot no longer stops on an unusual move, consecutive
            # losses, a daily loss, a $0 balance, or API failures. It keeps
            # trading until the user stops it (/stopbot or /livestop).
            # Fill check for live orders sent earlier: 201 means accepted,
            # not filled. Check each pending order once it is 10s old (the
            # docs say a hash can briefly 404 right after placing, so
            # 10s is enough for it to be known; 30s was needlessly slow
            # and pushed any retry late into the 5-min market window).
            for p in list(pending):
                if p["done"]:
                    continue
                age = time.time() - p["placed"]
                if age < 10:
                    continue
                try:
                    st, filled_wei, reason = _pf_order_status(p["hash"])
                except Exception as e:
                    print("[fill check failed]", e, flush=True)
                    continue
                if st is None:
                    continue  # hash not known yet, check again next poll
                if st == "FILLED":
                    filled[(p["round"], p["step"])] = True
                    # Partial fill? amountFilled is shares; x price ~= USDT
                    # actually spent. Report it when it is clearly less
                    # than the full stake, so a $0.62 fill is visible.
                    note = ""
                    try:
                        if filled_wei and p.get("price"):
                            spent = (filled_wei / 1e18) * float(p["price"])
                            if spent < float(p["stake"]) - 0.005:
                                note = f" (~${spent:.2f} of ${float(p['stake']):.2f} filled - partial fill)"
                    except Exception:
                        note = ""
                    send(chat_id, f"✅ Order FILLED round {p['round']} ${p['stake']:.0f} on {p['dir']}.{note}")
                    p["done"] = True
                elif st in ("CANCELLED", "EXPIRED", "INVALIDATED"):
                    filled[(p["round"], p["step"])] = False
                    p["done"] = True
                    if not _retry_pending(p):
                        why = f" ({reason})" if reason else ""
                        send(chat_id, f"⚠️ Order {st} round {p['round']} ${p['stake']:.0f}{why} - NO real trade for this step.")
                elif st == "OPEN" and age > 60 and not p["warned"]:
                    p["warned"] = True
                    # Do NOT place a second order while the first is still
                    # on the book - it could fill twice. Try to remove the
                    # old one first (up/down markets may lock removal);
                    # retry only if the removal really cancelled it.
                    if _pf_remove_order(p["hash"]):
                        send(chat_id, f"⏳ Order still OPEN round {p['round']} ${p['stake']:.0f} after 60s - removed it, retrying once.")
                        _retry_pending(p)
                        filled[(p["round"], p["step"])] = False  # old order is cancelled; the retry is tracked as a new entry
                        p["done"] = True
                    else:
                        p["done"] = True
                        send(chat_id, f"⚠️ Order still OPEN round {p['round']} ${p['stake']:.0f} and could not be removed (market lock) - leaving it, it may still fill.")
                elif time.time() > p["market_start"] + 300:
                    filled[(p["round"], p["step"])] = False
                    p["done"] = True
                    send(chat_id, f"⚠️ Order never filled round {p['round']} - NO real trade for this step.")
            pending[:] = [p for p in pending if not p["done"]]
            if state in ("bet1", "bet2") and bet_open is not None:
                done = [k for k in closed if int(k[0]) == bet_open]
                if not done and time.time() * 1000 > bet_open + 15 * 60 * 1000:
                    # Bet candle never appeared (e.g. long API outage) - the
                    # bet is stale; drop it instead of staying stuck forever.
                    send(chat_id, f"⚠️ Round {round_no} bet went stale (no candle for 15 min) - dropping it, no new bet placed.")
                    state, bet_open = "idle", None
                    bet_active = False
                elif done:
                    res = colour(done[0])
                    if res == win_col:
                        wins += 1
                        if state == "bet1":
                            # Step-1 win: +stake1 only if its order really
                            # filled, else $0 (paper-only win).
                            f1 = _step_filled(round_no, 1)
                            pnl = config["stake1"] if f1 is True else 0.0
                            balance += pnl
                            note = "" if f1 is True else (" (fill unknown - check wallet)" if f1 is None else " (paper only - step 1 never filled)")
                            send(chat_id, f"✅ WIN round {round_no} ${bet_stake:.0f} on {bet_dir}. +${pnl:.2f}{note}. Balance ${balance:.2f}")
                        else:
                            # Step-2 win: round PnL = step-1 loss (if its
                            # order filled) + step-2 profit (if its order
                            # filled). Unfilled steps move $0.
                            f1 = _step_filled(round_no, 1)
                            f2 = _step_filled(round_no, 2)
                            pnl = (-config["stake1"] if f1 is True else 0.0) + (config["stake2"] if f2 is True else 0.0)
                            balance += pnl
                            parts = []
                            for s, f in ((1, f1), (2, f2)):
                                if f is False:
                                    parts.append(f"step {s} never filled")
                                elif f is None:
                                    parts.append(f"step {s} fill unknown")
                            note = " (" + ", ".join(parts) + ")" if parts else ""
                            send(chat_id, f"✅ WIN round {round_no} ${bet_stake:.0f} on {bet_dir}. Real {pnl:+.2f}{note}. Balance ${balance:.2f}")
                        state, bet_open = "idle", None
                        bet_active = False
                    elif state == "bet1":
                        state, bet_stake = "bet2", config["stake2"]
                        bet_open = int(last[6]) + 1
                        send(chat_id, f"❌ LOSS round {round_no} step 1. Next: ${bet_stake:.0f} on {bet_dir}.")
                        if live_running:
                            try:
                                r = _pf_place_order(bet_dir, bet_stake, market_start=bet_open // 1000)
                                send(chat_id, ("CHECK (no order sent): " if r.get("check_only") else "LIVE ORDER SENT: ") + r.get("info", ""))
                                _track_order(r, 2)
                            except Exception as e:
                                send(chat_id, f"Live step-2 order FAILED round {round_no}: {e}")
                    else:
                        # STOP: deduct only the stakes that really filled.
                        # A step whose order never filled lost $0, so this
                        # round is -$1 or -$0, not always -$3.
                        f1 = _step_filled(round_no, 1)
                        f2 = _step_filled(round_no, 2)
                        loss = (config["stake1"] if f1 is True else 0.0) + (config["stake2"] if f2 is True else 0.0)
                        balance -= loss
                        losses += 1
                        parts = []
                        for s, f in ((1, f1), (2, f2)):
                            if f is False:
                                parts.append(f"step {s} never filled")
                            elif f is None:
                                parts.append(f"step {s} fill unknown")
                        note = " (" + ", ".join(parts) + ")" if parts else ""
                        send(chat_id, f"❌ LOSS round {round_no} step 2. STOP. -${loss:.2f}{note}. Balance ${balance:.2f}")
                        state, bet_open = "cooldown", None
                        bet_active = False
                        # The run that triggered this round: red run if we bet UP
                        # (win_col "G"), green run if we bet DOWN (win_col "R").
                        cooldown_run_col = "R" if win_col == "G" else "G"
                # 2s, not 10s: candles now come from the in-memory TWAP feed,
                # so an iteration costs no Binance call - triggers are seen
                # ~2s after a candle closes instead of ~10s.
                time.sleep(2)
                continue
            # Cooldown ends ONLY when the run breaks: a closed candle of the
            # opposite colour to the finished run. The old check
            # (last candle is G or R) was always true, so the bot re-entered
            # the same run at once and lost $3 again within minutes.
            if state == "cooldown" and cooldown_run_col is not None and cols[-1] != cooldown_run_col:
                state = "idle"
                cooldown_run_col = None
            if state == "idle":
                if cols[-4:] == ["R"] * 4 or cols[-4:] == ["G"] * 4:
                    is_up = cols[-4:] == ["R"] * 4
                    round_no += 1
                    state = "bet1"
                    bet_active = True
                    bet_stake = config["stake1"]
                    bet_dir, win_col = ("UP", "G") if is_up else ("DOWN", "R")
                    bet_open = int(last[6]) + 1
                    tag = "LIVE" if live_running else "Demo"
                    send(chat_id, f"🎯 TRIGGER round {round_no}: 4 {'red' if is_up else 'green'}. {tag} bet ${bet_stake:.0f} on {bet_dir}.")
                    if live_running:
                        try:
                            r = _pf_place_order(bet_dir, bet_stake, market_start=bet_open // 1000)
                            send(chat_id, ("CHECK (no order sent): " if r.get("check_only") else "LIVE ORDER SENT: ") + r.get("info", ""))
                            _track_order(r, 1)
                        except Exception as e:
                            send(chat_id, f"Live order FAILED round {round_no}: {e}")
        except Exception as e:
            api_errors += 1
            print("[loop error]", e, "api_errors:", api_errors, flush=True)
            # No API-fail safety stop - removed at the user's request
            # (2026-11-16). The loop just keeps retrying on failure.
        # 2s loop: the TWAP feed thread does the polling, so this loop only
        # reads in-memory candles - cheap, and triggers fire ~2s after close.
        time.sleep(2)


def main():
    global owner_chat_id
    load_config()
    start_feed()  # Predict.fun-chart feed (TWAP candles) in the background
    if not TG_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN.\n"
              "TELEGRAM_BOT_TOKEN=xxxx python3 telegram-demo-bot.py")
        return
    print(f"Demo bot running. Starting fund ${config['starting_fund']:.2f} (demo only, no real money)")
    offset = 0
    loop_thread = None
    while True:
        try:
            data = tg("getUpdates", {"offset": offset, "timeout": 30})
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text", "")
                if not chat_id or not text:
                    continue
                if owner_chat_id is None:
                    owner_chat_id = chat_id
                    loop_thread = threading.Thread(target=bot_loop, args=(chat_id,), daemon=True)
                    loop_thread.start()
                if chat_id != owner_chat_id:
                    send(chat_id, "This bot is for the owner only.")
                    continue
                reply = handle_command(chat_id, text)
                if reply:
                    send(chat_id, reply)
        except Exception as e:
            print("[poll error]", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
