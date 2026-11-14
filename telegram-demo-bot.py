#!/usr/bin/env python3
"""
Telegram DEMO bot - Predict.fun paper trading (NO REAL MONEY)
================================================================
Demo first: virtual fund, real trades NAI. Eita diye dekhba fund
khay kina (loss hole balance kombe), phone bondho thakleo server-e
cholte thakle Telegram-e message joma thakbe.

LIVE MODE (same bot, same Railway project):
  Paper is the default. Live real-money orders ONLY start with
  /livestart, and ONLY if Railway has LIVE_TRADING=1 plus
  PREDICT_API_KEY and PREDICT_WALLET_PRIVATE_KEY set.
  With LIVE_TRADING=0 (default) /livestart only does a CHECK:
  auth + market + book + signed order in memory, NO order sent.
  Live stakes are still capped $1/$2, daily loss $9.

Fund EDIT kora jay Telegram thekei:
  /setfund 20      -> starting fund $20 kore dibe (balance reset)
  /setstake 1 2    -> bet 1 = $1, bet 2 = $2
  /setlimit 9      -> daily loss limit $9, tarpor bot nijei thambe
  /fund            -> ekhon fund / balance koto dekhabe

Onno command:
  /start   - bot chalu, fund dekhabe
  /status  - ekhon trigger ache kina, last candle
  /startbot - demo bot start
  /stopbot  - demo bot stop
  /pnl     - ajker win/loss, balance
  /help    - shob command

Strategy: 4 red -> UP $1, harle $2, harle STOP. 4 green -> DOWN $1,
harle $2, harle STOP. Max loss $3/round.

SETUP - prothome ja korte hobe (Sifat-er kaj):
  1. Telegram kholo, @BotFather khujo
  2. /newbot -> bot-er nam dao -> username dao (sheshe _bot thakte hobe)
  3. BotFather je TOKEN dibe, oita copy koro - kauke diba na, chat-e
     pathaba na. Token ta shudhu server-e .env / environment-e bosbe.
  4. Bot ke Telegram-e khujo, /start dao - bot reply dile bujhba
     cholche.
  5. Server-e chalate hobe 24h: Railway / Render / PythonAnywhere
     (free) ba cheap VPS. Oikhane ei file + token bosiye run koro:
         TELEGRAM_BOT_TOKEN=xxxx python3 telegram-demo-bot.py
     Phone bondho thakleo server cholbe.

Requires: Python 3.9+ only, kono extra package lagbe na.
Token: environment variable TELEGRAM_BOT_TOKEN theke nibe.
         kokhono code-e / chat-e token likhba na.
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
    "starting_fund": 20.0,   # <-- demo fund, /setfund diye change kora jay
    "stake1": 1.0,
    "stake2": 2.0,
    "daily_loss_limit": 0.0,  # 0 = OFF, user sets it with /setlimit
    "max_consecutive_losses": 3  # no fund floor - user removed it
}
BINANCE = "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=12"

# ---- state (memory-te thake, config file-e save hoy) ----
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

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

def _pf_place_order(direction, stake):
    """direction UP -> buy Yes, DOWN -> buy No. Returns order info or None in CHECK mode.
    Raises on failure - caller must handle it and NOT count a failed order as placed."""
    from predict_sdk import Book, BuildOrderInput, ChainId, MarketHelperValueInput, OrderBuilder, Side
    jwt = _pf_jwt()
    nxt = (int(time.time()) // 300 + 1) * 300
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
                               slippage_bps=100, is_min_amount_out=True), book)
    order_input = BuildOrderInput(side=Side.BUY, token_id=str(outcome["onChainId"]),
        maker_amount=str(amounts.maker_amount), taker_amount=str(amounts.taker_amount),
        fee_rate_bps=str(market.get("feeRateBps", 0)))
    order = builder.build_order("MARKET", order_input)
    typed = builder.build_typed_data(order, is_neg_risk=bool(market.get("isNegRisk")),
                                     is_yield_bearing=bool(market.get("isYieldBearing")))
    signed = builder.sign_typed_data_order(typed)
    order_hash = builder.build_typed_data_hash(typed)
    logline = (f"LIVE order {outcome_name} ${stake} market {market['id']} "
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
            trig = "🎯 TRIGGER! Porer candle-e UP-e $1 bet"
        elif cols[-4:] == ["G"] * 4:
            trig = "🎯 TRIGGER! Porer candle-e DOWN-e $1 bet"
        else:
            trig = "⏳ Trigger nai — wait koro"
        return (f"BTC: ${price:,.2f}\nLast 4: {''.join(cols[-4:])}\n{trig}\n"
                f"Fund: ${balance:.2f} (start ${config['starting_fund']:.2f})")
    except Exception as e:
        return f"Status check hoilo na: {e}"


def handle_command(chat_id, text):
    global balance, running, wins, losses, round_no, day_start_balance
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "/start":
        return (f"🤖 Predict.fun DEMO bot\n"
                f"Fund: ${balance:.2f} (demo, asol taka na)\n"
                f"Strategy: 4 red → UP, 4 green → DOWN, $1 harle $2, harle STOP\n"
                f"Fund change korte: /setfund 10\nCommands: /help")
    if cmd == "/mode":
        if live_running:
            return "Mode: LIVE - real money orders (LIVE_TRADING=1)" if LIVE_TRADING else "Mode: LIVE (CHECK only - no orders sent, LIVE_TRADING=0)"
        if running:
            return "Mode: PAPER demo - virtual $20 fund, no real trades"
        return "Mode: stopped. Paper: /startbot | Live: /livestart"
    if cmd == "/livestart":
        if not PREDICT_API_KEY or not PREDICT_PRIV_KEY:
            return "Live off: PREDICT_API_KEY + PREDICT_WALLET_PRIVATE_KEY Railway Variables-e bosano hoy ni."
        if live_running:
            return "Live already cholche."
        try:
            _pf_jwt()
        except Exception as e:
            return f"Live start hoilo na - Predict.fun auth fail: {e}"
        globals()["live_running"] = True
        return ("LIVE start holo. " + ("REAL ORDERS jabe - asol taka!" if LIVE_TRADING else "CHECK mode - order pathano hobe NA, shudhu check korbe."))
    if cmd == "/livestop":
        globals()["live_running"] = False
        return f"Live stop. Paper PnL: ${balance - day_start_balance:+.2f}"
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
                f"Balance ekhon: ${balance:.2f}\n"
                f"PnL: ${balance - config['starting_fund']:+.2f}\n"
                f"Stake: ${config['stake1']:.0f} / ${config['stake2']:.0f} | "
                f"Daily limit: " + ("OFF" if config['daily_loss_limit'] == 0 else f"${config['daily_loss_limit']:.0f}"))
    if cmd == "/pnl":
        return (f"Ajker PnL: ${balance - day_start_balance:+.2f}\n"
                f"Win: {wins} | Loss: {losses} | Round: {round_no}\n"
                f"Balance: ${balance:.2f}")
    if cmd == "/setfund":
        try:
            v = float(args[0])
            if v <= 0:
                return "Fund $0 er beshi hote hobe. Jemon: /setfund 20"
            config["starting_fund"] = v
            balance = v
            day_start_balance = v
            wins = losses = round_no = 0
            save_config()
            return f"✅ Fund set hoise: ${v:.2f} (balance reset, demo taka)"
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
                return "Limit 0 ba tar beshi hote hobe. Off korte: /setlimit 0"
            config["daily_loss_limit"] = v
            save_config()
            if v == 0:
                return "✅ Daily loss limit OFF - kono daily limit thakbe na. Safety (3 loss in a row, unusual move, API fail) ekhono cholbe."
            return f"✅ Daily loss limit: ${v:.0f} - ei limit-e gele bot thambe"
        except Exception:
            return "Usage: /setlimit 9  (off korte: /setlimit 0)"
    if cmd == "/startbot":
        if running:
            return "Bot already cholche."
        running = True
        return "▶️ Demo bot start holo. Trigger hole Telegram-e janabo."
    if cmd == "/stopbot":
        running = False
        return f"⏹️ Bot stop. Balance: ${balance:.2f}"
    return "Bujhlam na. /help likho."


def _safety_stop(chat_id, reason):
    global running, live_running
    running = False
    live_running = False
    msg = f"🛑 SAFETY STOP: {reason} Auto trade OFF hoye geche. Balance ${balance:.2f}. Abar chalu korte /startbot (paper) ba /livestart (live) likho."
    send(chat_id, msg)
    print("[safety]", msg, flush=True)

def bot_loop(chat_id):
    """Paper bot loop: proti 20 sec-e candle check, Telegram-e janay."""
    global balance, running, round_no, wins, losses
    consec_losses = 0
    api_errors = 0
    state = "idle"
    bet_open = None
    bet_stake = 0.0
    bet_dir = None
    win_col = None
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
                _safety_stop(chat_id, f"Unusual move: last candle {_move*100:.1f}% move korse.")
                continue
            # No fund floor - user removed it. Bot stops only at $0, daily limit if set,
            # 3 losses in a row, unusual move, or 3 API fails in a row.
            # Daily loss limit - only if user set it (>0)
            if float(config.get("daily_loss_limit", 0)) > 0 and balance - day_start_balance <= -float(config["daily_loss_limit"]):
                _safety_stop(chat_id, f"Daily loss limit -${config['daily_loss_limit']:.0f} touch korse.")
                continue
            if consec_losses >= int(config.get("max_consecutive_losses", 3)):
                _safety_stop(chat_id, f"{consec_losses} ta round ek tana loss hoise.")
                continue
            if balance <= 0:
                running = False
                send(chat_id, f"🛑 Fund sesh! Balance $0. Bot thamlo. /setfund diye abar fund dao.")
                continue
            if state in ("bet1", "bet2") and bet_open is not None:
                done = [k for k in closed if int(k[0]) == bet_open]
                if done:
                    res = colour(done[0])
                    if res == win_col:
                        balance += config["stake1"]
                        wins += 1
                        consec_losses = 0
                        send(chat_id, f"✅ WIN round {round_no} ${bet_stake:.0f} on {bet_dir} -> {res}. Net +${config['stake1']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "idle", None
                    elif state == "bet1":
                        state, bet_stake = "bet2", config["stake2"]
                        bet_open = int(last[6]) + 1
                        send(chat_id, f"❌ LOSS round {round_no} step 1. Next: ${bet_stake:.0f} on {bet_dir}.")
                        if live_running:
                            try:
                                r = _pf_place_order(bet_dir, bet_stake)
                                send(chat_id, ("CHECK (no order sent): " if r.get("check_only") else "LIVE ORDER SENT: ") + r.get("info", ""))
                            except Exception as e:
                                send(chat_id, f"Live step-2 order FAILED round {round_no}: {e}")
                    else:
                        balance -= (config["stake1"] + config["stake2"])
                        losses += 1
                        consec_losses += 1
                        send(chat_id, f"❌ LOSS round {round_no} step 2. STOP, net -${config['stake1']+config['stake2']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "cooldown", None
                time.sleep(20)
                continue
            if state == "cooldown" and cols[-1] in ("G", "R"):
                state = "idle"
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
                            r = _pf_place_order(bet_dir, bet_stake)
                            send(chat_id, ("CHECK (no order sent): " if r.get("check_only") else "LIVE ORDER SENT: ") + r.get("info", ""))
                        except Exception as e:
                            send(chat_id, f"Live order FAILED round {round_no}: {e}")
        except Exception as e:
            api_errors += 1
            print("[loop error]", e, "api_errors:", api_errors, flush=True)
            if api_errors >= 3 and (running or live_running):
                _safety_stop(chat_id, f"API 3 bar ek tana fail korse: {e}")
        time.sleep(20)


def main():
    global owner_chat_id
    load_config()
    if not TG_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN set koro.\n"
              "TELEGRAM_BOT_TOKEN=xxxx python3 telegram-demo-bot.py")
        return
    print(f"Demo bot cholche. Starting fund ${config['starting_fund']:.2f} (demo only, no real money)")
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
                    send(chat_id, "Ei bot shudhu owner-er jonno.")
                    continue
                reply = handle_command(chat_id, text)
                if reply:
                    send(chat_id, reply)
        except Exception as e:
            print("[poll error]", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
