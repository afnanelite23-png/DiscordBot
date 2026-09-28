from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()
import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands

# Permanent Bot Owners (Your specified User IDs)
BOT_OWNERS = {1428428552310620326, 1115291777923027005}

# --- DATABASE SETUP ---
conn = sqlite3.connect("bot_data.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS server_config (
    guild_id INTEGER PRIMARY KEY,
    beastmode INTEGER DEFAULT 0,
    log_channel_id INTEGER DEFAULT NULL
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
conn.commit()

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Helper functions
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
    print(f"Logged in as {bot.user}")

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

# --- SLASH COMMANDS ---
@bot.tree.command(name="beastmode", description="Toggle security on/off")
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def beastmode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not is_owner_or_admin(interaction.guild, interaction.user.id):
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    state = 1 if mode.value == "on" else 0
    cursor.execute("INSERT INTO server_config (guild_id, beastmode) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET beastmode=?", (interaction.guild.id, state, state))
    conn.commit()
    await interaction.response.send_message(f"🔒 Beastmode set to **{mode.value.upper()}**.")

@bot.tree.command(name="whitelist", description="Manage whitelisted users")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def whitelist(interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User = None):
    if not is_owner_or_admin(interaction.guild, interaction.user.id):
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    if action.value == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='whitelist'", (interaction.guild.id,))
        users = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await interaction.response.send_message(embed=discord.Embed(title="Whitelist", description="\n".join(users) or "Empty"))
    if not user: return await interaction.response.send_message("Specify user.", ephemeral=True)
    if action.value == "add":
        cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'whitelist')", (interaction.guild.id, user.id))
    elif action.value == "remove":
        cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='whitelist'", (interaction.guild.id, user.id))
    conn.commit()
    await interaction.response.send_message(f"Updated whitelist for {user.mention}.")

@bot.tree.command(name="admin", description="Manage bot admins (Owner Only)")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def admin(interaction: discord.Interaction, action: app_commands.Choice[str], user: discord.User = None):
    if interaction.user.id not in BOT_OWNERS and interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Owners only.", ephemeral=True)
    if action.value == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='admin'", (interaction.guild.id,))
        admins = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await interaction.response.send_message(embed=discord.Embed(title="Admins", description="\n".join(admins) or "None"))
    if not user: return await interaction.response.send_message("Specify user.", ephemeral=True)
    if action.value == "add":
        cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'admin')", (interaction.guild.id, user.id))
    elif action.value == "remove":
        cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='admin'", (interaction.guild.id, user.id))
    conn.commit()
    await interaction.response.send_message(f"Updated admins for {user.mention}.")

@bot.tree.command(name="restore", description="Restore a user's lost roles")
async def restore(interaction: discord.Interaction, user: discord.Member):
    if not is_owner_or_admin(interaction.guild, interaction.user.id):
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    cursor.execute("SELECT role_id FROM role_backups WHERE guild_id=? AND user_id=?", (interaction.guild.id, user.id))
    roles = [interaction.guild.get_role(r[0]) for r in cursor.fetchall() if interaction.guild.get_role(r[0])]
    if roles:
        await user.add_roles(*roles)
        cursor.execute("DELETE FROM role_backups WHERE guild_id=? AND user_id=?", (interaction.guild.id, user.id))
        conn.commit()
        await interaction.response.send_message(f"✅ Restored roles to {user.mention}.")
    else:
        await interaction.response.send_message("❌ No backup found.", ephemeral=True)

@bot.tree.command(name="logs", description="Set log channel")
async def logs(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_owner_or_admin(interaction.guild, interaction.user.id):
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
    cursor.execute("INSERT INTO server_config (guild_id, log_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=?", (interaction.guild.id, channel.id, channel.id))
    conn.commit()
    await interaction.response.send_message(f"📋 Logs set to {channel.mention}.")

import os

# ... rest of your code ...

bot.run(os.getenv('DISCORD_TOKEN'))
