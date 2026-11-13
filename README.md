# Stateless Telegram Support Relay Bot

A polling-based customer-support relay bot using **Python 3.12+**, **aiogram 3.x**, and the official Telegram Bot API.

## What it does

- Relays private user messages to one configured admin group.
- Supports text, photos, videos, video notes, voice, audio, documents, stickers, animations, locations, contacts, venues, polls, and media albums whenever Telegram permits forwarding/copying them.
- Preserves message formatting and media by using Telegram's native forward/copy methods.
- Lets configured admins reply to forwarded user messages or to the bot-authored user-info notice.
- Supports multiple configured admins.
- Provides `/start`, `/help`, `/id`, `/user`, `/admin`, and `/stats`.
- Uses no database, no JSON storage, no external storage, and no permanent message mapping.

The only temporary in-memory state is a short-lived media-album buffer (normally about one second) used to batch album updates. It is discarded after forwarding and is not used for reply routing.

## Important Telegram limitations

The Telegram Bot API does not permit forwarding service messages or protected-content messages. Some message types also cannot be copied (for example, service messages and some paid/widget content). When Telegram rejects both forwarding and copying, the bot sends the admin group a user-information notice instead; admins can still reply to that notice. This is an API limitation, not a missing database feature.

When a user sends a message that was already forwarded to them, its `forward_origin` may refer to the original author rather than the person contacting support. To prevent a reply from reaching the wrong person, the bot copies that content and requires admins to reply to the bot-authored user-info notice. Normal messages sent directly by a user are forwarded, and admins can reply directly to those forwarded messages using `forward_origin`.

## 1. Create the bot and admin group

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a private Telegram group for support admins.
3. Add the bot to that group and allow it to send messages.
4. Add each support admin's numeric Telegram user ID to `ADMIN_IDS`.
5. Get the group ID. A supergroup ID normally looks like `-1001234567890`.

The bot does not automatically treat every Telegram group admin as a support admin. Only IDs explicitly listed in `ADMIN_IDS` may relay replies.

## 2. Install

```bash
python3 --version
# Python 3.12 or newer is recommended

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3. Configure environment variables

### Local `.env` file

Copy `.env.example` to `.env`, then edit the values. The bot loads `.env` automatically using `python-dotenv`. Never upload or commit a real `.env` file.

```bash
cp .env.example .env
# Edit .env and enter your real values
```

The required variables are:

```env
BOT_TOKEN=123456789:REPLACE_WITH_YOUR_BOT_TOKEN
ADMIN_GROUP_ID=-1001234567890
ADMIN_IDS=111111111,222222222
BOT_NAME=My Support Bot
WELCOME_MESSAGE=Welcome! Send your message here to contact support.
LOG_LEVEL=INFO
```

`ADMIN_IDS` is a comma-separated list of numeric Telegram user IDs. Do not put usernames there.

`WELCOME_MESSAGE` is plain text. Avoid putting your bot token in any message or log.


## 4. Deploy on Render (recommended: Background Worker)

This is a Telegram polling bot, so deploy it as a **Background Worker**, not a Web Service. It does not need a website, HTTP port, health-check endpoint, database, or persistent disk. The included `render.yaml` is a Render Blueprint configuration.

1. Push the project folder to a GitHub repository. Keep `.env` out of Git; `.gitignore` already excludes it.
2. In Render, choose **New + → Blueprint**, connect the repository, and apply `render.yaml`.
3. In the created worker's **Environment** page, set `BOT_TOKEN`, `ADMIN_GROUP_ID`, and `ADMIN_IDS` to your real values. The secret values are intentionally marked `sync: false` and are not stored in the repository.
4. Deploy and inspect the worker logs. A successful startup logs the bot username and `Polling started`.

If you create the worker manually instead, choose **Background Worker**, runtime **Python 3**, build command `pip install -r requirements.txt`, and start command `python bot.py`. Add the same environment variables in the Environment page. Render supports adding variables in bulk from a valid `.env` file using **Environment → Add from .env**. Do not add a port or web server.

Only run one polling instance for a bot token at a time. Running multiple instances with the same token can cause Telegram polling conflicts.

## 5. Run

```bash
python bot.py
```

Keep the process running with your VPS process manager (for example, systemd, Supervisor, or tmux). Only one polling instance should use a bot token at a time.

## 6. Termux

```bash
pkg update && pkg upgrade
pkg install python
mkdir -p ~/livegrambot && cd ~/livegrambot
# Copy bot.py and requirements.txt into this folder
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

export BOT_TOKEN='YOUR_TOKEN'
export ADMIN_GROUP_ID='-1001234567890'
export ADMIN_IDS='111111111,222222222'
export BOT_NAME='My Support Bot'
export WELCOME_MESSAGE='Welcome! Send your message here to contact support.'

python bot.py
```

Termux must remain active and connected to the internet. For long-running use, a VPS is more reliable.

## 7. Windows PowerShell (optional)

```powershell
$env:BOT_TOKEN = 'YOUR_TOKEN'
$env:ADMIN_GROUP_ID = '-1001234567890'
$env:ADMIN_IDS = '111111111,222222222'
$env:BOT_NAME = 'My Support Bot'
$env:WELCOME_MESSAGE = 'Welcome! Send your message here to contact support.'
py -m pip install -r requirements.txt
py bot.py
```

## 8. Admin workflow

- For a normal incoming user message, reply directly to the forwarded message, or reply to the bot's user-info notice.
- If the user sent content that was already forwarded to them, reply to the bot's user-info notice (not the copied content).
- The bot checks the admin's numeric ID on every reply.
- After attempting delivery, the bot posts a success or failure confirmation in the admin group.

## 9. Statelessness and reliability notes

- No users, message IDs, conversations, or statistics are written to disk.
- Reply targets are resolved from Telegram's `forward_origin` metadata or the user ID embedded in the bot's own group notice. These remain available in Telegram's message history after a bot restart.
- `/stats` reports process uptime only; it does not store historical statistics.
- If the process is offline, updates are only available for the period Telegram retains them (currently up to 24 hours). Because this project intentionally has no durable queue, a crash or prolonged outage can prevent an update from being relayed. This is an unavoidable trade-off of the zero-storage requirement.
