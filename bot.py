import discord
import asyncio
import os
import random
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────
# Replace these values to match your server setup before deploying.

TRIGGER_CHANNEL     = "your-channel-name-here"   # Channel the bot listens in
BOT_ADMIN_ROLE      = "BotAdmin"                  # Role name with admin bot control

# ── Tuning (safe defaults — adjust if needed) ──────────────────────────────
BULK_DELETE_CUTOFF  = 14      # Days — Discord hard limit for bulk delete
CHANNEL_CONCURRENCY = 5       # Simultaneous channels to process
MIN_BULK_BATCH      = 25      # Min messages before firing a bulk delete
MAX_BULK_BATCH      = 100     # Max Discord allows per bulk delete
BULK_BATCH_TIMEOUT  = 2.0     # Seconds before sending a partial batch
INITIAL_DELAY       = 0.3     # Starting delete delay (adaptive)
MIN_DELAY           = 0.1     # Floor delay
MAX_DELAY           = 5.0     # Ceiling delay
BACKOFF_FACTOR      = 2.0     # Multiply delay on 429
RECOVERY_FACTOR     = 0.9     # Shrink delay on success
STUCK_THRESHOLD     = 90      # Seconds without activity before flagging stuck
LOCKOUT_HOURS       = 24      # Hours BotAdmin lockout lasts

# ── Bot setup ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True

bot = discord.Client(intents=intents)

# ── Per-guild state ────────────────────────────────────────────────────────
def guild_state(guild_id: int) -> dict:
    if guild_id not in _guild_states:
        _guild_states[guild_id] = {
            "pending_nuke":     {},
            "active_nukes":     {},
            "cancel_flags":     {},
            "nuke_queue":       [],
            "queue_processing": False,
            "locked_until":     None,
        }
    return _guild_states[guild_id]

_guild_states: dict[int, dict] = {}


# ── Channel collection ─────────────────────────────────────────────────────
def get_all_messageable_channels(guild: discord.Guild) -> list:
    channels = []
    channels.extend(guild.text_channels)
    channels.extend(guild.voice_channels)
    channels.extend(guild.stage_channels)
    return channels

async def get_all_threads(guild: discord.Guild, channel: discord.abc.GuildChannel) -> list[discord.Thread]:
    threads = []
    if hasattr(channel, "threads"):
        threads.extend(channel.threads)
    if hasattr(channel, "archived_threads"):
        try:
            async for t in channel.archived_threads(limit=None):
                threads.append(t)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return threads

async def get_all_forum_threads(guild: discord.Guild) -> list[discord.Thread]:
    threads = []
    for forum in guild.forums:
        if hasattr(forum, "threads"):
            threads.extend(forum.threads)
        try:
            async for t in forum.archived_threads(limit=None):
                threads.append(t)
        except (discord.Forbidden, discord.HTTPException):
            pass
    return threads


# ── Helpers ────────────────────────────────────────────────────────────────
def has_bot_admin_role(member: discord.Member) -> bool:
    return any(r.name == BOT_ADMIN_ROLE for r in member.roles)

def is_locked(gs: dict) -> bool:
    if gs["locked_until"] is None:
        return False
    if datetime.now(timezone.utc) >= gs["locked_until"]:
        gs["locked_until"] = None
        return False
    return True

