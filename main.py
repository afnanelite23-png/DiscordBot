import os
import random
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
    return "FXY Security & Economy Bot is Online!"

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
    autorole_id INTEGER DEFAULT NULL,
    ticket_category_id INTEGER DEFAULT NULL
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
cursor.execute("""
CREATE TABLE IF NOT EXISTS economy (
    user_id INTEGER PRIMARY KEY,
    wallet INTEGER DEFAULT 0,
    bank INTEGER DEFAULT 0,
    last_work INTEGER DEFAULT 0,
    last_beg INTEGER DEFAULT 0,
    last_rob INTEGER DEFAULT 0
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

def get_economy_data(user_id: int):
    cursor.execute("SELECT wallet, bank, last_work, last_beg, last_rob FROM economy WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO economy (user_id, wallet, bank) VALUES (?, 100, 0)", (user_id,))
        conn.commit()
        return [100, 0, 0, 0, 0]
    return list(row)

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

# --- TICKET UI VIEWS ---
class TicketLauncher(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Open Ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        # Check if ticket channel already exists for user
        existing_channel = discord.utils.get(guild.text_channels, name=f"ticket-{user.name.lower()}")
        if existing_channel:
            return await interaction.response.send_message(f"❌ You already have an open ticket: {existing_channel.mention}", ephemeral=True)

        # Get or create ticket category
        cursor.execute("SELECT ticket_category_id FROM server_config WHERE guild_id = ?", (guild.id,))
        row = cursor.fetchone()
        category = guild.get_channel(row[0]) if row and row[0] else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {user}"
        )

        embed = discord.Embed(
            title="🎫 Support Ticket Created",
            description=f"Welcome {user.mention}! Please describe your issue in detail. A staff member will be with you shortly.",
            color=discord.Color.green()
        )
        await channel.send(content=f"{user.mention}", embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Ticket Closed")

# --- EVENTS ---
@bot.event
async def on_ready():
    bot.add_view(TicketLauncher())
    bot.add_view(CloseTicketView())
    await bot.tree.sync()
    print(f"Logged in as {bot.user} - Security, Ticket & Economy Active")

@bot.event
async def on_guild_channel_create(channel):
    if not is_beastmode_on(channel.guild.id) or channel.name.startswith("ticket-"): return
    async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
        await punish_user(channel.guild, entry.user, "Created a channel")

@bot.event
async def on_guild_channel_delete(channel):
    if not is_beastmode_on(channel.guild.id) or channel.name.startswith("ticket-"): return
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

# --- MESSAGE EVENT (AFK & PREFIX HANDLING) ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # Clear AFK status
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

    # Ping AFK user check
    if message.mentions:
        for mentioned in message.mentions:
            if mentioned.id == message.author.id: continue
            cursor.execute("SELECT reason, timestamp FROM afk_users WHERE user_id = ?", (mentioned.id,))
            row = cursor.fetchone()
            if row:
                reason, ts = row
                await message.channel.send(f"💤 `{mentioned.display_name}` is currently AFK: **{reason}** (<t:{ts}:R>)", delete_after=7)

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

    channel = discord.utils.get(member.guild.text_channels, name="🦇『👋』welcomes") or discord.utils.get(member.guild.text_channels, name="welcome")
    if channel:
        embed = discord.Embed(
            title=f"🛡️ Welcome to {member.guild.name}!",
            description=f"Welcome {member.mention}! Please make sure to follow the server rules.",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member Count", value=f"#{member.guild.member_count}", inline=True)
        embed.set_footer(text="FXY Security • Custom Bot Services by @plzdie", icon_url=bot.user.display_avatar.url)
        await channel.send(content=f"Welcome {member.mention}!", embed=embed)

# ==========================================
# COMMANDS (WORK AS BOTH ? AND SLASH /)
# ==========================================

# --- TICKET COMMANDS ---
@bot.hybrid_command(name="ticket-setup", description="Setup ticket panel channel")
@commands.has_permissions(administrator=True)
async def ticket_setup(ctx: commands.Context, channel: discord.TextChannel = None, category: discord.CategoryChannel = None):
    channel = channel or ctx.channel
    if category:
        cursor.execute("INSERT INTO server_config (guild_id, ticket_category_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET ticket_category_id=?", (ctx.guild.id, category.id, category.id))
        conn.commit()

    embed = discord.Embed(
        title="📩 Support Tickets",
        description="Need assistance or have questions? Click the button below to open a private support ticket.",
        color=discord.Color.blue()
    )
    await channel.send(embed=embed, view=TicketLauncher())
    await ctx.send(f"✅ Ticket setup sent to {channel.mention}.", ephemeral=True)

@bot.hybrid_command(name="close", description="Close current support ticket")
async def close(ctx: commands.Context):
    if not ctx.channel.name.startswith("ticket-"):
        return await ctx.send("❌ This command can only be used inside ticket channels.", ephemeral=True)
    await ctx.send("🔒 Closing this ticket in 5 seconds...")
    await asyncio.sleep(5)
    await ctx.channel.delete(reason="Ticket Closed")

# --- ECONOMY COMMANDS ---
@bot.hybrid_command(name="balance", aliases=["bal"], description="Check your cash and bank balance")
async def balance(ctx: commands.Context, user: discord.Member = None):
    target = user or ctx.author
    wallet, bank, _, _, _ = get_economy_data(target.id)
    embed = discord.Embed(title=f"💳 Balance - {target.display_name}", color=discord.Color.gold())
    embed.add_field(name="💵 Wallet", value=f"`${wallet:,}`", inline=True)
    embed.add_field(name="🏦 Bank", value=f"`${bank:,}`", inline=True)
    embed.add_field(name="💰 Net Worth", value=f"`${(wallet + bank):,}`", inline=False)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="work", description="Work to earn money (1 hr cooldown)")
async def work(ctx: commands.Context):
    wallet, bank, last_work, beg, rob = get_economy_data(ctx.author.id)
    now = int(datetime.datetime.now().timestamp())

    if now - last_work < 3600:
        remaining = 3600 - (now - last_work)
        return await ctx.send(f"⏳ You need to wait `{remaining // 60}m {remaining % 60}s` before working again.", ephemeral=True)

    earnings = random.randint(150, 600)
    cursor.execute("UPDATE economy SET wallet = wallet + ?, last_work = ? WHERE user_id = ?", (earnings, now, ctx.author.id))
    conn.commit()
    await ctx.send(f"💼 You worked and earned **${earnings:,}**!")

@bot.hybrid_command(name="beg", description="Beg for some spare change (5 min cooldown)")
async def beg(ctx: commands.Context):
    wallet, bank, work_ts, last_beg, rob = get_economy_data(ctx.author.id)
    now = int(datetime.datetime.now().timestamp())

    if now - last_beg < 300:
        remaining = 300 - (now - last_beg)
        return await ctx.send(f"⏳ Stop begging! Wait `{remaining}` seconds.", ephemeral=True)

    if random.choice([True, False]):
        earnings = random.randint(20, 100)
        cursor.execute("UPDATE economy SET wallet = wallet + ?, last_beg = ? WHERE user_id = ?", (earnings, now, ctx.author.id))
        conn.commit()
        await ctx.send(f"🥺 A kind stranger gave you **${earnings}**!")
    else:
        cursor.execute("UPDATE economy SET last_beg = ? WHERE user_id = ?", (now, ctx.author.id))
        conn.commit()
        await ctx.send("😢 Everyone ignored you...")

@bot.hybrid_command(name="deposit", aliases=["dep"], description="Deposit money into bank")
async def deposit(ctx: commands.Context, amount: str):
    wallet, bank, _, _, _ = get_economy_data(ctx.author.id)
    if amount.lower() == "all":
        amt = wallet
    else:
        try: amt = int(amount)
        except ValueError: return await ctx.send("❌ Enter a valid number or 'all'.", ephemeral=True)

    if amt <= 0 or wallet < amt:
        return await ctx.send("❌ Invalid amount or insufficient cash.", ephemeral=True)

    cursor.execute("UPDATE economy SET wallet = wallet - ?, bank = bank + ? WHERE user_id = ?", (amt, amt, ctx.author.id))
    conn.commit()
    await ctx.send(f"🏦 Deposited **${amt:,}** into your bank.")

@bot.hybrid_command(name="withdraw", aliases=["with"], description="Withdraw money from bank")
async def withdraw(ctx: commands.Context, amount: str):
    wallet, bank, _, _, _ = get_economy_data(ctx.author.id)
    if amount.lower() == "all":
        amt = bank
    else:
        try: amt = int(amount)
        except ValueError: return await ctx.send("❌ Enter a valid number or 'all'.", ephemeral=True)

    if amt <= 0 or bank < amt:
        return await ctx.send("❌ Invalid amount or insufficient bank funds.", ephemeral=True)

    cursor.execute("UPDATE economy SET bank = bank - ?, wallet = wallet + ? WHERE user_id = ?", (amt, amt, ctx.author.id))
    conn.commit()
    await ctx.send(f"💵 Withdrew **${amt:,}** from your bank.")

@bot.hybrid_command(name="pay", aliases=["transfer"], description="Pay cash to another member")
async def pay(ctx: commands.Context, member: discord.Member, amount: int):
    if member.bot or member.id == ctx.author.id:
        return await ctx.send("❌ Invalid user.", ephemeral=True)
    
    sender_wallet, _, _, _, _ = get_economy_data(ctx.author.id)
    if amount <= 0 or sender_wallet < amount:
        return await ctx.send("❌ Insufficient funds in wallet.", ephemeral=True)

    get_economy_data(member.id) # Ensure target exists
    cursor.execute("UPDATE economy SET wallet = wallet - ? WHERE user_id = ?", (amount, ctx.author.id))
    cursor.execute("UPDATE economy SET wallet = wallet + ? WHERE user_id = ?", (amount, member.id))
    conn.commit()
    await ctx.send(f"💸 Sent **${amount:,}** to `{member.display_name}`!")

@bot.hybrid_command(name="rob", description="Attempt to rob cash from a user (1 hr cooldown)")
async def rob(ctx: commands.Context, member: discord.Member):
    if member.bot or member.id == ctx.author.id:
        return await ctx.send("❌ Invalid target.", ephemeral=True)

    robber_wallet, _, _, _, last_rob = get_economy_data(ctx.author.id)
    victim_wallet, _, _, _, _ = get_economy_data(member.id)
    now = int(datetime.datetime.now().timestamp())

    if now - last_rob < 3600:
        remaining = 3600 - (now - last_rob)
        return await ctx.send(f"⏳ Wait `{remaining // 60}m` before robbing again.", ephemeral=True)

    if victim_wallet < 100:
        return await ctx.send(f"❌ `{member.display_name}` is too poor to rob!", ephemeral=True)

    cursor.execute("UPDATE economy SET last_rob = ? WHERE user_id = ?", (now, ctx.author.id))

    if random.randint(1, 100) <= 45: # 45% Success chance
        stolen = random.randint(50, min(victim_wallet, 1000))
        cursor.execute("UPDATE economy SET wallet = wallet + ? WHERE user_id = ?", (stolen, ctx.author.id))
        cursor.execute("UPDATE economy SET wallet = wallet - ? WHERE user_id = ?", (stolen, member.id))
        conn.commit()
        await ctx.send(f"🥷 Success! You stole **${stolen:,}** from `{member.display_name}`!")
    else:
        fine = min(robber_wallet, 250)
        cursor.execute("UPDATE economy SET wallet = wallet - ? WHERE user_id = ?", (fine, ctx.author.id))
        conn.commit()
        await ctx.send(f"🚨 You got caught and paid a fine of **${fine:,}**!")

# --- GENERAL & MODERATION COMMANDS ---
@bot.hybrid_command(name="afk", description="Set your AFK status")
async def afk(ctx: commands.Context, *, reason: str = "AFK"):
    ts = int(datetime.datetime.now().timestamp())
    original_nick = ctx.author.nick
    cursor.execute("INSERT OR REPLACE INTO afk_users VALUES (?, ?, ?, ?)", (ctx.author.id, original_nick, reason, ts))
    conn.commit()

    new_nick = f"[AFK] {ctx.author.display_name}"[:32]
    try: await ctx.author.edit(nick=new_nick)
    except discord.Forbidden: pass

    await ctx.send(f"💤 {ctx.author.mention}, I set your AFK status to: **{reason}**")

@bot.hybrid_command(name="ban", description="Ban a member")
@commands.has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"⛔ **Banned:** `{member.display_name}` | **Reason:** {reason}")

@bot.hybrid_command(name="kick", description="Kick a member")
@commands.has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 **Kicked:** `{member.display_name}` | **Reason:** {reason}")

@bot.hybrid_command(name="timeout", description="Timeout a member")
@commands.has_permissions(moderate_members=True)
async def timeout(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await ctx.send(f"⏳ **Timed out:** `{member.display_name}` for `{minutes}m` | **Reason:** {reason}")

@bot.hybrid_command(name="addrole", description="Add role to member")
@commands.has_permissions(manage_roles=True)
async def addrole(ctx: commands.Context, member: discord.Member, role: discord.Role):
    if role in member.roles: return await ctx.send(f"❌ `{member.display_name}` already has **{role.name}**.")
    await member.add_roles(role)
    await ctx.send(f"✅ Added **{role.name}** to `{member.display_name}`.")

@bot.hybrid_command(name="autorole", description="Set autorole for new members")
@commands.has_permissions(administrator=True)
async def autorole(ctx: commands.Context, role: discord.Role):
    cursor.execute("INSERT INTO server_config (guild_id, autorole_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET autorole_id=?", (ctx.guild.id, role.id, role.id))
    conn.commit()
    await ctx.send(f"✅ **Autorole** set to **{role.name}**.")

@bot.hybrid_command(name="roleall", description="Give role to all non-bot members")
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
            except Exception: continue
    await ctx.send(f"✅ Given **{role.name}** to `{count}` members!")

@bot.hybrid_command(name="beastmode", description="Toggle anti-nuke security")
@app_commands.choices(mode=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def beastmode(ctx: commands.Context, mode: str):
    if not is_owner_or_admin(ctx.guild, ctx.author.id): return await ctx.send("❌ Unauthorized.", ephemeral=True)
    state = 1 if mode.lower() == "on" else 0
    cursor.execute("INSERT INTO server_config (guild_id, beastmode) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET beastmode=?", (ctx.guild.id, state, state))
    conn.commit()
    await ctx.send(f"🔒 Beastmode set to **{mode.upper()}**.")

@bot.hybrid_command(name="whitelist", description="Manage whitelist")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def whitelist(ctx: commands.Context, action: str, user: discord.User = None):
    if not is_owner_or_admin(ctx.guild, ctx.author.id): return await ctx.send("❌ Unauthorized.", ephemeral=True)
    if action == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='whitelist'", (ctx.guild.id,))
        users = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await ctx.send(embed=discord.Embed(title="Whitelist", description="\n".join(users) or "Empty"))
    if not user: return await ctx.send("Specify user.", ephemeral=True)
    if action == "add": cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'whitelist')", (ctx.guild.id, user.id))
    elif action == "remove": cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='whitelist'", (ctx.guild.id, user.id))
    conn.commit()
    await ctx.send(f"Updated whitelist for {user.mention}.")

@bot.hybrid_command(name="admin", description="Manage bot admins (Owner Only)")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove"), app_commands.Choice(name="list", value="list")])
async def admin(ctx: commands.Context, action: str, user: discord.User = None):
    if ctx.author.id not in BOT_OWNERS and ctx.author.id != ctx.guild.owner_id: return await ctx.send("❌ Owners only.", ephemeral=True)
    if action == "list":
        cursor.execute("SELECT user_id FROM users WHERE guild_id=? AND role_type='admin'", (ctx.guild.id,))
        admins = [f"<@{r[0]}>" for r in cursor.fetchall()]
        return await ctx.send(embed=discord.Embed(title="Admins", description="\n".join(admins) or "None"))
    if not user: return await ctx.send("Specify user.", ephemeral=True)
    if action == "add": cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?, 'admin')", (ctx.guild.id, user.id))
    elif action == "remove": cursor.execute("DELETE FROM users WHERE guild_id=? AND user_id=? AND role_type='admin'", (ctx.guild.id, user.id))
    conn.commit()
    await ctx.send(f"Updated admins for {user.mention}.")

@bot.hybrid_command(name="restore", description="Restore lost roles")
async def restore(ctx: commands.Context, user: discord.Member):
    if not is_owner_or_admin(ctx.guild, ctx.author.id): return await ctx.send("❌ Unauthorized.", ephemeral=True)
    cursor.execute("SELECT role_id FROM role_backups WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
    roles = [ctx.guild.get_role(r[0]) for r in cursor.fetchall() if ctx.guild.get_role(r[0])]
    if roles:
        await user.add_roles(*roles)
        cursor.execute("DELETE FROM role_backups WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        conn.commit()
        await ctx.send(f"✅ Restored roles to {user.mention}.")
    else:
        await ctx.send("❌ No backup found.", ephemeral=True)

@bot.hybrid_command(name="logs", description="Set log channel")
async def logs(ctx: commands.Context, channel: discord.TextChannel):
    if not is_owner_or_admin(ctx.guild, ctx.author.id): return await ctx.send("❌ Unauthorized.", ephemeral=True)
    cursor.execute("INSERT INTO server_config (guild_id, log_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=?", (ctx.guild.id, channel.id, channel.id))
    conn.commit()
    await ctx.send(f"📋 Logs set to {channel.mention}.")

# --- START BOT ---
bot.run(os.getenv('DISCORD_TOKEN'))
