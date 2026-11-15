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
  Live stakes are still capped $1/$2, daily loss $9.

Fund can be EDITED from Telegram:
  /setfund 20      -> sets starting fund to $20 (balance reset)
  /setstake 1 2    -> bet 1 = $1, bet 2 = $2
  /setlimit 9      -> daily loss limit $9, then the bot stops itself
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

import json
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
    "daily_loss_limit": 0.0,  # 0 = OFF, user sets it with /setlimit
    "max_consecutive_losses": 3  # no fund floor - user removed it
}
BINANCE = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12"

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
_live_jwt = None
_live_jwt_at = 0


def load_config():
    global config, balance, day_start_balance
    if os.path.exists(CONFIG_FILE):
        try:
            config.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    config.setdefault("daily_loss_limit", 0.0)
    config.setdefault("max_consecutive_losses", 3)

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
    for path in (f"/v1/categories/btc-updown-5m-{nxt}", "/v1/series/btc-updown-5m/rounds"):
        try:
            res = _pf_rest("GET", path, jwt=jwt)
        except Exception:
            continue
        data = res.get("data", res)
        markets = data.get("markets") or []
        if not markets and isinstance(data.get("rounds"), list):
            for rd in data["rounds"]:
                markets += rd.get("markets", []) or []
        if markets:
            market = markets[0]
            break
    if not market:
        raise RuntimeError(f"No Predict.fun market for round {nxt}")
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
        return {"check_only": True, "info": logline, "order_hash": order_hash}
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
    return {"check_only": False, "info": logline, "response": res, "order_hash": order_hash}

def _pf_order_status(order_hash):
    """GET /v1/orders/{hash} -> status string (OPEN / FILLED / CANCELLED /
    EXPIRED / INVALIDATED), or None if the order is not known yet. Per the
    docs a hash can briefly 404 right after placing, so None means 'check
    again on the next poll', not 'failed'."""
    jwt = _pf_jwt()
    try:
        res = _pf_rest("GET", f"/v1/orders/{order_hash}", jwt=jwt)
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    data = res.get("data", res) or {}
    return (data.get("status") or "").upper() or None

def fetch_closed(n=10):
    req = urllib.request.Request(BINANCE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.load(r)
    now_ms = int(time.time() * 1000)
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
                f"Fund: ${balance:.2f} (start ${config['starting_fund']:.2f})")
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
                "/setstake 1 2 — bet change\n"
                "/setlimit 9 — daily loss limit\n"
                "/startbot — demo bot start\n/stopbot — demo bot stop")
    if cmd == "/status":
        return status_text()
    if cmd == "/fund":
        return (f"Starting fund: ${config['starting_fund']:.2f}\n"
                f"Balance now: ${balance:.2f}\n"
                f"PnL: ${balance - config['starting_fund']:+.2f}\n"
                f"Stake: ${config['stake1']:.0f} / ${config['stake2']:.0f} | "
                f"Daily limit: " + ("OFF" if config['daily_loss_limit'] == 0 else f"${config['daily_loss_limit']:.0f}"))
    if cmd == "/pnl":
        return (f"Today's PnL: ${balance - day_start_balance:+.2f}\n"
                f"Win: {wins} | Loss: {losses} | Round: {round_no}\n"
                f"Balance: ${balance:.2f}")
    if cmd == "/setfund":
        try:
            v = float(args[0])
            if v <= 0:
                return "Fund must be more than $0. Example: /setfund 20"
            config["starting_fund"] = v
            balance = v
            day_start_balance = v
            wins = losses = round_no = 0
            save_config()
            return f"✅ Fund set: ${v:.2f} (balance reset, demo money)"
        except Exception:
            return "Usage: /setfund 20"
    if cmd == "/setstake":
        try:
            config["stake1"] = float(args[0])
            config["stake2"] = float(args[1])
            save_config()
            return f"✅ Stake set: ${config['stake1']:.0f} / ${config['stake2']:.0f}"
        except Exception:
            return "Usage: /setstake 1 2"
    if cmd == "/setlimit":
        try:
            v = float(args[0])
            if v < 0:
                return "Limit must be 0 or more. To turn off: /setlimit 0"
            config["daily_loss_limit"] = v
            save_config()
            if v == 0:
                return "✅ Daily loss limit OFF."
            return f"✅ Daily loss limit: ${v:.0f} - the bot will stop at this limit"
        except Exception:
            return "Usage: /setlimit 9  (to turn off: /setlimit 0)"
    if cmd == "/startbot":
        if running:
            return "Bot is already running."
        running = True
        return "▶️ Demo bot started."
    if cmd == "/stopbot":
        running = False
        return f"⏹️ Bot stopped. Balance: ${balance:.2f}"
    return "I didn't understand. Type /help."


def _safety_stop(chat_id, reason):
    global running, live_running
    running = False
    live_running = False
    msg = f"🛑 SAFETY STOP: {reason} Balance ${balance:.2f}. Restart: /startbot or /livestart."
    send(chat_id, msg)
    print("[safety]", msg, flush=True)