def format_elapsed(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def format_lockout_remaining(gs: dict) -> str:
    if gs["locked_until"] is None:
        return ""
    rem = gs["locked_until"] - datetime.now(timezone.utc)
    s = int(rem.total_seconds())
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"

def queue_position(gs: dict, uid: int) -> int | None:
    for i, (u, _) in enumerate(gs["nuke_queue"]):
        if u.id == uid:
            return i + 1
    return None

async def dm(user: discord.User | discord.Member, msg: str):
    try:
        await user.send(msg)
    except Exception:
        pass

async def notify_queue_positions(gs: dict):
    for i, (u, _) in enumerate(gs["nuke_queue"]):
        pos   = i + 1
        ahead = pos - 1
        await dm(u,
            f"🕐 Queue update: you are now **position {pos}** "
            f"({'next up!' if ahead == 0 else f'{ahead} person(s) ahead of you'}).\n"
            f"Type `nuke cancel` to remove yourself."
        )


# ── Events ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[+] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[+] Watching: #{TRIGGER_CHANNEL} | Concurrency: {CHANNEL_CONCURRENCY}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not hasattr(message.channel, "name"):
        return
    if message.channel.name != TRIGGER_CHANNEL:
        return

    content = message.content.strip().lower()
    uid     = message.author.id
    member  = message.guild.get_member(uid)
    if member is None:
        try:
            member = await message.guild.fetch_member(uid)
        except Exception:
            member = None
    gs = guild_state(message.guild.id)

    # ── BotAdmin: no nukes ─────────────────────────────────────────────────
    if content == "no nukes":
        if not has_bot_admin_role(member):
            return
        gs["locked_until"] = datetime.now(timezone.utc) + timedelta(hours=LOCKOUT_HOURS)
        for active_uid in list(gs["active_nukes"].keys()):
            gs["cancel_flags"][active_uid] = True
        killed = len(gs["active_nukes"])
        queued = len(gs["nuke_queue"])
        gs["active_nukes"].clear()
        gs["pending_nuke"].clear()
        gs["nuke_queue"].clear()
        gs["queue_processing"] = False
        print(f"[!] BotAdmin {message.author} issued NO NUKES in guild {message.guild.id}.")
        quips = [
            f"🔒 Calm down, keyboard warriors. Nukes locked for **{LOCKOUT_HOURS} hours**. {killed} active job(s) terminated, {queued} queued job(s) cleared. BotAdmin {message.author.mention} has spoken — go touch grass.",
            f"🔒 Nuke button confiscated. {LOCKOUT_HOURS}-hour timeout. {killed} running + {queued} queued job(s) squashed. BotAdmin {message.author.mention} is the only adult here apparently.",
            f"🔒 Put down the delete key. Nukes disabled for **{LOCKOUT_HOURS} hours** by BotAdmin {message.author.mention}. {killed} active + {queued} queued job(s) stopped. Nobody's nuking anything on their watch.",
            f"🔒 BotAdmin {message.author.mention} entered the chat and ruined everyone's fun. Nukes offline **{LOCKOUT_HOURS} hours**. {killed} active + {queued} queued cancelled. You're welcome.",
            f"🔒 BotAdmin has decreed: no more chaos today. {killed} running + {queued} queued nuke(s) extinguished. Lockout: **{LOCKOUT_HOURS} hours**. {message.author.mention} is clearly the only responsible one here.",
            f"🔒 NUKE LOCKDOWN by BotAdmin {message.author.mention}. {killed} running + {queued} queued job(s) killed dead. **{LOCKOUT_HOURS} hours** of mandatory chill begins now.",
            f"🔒 BotAdmin {message.author.mention} confiscated the big red button. {killed} active + {queued} queued cancelled. No nukes for **{LOCKOUT_HOURS} hours**.",
            f"🔒 Server under BotAdmin protection by {message.author.mention}. {killed} nuke(s) stopped mid-flight, {queued} in queue cleared. **{LOCKOUT_HOURS}-hour** ceasefire now in effect.",
            f"🔒 {message.author.mention} said 'not today' and meant it. {killed} active + {queued} queued grounded. Nuking suspended **{LOCKOUT_HOURS} hours**.",
            f"🔒 BotAdmin {message.author.mention} pulled the emergency brake. {killed} running + {queued} queued halted. **{LOCKOUT_HOURS}-hour** ban is live.",
        ]
        await message.channel.send(random.choice(quips))
        return

    # ── BotAdmin: nuke it ──────────────────────────────────────────────────
    if content == "nuke it":
        if not has_bot_admin_role(member):
            return
        gs["locked_until"] = None
        print(f"[+] BotAdmin {message.author} lifted lockout in guild {message.guild.id}.")
        ac = len(gs["active_nukes"])
        qc = len(gs["nuke_queue"])
        quips = [
            f"🔓 BotAdmin {message.author.mention} re-armed the nukes. Lockout lifted. {ac} active, {qc} queued. May God have mercy on your message history.",
            f"🔓 Ban lifted. Nuke away, you beautiful disasters. BotAdmin {message.author.mention} restored the chaos. {ac} active, {qc} in queue.",
            f"🔓 {message.author.mention} handed the keys back to the inmates. Nukes live. {ac} active + {qc} queued. You asked for this.",
            f"🔓 BotAdmin {message.author.mention} re-enabled total message annihilation. {ac} active, {qc} queued. Godspeed to your chat history.",
            f"🔓 Green light from BotAdmin {message.author.mention}. Nuke functions restored. {ac} active, {qc} in queue. The delete button is back, baby.",
            f"🔓 {message.author.mention} has spoken: let there be nukes. Lockout cleared. {ac} active, {qc} queued. Server chaos resumes.",
            f"🔓 BotAdmin {message.author.mention} decided y'all can be trusted again. Bold choice. {ac} active, {qc} queued.",
            f"🔓 The admin giveth. Nukes back online per {message.author.mention}. {ac} active, {qc} in queue.",
            f"🔓 {message.author.mention} lifted the ceasefire. Nuke functions: **ONLINE**. {ac} active, {qc} queued.",
            f"🔓 Lockout terminated by BotAdmin {message.author.mention}. Nuke bay doors open. {ac} active, {qc} queued. Don't make them regret this.",
        ]
        await message.channel.send(random.choice(quips))
        return

    # ── BotAdmin: nuke nuke (delete bot's own messages) ───────────────────
    if content == "nuke nuke":
        if not has_bot_admin_role(member):
            return
        print(f"[!] BotAdmin {message.author} triggered NUKE NUKE in guild {message.guild.id}.")
        quips = [
            "🫧 Eating my own words. Every last one. Don't mind me.",
            "🧹 Sweeping my mess under the rug. Nothing to see here.",
            "🫣 Pretending I was never here. Classic.",
            "🗑️ Deleting the evidence. BotAdmin has spoken.",
            "💨 And just like that... I never existed.",
            "🤫 What messages? I don't see any messages.",
            "😶 Taking myself out back. It's fine. I'm fine.",
            "🧽 Scrubbing myself from history. This is fine.",
            "👻 Going ghost. Literally.",
            "🪦 This is my last message. Probably. Maybe. Bye.",
        ]
        farewell_msg = await message.channel.send(random.choice(quips))
        asyncio.create_task(nuke_bot_messages(guild=message.guild, farewell_id=farewell_msg.id))
        return

    # ── Help ───────────────────────────────────────────────────────────────
    if content in ("nuke help", "help", "commands", "nuke commands", "show commands"):
        await message.channel.send(
            "**🤖 Nuke_bot Commands**\n\n"
            "`nuke my history` — Permanently deletes **all your messages and threads** across every channel including voice and forum channels.\n"
            "`yes` — Confirm a pending nuke.\n"
            "`nuke status` — Check active progress or your queue position.\n"
            "`nuke cancel` — Remove yourself from the queue before your turn.\n"
            "`nuke help` — Show this command list.\n\n"
            "⚠️ **All commands must be typed in this channel.**\n"
            "⚠️ Deletion is permanent and cannot be undone.\n"
            "⚠️ DMs between users cannot be deleted by this bot — that is a Discord API limitation."
        )
        return

    # ── Cancel ─────────────────────────────────────────────────────────────
    if content == "nuke cancel":
        if uid in gs["active_nukes"]:
            await message.channel.send(
                f"{message.author.mention} Your nuke is already running and cannot be cancelled."
            )
            return
        pos = queue_position(gs, uid)
        if pos is not None:
            gs["nuke_queue"][:] = [(u, ch) for u, ch in gs["nuke_queue"] if u.id != uid]
            await message.channel.send(f"{message.author.mention} ✅ Removed from queue. Your messages are safe.")
            await notify_queue_positions(gs)
        elif uid in gs["pending_nuke"]:
            del gs["pending_nuke"][uid]
            await message.channel.send(f"{message.author.mention} ✅ Pending nuke cancelled.")
        else:
            await message.channel.send(f"{message.author.mention} No pending nuke or queue position found.")
        return

    # ── Status ─────────────────────────────────────────────────────────────
    if content == "nuke status":
        if uid in gs["active_nukes"]:
            s       = gs["active_nukes"][uid]
            elapsed = format_elapsed(datetime.now(timezone.utc) - s["start"])
            idle    = (datetime.now(timezone.utc) - s["last_activity"]).total_seconds()
            chs     = ", ".join(f"`#{c}`" for c in s["active_channels"]) or "none"
            if idle > STUCK_THRESHOLD:
                await message.channel.send(
                    f"{message.author.mention} ⚠️ **Nuke may be stuck.**\n"
                    f"Last activity **{int(idle)}s ago**. Active: {chs}\n"
                    f"Deleted: **{s['deleted']}** | Running: **{elapsed}**"
                )
            else:
                await message.channel.send(
                    f"{message.author.mention} ⚙️ **Nuke in progress.**\n"
                    f"Active: {chs} | Deleted: **{s['deleted']}** | Running: **{elapsed}**"
                )
        else:
            pos = queue_position(gs, uid)
            if pos is not None:
                ahead = pos - 1
                await message.channel.send(
                    f"{message.author.mention} 🕐 Position **{pos}** in queue "
                    f"({'next up!' if ahead == 0 else f'{ahead} ahead of you'}).\n"
                    f"Type `nuke cancel` to remove yourself."
                )
            else:
                await message.channel.send(f"{message.author.mention} No active nuke or queue position found.")
        return

    # ── Trigger ────────────────────────────────────────────────────────────
    if content == "nuke my history":
        if is_locked(gs):
            await message.channel.send(
                f"{message.author.mention} ❌ Nukes disabled. Try again in **{format_lockout_remaining(gs)}**."
            )
            return
        if uid in gs["active_nukes"]:
            await message.channel.send(f"{message.author.mention} Already running. Type `nuke status`.")
            return
        if queue_position(gs, uid) is not None:
            await message.channel.send(
                f"{message.author.mention} Already in queue at position **{queue_position(gs, uid)}**."
            )
            return
        gs["pending_nuke"][uid] = True
        await message.channel.send(
            f"{message.author.mention} ⚠️ **Are you sure?**\n"
            f"This permanently deletes **all your messages and threads** across text, voice, stage, and forum channels. No undo.\n\n"
            f"Reply `yes` to confirm, or anything else to cancel."
        )
        return

    # ── Confirmation ───────────────────────────────────────────────────────
    if uid in gs["pending_nuke"]:
        del gs["pending_nuke"][uid]
        if content == "yes":
            if is_locked(gs):
                await message.channel.send(
                    f"{message.author.mention} ❌ Locked before you confirmed. Try in **{format_lockout_remaining(gs)}**."
                )
                return
            gs["nuke_queue"].append((message.author, message.channel))
            pos = queue_position(gs, uid)
            if pos == 1 and not gs["queue_processing"]:
                await message.channel.send(
                    f"{message.author.mention} 💀 **Nuke initiated.** Covering text, voice, stage, and forum channels.\n"
                    f"Type `nuke status` to check progress."
                )
            else:
                await message.channel.send(
                    f"{message.author.mention} ✅ **Queued at position {pos}.** You'll receive a DM when it's your turn."
                )
            if not gs["queue_processing"]:
                asyncio.create_task(process_queue(message.guild, gs))
        else:
            await message.channel.send(f"{message.author.mention} Nuke cancelled.")


# ── Nuke bot's own messages ────────────────────────────────────────────────
async def nuke_bot_messages(guild: discord.Guild, farewell_id: int):
    channel = discord.utils.get(guild.text_channels, name=TRIGGER_CHANNEL)
    if channel is None:
        print(f"[!] nuke_nuke: #{TRIGGER_CHANNEL} not found in guild {guild.id}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=BULK_DELETE_CUTOFF)
    recent_batch: list[discord.Message] = []
    deleted = 0

    try:
        page_count = 0
        async for msg in channel.history(limit=None, oldest_first=False):
            page_count += 1
            if page_count % 100 == 0:
                await asyncio.sleep(1.0)

            if msg.author.id != bot.user.id:
                continue
            if msg.id == farewell_id:
                continue

            if msg.created_at > cutoff:
                recent_batch.append(msg)
                if len(recent_batch) >= MAX_BULK_BATCH:
                    try:
                        await channel.delete_messages(recent_batch)
                        deleted += len(recent_batch)
                    except Exception as ex:
                        print(f"[!] nuke_nuke bulk error: {ex}")
                    recent_batch = []
                    await asyncio.sleep(1.0)
            else:
                if recent_batch:
                    try:
                        await channel.delete_messages(recent_batch)
                        deleted += len(recent_batch)
                    except Exception as ex:
                        print(f"[!] nuke_nuke bulk error: {ex}")
                    recent_batch = []
                try:
                    await msg.delete()
                    deleted += 1
                except Exception:
                    pass
                await asyncio.sleep(0.75)

        if recent_batch:
            try:
                await channel.delete_messages(recent_batch)
                deleted += len(recent_batch)
            except Exception as ex:
                print(f"[!] nuke_nuke flush error: {ex}")

    except discord.Forbidden:
        print(f"[!] nuke_nuke: no permission in #{TRIGGER_CHANNEL}")
    except Exception as ex:
        print(f"[!] nuke_nuke error: {ex}")

    try:
        farewell_msg = await channel.fetch_message(farewell_id)
        await asyncio.sleep(0.5)
        await farewell_msg.delete()
        deleted += 1
    except Exception:
        pass

    print(f"[+] nuke_nuke complete — {deleted} bot message(s) purged from #{TRIGGER_CHANNEL} in guild {guild.id}.")


# ── Queue processor ────────────────────────────────────────────────────────
async def process_queue(guild: discord.Guild, gs: dict):
    gs["queue_processing"] = True
    while gs["nuke_queue"]:
        user, status_channel = gs["nuke_queue"][0]
        if queue_position(gs, user.id) and queue_position(gs, user.id) > 0:
            await dm(user,
                f"🚀 **It's your turn — nuke starting now.**\n"
                f"Type `nuke status` in #{TRIGGER_CHANNEL} to check progress."
            )
        await nuke_user_messages(guild, user, status_channel, gs)
        if gs["nuke_queue"] and gs["nuke_queue"][0][0].id == user.id:
            gs["nuke_queue"].pop(0)
        if gs["nuke_queue"]:
            await notify_queue_positions(gs)
    gs["queue_processing"] = False


# ── Core nuke ──────────────────────────────────────────────────────────────
async def nuke_user_messages(
    guild: discord.Guild,
    user: discord.Member | discord.User,
    status_channel: discord.TextChannel,
    gs: dict,
):
    uid = user.id
    now = datetime.now(timezone.utc)
    gs["cancel_flags"][uid] = False
    gs["active_nukes"][uid] = {
        "start":           now,
        "active_channels": [],
        "deleted":         0,
        "last_activity":   now,
    }

    total_deleted   = 0
    threads_deleted = 0
    semaphore       = asyncio.Semaphore(CHANNEL_CONCURRENCY)
    lock            = asyncio.Lock()
    cutoff          = now - timedelta(days=BULK_DELETE_CUTOFF)
    delay           = [INITIAL_DELAY]

    def is_cancelled() -> bool:
        return gs["cancel_flags"].get(uid, False)

    def touch():
        if uid in gs["active_nukes"]:
            gs["active_nukes"][uid]["last_activity"] = datetime.now(timezone.utc)

    async def add_ch(name: str):
        if uid in gs["active_nukes"]:
            gs["active_nukes"][uid]["active_channels"].append(name)

    async def rem_ch(name: str):
        if uid in gs["active_nukes"]:
            try:
                gs["active_nukes"][uid]["active_channels"].remove(name)
            except ValueError:
                pass

    async def add_deleted(count: int):
        nonlocal total_deleted
        async with lock:
            total_deleted += count
            if uid in gs["active_nukes"]:
                gs["active_nukes"][uid]["deleted"] = total_deleted

    async def flush_bulk(ch, batch: list[discord.Message]) -> int:
        if not batch:
            return 0
        attempts = 0
        while attempts < 5:
            if is_cancelled():
                return 0
            try:
                await ch.delete_messages(batch)
                delay[0] = max(MIN_DELAY, delay[0] * RECOVERY_FACTOR)
                touch()
                return len(batch)
            except discord.HTTPException as e:
                if e.status == 429:
                    wait = float(e.response.headers.get("Retry-After", delay[0] * BACKOFF_FACTOR))
                    delay[0] = min(MAX_DELAY, delay[0] * BACKOFF_FACTOR)
                    print(f"[~] Bulk 429 #{ch.name}, sleeping {wait:.2f}s")
                    await asyncio.sleep(wait)
                    attempts += 1
                else:
                    print(f"[!] Bulk HTTP {e.status} in #{ch.name}")
                    return 0
            except Exception as ex:
                print(f"[!] Bulk error: {ex}")
                return 0
        return 0

    async def delete_single(msg: discord.Message) -> int:
        attempts = 0
        while attempts < 5:
            if is_cancelled():
                return 0
            try:
                await msg.delete()
                delay[0] = max(MIN_DELAY, delay[0] * RECOVERY_FACTOR)
                touch()
                return 1
            except discord.HTTPException as e:
                if e.status == 429:
                    wait = float(e.response.headers.get("Retry-After", delay[0] * BACKOFF_FACTOR))
                    delay[0] = min(MAX_DELAY, delay[0] * BACKOFF_FACTOR)
                    print(f"[~] Single 429, sleeping {wait:.2f}s")
                    await asyncio.sleep(wait)
                    attempts += 1
                elif e.status == 404:
                    return 1
                else:
                    print(f"[!] Single HTTP {e.status}")
                    return 0
            except Exception as ex:
                print(f"[!] Single error: {ex}")
                return 0
        return 0

    async def process_channel(channel):
        async with semaphore:
            if is_cancelled():
                return
            bot_member = guild.get_member(bot.user.id)
            perms = channel.permissions_for(bot_member)
            if not (perms.read_message_history and perms.manage_messages):
                return

            await add_ch(channel.name)
            ch_deleted = 0

            try:
                recent_batch: list[discord.Message] = []
                batch_timer_task = None

                async def flush_on_timeout():
                    await asyncio.sleep(BULK_BATCH_TIMEOUT)
                    nonlocal recent_batch, ch_deleted
                    if recent_batch:
                        d = await flush_bulk(channel, recent_batch)
                        ch_deleted += d
                        await add_deleted(d)
                        recent_batch = []

                async for msg in channel.history(limit=None, oldest_first=False):
                    if is_cancelled():
                        break
                    if msg.author.id != uid:
                        continue
                    touch()

                    if msg.created_at > cutoff:
                        recent_batch.append(msg)
                        if batch_timer_task:
                            batch_timer_task.cancel()
                        if len(recent_batch) >= MAX_BULK_BATCH:
                            d = await flush_bulk(channel, recent_batch)
                            ch_deleted += d
                            await add_deleted(d)
                            recent_batch = []
                            batch_timer_task = None
                        elif len(recent_batch) >= MIN_BULK_BATCH:
                            batch_timer_task = asyncio.create_task(flush_on_timeout())
                    else:
                        if recent_batch:
                            if batch_timer_task:
                                batch_timer_task.cancel()
                                batch_timer_task = None
                            d = await flush_bulk(channel, recent_batch)
                            ch_deleted += d
                            await add_deleted(d)
                            recent_batch = []
                        d = await delete_single(msg)
                        ch_deleted += d
                        await add_deleted(d)
                        await asyncio.sleep(delay[0])

                if recent_batch and not is_cancelled():
                    if batch_timer_task:
                        batch_timer_task.cancel()
                    d = await flush_bulk(channel, recent_batch)
                    ch_deleted += d
                    await add_deleted(d)

            except discord.Forbidden:
                pass
            except Exception as ex:
                print(f"[!] Error #{channel.name}: {ex}")
            finally:
                await rem_ch(channel.name)

            if ch_deleted > 0:
                print(f"[+] #{channel.name}: {ch_deleted} deleted")
                await dm(user, f"🗑️ `#{channel.name}` — {ch_deleted} message(s) deleted")

    # ── Phase 1: All messageable channels ─────────────────────────────────
    all_channels = get_all_messageable_channels(guild)
    await asyncio.gather(*[process_channel(ch) for ch in all_channels])

    # ── Phase 2: Threads on text/voice/stage + forum posts ─────────────────
    if not is_cancelled():
        thread_list = []
        for channel in all_channels:
            if is_cancelled():
                break
            threads = await get_all_threads(guild, channel)
            thread_list.extend(threads)
        if not is_cancelled():
            forum_threads = await get_all_forum_threads(guild)
            thread_list.extend(forum_threads)
        seen = set()
        unique_threads = []
        for t in thread_list:
            if t.id not in seen:
                seen.add(t.id)
                unique_threads.append(t)
        await asyncio.gather(*[process_channel(t) for t in unique_threads])

    # ── Phase 3: Delete threads/posts created by user ──────────────────────
    if not is_cancelled():
        all_threads_for_deletion = []
        for channel in get_all_messageable_channels(guild):
            if is_cancelled():
                break
            threads = await get_all_threads(guild, channel)
            all_threads_for_deletion.extend(threads)
        forum_threads = await get_all_forum_threads(guild)
        all_threads_for_deletion.extend(forum_threads)
        seen = set()
        for thread in all_threads_for_deletion:
            if thread.id in seen or is_cancelled():
                break
            seen.add(thread.id)
            if thread.owner_id == uid:
                try:
                    await thread.delete()
                    threads_deleted += 1
                    touch()
                    await asyncio.sleep(delay[0])
                except discord.Forbidden:
                    pass
                except Exception as ex:
                    print(f"[!] Thread delete error: {ex}")

    # ── Wrap up ────────────────────────────────────────────────────────────
    was_cancelled = is_cancelled()
    gs["active_nukes"].pop(uid, None)
    gs["cancel_flags"].pop(uid, None)
    elapsed = format_elapsed(datetime.now(timezone.utc) - now)

    if was_cancelled:
        await status_channel.send(
            f"{user.mention} 🛑 **Nuke stopped by BotAdmin.**\n"
            f"Deleted **{total_deleted}** message(s) and **{threads_deleted}** thread(s) before halt."
        )
    else:
        await status_channel.send(
            f"{user.mention} ✅ **Done.** Nuked **{total_deleted}** message(s) and "
            f"**{threads_deleted}** thread(s) in {elapsed}."
        )
        await dm(user,
            f"✅ **Nuke complete.**\n"
            f"Messages deleted: **{total_deleted}**\n"
            f"Threads deleted: **{threads_deleted}**\n"
            f"Total time: **{elapsed}**\n\n"
            f"⚠️ Note: DMs between users cannot be deleted by this bot — that is a hard Discord API limitation."
        )


# ── Run ────────────────────────────────────────────────────────────────────
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN environment variable not set.")

bot.run(token)
