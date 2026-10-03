import os
import re
import time
import sqlite3
from datetime import timedelta
from collections import defaultdict, deque

import discord
from discord.ext import commands


TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from your environment variables.")

PREFIX = ">"
DATABASE = "security.db"

# Default security settings
DEFAULT_SETTINGS = {
    "antispam": 1,
    "antilink": 1,
    "antiraid": 1,
    "antibot": 1,
    "antinuke": 1,
    "log_channel": 0,
}

# How many messages within this period = spam
SPAM_MESSAGE_LIMIT = 6
SPAM_TIME_WINDOW = 5

# How many joins within this period = raid
RAID_JOIN_LIMIT = 8
RAID_TIME_WINDOW = 10

# How many dangerous actions before triggering anti-nuke
NUKE_ACTION_LIMIT = 4
NUKE_TIME_WINDOW = 10

# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(DATABASE, check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    antispam INTEGER DEFAULT 1,
    antilink INTEGER DEFAULT 1,
    antiraid INTEGER DEFAULT 1,
    antibot INTEGER DEFAULT 1,
    antinuke INTEGER DEFAULT 1,
    log_channel INTEGER DEFAULT 0
)
""")

db.commit()


def ensure_guild(guild_id):
    cursor.execute(
        "SELECT guild_id FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )

    if cursor.fetchone() is None:
        cursor.execute("""
            INSERT INTO guild_settings
            (guild_id, antispam, antilink, antiraid, antibot, antinuke, log_channel)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id,
            DEFAULT_SETTINGS["antispam"],
            DEFAULT_SETTINGS["antilink"],
            DEFAULT_SETTINGS["antiraid"],
            DEFAULT_SETTINGS["antibot"],
            DEFAULT_SETTINGS["antinuke"],
            DEFAULT_SETTINGS["log_channel"],
        ))
        db.commit()


def get_settings(guild_id):
    ensure_guild(guild_id)

    cursor.execute("""
        SELECT antispam, antilink, antiraid, antibot, antinuke, log_channel
        FROM guild_settings
        WHERE guild_id = ?
    """, (guild_id,))

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
        f"UPDATE guild_settings SET {setting} = ? WHERE guild_id = ?",
        (int(value), guild_id)
    )

    db.commit()
    return True


def set_log_channel(guild_id, channel_id):
    ensure_guild(guild_id)

    cursor.execute(
        "UPDATE guild_settings SET log_channel = ? WHERE guild_id = ?",
        (channel_id, guild_id)
    )

    db.commit()


# =========================================================
# BOT SETUP
# =========================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None
)


# =========================================================
# MEMORY / RATE LIMITS
# =========================================================

user_messages = defaultdict(deque)
guild_joins = defaultdict(deque)

# guild_id -> user_id -> deque(actions)
nuke_actions = defaultdict(lambda: defaultdict(deque))

# Users currently being punished
punishing = set()


# =========================================================
# EMBEDS
# =========================================================

def security_embed(title, description, color=discord.Color.blurple()):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
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
# PERMISSION CHECK
# =========================================================

def is_staff(member):
    if member.guild.owner_id == member.id:
        return True

    return member.guild_permissions.administrator


# =========================================================
# BOT READY
# =========================================================

@bot.event
async def on_ready():
    print("=" * 50)
    print(f"Logged in as: {bot.user}")
    print(f"Bot ID: {bot.user.id}")
    print(f"Servers: {len(bot.guilds)}")
    print("=" * 50)

    try:
        await bot.change_presence(
            activity=discord.Game(name=f"{PREFIX}security")
        )
    except Exception:
        pass


# =========================================================
# GUILD JOIN
# =========================================================

@bot.event
async def on_guild_join(guild):
    ensure_guild(guild.id)


# =========================================================
# ANTI-BOT
# =========================================================

