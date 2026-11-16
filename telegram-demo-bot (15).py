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

Strategy (per pair - see PAIRS below):
  BTC: 4 red -> UP $1, if lost $2, if lost STOP. 4 green -> DOWN $1,
  if lost $2, if lost STOP. Max loss $3/round. 5m candles.
  ETH: 2 red -> UP $1, if lost $2, if lost STOP. 2 green -> DOWN $1,
  if lost $2, if lost STOP. Max loss $3/round. 15m candles.
  ETH5: same as ETH but 5 red/green -> opposite. 15m candles.
  ETH and ETH5 run as two independent strategies on the same ETH
  15m candles (backtest 1y: N=2 +$1,317, N=5 +$243, together
  +$1,560, DD only $55->$65). Each pair can be turned off/on from
  Telegram (/btc off, /eth off, /eth5 off) and runs its own rounds;
  the fund is shared between all of them.

PAUSE HOUR (Dhaka 20:00-21:00, added 2026-11-16): the 1-year
backtest shows this is the ONLY losing hour for all 3 strategies
combined (-$84, 68.9% win) - the US stock market opens at 9:00 AM
Eastern then and the momentum hurts this reversal strategy. No NEW
trades are placed 20:00-21:00 Dhaka; an open round still settles,
the cooldown still runs, and trading resumes at 21:00. Toggle:
 /pause on | /pause off. See PAUSE_HOUR_DHAKA and _in_pause_window.

MINIMUM PAYOUT (added 2026-11-16): a live order is skipped when
the payout if it wins is below min_payout x the stake (default
1.5x: $1 needs $1.50 back, $2 needs $3.00). The price comes from
the orderbook at placement time, so a thin/expensive book means a
low payout and the order is skipped instead of placed. A skipped
step 1 abandons the round (no money risked); a skipped step 2 ends
the round with just the step-1 loss. Either way the pair enters
cooldown so it does not re-trigger on the same run. Change it with
 /setminpayout 1.5 (0 = no minimum). See LowPayoutSkip.

ETH 15m FILL FIX (2026-11-16): the ETH 15m orderbook is thin right
after a market opens, so a market order placed at the trigger often
finds no seller and gets CANCELLED (round 1 and round 2 both failed
this way). ETH orders are therefore delayed 30s after the trigger
(step 1) or after step 1 loses (step 2) so makers can populate the
book, and a cancelled order is retried up to 3 times (was 1).
See PAIRS["ETH"]["order_delay_sec"] and ORDER_MAX_RETRIES.
BTC fills fine at once, so its delay is 0.

WS FEED ROUTING FIX (2026-11-16): BTC's /status price showed
$2,197-$3,440 (real ~$64,000) and ETH's feed stayed "starting..."
for hours. That is what happens when WS messages arrive without the
"data" wrapper (or with an unexpected stream name): the old code
defaulted every message to BTC, ETH starved, and BTC's TWAP mixed
in ETH prices. Messages are now routed by the SYMBOL inside the
bookTicker ("s"), with the stream name only as a fallback. on_mid
also drops a tick that jumps >2x from the last accepted value (or
from the last seeded candle's close for the first sample), so one
bad tick can no longer corrupt the TWAP and the candles.
See _feed_on_ws_message and TWAPFeed.on_mid.

Price feed: Predict.fun's own chart series - Binance global top-of-book
mid-price, 60s TWAP - via WebSocket push (~0.1-0.3s) with 2s REST
polling as fallback, built into candles by a background thread.
BTC: 5m candles. ETH: 15m candles. Falls back to Binance US klines
if the feed goes stale. See the feed section below.

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
import datetime
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
    "starting_fund": 10.0,   # <-- demo fund, change with /setfund
    "stake1": 1.0,
    "stake2": 2.0,
    # Per-pair on/off - /btc off, /eth on, /eth5 off. Saved, so it survives a redeploy.
    "pairs": {"BTC": False, "BTC15": True, "ETH": True, "ETH5": True},
    # Pause hour (Dhaka 20:00-21:00) - /pause on|off. Saved, survives a redeploy.
    "pause_20": True,
    # Minimum payout ratio - /setminpayout 1.5. A live order is skipped
    # (no order placed, round abandoned) when the payout if it wins is
    # below this multiple of the stake: 1.5 means $1.50 back on $1,
    # $3.00 back on $2. 0 = no minimum, always place the order.
    "min_payout": 1.5,
    # No safety system - removed at the user's request (2026-11-16).
    # No daily loss limit, no consecutive-loss stop, no unusual-move stop,
    # no API-fail stop, no $0 stop. The bot keeps trading until stopped.
}
# ---- Pairs: one entry per market. tf_sec = candle length, n = run length
# that triggers a bet, category = Predict.fun category slug (the market
# the bot orders from). BTC15 is BTC on the SAVED 15m N=2 strategy
# (added 2026-11-16, v4.6: 1y backtest BTC 15m N=2 +$854 vs BTC 5m N=4
# +$415; 7-day: 15m +$34 at 77.6% win vs 5m N=2 +$17 at 70.5%).
# The old BTC 5m N=4 is KEPT but OFF by default (the user's choice -
# /btc on turns it back on; first run with BTC15 migrates it off).
# ETH is the 15m N=2 strategy; ETH5 is a SECOND ETH strategy on the
# same 15m candles with N=5 (added 2026-11-16: 1y backtest N=2 +$1,317,
# N=5 +$243, together +$1,560 with DD only $55->$65). BTC and BTC15
# share the BTCUSDT symbol (both feeds get every BTC message, like
# ETH/ETH5); each pair runs its own state machine and its own feed,
# and can be turned off/on from Telegram. The fund is SHARED
# between all pairs; each pair runs its own rounds independently.
PAIRS = {
    "BTC": {"symbol": "BTCUSDT", "tf_sec": 300, "n": 4, "category": "btc-updown-5m",
            "order_delay_sec": 0,
            "ws_stream": "btcusdt@bookTicker",
            "rest_urls": ["https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
                          "https://api.binance.us/api/v3/ticker/bookTicker?symbol=BTCUSDT"],
            "seed_url": "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12",
            "fallback_url": "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12"},
    # BTC15: BTC on the SAVED 15m N=2 strategy (added 2026-11-16, v4.6).
    # 1-year backtest: BTC 15m N=2 +$854 vs BTC 5m N=4 +$415; 7-day:
    # 15m +$34 (77.6% win) vs 5m N=2 +$17 (70.5%). Same BTCUSDT feed as
    # BTC (both feeds get every BTC message), its own 15m candles.
    "BTC15": {"symbol": "BTCUSDT", "tf_sec": 900, "n": 2, "category": "btc-updown-15m",
            "order_delay_sec": 0,
            "ws_stream": "btcusdt@bookTicker",
            "rest_urls": ["https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT",
                          "https://api.binance.us/api/v3/ticker/bookTicker?symbol=BTCUSDT"],
            "seed_url": "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=12",
            "fallback_url": "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=12"},
    "ETH": {"symbol": "ETHUSDT", "tf_sec": 900, "n": 2, "category": "eth-updown-15m",
            "order_delay_sec": 30,
            "ws_stream": "ethusdt@bookTicker",
            "rest_urls": ["https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=ETHUSDT",
                          "https://api.binance.us/api/v3/ticker/bookTicker?symbol=ETHUSDT"],
            "seed_url": "https://data-api.binance.vision/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=12",
            "fallback_url": "https://api.binance.us/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=12"},
    "ETH5": {"symbol": "ETHUSDT", "tf_sec": 900, "n": 5, "category": "eth-updown-15m",
             "order_delay_sec": 30,
             "ws_stream": "ethusdt@bookTicker",
             "rest_urls": ["https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=ETHUSDT",
                           "https://api.binance.us/api/v3/ticker/bookTicker?symbol=ETHUSDT"],
             "seed_url": "https://data-api.binance.vision/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=12",
             "fallback_url": "https://api.binance.us/api/v3/klines?symbol=ETHUSDT&interval=15m&limit=12"},
}
# ETH 15m fill fix (2026-11-16): the ETH 15m orderbook is thin right after
# a market opens, so a market order placed at the trigger often finds no
# seller and gets CANCELLED (round 1 and round 2 both failed this way).
# order_delay_sec: wait this long after the trigger (step 1) or after step 1
# loses (step 2) before placing the LIVE order, so makers have time to
# populate the book. 30s is ~3% of a 15m candle - the entry price moves a
# little, but the bet is judged on the candle's close vs open, so the
# win/loss logic is unchanged. BTC fills fine at once, so its delay is 0.
# ORDER_MAX_RETRIES: how many times a cancelled order is retried (was 1).
ORDER_MAX_RETRIES = 3

