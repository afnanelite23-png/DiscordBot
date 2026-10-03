import os
import re
import time
import sqlite3
import threading
from datetime import timedelta
from collections import defaultdict, deque

import discord
from discord.ext import commands
from flask import Flask

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing from Render Environment Variables."
    )

PREFIX = ">"
DATABASE = "security.db"
BOT_OWNER_ID = None

# =========================================================
# RENDER WEB SERVER
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Discord Security Bot is online!"


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_web_server, daemon=True).start()

# =========================================================
# SECURITY SETTINGS
# =========================================================

DEFAULT_SETTINGS = {
    "antispam": 1,
    "antilink": 1,
    "antiraid": 1,
    "antibot": 1,
    "antinuke": 1,
    "log_channel": 0,
}

SPAM_MESSAGE_LIMIT = 6
SPAM_TIME_WINDOW = 5

RAID_JOIN_LIMIT = 8
RAID_TIME_WINDOW = 10

# 4 destructive actions in 10 seconds = severe nuke activity.
NUKE_ACTION_LIMIT = 4
NUKE_TIME_WINDOW = 10

# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DATABASE, check_same_thread=False)
cursor = db.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        antispam INTEGER DEFAULT 1,
        antilink INTEGER DEFAULT 1,
        antiraid INTEGER DEFAULT 1,
        antibot INTEGER DEFAULT 1,
        antinuke INTEGER DEFAULT 1,
        log_channel INTEGER DEFAULT 0
    )
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS managers (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        added_by INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """
)

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS whitelist (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        added_by INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
    """
)

db.commit()


def ensure_guild(guild_id):
    cursor.execute(
        "SELECT guild_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,),
    )

    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO guild_settings
            (
                guild_id,
                antispam,
                antilink,
                antiraid,
                antibot,
                antinuke,
                log_channel
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                DEFAULT_SETTINGS["antispam"],
                DEFAULT_SETTINGS["antilink"],
                DEFAULT_SETTINGS["antiraid"],
                DEFAULT_SETTINGS["antibot"],
                DEFAULT_SETTINGS["antinuke"],
                DEFAULT_SETTINGS["log_channel"],
            ),
        )
        db.commit()


def get_settings(guild_id):
    ensure_guild(guild_id)

    cursor.execute(
        """
        SELECT antispam, antilink, antiraid, antibot, antinuke, log_channel
        FROM guild_settings
        WHERE guild_id = ?
        """,
        (guild_id,),
    )

    row = cursor.fetchone()

    return {
        "antispam": bool(row[0]),
        "antilink": bool(row[1]),
        "antiraid": bool(row[2]),
        "antibot": bool(row[3]),
        "antinuke": bool(row[4]),
        "log_channel": row[5],
    }


def update_setting(guild_id, setting, value):
    ensure_guild(guild_id)

    allowed = {
        "antispam",
        "antilink",
        "antiraid",
        "antibot",
        "antinuke",
    }

    if setting not in allowed:
        return False

    cursor.execute(
        f"""
        UPDATE guild_settings
        SET {setting} = ?
        WHERE guild_id = ?
        """,
        (int(value), guild_id),
    )

    db.commit()
    return True


def set_log_channel(guild_id, channel_id):
    ensure_guild(guild_id)

    cursor.execute(
        """
        UPDATE guild_settings
        SET log_channel = ?
        WHERE guild_id = ?
        """,
        (channel_id, guild_id),
    )

    db.commit()


def add_manager(guild_id, user_id, added_by):
    cursor.execute(
        """
        INSERT OR REPLACE INTO managers (guild_id, user_id, added_by)
        VALUES (?, ?, ?)
        """,
        (guild_id, user_id, added_by),
    )
    db.commit()


