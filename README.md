# Claude Telegram Bot

Control Claude Code from Telegram. Send prompts, run git commands, and manage projects — all from your phone.

## Prerequisites

- Python 3.10+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` CLI available in PATH)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))

## Setup

**1. Clone and enter the directory**
```bash
git clone https://github.com/shivanshu814/claude-telegram-bot
cd claude-telegram-bot
```

**2. Create a virtual environment and install dependencies**
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**3. Configure environment variables**
```bash
cp .env.example .env
```

Edit `.env` and fill in your values:
```
BOT_TOKEN=your_telegram_bot_token
TELEGRAM_USER_ID=your_telegram_user_id
```

**4. Run the bot**
```bash
python bot.py
```

Or use the start script:
```bash
./start.sh
```

## Commands

| Command | Description |
|---|---|
| `/start` | Show help |
| `/use <path>` | Set the active project folder |
| `/dir` | Show current active folder |
| `/status` | Git status of active folder |
| `/add` | Run `git add .` |
| `/commit <message>` | Commit with a message |
| `/push` | Push current branch to origin |
| `/reset` | Start a fresh Claude session |

Any message that isn't a command is sent directly to Claude. The session persists across messages — Claude remembers the full conversation until you `/reset`.

## Security

The bot only responds to the Telegram user ID set in `TELEGRAM_USER_ID`. All other users are ignored.