@bot.event
async def on_member_join(member):
    guild = member.guild
    settings = get_settings(guild.id)

    # -------------------------
    # Anti bot
    # -------------------------

    if settings["antibot"] and member.bot:

        # Don't interfere with the security bot itself
        if member.id != bot.user.id:

            try:
                await member.kick(
                    reason="Security: unauthorized bot detected"
                )

                embed = security_embed(
                    "🤖 Unauthorized Bot Blocked",
                    f"{member.mention} was removed because the server's "
                    f"anti-bot protection is enabled.",
                    discord.Color.red()
                )

                embed.add_field(
                    name="Bot",
                    value=f"{member} (`{member.id}`)",
                    inline=False
                )

                await send_log(guild, embed)

            except discord.Forbidden:
                await send_log(
                    guild,
                    security_embed(
                        "⚠️ Anti-Bot Failed",
                        f"I couldn't remove {member.mention}. "
                        f"Check my role position and permissions.",
                        discord.Color.orange()
                    )
                )

            return

    # -------------------------
    # Anti raid
    # -------------------------

    if settings["antiraid"]:

        now = time.monotonic()

        joins = guild_joins[guild.id]

        joins.append(now)

        while joins and now - joins[0] > RAID_TIME_WINDOW:
            joins.popleft()

        if len(joins) >= RAID_JOIN_LIMIT:

            embed = security_embed(
                "🚨 RAID DETECTED",
                f"**{len(joins)} members** joined within "
                f"{RAID_TIME_WINDOW} seconds.",
                discord.Color.dark_red()
            )

            await send_log(guild, embed)


# =========================================================
# MESSAGE SECURITY
# =========================================================

INVITE_REGEX = re.compile(
    r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)",
    re.IGNORECASE
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

    # Staff bypass
    if isinstance(message.author, discord.Member):
        staff = is_staff(message.author)
    else:
        staff = False

    # =====================================================
    # ANTI-LINK / INVITE
    # =====================================================

    if settings["antilink"] and not staff:

        if INVITE_REGEX.search(message.content):

            try:
                await message.delete()

                warning = await message.channel.send(
                    embed=security_embed(
                        "🔗 Invite Removed",
                        f"{message.author.mention}, Discord invites "
                        f"aren't allowed here.",
                        discord.Color.orange()
                    )
                )

                await warning.delete(delay=5)

                await send_log(
                    guild,
                    security_embed(
                        "🔗 Invite Blocked",
                        f"Removed a Discord invite sent by "
                        f"{message.author.mention}.",
                        discord.Color.orange()
                    )
                )

            except (discord.Forbidden, discord.HTTPException):
                pass

            return

    # =====================================================
    # ANTI-SPAM
    # =====================================================

    if settings["antispam"] and not staff:

        now = time.monotonic()

        messages = user_messages[
            (guild.id, message.author.id)
        ]

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
                    )

                    await message.author.edit(
                        timed_out_until=timeout,
                        reason="Security: spam detected"
                    )

                    await send_log(
                        guild,
                        security_embed(
                            "🚨 Spam Detected",
                            f"{message.author.mention} was timed out "
                            f"for repeatedly sending messages.",
                            discord.Color.red()
                        )
                    )

                except (discord.Forbidden, discord.HTTPException):
                    pass

                finally:
                    punishing.discard(message.author.id)

            return

    await bot.process_commands(message)


# =========================================================
# ANTI-NUKE
# =========================================================

DANGEROUS_ACTIONS = {
    "channel_delete",
    "role_delete",
    "guild_ban",
    "guild_kick",
}


async def anti_nuke_check(guild, user_id, action):

    settings = get_settings(guild.id)

    if not settings["antinuke"]:
        return False

    if action not in DANGEROUS_ACTIONS:
        return False

    if user_id == guild.owner_id:
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

    try:
        # Remove administrator permission
        for role in member.roles:

            if role.is_default():
                continue

            if role.permissions.administrator:

                try:
                    await role.edit(
                        permissions=discord.Permissions(
                            role.permissions.value
                            & ~discord.Permissions(administrator=True).value
                        ),
                        reason="Security: possible anti-nuke violation"
                    )
                except Exception:
                    pass

        await send_log(
            guild,
            security_embed(
                "☢️ ANTI-NUKE TRIGGERED",
                f"Suspicious activity was detected from "
                f"{member.mention}.",
                discord.Color.dark_red()
            )
        )

        return True

    except Exception:
        return False


