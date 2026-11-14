#!/usr/bin/env python3
"""
Telegram DEMO bot - Predict.fun paper trading (NO REAL MONEY)
================================================================
Demo first: virtual fund, real trades NAI. Eita diye dekhba fund
khay kina (loss hole balance kombe), phone bondho thakleo server-e
cholte thakle Telegram-e message joma thakbe.

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
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.json")
DEFAULT_CONFIG = {
    "starting_fund": 20.0,   # <-- demo fund, /setfund diye change kora jay
    "stake1": 1.0,
    "stake2": 2.0,
    "daily_loss_limit": 9.0,
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
                f"Fund change korte: /setfund 20\nCommands: /help")
    if cmd == "/help":
        return ("/start, /status, /fund, /pnl\n"
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
                f"Daily limit: ${config['daily_loss_limit']:.0f}")
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
            config["daily_loss_limit"] = float(args[0])
            save_config()
            return f"✅ Daily loss limit: ${config['daily_loss_limit']:.0f}"
        except Exception:
            return "Usage: /setlimit 9"
    if cmd == "/startbot":
        if running:
            return "Bot already cholche."
        running = True
        return "▶️ Demo bot start holo. Trigger hole Telegram-e janabo."
    if cmd == "/stopbot":
        running = False
        return f"⏹️ Bot stop. Balance: ${balance:.2f}"
    return "Bujhlam na. /help likho."


def bot_loop(chat_id):
    """Paper bot loop: proti 20 sec-e candle check, Telegram-e janay."""
    global balance, running, round_no, wins, losses
    state = "idle"
    bet_open = None
    bet_stake = 0.0
    bet_dir = None
    win_col = None
    while True:
        if not running:
            time.sleep(5)
            continue
        try:
            closed = fetch_closed(10)
            cols = [colour(k) for k in closed]
            last = closed[-1]
            if balance <= 0:
                running = False
                send(chat_id, f"🛑 Fund sesh! Balance $0. Bot thamlo. /setfund diye abar fund dao.")
                continue
            if balance - day_start_balance <= -config["daily_loss_limit"]:
                running = False
                send(chat_id, f"🛑 Daily loss limit -${config['daily_loss_limit']:.0f} touch korse. Bot thamlo. Balance ${balance:.2f}")
                continue
            if state in ("bet1", "bet2") and bet_open is not None:
                done = [k for k in closed if int(k[0]) == bet_open]
                if done:
                    res = colour(done[0])
                    if res == win_col:
                        balance += config["stake1"]
                        wins += 1
                        send(chat_id, f"✅ WIN round {round_no} ${bet_stake:.0f} on {bet_dir} -> {res}. Net +${config['stake1']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "idle", None
                    elif state == "bet1":
                        state, bet_stake = "bet2", config["stake2"]
                        bet_open = int(last[6]) + 1
                        send(chat_id, f"❌ LOSS round {round_no} step 1. Next: ${bet_stake:.0f} on {bet_dir}.")
                    else:
                        balance -= (config["stake1"] + config["stake2"])
                        losses += 1
                        send(chat_id, f"❌ LOSS round {round_no} step 2. STOP, net -${config['stake1']+config['stake2']:.0f}. Balance ${balance:.2f}")
                        state, bet_open = "cooldown", None
                time.sleep(20)
                continue
            if state == "cooldown" and cols[-1] in ("G", "R"):
                state = "idle"
            if state == "idle":
                if cols[-4:] == ["R"] * 4:
                    round_no += 1
                    state, bet_stake, bet_dir, win_col = "bet1", config["stake1"], "UP", "G"
                    bet_open = int(last[6]) + 1
                    send(chat_id, f"🎯 TRIGGER round {round_no}: 4 red. Demo bet ${bet_stake:.0f} on UP.")
                elif cols[-4:] == ["G"] * 4:
                    round_no += 1
                    state, bet_stake, bet_dir, win_col = "bet1", config["stake1"], "DOWN", "R"
                    bet_open = int(last[6]) + 1
                    send(chat_id, f"🎯 TRIGGER round {round_no}: 4 green. Demo bet ${bet_stake:.0f} on DOWN.")
        except Exception as e:
            print("[loop error]", e)
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
