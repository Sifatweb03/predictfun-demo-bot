Telegram Demo Bot - Railway deploy
===================================
Files:
- telegram-demo-bot.py (demo/paper only, no real trades)
- bot_config.json (starting_fund 20, stake1 1, stake2 2, daily_loss_limit 9)
- Procfile (worker: python3 telegram-demo-bot.py)
- requirements.txt (empty, stdlib only)

Steps:
1. GitHub account khulo, notun repo banao (naam: predictfun-demo-bot), ei 4 ta file upload koro.
2. Railway (railway.app) e GitHub diye sign in koro.
3. New Project > Deploy from GitHub repo > repo ta select koro.
4. Variables e jao, add: TELEGRAM_BOT_TOKEN = BotFather theke pawa token (pura ta).
5. Deploy hoye gele Telegram e @Predictfun_testing_bot khule /start likho, tarpor /fund likho.