# =========================================================
# CHANNEL DELETE PROTECTION
# =========================================================

@bot.event
async def on_guild_channel_delete(channel):

    guild = channel.guild

    try:
        async for entry in guild.audit_logs(
            limit=5,
            action=discord.AuditLogAction.channel_delete
        ):

            if entry.target.id == channel.id:

                user = entry.user

                triggered = await anti_nuke_check(
                    guild,
                    user.id,
                    "channel_delete"
                )

                await send_log(
                    guild,
                    security_embed(
                        "🗑️ Channel Deleted",
                        f"**Channel:** `{channel.name}`\n"
                        f"**Deleted by:** {user.mention}\n"
                        f"**Anti-Nuke:** "
                        f"{'TRIGGERED' if triggered else 'Monitored'}",
                        discord.Color.red()
                    )
                )

                break

    except (discord.Forbidden, discord.HTTPException):
        pass


# =========================================================
# ROLE DELETE PROTECTION
# =========================================================

@bot.event
async def on_guild_role_delete(role):

    guild = role.guild

    try:
        async for entry in guild.audit_logs(
            limit=5,
            action=discord.AuditLogAction.role_delete
        ):

            if entry.target.id == role.id:

                user = entry.user

                triggered = await anti_nuke_check(
                    guild,
                    user.id,
                    "role_delete"
                )

                await send_log(
                    guild,
                    security_embed(
                        "🗑️ Role Deleted",
                        f"**Role:** `{role.name}`\n"
                        f"**Deleted by:** {user.mention}\n"
                        f"**Anti-Nuke:** "
                        f"{'TRIGGERED' if triggered else 'Monitored'}",
                        discord.Color.red()
                    )
                )

                break

    except (discord.Forbidden, discord.HTTPException):
        pass


# =========================================================
# SECURITY COMMAND
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def security(ctx):

    settings = get_settings(ctx.guild.id)

    embed = security_embed(
        "🛡️ Security Dashboard",
        "Current security configuration:",
        discord.Color.blurple()
    )

    embed.add_field(
        name="🛡️ Anti-Spam",
        value="🟢 Enabled" if settings["antispam"] else "🔴 Disabled",
        inline=True
    )

    embed.add_field(
        name="🔗 Anti-Invite",
        value="🟢 Enabled" if settings["antilink"] else "🔴 Disabled",
        inline=True
    )

    embed.add_field(
        name="🚨 Anti-Raid",
        value="🟢 Enabled" if settings["antiraid"] else "🔴 Disabled",
        inline=True
    )

    embed.add_field(
        name="🤖 Anti-Bot",
        value="🟢 Enabled" if settings["antibot"] else "🔴 Disabled",
        inline=True
    )

    embed.add_field(
        name="☢️ Anti-Nuke",
        value="🟢 Enabled" if settings["antinuke"] else "🔴 Disabled",
        inline=True
    )

    log_channel = ctx.guild.get_channel(settings["log_channel"])

    embed.add_field(
        name="📋 Log Channel",
        value=log_channel.mention if log_channel else "Not configured",
        inline=True
    )

    await ctx.send(embed=embed)


# =========================================================
# TOGGLE COMMAND
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
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
                "Use: `antispam`, `antilink`, `antiraid`, "
                "`antibot`, or `antinuke`.",
                discord.Color.red()
            )
        )
        return

    if state not in ("on", "off"):

        await ctx.send(
            embed=security_embed(
                "❌ Invalid State",
                "Use `on` or `off`.",
                discord.Color.red()
            )
        )
        return

    enabled = state == "on"

    update_setting(
        ctx.guild.id,
        options[option],
        enabled
    )

    await ctx.send(
        embed=security_embed(
            "⚙️ Security Updated",
            f"**{option}** has been "
            f"{'enabled 🟢' if enabled else 'disabled 🔴'}.",
            discord.Color.green()
        )
    )


