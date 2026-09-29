import os
import asyncio
import sqlite3
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- KEEP-ALIVE FLASK SERVER ---
app = Flask('')

@app.route('/')
def home():
    return "FXY Security Bot is Online!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

# --- PERMANENT BOT OWNERS ---
BOT_OWNERS = {1428428552310620326, 1115291777923027005}

# --- DATABASE SETUP ---
conn = sqlite3.connect("bot_data.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS server_config (
    guild_id INTEGER PRIMARY KEY,
    beastmode INTEGER DEFAULT 0,
    log_channel_id INTEGER DEFAULT NULL,
    autorole_id INTEGER DEFAULT NULL
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    guild_id INTEGER,
    user_id INTEGER,
    role_type TEXT,
    PRIMARY KEY (guild_id, user_id, role_type)
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS role_backups (
    guild_id INTEGER,
    user_id INTEGER,
    role_id INTEGER,
    PRIMARY KEY (guild_id, user_id, role_id)
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS afk_users (
    user_id INTEGER PRIMARY KEY,
    original_nick TEXT,
    reason TEXT,
    timestamp INTEGER
)
""")
conn.commit()

# --- BOT SETUP ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)

# --- HELPER FUNCTIONS ---
def is_owner_or_admin(guild: discord.Guild, user_id: int) -> bool:
    if user_id in BOT_OWNERS or user_id == guild.owner_id:
        return True
    cursor.execute("SELECT 1 FROM users WHERE guild_id = ? AND user_id = ? AND role_type = 'admin'", (guild.id, user_id))
    return cursor.fetchone() is not None

def is_whitelisted(guild_id: int, user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM users WHERE guild_id = ? AND user_id = ? AND role_type = 'whitelist'", (guild_id, user_id))
    return cursor.fetchone() is not None

def is_beastmode_on(guild_id: int) -> bool:
    cursor.execute("SELECT beastmode FROM server_config WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    return bool(row[0]) if row else False

async def log_alert(guild: discord.Guild, title: str, description: str, alert: bool = False):
    cursor.execute("SELECT log_channel_id FROM server_config WHERE guild_id = ?", (guild.id,))
    row = cursor.fetchone()
    if not row or not row[0]: return
    
    channel = guild.get_channel(row[0])
    if channel:
        embed = discord.Embed(title=title, description=description, color=discord.Color.red() if alert else discord.Color.blue())
        content = f"⚠️ {guild.owner.mention} **BEASTMODE ALERT**" if alert else None
        await channel.send(content=content, embed=embed)

async def punish_user(guild: discord.Guild, member: discord.Member, reason: str):
    if member.id in BOT_OWNERS or member.id == guild.owner_id or is_whitelisted(guild.id, member.id) or member.bot:
        return

    roles_to_strip = [r for r in member.roles if not r.is_default() and r.is_assignable()]
    if not roles_to_strip: return

    cursor.execute("DELETE FROM role_backups WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
    for r in roles_to_strip:
        cursor.execute("INSERT INTO role_backups VALUES (?, ?, ?)", (guild.id, member.id, r.id))
    conn.commit()

    try:
        await member.remove_roles(*roles_to_strip, reason=f"[BEASTMODE] {reason}")
        await log_alert(guild, "🚨 Member Stripped", f"**Offender:** {member.mention}\n**Reason:** {reason}", alert=True)
    except discord.Forbidden:
        await log_alert(guild, "❌ Action Failed", f"Bot lacked permissions to strip roles from {member.mention}.", alert=True)

# --- EVENTS ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} - FXY Security Bot Active")

@bot.event
async def on_guild_channel_create(channel):
    if not is_beastmode_on(channel.guild.id): return
    async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
        await punish_user(channel.guild, entry.user, "Created a channel")

@bot.event
async def on_guild_channel_delete(channel):
    if not is_beastmode_on(channel.guild.id): return
    async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        await punish_user(channel.guild, entry.user, f"Deleted channel: {channel.name}")

@bot.event
async def on_guild_role_create(role):
    if not is_beastmode_on(role.guild.id): return
    async for entry in role.guild.audit_logs(limit=1, action=discord.AuditLogAction.role_create):
        await punish_user(role.guild, entry.user, "Created a role")

@bot.event
async def on_guild_role_delete(role):
    if not is_beastmode_on(role.guild.id): return
    async for entry in role.guild.audit_logs(limit=1, action=discord.AuditLogAction.role_delete):
        await punish_user(role.guild, entry.user, f"Deleted role: {role.name}")

@bot.event
async def on_member_update(before, after):
    if not is_beastmode_on(after.guild.id): return
    if len(after.roles) > len(before.roles):
        async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_role_update):
            await punish_user(after.guild, entry.user, f"Gave role to {after.mention}")

# --- MESSAGE EVENT (AFK CHECKER & NICKNAME RESTORE) ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # 1. Remove AFK status when the user speaks
    cursor.execute("SELECT original_nick FROM afk_users WHERE user_id = ?", (message.author.id,))
    afk_data = cursor.fetchone()
    if afk_data:
        original_nick = afk_data[0]
        cursor.execute("DELETE FROM afk_users WHERE user_id = ?", (message.author.id,))
        conn.commit()

        try:
            await message.author.edit(nick=original_nick)
        except discord.Forbidden:
            pass

        await message.channel.send(f"👋 Welcome back {message.author.mention}, I removed your AFK status.", delete_after=5)

    # 2. Alert when pinging an AFK member
    if message.mentions:
        for mentioned in message.mentions:
            if mentioned.id == message.author.id:
                continue
            cursor.execute("SELECT reason, timestamp FROM afk_users WHERE user_id = ?", (mentioned.id,))
            row = cursor.fetchone()
            if row:
                reason, ts = row
                await message.channel.send(
                    f"💤 `{mentioned.display_name}` is currently AFK: **{reason}** ()", 
                    delete_after=7
                )

    await bot.process_commands(message)

# --- WELCOME & AUTOROLE SYSTEM ---
@bot.event
async def on_member_join(member: discord.Member):
    cursor.execute("SELECT autorole_id FROM server_config WHERE guild_id = ?", (member.guild.id,))
    row = cursor.fetchone()
    if row and row[0]:
        auto_role = member.guild.get_role(row[0])
        if auto_role:
            try:
                await member.add_roles(auto_role)
            except discord.Forbidden:
                pass

    channel = discord.utils.get(member.guild.text_channels, name="🦇『👋』welcomes")
    if not channel:
        channel = discord.utils.get(member.guild.text_channels, name="welcomes")
    if not channel:
        channel = discord.utils.get(member.guild.text_channels, name="welcome")

    if channel:
        embed = discord.Embed(
            title=f"🛡️️ Welcome to {member.guild.name}!",
            description=f"Welcome {member.mention}! Please make sure to follow the server rules.",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member Count", value=f"#{member.guild.member_count}", inline=True)
        embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
        embed.set_footer(
            text="FXY Security • Custom Bot Services by @plzdie",
            icon_url=bot.user.display_avatar.url
        )
        await channel.send(content=f"Welcome {member.mention}!", embed=embed)

# ==========================================
# DUAL COMMANDS (WORK AS BOTH ? AND SLASH /)
# ==========================================

# --- AFK COMMAND ---
@bot.hybrid_command(name="afk", description="Set your AFK status")
async def afk(ctx: commands.Context, *, reason: str = "AFK"):
    ts = int(datetime.datetime.now().timestamp())
    original_nick = ctx.author.nick

    cursor.execute("INSERT OR REPLACE INTO afk_users VALUES (?, ?, ?, ?)", (ctx.author.id, original_nick, reason, ts))
    conn.commit()

    new_nick = f"[AFK] {ctx.author.display_name}"
    if len(new_nick) > 32:
        new_nick = new_nick[:32]

    try:
        await ctx.author.edit(nick=new_nick)
    except discord.Forbidden:
        pass

    await ctx.send(f"💤 {ctx.author.mention}, I set your AFK status to: **{reason}**")

# --- BAN COMMAND ---
@bot.hybrid_command(name="ban", description="Ban a member from the server")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"⛔ **Banned:** `{member.display_name}` | **Reason:** {reason}")

# --- KICK COMMAND ---
@bot.hybrid_command(name="kick", description="Kick a member from the server")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 **Kicked:** `{member.display_name}` | **Reason:** {reason}")

# --- TIMEOUT COMMAND ---
@bot.hybrid_command(name="timeout", description="Timeout a member for a specified duration in minutes")
@commands.has_permissions(moderate_members=True)
async def timeout(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"⏳ **Timed out:** `{member.display_name}` for `{minutes}m` | **Reason:** {reason}")

# --- ADD ROLE COMMAND ---
@bot.hybrid_command(name="addrole", description="Add a role to a member")
@commands.has_permissions(manage_roles=True)
async def addrole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if role in member.roles:
        return await ctx.send(f"❌ `{member.display_name}` already has **{role.name}**.")
    await member.add_roles(role)
    await ctx.send(f"✅ Added **{role.name}** to `{member.display_name}`.")

# --- SET AUTOROLE COMMAND ---
@bot.hybrid_command(name="autorole", description="Set automatic role for new members")
@commands.has_permissions(administrator=True)
async def autorole(ctx: commands.Context, role: discord.Role):
    cursor.execute("INSERT INTO server_config (guild_id, autorole_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET autorole_id=?", (ctx.guild.id, role.id, role.id))
    conn.commit()
    await ctx.send(f"✅ **Autorole** set to **{role.name}**. New members will receive this role automatically.")

# --- MASS ROLE ALL COMMAND ---
@bot.hybrid_command(name="roleall", description="Give a role to all human members")
@commands.has_permissions(administrator=True)
async def roleall(ctx: commands.Context, role: discord.Role):
    await ctx.send(f"⏳ Giving **{role.name}** to all members. Please wait...")
    count = 0
    for member in ctx.guild.members:
        if not member.bot and role not in member.roles:
            try:
                await member.add_roles(role)
                count += 1
                await asyncio.sleep(0.4)
            except Exception:
                continue
    await ctx.send(f"✅ Given **{role.name}** to `{count}` members!")

# --- BEASTMODE COMMAND ---
@bot.hybrid_command(name="beastmode", description="Toggle anti-nuke security on or off")
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def beastmode(ctx: commands.Context, mode: str):
    if not is_owner_or_admin(ctx.guild, ctx.author.id):
        return await ctx.send("❌ Unauthorized.", ephemeral=True)
    state = 1 if mode.lower() == "on" else 0
    cursor.execute("INSERT INTO server_config (guild_id, beastmode) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET beastmode=?", (ctx.guild.id, state, state))
    conn.commit()
    await ctx.send(f"🔒 Beastmode set to **{mode.upper()}**.")

# --- WHITELIST COMMAND ---
@bot.hybrid_command(name="whitelist", description="Manage whitelisted security users")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def whitelist(ctx: commands.Context, action: str, user: discord.User = None):
    if not is_owner_or_admin(ctx.guild, ctx.author.id):
        return await ctx.send("❌ Unauthorized.", ephemeral=True)
    if action == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='whitelist'", (ctx.guild.id,))
        users = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await ctx.send(embed=discord.Embed(title="Whitelist", description="\n".join(users) or "Empty"))
    if not user: 
        return await ctx.send("Please specify a user.", ephemeral=True)
    if action == "add":
        cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'whitelist')", (ctx.guild.id, user.id))
    elif action == "remove":
        cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='whitelist'", (ctx.guild.id, user.id))
    conn.commit()
    await ctx.send(f"Updated whitelist for {user.mention}.")

# --- ADMIN COMMAND ---
@bot.hybrid_command(name="admin", description="Manage bot admins (Owner Only)")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def admin(ctx: commands.Context, action: str, user: discord.User = None):
    if ctx.author.id not in BOT_OWNERS and ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ Owners only.", ephemeral=True)
    if action == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='admin'", (ctx.guild.id,))
        admins = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await ctx.send(embed=discord.Embed(title="Admins", description="\n".join(admins) or "None"))
    if not user: 
        return await ctx.send("Please specify a user.", ephemeral=True)
    if action == "add":
        cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'admin')", (ctx.guild.id, user.id))
    elif action == "remove":
        cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='admin'", (ctx.guild.id, user.id))
    conn.commit()
    await ctx.send(f"Updated admins for {user.mention}.")

# --- RESTORE COMMAND ---
@bot.hybrid_command(name="restore", description="Restore a user's lost roles stripped by Beastmode")
async def restore(ctx: commands.Context, user: discord.Member):
    if not is_owner_or_admin(ctx.guild, ctx.author.id):
        return await ctx.send("❌ Unauthorized.", ephemeral=True)
    cursor.execute("SELECT role_id FROM role_backups WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
    roles = [ctx.guild.get_role(r[0]) for r in cursor.fetchall() if ctx.guild.get_role(r[0])]
    if roles:
        await user.add_roles(*roles)
        cursor.execute("DELETE FROM role_backups WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        conn.commit()
        await ctx.send(f"✅ Restored roles to {user.mention}.")
    else:
        await ctx.send("❌ No backup found.", ephemeral=True)

# --- LOGS COMMAND ---
@bot.hybrid_command(name="logs", description="Set log channel for security alerts")
async def logs(ctx: commands.Context, channel: discord.TextChannel):
    if not is_owner_or_admin(ctx.guild, ctx.author.id):
        return await ctx.send("❌ Unauthorized.", ephemeral=True)
    cursor.execute("INSERT INTO server_config (guild_id, log_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=?", (ctx.guild.id, channel.id, channel.id))
    conn.commit()
    await ctx.send(f"📋 Logs set to {channel.mention}.")

# --- START BOT ---
bot.run(os.getenv('DISCORD_TOKEN'))