def remove_manager(guild_id, user_id):
    cursor.execute(
        "DELETE FROM managers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


def is_manager_sync(guild_id, user_id):
    cursor.execute(
        "SELECT 1 FROM managers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return cursor.fetchone() is not None


def add_whitelist(guild_id, user_id, added_by):
    cursor.execute(
        """
        INSERT OR REPLACE INTO whitelist (guild_id, user_id, added_by)
        VALUES (?, ?, ?)
        """,
        (guild_id, user_id, added_by),
    )
    db.commit()


def remove_whitelist(guild_id, user_id):
    cursor.execute(
        "DELETE FROM whitelist WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


def is_whitelisted(guild_id, user_id):
    cursor.execute(
        "SELECT 1 FROM whitelist WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return cursor.fetchone() is not None


def get_managers(guild_id):
    cursor.execute(
        "SELECT user_id FROM managers WHERE guild_id = ? ORDER BY user_id",
        (guild_id,),
    )
    return [row[0] for row in cursor.fetchall()]


def get_whitelist(guild_id):
    cursor.execute(
        "SELECT user_id FROM whitelist WHERE guild_id = ? ORDER BY user_id",
        (guild_id,),
    )
    return [row[0] for row in cursor.fetchall()]

# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
)

# =========================================================
# MEMORY / RATE LIMITS
# =========================================================

user_messages = defaultdict(deque)
guild_joins = defaultdict(deque)
nuke_actions = defaultdict(lambda: defaultdict(deque))
punishing = set()

# =========================================================
# EMBEDS
# =========================================================


def security_embed(title, description, color=discord.Color.blurple()):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Security System")
    return embed


async def send_log(guild, embed):
    settings = get_settings(guild.id)
    channel_id = settings["log_channel"]

    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        return

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

# =========================================================
# OWNER / MANAGER / WHITELIST
# =========================================================


async def get_bot_owner_id():
    global BOT_OWNER_ID

    if BOT_OWNER_ID is not None:
        return BOT_OWNER_ID

    try:
        application = await bot.application_info()
        if application.owner:
            BOT_OWNER_ID = application.owner.id
    except (discord.HTTPException, discord.Forbidden):
        pass

    return BOT_OWNER_ID


async def is_bot_owner(user_id):
    owner_id = await get_bot_owner_id()
    return owner_id is not None and user_id == owner_id


async def is_authorized_manager(guild_id, user_id):
    if await is_bot_owner(user_id):
        return True
    return is_manager_sync(guild_id, user_id)


async def punish_unauthorized_admin(guild, member, reason):
    """Kick an unauthorized administrator and notify the server owner."""

    if member.id == guild.owner_id:
        return False

    if await is_bot_owner(member.id):
        return False

    if is_manager_sync(guild.id, member.id):
        return False

    dm_sent = False

    owner = guild.owner
    if owner and owner.id != member.id:
        try:
            embed = security_embed(
                "🚨 Unauthorized Administrator Action",
                (
                    f"**Server:** {guild.name}\n"
                    f"**User:** {member} (`{member.id}`)\n"
                    f"**Action:** {reason}\n\n"
                    "The user was detected as an administrator without "
                    "Security Manager access."
                ),
                discord.Color.red(),
            )
            await owner.send(embed=embed)
            dm_sent = True
        except (discord.Forbidden, discord.HTTPException):
            pass

    kicked = False
    try:
        await member.kick(reason=f"Security: unauthorized administrator action - {reason}")
        kicked = True
    except (discord.Forbidden, discord.HTTPException):
        pass

    await send_log(
        guild,
        security_embed(
            "🚨 Unauthorized Admin Blocked",
            (
                f"**User:** {member.mention}\n"
                f"**Action:** {reason}\n"
                f"**Kicked:** {'Yes' if kicked else 'No - hierarchy/permissions'}\n"
                f"**Owner DM:** {'Sent' if dm_sent else 'Failed/Unavailable'}"
            ),
            discord.Color.red(),
        ),
    )

    return kicked


async def manager_check(ctx):
    if ctx.guild is None:
        return False

    if await is_authorized_manager(ctx.guild.id, ctx.author.id):
        return True

    # Administrators are deliberately NOT allowed to use security commands.
    if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
        await punish_unauthorized_admin(
            ctx.guild,
            ctx.author,
            f"Attempted to use `{ctx.message.content.split()[0]}`",
        )

    return False


def manager_only():
    return commands.check(manager_check)


async def owner_check(ctx):
    if ctx.guild is None:
        return False

    if await is_bot_owner(ctx.author.id):
        return True

    if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator:
        await punish_unauthorized_admin(
            ctx.guild,
            ctx.author,
            f"Attempted to use `{ctx.message.content.split()[0]}`",
        )

    return False


def bot_owner_only():
    return commands.check(owner_check)

# =========================================================
# BOT READY
# =========================================================


@bot.event
async def on_ready():
    global BOT_OWNER_ID

    try:
        application = await bot.application_info()
        if application.owner:
            BOT_OWNER_ID = application.owner.id
    except (discord.HTTPException, discord.Forbidden):
        pass

    print("=" * 50)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Bot Owner ID: {BOT_OWNER_ID}")
    print(f"Servers: {len(bot.guilds)}")
    print("=" * 50)

    try:
        await bot.change_presence(
            activity=discord.Game(name=f"{PREFIX}security")
        )
    except Exception as error:
        print(f"Presence error: {error}")

# =========================================================
# GUILD JOIN
# =========================================================


@bot.event
async def on_guild_join(guild):
    ensure_guild(guild.id)
    print(f"Joined server: {guild.name} ({guild.id})")

# =========================================================
# MEMBER JOIN
# =========================================================


@bot.event
async def on_member_join(member):
    guild = member.guild
    settings = get_settings(guild.id)

    # =====================================================
    # ANTI BOT
    # =====================================================

    if settings["antibot"] and member.bot:
        if member.id != bot.user.id and not is_whitelisted(guild.id, member.id):
            try:
                await member.kick(reason="Security: unauthorized bot detected")

                await send_log(
                    guild,
                    security_embed(
                        "🤖 Unauthorized Bot Blocked",
                        (
                            f"{member.mention} was removed because anti-bot "
                            "protection is enabled."
                        ),
                        discord.Color.red(),
                    ),
                )
            except discord.Forbidden:
                await send_log(
                    guild,
                    security_embed(
                        "⚠️ Anti-Bot Failed",
                        (
                            f"I couldn't remove {member.mention}.\n\n"
                            "Make sure my role is above the bot's role."
                        ),
                        discord.Color.orange(),
                    ),
                )
            except discord.HTTPException:
                pass

            return

    # =====================================================
    # ANTI RAID
    # =====================================================

    if settings["antiraid"]:
        now = time.monotonic()
        joins = guild_joins[guild.id]
        joins.append(now)

        while joins and now - joins[0] > RAID_TIME_WINDOW:
            joins.popleft()

        if len(joins) >= RAID_JOIN_LIMIT:
            await send_log(
                guild,
                security_embed(
                    "🚨 RAID DETECTED",
                    (
                        f"**{len(joins)} members** joined within "
                        f"**{RAID_TIME_WINDOW} seconds**."
                    ),
                    discord.Color.dark_red(),
                ),
            )

# =========================================================
# MESSAGE SECURITY
# =========================================================

INVITE_REGEX = re.compile(
    r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)",
    re.IGNORECASE,
)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        await bot.process_commands(message)
        return

    guild = message.guild
    settings = get_settings(guild.id)

    # Bot owner, managers and whitelisted users bypass automatic message protection.
    authorized = await is_authorized_manager(guild.id, message.author.id)
    whitelisted = is_whitelisted(guild.id, message.author.id)

    if not authorized and not whitelisted:
        # =====================================================
        # ANTI INVITE
        # =====================================================

        if settings["antilink"] and INVITE_REGEX.search(message.content):
            try:
                await message.delete()

                warning = await message.channel.send(
                    embed=security_embed(
                        "🔗 Invite Removed",
                        f"{message.author.mention}, Discord invites aren't allowed here.",
                        discord.Color.orange(),
                    )
                )
                await warning.delete(delay=5)

                await send_log(
                    guild,
                    security_embed(
                        "🔗 Invite Blocked",
                        f"Removed a Discord invite sent by {message.author.mention}.",
                        discord.Color.orange(),
                    ),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

            return

        # =====================================================
        # ANTI SPAM
        # =====================================================

        if settings["antispam"]:
            now = time.monotonic()
            messages = user_messages[(guild.id, message.author.id)]
            messages.append(now)

            while messages and now - messages[0] > SPAM_TIME_WINDOW:
                messages.popleft()

            if len(messages) >= SPAM_MESSAGE_LIMIT:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

                if message.author.id not in punishing:
                    punishing.add(message.author.id)

                    try:
                        timeout = discord.utils.utcnow() + timedelta(seconds=30)
                        await message.author.edit(
                            timed_out_until=timeout,
                            reason="Security: spam detected",
                        )

                        await send_log(
                            guild,
                            security_embed(
                                "🚨 Spam Detected",
                                (
                                    f"{message.author.mention} was timed out for "
                                    "**30 seconds** because of spam."
                                ),
                                discord.Color.red(),
                            ),
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                    finally:
                        punishing.discard(message.author.id)

                return

    await bot.process_commands(message)

# =========================================================
# ANTI NUKE
# =========================================================

DANGEROUS_ACTIONS = {
    "channel_delete",
    "role_delete",
}


async def anti_nuke_check(guild, user_id, action):
    settings = get_settings(guild.id)

    if not settings["antinuke"]:
        return False

    if action not in DANGEROUS_ACTIONS:
        return False

    # Never punish the server owner or bot owner.
    if user_id == guild.owner_id or await is_bot_owner(user_id):
        return False

    # Managers are trusted for security administration.
    if is_manager_sync(guild.id, user_id):
        return False

    now = time.monotonic()
    actions = nuke_actions[guild.id][user_id]
    actions.append(now)

    while actions and now - actions[0] > NUKE_TIME_WINDOW:
        actions.popleft()

    if len(actions) < NUKE_ACTION_LIMIT:
        return False

    member = guild.get_member(user_id)
    if member is None:
        return False

    severe = len(actions) >= NUKE_ACTION_LIMIT
    is_admin = member.guild_permissions.administrator

    if severe and is_admin:
        await punish_unauthorized_admin(
            guild,
            member,
            f"Severe anti-nuke activity: {len(actions)} destructive actions in {NUKE_TIME_WINDOW}s",
        )

        await send_log(
            guild,
            security_embed(
                "☢️ SEVERE NUKE BLOCKED",
                (
                    f"{member.mention} triggered the severe anti-nuke threshold.\n\n"
                    f"**Actions:** `{len(actions)}`\n"
                    f"**Window:** `{NUKE_TIME_WINDOW}s`\n"
                    f"**Action:** `{action}`\n"
                    "The administrator was removed if Discord permissions allowed it."
                ),
                discord.Color.dark_red(),
            ),
        )
        return True

    await send_log(
        guild,
        security_embed(
            "⚠️ Suspicious Destructive Activity",
            (
                f"**User:** {member.mention}\n"
                f"**Action:** `{action}`\n"
                f"**Actions detected:** `{len(actions)}`"
            ),
            discord.Color.orange(),
        ),
    )

    return False

# =========================================================
# CHANNEL DELETE
# =========================================================


@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild

    try:
        async for entry in guild.audit_logs(
            limit=5,
            action=discord.AuditLogAction.channel_delete,
        ):
            if entry.target and entry.target.id == channel.id:
                user = entry.user
                triggered = await anti_nuke_check(
                    guild,
                    user.id,
                    "channel_delete",
                )

                await send_log(
                    guild,
                    security_embed(
                        "🗑️ Channel Deleted",
                        (
                            f"**Channel:** `{channel.name}`\n"
                            f"**Deleted by:** {user.mention}\n"
                            f"**Anti-Nuke:** {'TRIGGERED' if triggered else 'Monitored'}"
                        ),
                        discord.Color.red(),
                    ),
                )
                break
    except (discord.Forbidden, discord.HTTPException):
        pass

# =========================================================
# ROLE DELETE
# =========================================================


@bot.event
async def on_guild_role_delete(role):
    guild = role.guild

    try:
        async for entry in guild.audit_logs(
            limit=5,
            action=discord.AuditLogAction.role_delete,
        ):
            if entry.target and entry.target.id == role.id:
                user = entry.user
                triggered = await anti_nuke_check(
                    guild,
                    user.id,
                    "role_delete",
                )

                await send_log(
                    guild,
                    security_embed(
                        "🗑️ Role Deleted",
                        (
                            f"**Role:** `{role.name}`\n"
                            f"**Deleted by:** {user.mention}\n"
                            f"**Anti-Nuke:** {'TRIGGERED' if triggered else 'Monitored'}"
                        ),
                        discord.Color.red(),
                    ),
                )
                break
    except (discord.Forbidden, discord.HTTPException):
        pass

# =========================================================
# MANAGER COMMANDS
# =========================================================


@bot.command(name="addmanager")
@commands.guild_only()
@bot_owner_only()
async def addmanager(ctx, member: discord.Member):
    if member.id == bot.user.id:
        await ctx.send(
            embed=security_embed(
                "❌ Invalid Manager",
                "The bot cannot be added as a manager.",
                discord.Color.red(),
            )
        )
        return

    add_manager(ctx.guild.id, member.id, ctx.author.id)

    await ctx.send(
        embed=security_embed(
            "👑 Manager Added",
            f"{member.mention} is now a Security Manager.",
            discord.Color.green(),
        )
    )


@bot.command(name="removemanager")
@commands.guild_only()
@bot_owner_only()
async def removemanager(ctx, member: discord.Member):
    removed = remove_manager(ctx.guild.id, member.id)

    await ctx.send(
        embed=security_embed(
            "👑 Manager Removed" if removed else "ℹ️ Not a Manager",
            (
                f"{member.mention} is no longer a Security Manager."
                if removed
                else f"{member.mention} was not a Security Manager."
            ),
            discord.Color.green() if removed else discord.Color.orange(),
        )
    )


@bot.command(name="managers")
@commands.guild_only()
@bot_owner_only()
async def managers(ctx):
    ids = get_managers(ctx.guild.id)
    mentions = [f"<@{user_id}>" for user_id in ids]

    await ctx.send(
        embed=security_embed(
            "👑 Security Managers",
            "\n".join(mentions) if mentions else "No managers have been added.",
            discord.Color.blurple(),
        )
    )

# =========================================================
# WHITELIST COMMANDS
# =========================================================


@bot.command(name="whitelist")
@commands.guild_only()
@manager_only()
async def whitelist(ctx, member: discord.Member):
    add_whitelist(ctx.guild.id, member.id, ctx.author.id)

    await ctx.send(
        embed=security_embed(
            "✅ User Whitelisted",
            (
                f"{member.mention} has been whitelisted.\n\n"
                "They will bypass this bot's anti-spam, anti-invite and anti-bot enforcement."
            ),
            discord.Color.green(),
        )
    )


@bot.command(name="unwhitelist")
@commands.guild_only()
@manager_only()
async def unwhitelist(ctx, member: discord.Member):
    removed = remove_whitelist(ctx.guild.id, member.id)

    await ctx.send(
        embed=security_embed(
            "✅ User Unwhitelisted" if removed else "ℹ️ Not Whitelisted",
            (
                f"{member.mention} was removed from the whitelist."
                if removed
                else f"{member.mention} was not on the whitelist."
            ),
            discord.Color.green() if removed else discord.Color.orange(),
        )
    )


@bot.command(name="whitelistlist")
@commands.guild_only()
@manager_only()
async def whitelistlist(ctx):
    ids = get_whitelist(ctx.guild.id)
    mentions = [f"<@{user_id}>" for user_id in ids]

    await ctx.send(
        embed=security_embed(
            "✅ Whitelist",
            "\n".join(mentions) if mentions else "No users are whitelisted.",
            discord.Color.blurple(),
        )
    )

# =========================================================
# SECURITY DASHBOARD
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def security(ctx):
    settings = get_settings(ctx.guild.id)

    embed = security_embed(
        "🛡️ Security Dashboard",
        "Current security configuration:",
        discord.Color.blurple(),
    )

    for name, key in [
        ("🛡️ Anti-Spam", "antispam"),
        ("🔗 Anti-Invite", "antilink"),
        ("🚨 Anti-Raid", "antiraid"),
        ("🤖 Anti-Bot", "antibot"),
        ("☢️ Anti-Nuke", "antinuke"),
    ]:
        embed.add_field(
            name=name,
            value="🟢 Enabled" if settings[key] else "🔴 Disabled",
            inline=True,
        )

    log_channel = ctx.guild.get_channel(settings["log_channel"])
    embed.add_field(
        name="📋 Log Channel",
        value=log_channel.mention if log_channel else "Not configured",
        inline=True,
    )

    embed.add_field(
        name="👑 Managers",
        value=str(len(get_managers(ctx.guild.id))),
        inline=True,
    )

    embed.add_field(
        name="✅ Whitelisted",
        value=str(len(get_whitelist(ctx.guild.id))),
        inline=True,
    )

    await ctx.send(embed=embed)

# =========================================================
# SECURITY TOGGLE
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def security_toggle(ctx, option: str, state: str):
    options = {
        "antispam": "antispam",
        "antilink": "antilink",
        "antiraid": "antiraid",
        "antibot": "antibot",
        "antinuke": "antinuke",
    }

    option = option.lower()
    state = state.lower()

    if option not in options:
        await ctx.send(
            embed=security_embed(
                "❌ Invalid Option",
                "Use: `antispam`, `antilink`, `antiraid`, `antibot`, or `antinuke`.",
                discord.Color.red(),
            )
        )
        return

    if state not in ("on", "off"):
        await ctx.send(
            embed=security_embed(
                "❌ Invalid State",
                "Use `on` or `off`.",
                discord.Color.red(),
            )
        )
        return

    enabled = state == "on"
    update_setting(ctx.guild.id, options[option], enabled)

    await ctx.send(
        embed=security_embed(
            "⚙️ Security Updated",
            f"**{option}** has been {'enabled 🟢' if enabled else 'disabled 🔴'}.",
            discord.Color.green(),
        )
    )

# =========================================================
# SECURITY LOG CHANNEL
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def security_logs(ctx, channel: discord.TextChannel):
    set_log_channel(ctx.guild.id, channel.id)

    await ctx.send(
        embed=security_embed(
            "📋 Security Logs Configured",
            f"Security events will now be logged in {channel.mention}.",
            discord.Color.green(),
        )
    )

# =========================================================
# LOCKDOWN
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def lockdown(ctx):
    changed = 0

    for channel in ctx.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(ctx.guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason="Security lockdown",
            )
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    await ctx.send(
        embed=security_embed(
            "🔒 SERVER LOCKDOWN",
            f"Lockdown activated.\n\nLocked channels: **{changed}**",
            discord.Color.dark_red(),
        )
    )

# =========================================================
# UNLOCK
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def unlock(ctx):
    changed = 0

    for channel in ctx.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(ctx.guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason="Security lockdown removed",
            )
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            pass

    await ctx.send(
        embed=security_embed(
            "🔓 SERVER UNLOCKED",
            f"Lockdown removed.\n\nUpdated channels: **{changed}**",
            discord.Color.green(),
        )
    )

# =========================================================
# PURGE
# =========================================================


@bot.command()
@commands.guild_only()
@manager_only()
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        await ctx.send(
            embed=security_embed(
                "❌ Invalid Amount",
                "Choose a number between **1 and 100**.",
                discord.Color.red(),
            )
        )
        return

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)

        msg = await ctx.send(
            embed=security_embed(
                "🧹 Messages Purged",
                f"Deleted **{len(deleted) - 1}** messages.",
                discord.Color.green(),
            )
        )
        await msg.delete(delay=5)
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(
            embed=security_embed(
                "❌ Purge Failed",
                "I don't have permission to delete messages.",
                discord.Color.red(),
            )
        )

# =========================================================
# HELP
# =========================================================


@bot.command(name="security_help")
async def security_help(ctx):
    embed = security_embed(
        "🛡️ Security Commands",
        f"Prefix: `{PREFIX}`",
        discord.Color.blurple(),
    )

    embed.add_field(
        name="👑 Management",
        value=(
            f"`{PREFIX}addmanager @user` — Bot owner only\n"
            f"`{PREFIX}removemanager @user` — Bot owner only\n"
            f"`{PREFIX}managers` — Bot owner only\n"
            f"`{PREFIX}whitelist @user` — Manager/owner\n"
            f"`{PREFIX}unwhitelist @user` — Manager/owner\n"
            f"`{PREFIX}whitelistlist` — Manager/owner"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 Dashboard",
        value=f"`{PREFIX}security`",
        inline=False,
    )

    embed.add_field(
        name="⚙️ Protection",
        value=(
            f"`{PREFIX}security_toggle antispam on/off`\n"
            f"`{PREFIX}security_toggle antilink on/off`\n"
            f"`{PREFIX}security_toggle antiraid on/off`\n"
            f"`{PREFIX}security_toggle antibot on/off`\n"
            f"`{PREFIX}security_toggle antinuke on/off`"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Logging",
        value=f"`{PREFIX}security_logs #channel`",
        inline=False,
    )

    embed.add_field(
        name="🚨 Emergency",
        value=f"`{PREFIX}lockdown`\n`{PREFIX}unlock`",
        inline=False,
    )

    embed.add_field(
        name="🧹 Moderation",
        value=f"`{PREFIX}purge 50`",
        inline=False,
    )

    embed.add_field(
        name="🔐 Permission System",
        value=(
            "Server administrators cannot use Security commands unless they are "
            "the bot owner or a registered Security Manager. Unauthorized admins "
            "are reported to the server owner and kicked when Discord permissions allow it."
        ),
        inline=False,
    )

    await ctx.send(embed=embed)

# =========================================================
# ERROR HANDLER
# =========================================================


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CheckFailure):
        # manager_check already handles unauthorized administrators.
        # For normal non-manager users, simply deny access.
        if ctx.guild:
            is_admin = isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator
            if not is_admin:
                await ctx.send(
                    embed=security_embed(
                        "🔒 Manager Only",
                        "You don't have permission to use this Security command.",
                        discord.Color.red(),
                    )
                )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            embed=security_embed(
                "❌ Missing Argument",
                f"You're missing: `{error.param.name}`",
                discord.Color.orange(),
            )
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            embed=security_embed(
                "❌ Invalid Argument",
                "Please check the command arguments and try again.",
                discord.Color.orange(),
            )
        )
        return

    print(f"Command error: {repr(error)}")

# =========================================================
# START BOT
# =========================================================

print("Starting Security Bot...")
bot.run(TOKEN)