# Pause hour (Dhaka 20:00-21:00): the 1-year backtest (2026-11-16,
# backtest-weekday-hour.py) shows this is the ONLY losing hour for
# all 3 strategies combined (-$84, 68.9% win, 684 rounds) - the US
# stock market opens at 9:00 AM Eastern (14:00 UTC = 20:00 Dhaka)
# and the momentum hurts this reversal strategy. Skipping new
# triggers in this hour would have added +$84/yr. Only NEW triggers
# are skipped; an open round still settles, the cooldown still runs,
# and triggers resume at 21:00. Toggle from Telegram: /pause on|off.
PAUSE_HOUR_DHAKA = 20
DHAKA_TZ = datetime.timezone(datetime.timedelta(hours=6))


def _in_pause_window(now=None):
    """True if the pause hour is ON and `now` (default: now, UTC) falls
    in the pause hour in Dhaka. `now` is a datetime, for testing."""
    if not config.get("pause_20", True):
        return False
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(DHAKA_TZ).hour == PAUSE_HOUR_DHAKA

# ---- Predict.fun chart feed: Binance global top-of-book mid-price + 60s TWAP ----
# What Predict.fun's chart actually is: its markets settle on Chainlink
# Data Streams - the TopOfBook stream (mid-price = (best bid + best ask)/2
# from Binance's top of book), and current markets name the TWAP 60s stream
# (60-second average of that stream). This is NOT the classic Chainlink
# price feed (0x2649..., one update ~60s) the backtests used, and NOT
# Binance US spot klines, which is what the bot read until now (REST).
#
# So the bot now samples the same raw input Predict.fun settles on:
# Binance GLOBAL top of book, mid-price, via WebSocket push (~0.1-0.3s)
# with 2s REST polling as fallback, keeps a rolling 60s TWAP, and builds
# its own candles from the TWAP - the same series the chart shows.
# BTC: 5m candles. ETH: 15m candles. One combined WebSocket stream
# carries both symbols; if it drops, REST polling covers both.
# If a feed ever goes stale (>15s without a sample) fetch_closed falls
# back to that pair's Binance US klines so the bot keeps running.
FEED_WS_HOSTS = [
    ("data-stream.binance.vision", 9443),
    ("stream.binance.com", 9443),
    ("stream.binance.us", 9443),
]
FEED_WS_PATH = "/ws/btcusdt@bookTicker/ethusdt@bookTicker"
FEED_STALE_AFTER = 15.0  # seconds without a sample -> fall back to klines