# =========================================================
# LOG CHANNEL
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def security_logs(ctx, channel: discord.TextChannel):

    set_log_channel(
        ctx.guild.id,
        channel.id
    )

    await ctx.send(
        embed=security_embed(
            "📋 Security Logs Configured",
            f"Security events will now be logged in {channel.mention}.",
            discord.Color.green()
        )
    )


# =========================================================
# LOCKDOWN
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def lockdown(ctx):

    changed = 0

    for channel in ctx.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                ctx.guild.default_role
            )

            overwrite.send_messages = False

            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason="Security lockdown"
            )

            changed += 1

        except (discord.Forbidden, discord.HTTPException):
            pass

    await ctx.send(
        embed=security_embed(
            "🔒 SERVER LOCKDOWN",
            f"Lockdown activated.\n\n"
            f"Locked channels: **{changed}**",
            discord.Color.dark_red()
        )
    )


# =========================================================
# UNLOCK
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def unlock(ctx):

    changed = 0

    for channel in ctx.guild.text_channels:

        try:

            overwrite = channel.overwrites_for(
                ctx.guild.default_role
            )

            overwrite.send_messages = None

            await channel.set_permissions(
                ctx.guild.default_role,
                overwrite=overwrite,
                reason="Security lockdown removed"
            )

            changed += 1

        except (discord.Forbidden, discord.HTTPException):
            pass

    await ctx.send(
        embed=security_embed(
            "🔓 SERVER UNLOCKED",
            f"Lockdown removed.\n\n"
            f"Updated channels: **{changed}**",
            discord.Color.green()
        )
    )


# =========================================================
# PURGE
# =========================================================

@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):

    if amount < 1 or amount > 100:

        await ctx.send(
            embed=security_embed(
                "❌ Invalid Amount",
                "Choose a number between **1 and 100**.",
                discord.Color.red()
            )
        )

        return

    deleted = await ctx.channel.purge(limit=amount + 1)

    msg = await ctx.send(
        embed=security_embed(
            "🧹 Messages Purged",
            f"Deleted **{len(deleted) - 1}** messages.",
            discord.Color.green()
        )
    )

    await msg.delete(delay=5)


# =========================================================
# SECURITY HELP
# =========================================================

@bot.command(name="security_help")
async def security_help(ctx):

    embed = security_embed(
        "🛡️ Security Commands",
        f"Prefix: `{PREFIX}`",
        discord.Color.blurple()
    )

    embed.add_field(
        name="Dashboard",
        value=f"`{PREFIX}security`",
        inline=False
    )

    embed.add_field(
        name="Toggle Protection",
        value=(
            f"`{PREFIX}security_toggle antispam on`\n"
            f"`{PREFIX}security_toggle antilink on`\n"
            f"`{PREFIX}security_toggle antiraid on`\n"
            f"`{PREFIX}security_toggle antibot on`\n"
            f"`{PREFIX}security_toggle antinuke on`"
        ),
        inline=False
    )

    embed.add_field(
        name="Logging",
        value=f"`{PREFIX}security_logs #channel`",
        inline=False
    )

    embed.add_field(
        name="Emergency",
        value=(
            f"`{PREFIX}lockdown`\n"
            f"`{PREFIX}unlock`"
        ),
        inline=False
    )

    embed.add_field(
        name="Moderation",
        value=f"`{PREFIX}purge 50`",
        inline=False
    )

    await ctx.send(embed=embed)


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):

        await ctx.send(
            embed=security_embed(
                "🔒 Permission Denied",
                "You don't have permission to use this command.",
                discord.Color.red()
            )
        )

        return

    if isinstance(error, commands.MissingRequiredArgument):

        await ctx.send(
            embed=security_embed(
                "❌ Missing Argument",
                f"You're missing: `{error.param.name}`",
                discord.Color.orange()
            )
        )

        return

    if isinstance(error, commands.BadArgument):

        await ctx.send(
            embed=security_embed(
                "❌ Invalid Argument",
                "Please check the command arguments and try again.",
                discord.Color.orange()
            )
        )

        return

    print(f"Command error: {repr(error)}")


# =========================================================
# START BOT
# =========================================================

bot.run(TOKEN)

