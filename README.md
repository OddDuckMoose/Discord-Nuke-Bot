# Discord-Nuke-Bot
Bot that provides the option for users to erase all of their discord comments and threads nuking their identity from channel
# Nuke_bot

A Discord bot that lets users permanently delete their entire message history from a server — messages, threads, voice channel chat, forum posts, all of it. Built for servers where privacy matters and people should have a clean way to leave without a trace.

---

## How it works

The bot listens in a single designated channel. A user types `nuke my history`, confirms, and the bot works through every channel in the server deleting their messages. It handles Discord's rate limits automatically, processes multiple channels concurrently, and keeps the user updated via DM as it goes. When it's done, it posts a completion summary.

Admins with the `BotAdmin` role have a separate set of commands to pause, resume, or stop nuke operations server-wide.

---

## Setup

**Prerequisites**
- Docker and Docker Compose installed on your host
- A Discord bot token ([create one here](https://discord.com/developers/applications))

**1. Clone the repo**
```bash
git clone https://github.com/yourusername/nuke-bot.git
cd nuke-bot
```

**2. Create your `.env` file**
```bash
cp .env.example .env
```
Open `.env` and add your bot token:
```
DISCORD_TOKEN=your_token_here
```

**3. Configure the bot**

Open `bot.py` and update the two required values at the top:
```python
TRIGGER_CHANNEL = "your-channel-name-here"   # Channel the bot listens in
BOT_ADMIN_ROLE  = "BotAdmin"                  # Role with admin control over the bot
```

**4. Discord Developer Portal**

In your bot's settings at [discord.com/developers](https://discord.com/developers/applications), enable these under **Privileged Gateway Intents**:
- Message Content Intent
- Server Members Intent

**5. Invite the bot**

Use OAuth2 → URL Generator with the `bot` scope and these permissions:
- View Channels
- Read Message History
- Manage Messages
- Manage Threads
- Send Messages

**6. Deploy**
```bash
docker compose up -d --build
docker compose logs -f
```

---

## Commands

| Command | Description |
|---|---|
| `nuke my history` | Starts the nuke sequence — prompts for confirmation first |
| `yes` | Confirms a pending nuke |
| `nuke status` | Shows active progress or your position in the queue |
| `nuke cancel` | Removes you from the queue before your turn starts |
| `nuke help` | Shows the command list |

All commands must be typed in the designated trigger channel.

**BotAdmin only** (not listed in help):

| Command | Description |
|---|---|
| `no nukes` | Cancels all active and queued nukes, locks the bot for 24 hours |
| `nuke it` | Lifts the lockout |
| `nuke nuke` | Deletes the bot's own message history from the trigger channel |

---

## What gets deleted

- All messages in text channels
- Voice channel text chat
- Stage channel text chat
- Forum channel posts
- Threads created by the user (active and archived)

**What cannot be deleted:** Direct messages between users. This is a hard Discord API limitation — bots have no access to DMs regardless of permissions.

---

## Notes

- Messages under 14 days old are bulk deleted (Discord API limit)
- Older messages are deleted individually with adaptive rate limiting
- If multiple users queue at once, they are processed one at a time to avoid token-level rate limit issues
- The bot will DM you per-channel progress updates and a full summary when complete

---

## License

MIT