def bot_loop(chat_id):
    """Paper bot loop: checks candles every 20 sec, notifies on Telegram."""
    global balance, running, round_no, wins, losses
    consec_losses = 0
    api_errors = 0
    state = "idle"
    bet_open = None
    bet_stake = 0.0
    bet_dir = None
    win_col = None
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
                                "placed": time.time(), "warned": False, "retried": True, "done": False})
            return True
        except Exception as e:
            send(chat_id, f"⚠️ Retry FAILED round {p['round']}: {e}")
            return False

    def _track_order(r, step):
        """Remember a just-sent live order so the loop checks its fill."""
        if live_running and r and not r.get("check_only") and r.get("order_hash"):
            pending.append({"hash": r["order_hash"], "round": round_no, "step": step,
                            "dir": bet_dir, "stake": bet_stake, "market_start": bet_open // 1000,
                            "placed": time.time(), "warned": False, "retried": False, "done": False})

    while True:
        if not running and not live_running:
            time.sleep(5)
            continue
        try:
            closed = fetch_closed(10)
            api_errors = 0
            cols = [colour(k) for k in closed]
            last = closed[-1]
            # Unusual move: last closed candle moved >2% in 5 min -> do NOT bet, safety stop
            _move = abs(float(last[4]) - float(last[1])) / float(last[1]) if float(last[1]) else 0
            if _move > 0.02:
                _safety_stop(chat_id, f"Unusual move: last candle moved {_move*100:.1f}%.")
                continue
            # No fund floor - user removed it. Bot stops only at $0, daily limit if set,
            # 3 losses in a row, unusual move, or 3 API fails in a row.
            # Daily loss limit - only if user set it (>0)
            if float(config.get("daily_loss_limit", 0)) > 0 and balance - day_start_balance <= -float(config["daily_loss_limit"]):
                _safety_stop(chat_id, f"Daily loss limit -${config['daily_loss_limit']:.0f} reached.")
                continue
            if consec_losses >= int(config.get("max_consecutive_losses", 3)):
                _safety_stop(chat_id, f"{consec_losses} rounds lost in a row.")
                continue
            if balance <= 0:
                running = False
                send(chat_id, f"🛑 Fund finished! Balance $0. Bot stopped.")
                continue
            # Fill check for live orders sent earlier: 201 means accepted,
            # not filled. Check each pending order once it is 30s old.
            for p in list(pending):
                if p["done"]:
                    continue
                age = time.time() - p["placed"]
                if age < 30:
                    continue
                try:
                    st = _pf_order_status(p["hash"])
                except Exception as e:
                    print("[fill check failed]", e, flush=True)
                    continue
                if st is None:
                    continue  # hash not known yet, check again next poll
                if st == "FILLED":
                    send(chat_id, f"✅ Order FILLED round {p['round']} ${p['stake']:.0f} on {p['dir']}.")
                    p["done"] = True
                elif st in ("CANCELLED", "EXPIRED", "INVALIDATED"):
                    p["done"] = True
                    if not _retry_pending(p):
                        send(chat_id, f"⚠️ Order {st} round {p['round']} ${p['stake']:.0f} - NO real trade for this step.")
                elif st == "OPEN" and age > 90 and not p["warned"]:
                    p["warned"] = True
                    send(chat_id, f"⏳ Order still OPEN round {p['round']} ${p['stake']:.0f} - not filled after 90s, retrying once.")
                    _retry_pending(p)
                elif time.time() > p["market_start"] + 300:
                    p["done"] = True
                    send(chat_id, f"⚠️ Order never filled round {p['round']} - NO real trade for this step.")
            pending[:] = [p for p in pending if not p["done"]]
            if state in ("bet1", "bet2") and bet_open is not None:
                done = [k for k in closed if int(k[0]) == bet_open]
                if done:
                    res = colour(done[0])
                    if res == win_col:
                        balance += config["stake1"]
                        wins += 1
                        consec_losses = 0
                        send(chat_id, f"✅ WIN round {round_no} ${bet_stake:.0f} on {bet_dir}. +${config['stake1']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "idle", None
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
                        balance -= (config["stake1"] + config["stake2"])
                        losses += 1
                        consec_losses += 1
                        send(chat_id, f"❌ LOSS round {round_no} step 2. STOP. -${config['stake1']+config['stake2']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "cooldown", None
                        # The run that triggered this round: red run if we bet UP
                        # (win_col "G"), green run if we bet DOWN (win_col "R").
                        cooldown_run_col = "R" if win_col == "G" else "G"
                time.sleep(20)
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
            if api_errors >= 3 and (running or live_running):
                _safety_stop(chat_id, f"API failed 3 times in a row: {e}")
        time.sleep(20)


def main():
    global owner_chat_id
    load_config()
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