class TWAPFeed:
    """One asset's Predict.fun-chart feed: Binance global top-of-book
    mid-price -> rolling 60s TWAP -> own candles at tf_sec."""

    def __init__(self, name, cfg):
        self.name = name
        self.symbol = cfg["symbol"]
        self.tf_sec = cfg["tf_sec"]
        self.ws_stream = cfg["ws_stream"]
        self.rest_urls = cfg["rest_urls"]
        self.seed_url = cfg["seed_url"]
        self.fallback_url = cfg["fallback_url"]
        self.lock = threading.RLock()
        self.samples = collections.deque(maxlen=240)  # (ts, mid) - last ~8 min at 2s
        self.twap_series = collections.deque(maxlen=240)  # (ts, twap)
        self.candles = []  # kline-format candles: [open_ms, o, h, l, c, 0, close_ms]
        self.last_ts = 0.0
        self.mode = "starting"  # ws | rest | seeded | starting

    def twap(self, now=None):
        """60s time-weighted average of the mid-price. None if no samples yet."""
        now = now if now is not None else time.time()
        with self.lock:
            pts = [(ts, mid) for ts, mid in self.samples if ts >= now - 60]
            before = [(ts, mid) for ts, mid in self.samples if ts < now - 60]
        if not pts:
            return None
        start_val = before[-1][1] if before else pts[0][1]
        total = 0.0
        prev_ts, prev_val = now - 60, start_val
        for ts, val in pts:
            total += prev_val * (ts - prev_ts)
            prev_ts, prev_val = ts, val
        total += prev_val * (now - prev_ts)
        return total / 60.0

    def update_candle_locked(self, ts, twap):
        """Update the current candle with a TWAP value; close it on a boundary.
        A whole window with no samples becomes a flat (doji) candle at the
        last TWAP - a doji breaks runs, so a gap can never fake a trigger."""
        tf_ms = self.tf_sec * 1000
        open_ms = int(ts // self.tf_sec) * tf_ms
        if self.candles and self.candles[-1][0] == open_ms:
            c = self.candles[-1]
            c[4] = twap
            if twap > c[2]:
                c[2] = twap
            if twap < c[3]:
                c[3] = twap
            return
        last_close = self.candles[-1][4] if self.candles else twap
        nxt = (self.candles[-1][0] + tf_ms) if self.candles else open_ms
        while nxt < open_ms:
            self.candles.append([nxt, last_close, last_close, last_close, last_close, 0, nxt + tf_ms - 1])
            nxt += tf_ms
        self.candles.append([open_ms, twap, twap, twap, twap, 0, open_ms + tf_ms - 1])
        del self.candles[:-14]

    def on_mid(self, mid, ts=None):
        """One mid-price sample in: store it, recompute the TWAP, update candles."""
        ts = ts if ts is not None else time.time()
        # Sanity: a mid-price must be a positive, finite number, and samples
        # must arrive in time order. A bad tick (0, NaN, infinity, or a
        # timestamp older than the last sample) is dropped instead of
        # corrupting the TWAP and the candle list.
        try:
            mid = float(mid)
        except Exception:
            return
        if not (math.isfinite(mid) and mid > 0):
            return
        with self.lock:
            if self.samples and ts < self.samples[-1][0]:
                return
            # Plausibility guard (2026-11-16): a single BTC/ETH tick cannot
            # jump 2x in 2 seconds - a sample that far from the last
            # accepted value (or, for the very first sample, from the last
            # seeded candle's close) is a data error, not a price move.
            # Without this, one bad tick drags the 60s TWAP to nonsense
            # (BTC showed $2,197-$3,440 while the real price was ~$64,000)
            # and bakes wrong closes into the candles.
            ref = None
            if self.samples:
                ref = self.samples[-1][1]
            elif self.candles:
                ref = self.candles[-1][4]
            if ref and ref > 0 and (mid < ref * 0.5 or mid > ref * 2.0):
                print(f"[feed {self.name}] dropping implausible tick ${mid:,.2f} (ref ${ref:,.2f})", flush=True)
                return
            self.samples.append((ts, mid))
            self.last_ts = ts
            twap = self.twap(ts)
            if twap is not None:
                self.twap_series.append((ts, twap))
                self.update_candle_locked(ts, twap)

    def seed(self):
        """Seed candle history from Binance GLOBAL klines so the bot can see
        the last candles (and trigger) right after a restart instead of
        waiting for live TWAP candles. Spot closes are only an approximation
        of TWAP values, but only the run-up uses them - every bet candle is
        judged on live TWAP candles."""
        try:
            req = urllib.request.Request(self.seed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = json.load(r)
            now_ms = int(time.time() * 1000)
            with self.lock:
                self.candles.clear()
                for k in raw:
                    if int(k[6]) <= now_ms:
                        self.candles.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), 0, int(k[6])])
                del self.candles[:-14]
            self.mode = "seeded"
            print(f"[feed {self.name}] seeded {len(self.candles)} candles from Binance global klines", flush=True)
            return True
        except Exception as e:
            print(f"[feed {self.name}] seed failed:", e, flush=True)
            return False

    def rest_poll_once(self):
        """One bookTicker read (global first, Binance US second). Returns the mid-price or None."""
        for url in self.rest_urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.load(r)
                return (float(d["bidPrice"]) + float(d["askPrice"])) / 2.0
            except Exception:
                continue
        return None

    def closed(self, n=10):
        """Closed candles, newest last. Prefers the Predict.fun-chart feed
        (TWAP candles built in the background thread); falls back to
        Binance US klines only if the feed is stale."""
        now_ms = int(time.time() * 1000)
        with self.lock:
            fresh = self.last_ts and (time.time() - self.last_ts) < FEED_STALE_AFTER
            if fresh and self.candles:
                closed = [list(k) for k in self.candles if int(k[6]) <= now_ms]
                if len(closed) >= 4:
                    return closed[-n:]
        req = urllib.request.Request(self.fallback_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.load(r)
        closed = [k for k in raw if int(k[6]) <= now_ms]
        return closed[-n:]

    def status_line(self):
        """One line for /status: which feed is live, the TWAP price, how fresh it is."""
        with self.lock:
            mode = self.mode
            last = self.last_ts
            twap = self.twap_series[-1][1] if self.twap_series else None
        if not last:
            return f"{self.name} feed: starting..."
        age = time.time() - last
        if twap is None:
            return f"{self.name} feed: {mode} (no TWAP yet)"
        if age < FEED_STALE_AFTER:
            return f"{self.name} feed: Predict.fun chart (TWAP-{mode}) ${twap:,.2f} - {age:.0f}s ago"
        return f"{self.name} feed: STALE ({age:.0f}s) - using Binance US klines fallback"


FEEDS = {name: TWAPFeed(name, cfg) for name, cfg in PAIRS.items()}
_feed_thread_started = False


def _feed_on_ws_message(d):
    """Route one parsed WebSocket message to the right pair's feed.
    Combined stream: {"stream": "...@bookTicker", "data": {...}}.
    Single stream: the bookTicker object itself.
    Routing is by the SYMBOL inside the bookTicker ("s": "BTCUSDT" /
    "ETHUSDT"), with the "stream" name only as a fallback - the symbol
    is in the data itself, so routing survives messages that arrive
    without the "data" wrapper or with an unexpected stream name."""
    if not isinstance(d, dict):
        return
    if "data" in d:
        stream = d.get("stream", "")
        d = d["data"]
    else:
        stream = "btcusdt@bookTicker"
    if not isinstance(d, dict):
        return
    # Route to EVERY feed whose symbol matches (ETH and ETH5 share the
    # ETHUSDT symbol, BTC and BTC15 share BTCUSDT, and each builds its
    # own candles, so all matching feeds must get the message - routing
    # to only the first would starve the second).
    feeds = []
    sym = str(d.get("s", "")).upper()
    if sym:
        feeds = [f for f in FEEDS.values() if f.symbol == sym]
    if not feeds:
        feeds = [f for f in FEEDS.values() if f.ws_stream in stream]
    for feed in feeds:
        try:
            feed.on_mid((float(d["b"]) + float(d["a"])) / 2.0)
        except Exception:
            pass


def _feed_ws_run(host, port, path):
    """One WebSocket connection on the COMBINED stream (BTC + ETH
    bookTicker). Minimal stdlib client (no extra package): handshake,
    text frames, ping->pong, close frames, fragmented messages. Each
    message carries a "stream" field naming its symbol; it is routed to
    that pair's feed. Raises on any failure - the caller reconnects or
    falls back to REST polling."""
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
        for f in FEEDS.values():
            f.mode = "ws"
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
                            _feed_on_ws_message(json.loads(frag))
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
    for f in FEEDS.values():
        f.seed()
    while True:
        try:
            for host, port in FEED_WS_HOSTS:
                try:
                    _feed_ws_run(host, port, FEED_WS_PATH)
                except Exception as e:
                    print(f"[feed] WS {host} failed: {e}", flush=True)
                    continue
                break  # _feed_ws_run only returns via exception; kept for safety
            for f in FEEDS.values():
                f.mode = "rest"
            end = time.time() + 60
            while time.time() < end:
                for f in FEEDS.values():
                    mid = f.rest_poll_once()
                    if mid is not None:
                        f.on_mid(mid)
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
    """One line per pair for /status."""
    return "\n".join(f.status_line() for f in FEEDS.values())

# ---- state (kept in memory, saved in the config file) ----
config = dict(DEFAULT_CONFIG)
balance = DEFAULT_CONFIG["starting_fund"]
running = False
day_start_balance = DEFAULT_CONFIG["starting_fund"]
owner_chat_id = None
live_running = False
bet_active = False  # True while ANY pair has an open bet (set by bot_loop)
_live_jwt = None
_live_jwt_at = 0
# Per-pair round state: each pair runs its own rounds independently.
# state: idle | bet1 | bet2 | cooldown. cooldown_run_col: the colour of
# the run that just finished - cooldown ends only when a candle of the
# opposite colour closes (see bot_loop).
pair_state = {
    name: {"state": "idle", "bet_open": None, "bet_stake": 0.0, "bet_dir": None,
           "win_col": None, "cooldown_run_col": None,
           "order_placed": True, "order_due_at": 0.0,
           "round_no": 0, "wins": 0, "losses": 0}
    for name in PAIRS
}


def pair_enabled(name):
    """Is this pair turned on? Defaults to on if the config has no entry."""
    return bool(config.get("pairs", {}).get(name, True))


def total_rounds():
    return sum(ps["round_no"] for ps in pair_state.values())


def total_wins():
    return sum(ps["wins"] for ps in pair_state.values())


def total_losses():
    return sum(ps["losses"] for ps in pair_state.values())


def load_config():
    global config, balance, day_start_balance
    if os.path.exists(CONFIG_FILE):
        try:
            config.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    # A config saved before per-pair toggles existed has no "pairs" key;
    # fill in any missing pair from the DEFAULT_CONFIG values.
    pairs = config.get("pairs") or {}
    # Migration (v4.6): BTC15 (BTC 15m N=2) was added. The old BTC 5m N=4
    # is KEPT but turned OFF by default (the user's choice); BTC15 is ON.
    # Runs once: if BTC15 is missing from the saved pairs, this is the
    # first run with BTC15 support, so turn BTC off and BTC15 on, then save.
    # The user can turn BTC 5m back on anytime with /btc on.
    # NOTE: check BEFORE the setdefault loop below, or it would add BTC15
    # first and the migration would never fire.
    first_btc15 = "BTC15" not in pairs
    for name in PAIRS:
        pairs.setdefault(name, DEFAULT_CONFIG["pairs"].get(name, True))
    if first_btc15:
        pairs["BTC"] = False
        pairs["BTC15"] = True
        config["pairs"] = pairs
        save_config()
    config["pairs"] = pairs
    balance = config.get("starting_fund", 10.0)
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

class LowPayoutSkip(Exception):
    """Raised by _pf_place_order when the payout if the order wins is
    below the configured minimum (config["min_payout"]). Not an error -
    the order is deliberately skipped, so the caller must abandon the
    round (not count it as a failed order)."""
    def __init__(self, payout, stake, ratio, min_ratio, price):
        super().__init__(f"payout ${payout:.2f} on ${stake:.0f} ({ratio:.2f}x < {min_ratio:.2f}x)")
        self.payout = payout
        self.stake = stake
        self.ratio = ratio
        self.min_ratio = min_ratio
        self.price = price


def _pf_place_order(direction, stake, market_start=None, pair="BTC"):
    """direction UP -> buy Yes, DOWN -> buy No. Returns order info or None in CHECK mode.
    Raises on failure - caller must handle it and NOT count a failed order as placed.
    pair: "BTC" (5m market, category btc-updown-5m), "BTC15" (15m market,
    category btc-updown-15m), "ETH" (15m market, category eth-updown-15m)
    or "ETH5" (15m market, category eth-updown-15m). The category slug IS
    the market we want.
    market_start: epoch seconds of the market to bet on. MUST be the start of the
    candle the bot judges the bet on (bet_open // 1000). The old code used the
    NEXT 5-min boundary, which put the order on the FOLLOWING market while the
    bot judged WIN/LOSS on the current candle - paper and real results diverged
    (e.g. 2026-11-15 round 2: bot said WIN, real 2:40-2:45 PM market candle was
    green so the No bet actually lost)."""
    from predict_sdk import Book, BuildOrderInput, ChainId, MarketHelperValueInput, OrderBuilder, Side
    jwt = _pf_jwt()
    tf_sec = PAIRS[pair]["tf_sec"]
    nxt = int(market_start) if market_start else (int(time.time()) // tf_sec + 1) * tf_sec
    market = None
    # The category slug IS the market we want. Retry it briefly (a market
    # that just opened may take a second to appear), but never fall back to
    # "first market of the rounds list" - that could be a DIFFERENT market
    # (past or future) and the order would silently bet on the wrong candle.
    cat = PAIRS[pair]["category"]
    for _attempt in range(3):
        try:
            res = _pf_rest("GET", f"/v1/categories/{cat}-{nxt}", jwt=jwt)
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
        raise RuntimeError(f"No Predict.fun {pair} market for round {nxt} (tried 3x)")
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
    # Minimum payout: if this order wins it pays stake / price_per_share.
    # Skip the order (raise LowPayoutSkip - the caller abandons the round)
    # when that payout is below min_payout x the stake. E.g. min 1.5x:
    # $1 at $0.67/share pays $1.49 -> skip; $1 at $0.50 pays $2.00 -> place.
    # 0 disables the check. Applies in CHECK mode too (the price is real).
    price = amounts.price_per_share / 1e18
    min_ratio = config.get("min_payout", 1.5)
    if min_ratio and min_ratio > 0 and price > 0:
        payout = stake / price
        ratio = payout / stake
        if ratio < min_ratio:
            raise LowPayoutSkip(payout, stake, ratio, min_ratio, price)
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
    logline = (f"LIVE {pair} order {outcome_name} ${stake} market {market['id']} ({_mkt_time}) "
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

def fetch_closed(pair, n=10):
    """Closed candles for a pair, newest last. Uses that pair's
    Predict.fun-chart feed (TWAP candles); the feed itself falls back to
    Binance US klines if it goes stale."""
    return FEEDS[pair].closed(n)


def colour(k):
    o, c = float(k[1]), float(k[4])
    return "G" if c > o else ("R" if c < o else "D")


def status_text():
    try:
        lines = []
        for name in PAIRS:
            ps = pair_state[name]
            n = PAIRS[name]["n"]
            tf = PAIRS[name]["tf_sec"] // 60
            on = "✅ ON" if pair_enabled(name) else "❌ OFF"
            try:
                closed = fetch_closed(name, 6)
                cols = [colour(k) for k in closed]
                price = float(closed[-1][4])
                if cols[-n:] == ["R"] * n:
                    trig = f"🎯 TRIGGER! Next candle: $1 trade on UP"
                elif cols[-n:] == ["G"] * n:
                    trig = f"🎯 TRIGGER! Next candle: $1 trade on DOWN"
                else:
                    trig = "⏳ No trigger — please wait"
                run = "".join(cols[-n:])
                lines.append(f"{name} ({tf}m, N={n}) [{on}]: ${price:,.2f}\nLast {n}: {run}\n{trig}")
            except Exception as e:
                lines.append(f"{name} [{on}]: status failed: {e}")
        pause_on = config.get("pause_20", True)
        pause_now = _in_pause_window()
        pause_line = (f"Pause 20:00-21:00 Dhaka: {'✅ ON' if pause_on else '❌ OFF'}"
                      + (" (paused now - no new trades until 21:00)" if pause_now else ""))
        return ("\n\n".join(lines) +
                f"\n\nFund: ${balance:.2f} (start ${config['starting_fund']:.2f})\n" +
                pause_line + "\n" +
                feed_status_line())
    except Exception as e:
        return f"Status check failed: {e}"


def handle_command(chat_id, text):
    global balance, running, day_start_balance
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "/start":
        return (f"🤖 Predict.fun DEMO bot\n"
                f"Fund: ${balance:.2f} (demo)\n"
                f"BTC 5m: 4 red → UP, 4 green → DOWN (OFF by default)\n"
                f"BTC15 15m: 2 red → UP, 2 green → DOWN\n"
                f"ETH 15m: 2 red → UP, 2 green → DOWN\n"
                f"ETH5 15m: 5 red → UP, 5 green → DOWN\n"
                f"$1 then $2, then STOP. /help")
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
                "/setstake 1 2 — trade change\n"
                "/setminpayout 1.5 — skip a trade if it pays back less than 1.5x (0 = off)\n"
                "/btc on / /btc off — turn BTC 5m on or off (OFF by default)\n"
                "/btc15 on / /btc15 off — turn BTC 15m on or off\n"
                "/eth on / /eth off — turn ETH on or off\n"
                "/eth5 on / /eth5 off — turn ETH N=5 on or off\n"
                "/pause on / /pause off — pause new trades 20:00-21:00 Dhaka\n"
                "/startbot — demo bot start\n/stopbot — demo bot stop")
    if cmd == "/status":
        return status_text()
    if cmd == "/fund":
        pairs_line = " | ".join(
            f"{name}: {'✅ ON' if pair_enabled(name) else '❌ OFF'}" for name in PAIRS)
        mp = config.get("min_payout", 1.5)
        mp_line = f"Min payout: {mp:.2f}x" if mp else "Min payout: OFF"
        return (f"Starting fund: ${config['starting_fund']:.2f}\n"
                f"Balance now: ${balance:.2f}\n"
                f"PnL: ${balance - config['starting_fund']:+.2f}\n"
                f"Stake: ${config['stake1']:.0f} / ${config['stake2']:.0f}\n"
                f"{mp_line}\n"
                f"Pairs: {pairs_line}")
    if cmd == "/pnl":
        per = "\n".join(
            f"{name}: Win {pair_state[name]['wins']} | Loss {pair_state[name]['losses']} | Round {pair_state[name]['round_no']}"
            for name in PAIRS)
        return (f"Today's PnL: ${balance - day_start_balance:+.2f}\n"
                f"{per}\n"
                f"Total Win: {total_wins()} | Loss: {total_losses()} | Round: {total_rounds()}\n"
                f"Balance: ${balance:.2f}")
    if cmd in ("/btc", "/btc15", "/eth", "/eth5"):
        name = cmd[1:].upper()
        if not args or args[0].lower() not in ("on", "off"):
            return f"Usage: {cmd} on  or  {cmd} off  (now: {'✅ ON' if pair_enabled(name) else '❌ OFF'})"
        on = args[0].lower() == "on"
        config.setdefault("pairs", {})[name] = on
        save_config()
        if not on:
            # Turning a pair off drops its half-open bet so a later restart
            # does not resolve a stale round; its cooldown is left alone.
            # Also clears a deferred order (ETH 30s delay) so it is never
            # placed after the pair was turned off.
            ps = pair_state[name]
            if ps["state"] in ("bet1", "bet2"):
                ps["state"], ps["bet_open"] = "idle", None
                ps["order_placed"], ps["order_due_at"] = True, 0.0
        return f"{name} is now {'✅ ON' if on else '❌ OFF'}."
    if cmd == "/pause":
        if not args or args[0].lower() not in ("on", "off"):
            return (f"Usage: /pause on  or  /pause off  (now: {'✅ ON' if config.get('pause_20', True) else '❌ OFF'})\n"
                    "ON = no new trades 20:00-21:00 Dhaka (the losing hour).")
        on = args[0].lower() == "on"
        config["pause_20"] = on
        save_config()
        return (f"Pause 20:00-21:00 Dhaka is now {'✅ ON' if on else '❌ OFF'}."
                + (" No new trades in that hour." if on else ""))
    if cmd == "/setfund":
        try:
            v = float(args[0])
            if v <= 0:
                return "Fund must be more than $0. Example: /setfund 20"
            if any(ps["state"] in ("bet1", "bet2") for ps in pair_state.values()):
                return "A round is running right now - wait for it to finish, or /stopbot first, then /setfund."
            config["starting_fund"] = v
            balance = v
            day_start_balance = v
            for ps in pair_state.values():
                ps["wins"] = ps["losses"] = ps["round_no"] = 0
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
    if cmd == "/setminpayout":
        try:
            v = float(args[0])
            if not (0 <= v <= 5):
                return "Min payout must be 0-5. Example: /setminpayout 1.5 (0 = no minimum)"
            config["min_payout"] = v
            save_config()
            if v == 0:
                return "✅ Min payout OFF - every order is placed."
            return (f"✅ Min payout set: {v:.2f}x "
                    f"(a ${config['stake1']:.0f} trade needs ${config['stake1'] * v:.2f} back, "
                    f"${config['stake2']:.0f} needs ${config['stake2'] * v:.2f}).")
        except Exception:
            return "Usage: /setminpayout 1.5  (0 = no minimum)"
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
    """Paper bot loop: checks candles every 2 sec, notifies on Telegram.
    Four pairs run independently in this one loop (single thread, so the
    shared balance is never updated by two pairs at once):
      BTC 5m, N=4 reversal (OFF by default). BTC15 15m, N=2 reversal.
      ETH 15m, N=2 reversal. ETH5 15m, N=5 reversal.
    Each pair has its own state (idle/bet1/bet2/cooldown), its own round
    counter and win/loss stats, and can be turned off/on from Telegram
    (/btc off, /btc15 off, /eth off, /eth5 off). The fund is shared between the four."""
    global balance, running, day_start_balance, bet_active
    api_errors = 0
    current_day = time.strftime("%Y-%m-%d", time.gmtime())  # UTC day, for /pnl
    # Live orders waiting for a fill check: {"hash","pair","round","step",
    # "dir","stake","market_start","placed","warned","retries","done"}.
    # "retries" counts how many retries have been used for that step
    # (capped at ORDER_MAX_RETRIES by _retry_pending).
    # A 201 from POST /v1/orders only means "accepted", NOT filled - so
    # every live order is checked with GET /v1/orders/{hash} until it is
    # FILLED or final.
    pending = []
    # (pair, round, step) -> True if that step's live order really filled,
    # False if it ended unfilled. The paper balance is adjusted from this,
    # NOT from candle colour alone: a step whose order never filled moves
    # $0, so a "LOSS -$3" can really be -$1 (or $0). Without this the paper
    # balance drifts away from the wallet and scares everyone.
    filled = {}

    def _step_filled(pair, rnd, step):
        """Did this pair-round-step's live order actually fill? True/False,
        or None if not known yet. Paper mode / CHECK mode (LIVE_TRADING=0):
        True - no real orders exist, paper counts as before."""
        if not LIVE_TRADING:
            return True
        key = (pair, rnd, step)
        if key in filled:
            return filled[key]
        for p in pending:
            if p["pair"] == pair and p["round"] == rnd and p["step"] == step and not p["done"]:
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
        pair's round is still active and its market is still open. Up to
        ORDER_MAX_RETRIES per step (was 1) - the ETH 15m book can stay
        thin for a while after a market opens, so one retry is not enough."""
        pair = p["pair"]
        ps = pair_state[pair]
        tf_sec = PAIRS[pair]["tf_sec"]
        if p.get("retries", 0) >= ORDER_MAX_RETRIES or ps["state"] not in ("bet1", "bet2"):
            return False
        if time.time() >= p["market_start"] + tf_sec:
            return False
        p["retries"] = p.get("retries", 0) + 1
        try:
            r2 = _pf_place_order(p["dir"], p["stake"], market_start=p["market_start"], pair=pair)
            send(chat_id, "🔄 Retrying order %s round %d (try %d/%d): %s" % (pair, p["round"], p["retries"], ORDER_MAX_RETRIES, r2.get("info", "")))
            if not r2.get("check_only"):
                pending.append({"hash": r2["order_hash"], "pair": pair, "round": p["round"], "step": p["step"],
                                "dir": p["dir"], "stake": p["stake"], "market_start": p["market_start"],
                                "price": p.get("price"), "placed": time.time(), "warned": False, "retries": p["retries"], "done": False})
            return True
        except Exception as e:
            send(chat_id, f"⚠️ Retry FAILED {pair} round {p['round']}: {e}")
            return False

    def _track_order(pair, r, step, market_start):
        """Remember a just-sent live order so the loop checks its fill.
        market_start is passed in (not read from pair_state) so a toggle
        (/btc, /btc15, /eth or /eth5 off) landing between the order and this call
        cannot make it read a bet_open that was just cleared to None.
        retries: how many retries have been used for this step so far (0
        for a fresh order); _retry_pending caps it at ORDER_MAX_RETRIES."""
        ps = pair_state[pair]
        if live_running and r and not r.get("check_only") and r.get("order_hash"):
            pending.append({"hash": r["order_hash"], "pair": pair, "round": ps["round_no"], "step": step,
                            "dir": ps["bet_dir"], "stake": ps["bet_stake"], "market_start": market_start,
                            "price": r.get("price"), "placed": time.time(), "warned": False, "retries": 0, "done": False})

    def _place_current_step(pair):
        """Place the LIVE order for the pair's current step (1 if state is
        bet1, 2 if bet2) and track it for fill checks. Sets
        order_placed=True. Only called when live_running is on.
        Reads bet_open once and bails out if a toggle cleared it to None
        in the meantime - the round is already dropped then.
        A LowPayoutSkip (payout below the configured minimum) abandons
        the round instead: step 1 risks no money; step 2 ends the round
        with just the step-1 loss. Either way the pair goes to cooldown
        so it does not re-trigger on the same run."""
        global balance
        ps = pair_state[pair]
        step = 1 if ps["state"] == "bet1" else 2
        bet_open = ps["bet_open"]
        ps["order_placed"] = True
        if bet_open is None:
            return
        try:
            r = _pf_place_order(ps["bet_dir"], ps["bet_stake"], market_start=bet_open // 1000, pair=pair)
            send(chat_id, ("CHECK (no order sent): " if r.get("check_only") else "LIVE ORDER SENT: ") + r.get("info", ""))
            _track_order(pair, r, step, bet_open // 1000)
        except LowPayoutSkip as e:
            if step == 2:
                f1 = _step_filled(pair, ps["round_no"], 1)
                if f1 is True:
                    balance -= config["stake1"]
                    ps["losses"] += 1
                    send(chat_id, f"⏭️ SKIP {pair} round {ps['round_no']} step 2: payout ${e.payout:.2f} on ${e.stake:.0f} ({e.ratio:.2f}x < {e.min_ratio:.2f}x) - too low. Round ends -${config['stake1']:.2f} (step 1). Balance ${balance:.2f}")
                else:
                    send(chat_id, f"⏭️ SKIP {pair} round {ps['round_no']} step 2: payout ${e.payout:.2f} on ${e.stake:.0f} ({e.ratio:.2f}x < {e.min_ratio:.2f}x) - too low. Step 1 never filled, $0 lost. Balance ${balance:.2f}")
            else:
                send(chat_id, f"⏭️ SKIP {pair} round {ps['round_no']} step 1: payout ${e.payout:.2f} on ${e.stake:.0f} ({e.ratio:.2f}x < {e.min_ratio:.2f}x) - too low, no order placed.")
            ps["state"], ps["bet_open"] = "cooldown", None
            ps["cooldown_run_col"] = "R" if ps["win_col"] == "G" else "G"
        except Exception as e:
            send(chat_id, f"Live order FAILED {pair} round {ps['round_no']}: {e}")

    def _schedule_order(pair):
        """Decide when the current step's LIVE order goes out. If the pair
        has an order delay (ETH 30s) and we are live, defer it until
        order_due_at so the book can populate; otherwise place it now.
        In paper mode there is no real order, so nothing is deferred."""
        ps = pair_state[pair]
        delay = PAIRS[pair].get("order_delay_sec", 0)
        if live_running and delay > 0:
            ps["order_due_at"] = time.time() + delay
            ps["order_placed"] = False
        else:
            if live_running:
                _place_current_step(pair)
            else:
                ps["order_placed"] = True

    def _pair_step(pair):
        """One iteration of one pair's state machine. Returns nothing;
        updates pair_state[pair], the shared balance, and Telegram."""
        global balance
        ps = pair_state[pair]
        cfg = PAIRS[pair]
        n = cfg["n"]
        tf_sec = cfg["tf_sec"]
        try:
            closed = fetch_closed(pair, 10)
        except Exception as e:
            print(f"[loop error {pair}] fetch_closed failed:", e, flush=True)
            return
        if not closed:
            return
        cols = [colour(k) for k in closed]
        last = closed[-1]
        # Read bet_open ONCE into a local: a toggle (/btc, /btc15, /eth or /eth5
        # off) runs on another thread and can clear ps["bet_open"] to None
        # mid-branch. Reading it three times (as the old code did) could
        # hit None in the stale check and raise a TypeError for that iteration.
        bet_open = ps["bet_open"]
        # Place a deferred LIVE order whose delay has elapsed (ETH: 30s so
        # the book can populate). Only while the round is still active and
        # the bet candle is known - a toggle off in the meantime drops the
        # state to idle, so nothing is placed after a toggle.
        if (ps["state"] in ("bet1", "bet2") and not ps["order_placed"]
                and bet_open is not None and time.time() >= ps["order_due_at"]):
            _place_current_step(pair)
        if ps["state"] in ("bet1", "bet2") and bet_open is not None:
            done = [k for k in closed if int(k[0]) == bet_open]
            if not done and time.time() * 1000 > bet_open + 3 * tf_sec * 1000:
                # Bet candle never appeared (e.g. long API outage) - the
                # bet is stale; drop it instead of staying stuck forever.
                # (3x the candle length: 15 min for BTC 5m, 45 min for
                # BTC15, ETH and ETH5 15m.)
                send(chat_id, f"⚠️ {pair} round {ps['round_no']} trade went stale (no candle for {3 * tf_sec // 60} min) - dropping it, no new trade placed.")
                ps["state"], ps["bet_open"] = "idle", None
            elif done:
                res = colour(done[0])
                if res == ps["win_col"]:
                    if ps["state"] == "bet1":
                        # Step-1 win: +stake1 only if its order really
                        # filled, else $0 (paper-only win).
                        f1 = _step_filled(pair, ps["round_no"], 1)
                        pnl = config["stake1"] if f1 is True else 0.0
                        balance += pnl
                        note = "" if f1 is True else (" (fill unknown - check wallet)" if f1 is None else " (paper only - step 1 never filled)")
                        send(chat_id, f"✅ WIN {pair} round {ps['round_no']} ${ps['bet_stake']:.0f} on {ps['bet_dir']}. +${pnl:.2f}{note}. Balance ${balance:.2f}")
                        ps["wins"] += 1
                    else:
                        # Step-2 win: round PnL = step-1 loss (if its
                        # order filled) + step-2 profit (if its order
                        # filled). Unfilled steps move $0.
                        f1 = _step_filled(pair, ps["round_no"], 1)
                        f2 = _step_filled(pair, ps["round_no"], 2)
                        pnl = (-config["stake1"] if f1 is True else 0.0) + (config["stake2"] if f2 is True else 0.0)
                        balance += pnl
                        parts = []
                        for s, f in ((1, f1), (2, f2)):
                            if f is False:
                                parts.append(f"step {s} never filled")
                            elif f is None:
                                parts.append(f"step {s} fill unknown")
                        note = " (" + ", ".join(parts) + ")" if parts else ""
                        send(chat_id, f"✅ WIN {pair} round {ps['round_no']} ${ps['bet_stake']:.0f} on {ps['bet_dir']}. Real {pnl:+.2f}{note}. Balance ${balance:.2f}")
                        ps["wins"] += 1
                    ps["state"], ps["bet_open"] = "idle", None
                elif ps["state"] == "bet1":
                    ps["state"], ps["bet_stake"] = "bet2", config["stake2"]
                    ps["bet_open"] = int(last[6]) + 1
                    send(chat_id, f"❌ LOSS {pair} round {ps['round_no']} step 1. Next: ${ps['bet_stake']:.0f} on {ps['bet_dir']}.")
                    # Step-2 order goes through the same delay as step 1:
                    # its market just opened too, so its book is thin as
                    # well. _schedule_order places it now (BTC) or after
                    # the pair's delay (ETH 30s).
                    _schedule_order(pair)
                else:
                    # STOP: deduct only the stakes that really filled.
                    # A step whose order never filled lost $0, so this
                    # round is -$1 or -$0, not always -$3.
                    f1 = _step_filled(pair, ps["round_no"], 1)
                    f2 = _step_filled(pair, ps["round_no"], 2)
                    loss = (config["stake1"] if f1 is True else 0.0) + (config["stake2"] if f2 is True else 0.0)
                    balance -= loss
                    ps["losses"] += 1
                    parts = []
                    for s, f in ((1, f1), (2, f2)):
                        if f is False:
                            parts.append(f"step {s} never filled")
                        elif f is None:
                            parts.append(f"step {s} fill unknown")
                    note = " (" + ", ".join(parts) + ")" if parts else ""
                    send(chat_id, f"❌ LOSS {pair} round {ps['round_no']} step 2. STOP. -${loss:.2f}{note}. Balance ${balance:.2f}")
                    ps["state"], ps["bet_open"] = "cooldown", None
                    # The run that triggered this round: red run if we bet UP
                    # (win_col "G"), green run if we bet DOWN (win_col "R").
                    ps["cooldown_run_col"] = "R" if ps["win_col"] == "G" else "G"
            return
        # Cooldown ends ONLY when the run breaks: a closed candle of the
        # opposite colour to the finished run. The old check
        # (last candle is G or R) was always true, so the bot re-entered
        # the same run at once and lost $3 again within minutes.
        if ps["state"] == "cooldown" and ps["cooldown_run_col"] is not None and cols[-1] != ps["cooldown_run_col"]:
            ps["state"] = "idle"
            ps["cooldown_run_col"] = None
        if ps["state"] == "idle":
            # Pause hour (Dhaka 20:00-21:00): skip NEW triggers only.
            # An open round still settles (handled above) and the
            # cooldown still runs. Triggers resume at 21:00.
            if _in_pause_window():
                return
            if cols[-n:] == ["R"] * n or cols[-n:] == ["G"] * n:
                is_up = cols[-n:] == ["R"] * n
                ps["round_no"] += 1
                ps["state"] = "bet1"
                ps["bet_stake"] = config["stake1"]
                ps["bet_dir"], ps["win_col"] = ("UP", "G") if is_up else ("DOWN", "R")
                ps["bet_open"] = int(last[6]) + 1
                tag = "LIVE" if live_running else "Demo"
                send(chat_id, f"🎯 TRIGGER {pair} round {ps['round_no']}: {n} {'red' if is_up else 'green'}. {tag} trade ${ps['bet_stake']:.0f} on {ps['bet_dir']}.")
                # Step-1 order: placed now (BTC) or after the pair's delay
                # (ETH 30s, so the thin book can populate first).
                _schedule_order(pair)

    while True:
        if not running and not live_running:
            # Stopped: drop any half-open paper bet so a later restart does
            # not resolve a stale round against an old candle. (Cooldown is
            # left alone - it must survive a restart or the bot re-enters
            # the same run.) Pending fill checks are kept; they resume on
            # restart. A deferred order is cleared too, so it is never
            # placed after the bot was stopped.
            for ps in pair_state.values():
                if ps["state"] in ("bet1", "bet2"):
                    ps["state"], ps["bet_open"] = "idle", None
                    ps["order_placed"], ps["order_due_at"] = True, 0.0
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
                for ps in pair_state.values():
                    ps["wins"] = 0
                    ps["losses"] = 0
            # No safety stops - removed at the user's request (2026-11-16).
            # The bot no longer stops on an unusual move, consecutive
            # losses, a daily loss, a $0 balance, or API failures. It keeps
            # trading until the user stops it (/stopbot or /livestop).
            # Fill check for live orders sent earlier: 201 means accepted,
            # not filled. Check each pending order once it is 10s old (the
            # docs say a hash can briefly 404 right after placing, so
            # 10s is enough for it to be known; 30s was needlessly slow
            # and pushed any retry late into the market window).
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
                tf_sec = PAIRS[p["pair"]]["tf_sec"]
                if st == "FILLED":
                    filled[(p["pair"], p["round"], p["step"])] = True
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
                    send(chat_id, f"✅ Order FILLED {p['pair']} round {p['round']} ${p['stake']:.0f} on {p['dir']}.{note}")
                    p["done"] = True
                elif st in ("CANCELLED", "EXPIRED", "INVALIDATED"):
                    filled[(p["pair"], p["round"], p["step"])] = False
                    p["done"] = True
                    if not _retry_pending(p):
                        why = f" ({reason})" if reason else ""
                        send(chat_id, f"⚠️ Order {st} {p['pair']} round {p['round']} ${p['stake']:.0f}{why} - NO real trade for this step.")
                elif st == "OPEN" and age > 60 and not p["warned"]:
                    p["warned"] = True
                    # Do NOT place a second order while the first is still
                    # on the book - it could fill twice. Try to remove the
                    # old one first (up/down markets may lock removal);
                    # retry only if the removal really cancelled it.
                    if _pf_remove_order(p["hash"]):
                        send(chat_id, f"⏳ Order still OPEN {p['pair']} round {p['round']} ${p['stake']:.0f} after 60s - removed it, retrying once.")
                        _retry_pending(p)
                        filled[(p["pair"], p["round"], p["step"])] = False  # old order is cancelled; the retry is tracked as a new entry
                        p["done"] = True
                    else:
                        p["done"] = True
                        send(chat_id, f"⚠️ Order still OPEN {p['pair']} round {p['round']} ${p['stake']:.0f} and could not be removed (market lock) - leaving it, it may still fill.")
                elif time.time() > p["market_start"] + tf_sec:
                    filled[(p["pair"], p["round"], p["step"])] = False
                    p["done"] = True
                    send(chat_id, f"⚠️ Order never filled {p['pair']} round {p['round']} - NO real trade for this step.")
            pending[:] = [p for p in pending if not p["done"]]
            # Each enabled pair runs its own state machine this iteration.
            for name in PAIRS:
                if not pair_enabled(name):
                    continue
                try:
                    _pair_step(name)
                except Exception as e:
                    api_errors += 1
                    print(f"[loop error {name}]", e, "api_errors:", api_errors, flush=True)
            # /setfund blocks while a round is open on either pair.
            bet_active = any(ps["state"] in ("bet1", "bet2") for ps in pair_state.values())
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
