
import os
import re
import json
import random
import asyncio
import time
import threading
from collections import defaultdict
from datetime import timedelta, timezone, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from dotenv import load_dotenv

load_dotenv()
TOKEN         = os.getenv("DISCORD_TOKEN")
DB_PATH       = os.getenv("BOT_DB_PATH", "bot.db")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:5000")

intents = discord.Intents.default()
intents.message_content = True
intents.members          = True

bot      = commands.Bot(command_prefix="!", intents=intents)
bot.db   = None  # assigned in setup_hook

INVITE_RE = re.compile(
    r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)\S+",
    re.IGNORECASE,
)

# ── Anti-nuke in-memory tracking ──────────────────────────────────────────────
# { guild_id: { user_id: { action: [epoch_ts, ...] } } }
nuke_tracker: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
NUKE_WINDOW              = 10   # seconds to look back
NUKE_BAN_THRESH          = 3
NUKE_CHANNEL_DEL_THRESH  = 3
NUKE_ROLE_DEL_THRESH     = 3

# ── Anti-raid in-memory tracking ──────────────────────────────────────────────
# { guild_id: [epoch_ts, ...] }
raid_tracker: dict = defaultdict(list)

# ── RPG constants ─────────────────────────────────────────────────────────────
RPG_CLASSES = {
    "warrior": {"emoji": "⚔️",  "hp": 120, "attack": 12, "defense": 10, "crit": 5},
    "mage":    {"emoji": "🔮",  "hp":  80, "attack": 20, "defense":  5, "crit": 10},
    "archer":  {"emoji": "🏹",  "hp": 100, "attack": 15, "defense":  7, "crit": 15},
    "rogue":   {"emoji": "🗡️", "hp":  90, "attack": 18, "defense":  6, "crit": 20},
}

MONSTERS = [
    {"name": "Slime 🟢",    "hp":  20, "atk":  3, "def":  0, "xp":  15, "gmin":   5, "gmax":  15},
    {"name": "Goblin 👺",   "hp":  45, "atk":  8, "def":  2, "xp":  30, "gmin":  10, "gmax":  30},
    {"name": "Skeleton 💀", "hp":  60, "atk": 12, "def":  3, "xp":  50, "gmin":  20, "gmax":  50},
    {"name": "Orc ⚔️",     "hp":  90, "atk": 18, "def":  6, "xp":  80, "gmin":  40, "gmax":  80},
    {"name": "Troll 🧌",    "hp": 140, "atk": 25, "def":  8, "xp": 130, "gmin":  60, "gmax": 120},
    {"name": "Dragon 🐉",   "hp": 250, "atk": 40, "def": 15, "xp": 300, "gmin": 150, "gmax": 300},
]

SHOP_ITEMS = {
    "iron_sword":   {"name": "Iron Sword",   "type": "weapon", "atk":  5, "def":  0, "cost":  50},
    "steel_sword":  {"name": "Steel Sword",  "type": "weapon", "atk": 12, "def":  0, "cost": 150},
    "magic_staff":  {"name": "Magic Staff",  "type": "weapon", "atk": 18, "def":  0, "cost": 200},
    "elven_bow":    {"name": "Elven Bow",    "type": "weapon", "atk": 14, "def":  0, "cost": 175},
    "leather_armor":{"name": "Leather Armor","type": "armor",  "atk":  0, "def":  3, "cost":  30},
    "iron_shield":  {"name": "Iron Shield",  "type": "armor",  "atk":  0, "def":  5, "cost":  40},
    "steel_shield": {"name": "Steel Shield", "type": "armor",  "atk":  0, "def": 10, "cost": 120},
    "dragon_plate": {"name": "Dragon Plate", "type": "armor",  "atk":  0, "def": 18, "cost": 350},
}

HUNT_COOLDOWNS: dict[int, float] = {}   # user_id → last hunt timestamp
HUNT_COOLDOWN_SECS = 30

def xp_for_level(level: int) -> int:
    """XP needed to reach the next level."""
    return level * 150

def equipment_bonuses(equip: dict) -> tuple[int, int]:
    atk = def_ = 0
    for slot_item in equip.values():
        item = SHOP_ITEMS.get(slot_item, {})
        atk  += item.get("atk", 0)
        def_ += item.get("def", 0)
    return atk, def_


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
async def init_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    # WAL mode lets the web dashboard read bot.db concurrently while the
    # bot is writing to it, without locking either side out.
    await db.execute("PRAGMA journal_mode=WAL;")

    await db.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id            INTEGER PRIMARY KEY,
            welcome_channel_id  INTEGER,
            welcome_message     TEXT,
            leave_channel_id    INTEGER,
            leave_message       TEXT,
            log_channel_id      INTEGER,
            automod_enabled     INTEGER DEFAULT 0,
            block_invites       INTEGER DEFAULT 0,
            autorole_id         INTEGER,
            block_staff_mentions INTEGER DEFAULT 0,
            welcome_style       TEXT DEFAULT 'plain',
            welcome_color       TEXT DEFAULT '#E8A33D',
            welcome_banner_url  TEXT,
            welcome_show_count  INTEGER DEFAULT 1,
            welcome_title       TEXT,
            welcome_author_text TEXT,
            welcome_footer_text TEXT
        );

        CREATE TABLE IF NOT EXISTS banned_words (
            guild_id INTEGER,
            word     TEXT,
            PRIMARY KEY (guild_id, word)
        );

        CREATE TABLE IF NOT EXISTS tags (
            guild_id   INTEGER,
            name       TEXT,
            content    TEXT,
            creator_id INTEGER,
            PRIMARY KEY (guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS reaction_roles (
            message_id INTEGER,
            emoji      TEXT,
            role_id    INTEGER,
            guild_id   INTEGER,
            PRIMARY KEY (message_id, emoji)
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            reason     TEXT    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS antinuke_config (
            guild_id                  INTEGER PRIMARY KEY,
            enabled                   INTEGER DEFAULT 0,
            log_channel_id            INTEGER,
            action                    TEXT    DEFAULT 'kick',
            ban_threshold             INTEGER DEFAULT 3,
            channel_delete_threshold  INTEGER DEFAULT 3,
            role_delete_threshold     INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS antinuke_whitelist (
            guild_id INTEGER,
            user_id  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS antiraid_config (
            guild_id            INTEGER PRIMARY KEY,
            enabled             INTEGER DEFAULT 0,
            log_channel_id      INTEGER,
            join_threshold      INTEGER DEFAULT 10,
            join_window         INTEGER DEFAULT 10,
            action              TEXT    DEFAULT 'kick',
            min_account_age_days INTEGER DEFAULT 7
        );

        CREATE TABLE IF NOT EXISTS rpg_characters (
            user_id   INTEGER,
            guild_id  INTEGER,
            class     TEXT,
            level     INTEGER DEFAULT 1,
            xp        INTEGER DEFAULT 0,
            hp        INTEGER,
            max_hp    INTEGER,
            attack    INTEGER,
            defense   INTEGER,
            gold      INTEGER DEFAULT 100,
            equipment TEXT    DEFAULT '{}',
            PRIMARY KEY (user_id, guild_id)
        );

        CREATE TABLE IF NOT EXISTS whitelist (
            guild_id    INTEGER,
            user_id     INTEGER,
            expires_at  TEXT    NOT NULL,
            granted_by  INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS afk (
            guild_id    INTEGER,
            user_id     INTEGER,
            reason      TEXT    NOT NULL DEFAULT 'AFK',
            old_nick    TEXT,
            set_at      TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            message_id  INTEGER PRIMARY KEY,
            channel_id  INTEGER NOT NULL,
            guild_id    INTEGER NOT NULL,
            host_id     INTEGER NOT NULL,
            prize       TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            winners     INTEGER DEFAULT 1,
            ends_at     TEXT    NOT NULL,
            ended       INTEGER DEFAULT 0,
            entries     TEXT    DEFAULT '[]',
            daily_messages       INTEGER DEFAULT 0,
            weekly_messages      INTEGER DEFAULT 0,
            monthly_messages     INTEGER DEFAULT 0,
            bypass_role_id       INTEGER,
            extra_entries_role_id INTEGER,
            extra_entries        INTEGER DEFAULT 1,
            image_filename       TEXT
        );

        CREATE TABLE IF NOT EXISTS message_activity (
            guild_id       INTEGER,
            user_id        INTEGER,
            day_start      TEXT NOT NULL,
            week_start     TEXT NOT NULL,
            month_start    TEXT NOT NULL,
            daily_count    INTEGER DEFAULT 0,
            weekly_count   INTEGER DEFAULT 0,
            monthly_count  INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS command_settings (
            guild_id         INTEGER NOT NULL,
            command_name     TEXT    NOT NULL,
            enabled          INTEGER DEFAULT 1,
            allowed_role_ids TEXT    DEFAULT '[]',
            PRIMARY KEY (guild_id, command_name)
        );
    """)
    await db.commit()
    return db


# ── Migration: add columns added after initial schema ─────────────────────────
async def run_migrations(db: aiosqlite.Connection):
    migrations = [
        "ALTER TABLE guild_config ADD COLUMN block_invites INTEGER DEFAULT 0",
        "ALTER TABLE guild_config ADD COLUMN autorole_id INTEGER",
        "ALTER TABLE guild_config ADD COLUMN block_staff_mentions INTEGER DEFAULT 0",
        "ALTER TABLE tags         ADD COLUMN creator_id INTEGER",
        "ALTER TABLE giveaways ADD COLUMN daily_messages INTEGER DEFAULT 0",
        "ALTER TABLE giveaways ADD COLUMN weekly_messages INTEGER DEFAULT 0",
        "ALTER TABLE giveaways ADD COLUMN monthly_messages INTEGER DEFAULT 0",
        "ALTER TABLE giveaways ADD COLUMN bypass_role_id INTEGER",
        "ALTER TABLE giveaways ADD COLUMN extra_entries_role_id INTEGER",
        "ALTER TABLE giveaways ADD COLUMN extra_entries INTEGER DEFAULT 1",
        "ALTER TABLE giveaways ADD COLUMN image_filename TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_style TEXT DEFAULT 'plain'",
        "ALTER TABLE guild_config ADD COLUMN welcome_color TEXT DEFAULT '#E8A33D'",
        "ALTER TABLE guild_config ADD COLUMN welcome_banner_url TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_show_count INTEGER DEFAULT 1",
        "ALTER TABLE guild_config ADD COLUMN welcome_title TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_author_text TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_footer_text TEXT",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass
    await db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────
async def ensure_guild_config(guild_id: int):
    await bot.db.execute(
        "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
    )
    await bot.db.commit()


async def get_guild_config(guild_id: int):
    await ensure_guild_config(guild_id)
    async with bot.db.execute(
        "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
    ) as cur:
        return await cur.fetchone()


def has_perm(interaction: discord.Interaction, perm: str) -> bool:
    return getattr(interaction.user.guild_permissions, perm, False)


def is_mod(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions
    return p.manage_messages or p.manage_guild or p.administrator


def is_staff_member(member: discord.Member) -> bool:
    """Return whether a member has moderator-or-higher permissions."""
    p = member.guild_permissions
    return (
        p.manage_messages
        or p.manage_guild
        or p.administrator
        or any(is_staff_role(role) for role in member.roles)
    )


def is_staff_role(role: discord.Role) -> bool:
    """Recognize staff roles by permissions or common staff naming."""
    p = role.permissions
    return (
        p.manage_messages
        or p.manage_guild
        or p.administrator
        or bool(
            re.search(
                r"\b(owner|admin|administrator|moderator|mod|staff|manager|helper)\b",
                role.name,
                re.IGNORECASE,
            )
        )
    )


async def is_whitelisted(guild_id: int, user_id: int) -> bool:
    async with bot.db.execute(
        "SELECT 1 FROM whitelist WHERE guild_id=? AND user_id=? AND expires_at > datetime('now')",
        (guild_id, user_id),
    ) as cur:
        return await cur.fetchone() is not None


# ── Dashboard-managed command gate ─────────────────────────────────────────
# The web dashboard lets admins turn whole command groups on/off per server,
# and restrict them to specific roles. This runs before every slash command.
def command_root_name(command: app_commands.Command | app_commands.Group) -> str:
    return command.root_parent.name if command.root_parent else command.name


async def command_gate(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.command is None:
        return True

    root = command_root_name(interaction.command)
    async with bot.db.execute(
        "SELECT enabled, allowed_role_ids FROM command_settings WHERE guild_id=? AND command_name=?",
        (interaction.guild.id, root),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        return True  # no dashboard override on file -> allowed by default

    enabled, allowed_role_ids = row
    if not enabled:
        await interaction.response.send_message(
            f"🚫 `/{root}` has been turned off for this server.", ephemeral=True
        )
        return False

    allowed = json.loads(allowed_role_ids or "[]")
    if allowed and isinstance(interaction.user, discord.Member):
        member = interaction.user
        if not member.guild_permissions.administrator and not any(
            r.id in allowed for r in member.roles
        ):
            await interaction.response.send_message(
                f"🚫 You don't have a role that's allowed to use `/{root}` here.",
                ephemeral=True,
            )
            return False

    return True


bot.tree.interaction_check = command_gate


def _message_period_starts(now: datetime | None = None) -> tuple[str, str, str]:
    now = now or datetime.utcnow()
    day_start = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m")
    return day_start, week_start, month_start


async def record_message_activity(guild_id: int, user_id: int):
    """Count one human message in the current UTC day, week, and month."""
    day_start, week_start, month_start = _message_period_starts()
    async with bot.db.execute(
        """SELECT day_start, week_start, month_start, daily_count,
                  weekly_count, monthly_count
           FROM message_activity WHERE guild_id=? AND user_id=?""",
        (guild_id, user_id),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        await bot.db.execute(
            """INSERT INTO message_activity
                   (guild_id, user_id, day_start, week_start, month_start,
                    daily_count, weekly_count, monthly_count)
               VALUES (?, ?, ?, ?, ?, 1, 1, 1)""",
            (guild_id, user_id, day_start, week_start, month_start),
        )
    else:
        old_day, old_week, old_month, daily, weekly, monthly = row
        await bot.db.execute(
            """UPDATE message_activity
               SET day_start=?, week_start=?, month_start=?,
                   daily_count=?, weekly_count=?, monthly_count=?
               WHERE guild_id=? AND user_id=?""",
            (
                day_start,
                week_start,
                month_start,
                daily + 1 if old_day == day_start else 1,
                weekly + 1 if old_week == week_start else 1,
                monthly + 1 if old_month == month_start else 1,
                guild_id,
                user_id,
            ),
        )
    await bot.db.commit()


async def get_message_activity(guild_id: int, user_id: int) -> tuple[int, int, int]:
    """Return current UTC day, week, and month message counts."""
    day_start, week_start, month_start = _message_period_starts()
    async with bot.db.execute(
        """SELECT day_start, week_start, month_start, daily_count,
                  weekly_count, monthly_count
           FROM message_activity WHERE guild_id=? AND user_id=?""",
        (guild_id, user_id),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0, 0, 0
    old_day, old_week, old_month, daily, weekly, monthly = row
    return (
        daily if old_day == day_start else 0,
        weekly if old_week == week_start else 0,
        monthly if old_month == month_start else 0,
    )


# ── RPG helpers ───────────────────────────────────────────────────────────────
async def get_character(user_id: int, guild_id: int):
    async with bot.db.execute(
        "SELECT * FROM rpg_characters WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    ) as cur:
        return await cur.fetchone()


async def save_character(user_id: int, guild_id: int, data: dict):
    await bot.db.execute(
        """UPDATE rpg_characters
           SET level=?, xp=?, hp=?, max_hp=?, attack=?, defense=?, gold=?, equipment=?
           WHERE user_id=? AND guild_id=?""",
        (
            data["level"], data["xp"], data["hp"], data["max_hp"],
            data["attack"], data["defense"], data["gold"],
            json.dumps(data["equipment"]),
            user_id, guild_id,
        ),
    )
    await bot.db.commit()


def row_to_char(row) -> dict:
    return {
        "user_id":  row[0], "guild_id": row[1], "class":   row[2],
        "level":    row[3], "xp":       row[4], "hp":      row[5],
        "max_hp":   row[6], "attack":   row[7], "defense": row[8],
        "gold":     row[9], "equipment": json.loads(row[10] or "{}"),
    }


async def check_level_up(user_id: int, guild_id: int, char: dict) -> list[str]:
    msgs = []
    while char["xp"] >= xp_for_level(char["level"]):
        char["xp"]     -= xp_for_level(char["level"])
        char["level"]  += 1
        char["max_hp"] += 10
        char["hp"]      = char["max_hp"]
        char["attack"]  += 2
        char["defense"] += 1
        msgs.append(f"🎉 Level up! Now **Level {char['level']}**!")
    return msgs


# ═══════════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def setup_hook():
    bot.db = await init_db()
    await run_migrations(bot.db)
    cleanup_whitelist.start()
    # Register persistent views so buttons survive bot restarts
    bot.add_view(GiveawayView())
    bot.add_view(GiveawayEndedView())
    giveaway_ticker.start()


BOT_NAME        = "HEAVENLY"
BOT_AVATAR_PATH = "avatar.jpg"


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Sync error: {e}")

    try:
        if bot.user.name != BOT_NAME:
            await bot.user.edit(username=BOT_NAME)
        if os.path.exists(BOT_AVATAR_PATH):
            with open(BOT_AVATAR_PATH, "rb") as f:
                await bot.user.edit(avatar=f.read())
    except discord.HTTPException as e:
        print(f"Profile update skipped (rate-limited?): {e}")

    print(f"Online as {bot.user} ({bot.user.id})")


@tasks.loop(minutes=5)
async def cleanup_whitelist():
    """Purge expired whitelist entries every 5 minutes."""
    await bot.db.execute("DELETE FROM whitelist WHERE expires_at <= datetime('now')")
    await bot.db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# HELP
# ═══════════════════════════════════════════════════════════════════════════════
HELP_CATEGORIES = {
    "moderation": {
        "label": "Moderation",
        "emoji": "🛡️",
        "description": "Keep conversations safe and handle members with precision.",
        "color": discord.Color.red(),
        "access": "Most actions require the matching Discord moderation permission.",
        "commands": (
            "`/kick member [reason]` — Kick a member\n"
            "`/ban member [reason]` — Ban a member\n"
            "`/unban user_id` — Unban a user by ID\n"
            "`/mute member minutes [reason]` — Timeout a member\n"
            "`/unmute member` — Remove a timeout\n"
            "`/warn member [reason]` — Add a warning\n"
            "`/warnings member` — View warning history\n"
            "`/warnings-clear member` — Clear all warnings\n"
            "`/clear amount` — Delete up to 100 messages"
        ),
    },
    "security": {
        "label": "Security",
        "emoji": "🔐",
        "description": "Configure temporary staff access and anti-destruction protection.",
        "color": discord.Color.dark_red(),
        "access": "Requires Manage Server unless otherwise stated.",
        "commands": (
            "`/whitelist add user minutes` — Grant temporary warn/mute access\n"
            "`/whitelist remove user` — Revoke temporary access\n"
            "`/whitelist list` — Show active temporary grants\n"
            "`/antinuke setup channel` — Set anti-nuke alert channel\n"
            "`/antinuke toggle enabled` — Enable or disable anti-nuke\n"
            "`/antinuke action action` — Choose kick or ban\n"
            "`/antinuke thresholds [bans] [channel_deletes] [role_deletes]`\n"
            "`/antinuke whitelist-add user` — Exempt a user or bot\n"
            "`/antinuke whitelist-remove user` — Remove an exemption"
        ),
    },
    "antiraid": {
        "label": "Anti-Raid",
        "emoji": "🚨",
        "description": "Detect join floods and respond before your server gets overwhelmed.",
        "color": discord.Color.orange(),
        "access": "Requires Manage Server.",
        "commands": (
            "`/antiraid setup channel` — Set raid alert channel\n"
            "`/antiraid toggle enabled` — Enable or disable anti-raid\n"
            "`/antiraid action action` — Kick, ban, or lockdown\n"
            "`/antiraid thresholds [joins] [window] [min_age_days]` — Tune detection"
        ),
    },
    "automod": {
        "label": "Auto-Moderation",
        "emoji": "🤖",
        "description": "Automatically block unwanted content and invite links.",
        "color": discord.Color.teal(),
        "access": "Requires Manage Server.",
        "commands": (
            "`/automod toggle enabled` — Enable or disable auto-moderation\n"
            "`/automod block-invites enabled` — Block Discord invite links\n"
        "`/automod block-staff-mentions enabled` — Delete pings targeting moderators or admins\n"
            "`/automod addword word` — Add one or more banned words, separated by commas\n"
            "`/automod removeword word` — Remove a banned word\n"
            "`/automod listwords` — View all banned words"
        ),
    },
    "community": {
        "label": "Community",
        "emoji": "🌟",
        "description": "Welcome new members, manage tags, and automate role experiences.",
        "color": discord.Color.gold(),
        "access": "Some setup commands require Manage Server, Manage Messages, or Manage Roles.",
        "commands": (
            "`/welcome set channel [message]` — Configure welcome messages\n"
            "`/welcome style/color/banner/title/author/footer/count/preview` — Customize the welcome card\n"
            "`/leave set channel [message]` — Configure leave messages\n"
            "`/autorole set role` — Assign a role to new members\n"
            "`/autorole disable` — Disable autorole\n"
            "`/autorole view` — Show the current autorole\n"
            "`/tag create name content` — Create a custom response\n"
            "`/tag delete name` — Delete a custom response\n"
            "`/tag get name` — Display a tag\n"
            "`/tag list` — List server tags\n"
            "`!tagname` — Trigger a tag with the prefix"
        ),
    },
    "roles": {
        "label": "Roles",
        "emoji": "🎭",
        "description": "Build self-serve role systems and manage member roles safely.",
        "color": discord.Color.purple(),
        "access": "Requires Manage Roles.",
        "commands": (
            "`/reactionrole add channel message_id emoji role` — Link a reaction to a role\n"
            "`/reactionrole remove message_id emoji` — Remove a reaction-role link\n"
            "`/role give member role` — Give a role to a member\n"
            "`/role take member role` — Remove a role from a member\n"
            "`/role removeall member` — Remove all assignable roles"
        ),
    },
    "rpg": {
        "label": "RPG Adventure",
        "emoji": "⚔️",
        "description": "Create a character, hunt monsters, collect loot, and duel your friends.",
        "color": discord.Color.blurple(),
        "access": "Available to all members. Characters are stored separately per server.",
        "commands": (
            "`/rpg start cls` — Choose Warrior, Mage, Archer, or Rogue\n"
            "`/rpg profile [user]` — View a character\n"
            "`/rpg hunt` — Fight a monster for XP and gold\n"
            "`/rpg rest` — Restore HP for gold\n"
            "`/rpg shop` — Browse weapons and armor\n"
            "`/rpg buy item_id` — Buy and equip an item\n"
            "`/rpg inventory` — View equipped items\n"
            "`/rpg duel user` — Challenge another player"
        ),
    },
    "giveaways": {
        "label": "Giveaways",
        "emoji": "🎁",
        "description": "Launch polished giveaways with requirements, bonus chances, and images.",
        "color": discord.Color.magenta(),
        "access": "Requires Manage Server to create, end, or reroll giveaways.",
        "commands": (
            "`/giveaway create prize duration [winners] [channel] [description]`\n"
            "   Add `[daily_messages] [weekly_messages] [monthly_messages]` requirements.\n"
            "   Add `[bypass_role] [extra_entries_role] [extra_entries] [image]` options.\n"
            "`/giveaway end message_id` — End a giveaway immediately\n"
            "`/giveaway reroll message_id` — Pick a new winner"
        ),
    },
    "utility": {
        "label": "Utility",
        "emoji": "✨",
        "description": "Small tools that make the server feel more alive.",
        "color": discord.Color.green(),
        "access": "Available to all members unless Discord permissions are noted.",
        "commands": (
            "`/afk [reason]` — Set your AFK status\n"
            "`/setlogchannel channel` — Configure message, member, role, moderation, and server event logs\n"
            "`/help` — Open this interactive command center"
        ),
    },
}


def _help_overview_embed(interaction: discord.Interaction) -> discord.Embed:
    embed = discord.Embed(
        title="✦ HEAVENLY COMMAND CENTER ✦",
        description=(
            "Your server toolkit, organized and ready.\n"
            "Choose a category below to explore commands, options, and access notes."
        ),
        color=discord.Color.from_rgb(124, 92, 255),
    )
    embed.set_author(
        name=f"{interaction.guild.name} • Interactive Help",
        icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else discord.Embed.Empty,
    )
    embed.add_field(
        name="⚡ Quick start",
        value="Use the menu below to jump straight to the tools you need.",
        inline=False,
    )
    category_lines = "\n".join(
        f"{data['emoji']} **{data['label']}** — {data['description']}"
        for data in HELP_CATEGORIES.values()
    )
    embed.add_field(name="Explore the modules", value=category_lines, inline=False)
    embed.add_field(
        name="🧭 Command style",
        value="Everything is a slash command except custom tags, which can also be called with `!tagname`.",
        inline=False,
    )
    embed.set_footer(text="HEAVENLY • Select a category to reveal its controls")
    return embed


def _help_category_embed(interaction: discord.Interaction, category_key: str) -> discord.Embed:
    data = HELP_CATEGORIES[category_key]
    embed = discord.Embed(
        title=f"{data['emoji']} {data['label']}",
        description=data["description"],
        color=data["color"],
    )
    embed.set_author(
        name=f"{interaction.guild.name} • Command Module",
        icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else discord.Embed.Empty,
    )
    embed.add_field(name="Available commands", value=data["commands"], inline=False)
    embed.add_field(name="Access", value=f"🔑 {data['access']}", inline=False)
    embed.set_footer(text="Use the menu to switch modules • /help to return home")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        options = [
            discord.SelectOption(
                label="Overview",
                value="overview",
                description="Return to the command center",
            )
        ]
        options.extend(
            discord.SelectOption(
                label=data["label"],
                value=key,
                description=data["description"][:100],
            )
            for key, data in HELP_CATEGORIES.items()
        )
        super().__init__(
            placeholder="Select a command category…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"help_category_{owner_id}",
        )
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message(
                "This help panel belongs to the person who opened it.", ephemeral=True)
        if self.values[0] == "overview":
            embed = _help_overview_embed(interaction)
        else:
            embed = _help_category_embed(interaction, self.values[0])
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(HelpCategorySelect(owner_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This help panel belongs to the person who opened it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Home", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=_help_overview_embed(interaction), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✨ Help panel closed. Run `/help` whenever you need it again.",
            embed=None,
            view=None,
        )


@bot.tree.command(name="help", description="List all available commands")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=_help_overview_embed(interaction),
        view=HelpView(interaction.user.id),
        ephemeral=True,
    )


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def _send_welcome(member: discord.Member, cfg):
    channel_id, raw_message = cfg[1], cfg[2]
    if not channel_id:
        return
    ch = member.guild.get_channel(channel_id)
    if not ch:
        return

    style = cfg[10] or "plain"
    color_hex = cfg[11] or "#E8A33D"
    banner_url = cfg[12]
    show_count = bool(cfg[13]) if cfg[13] is not None else True
    title_tpl = cfg[14]
    author_tpl = cfg[15]
    footer_tpl = cfg[16]

    count = member.guild.member_count
    fmt_kwargs = dict(
        member=member.mention, server=member.guild.name,
        count=count, ordinal=_ordinal(count),
    )
    text = (raw_message or "Welcome {member} to {server}!").format(**fmt_kwargs)

    if style == "embed":
        try:
            color = discord.Color(int(color_hex.lstrip("#"), 16))
        except (ValueError, AttributeError):
            color = discord.Color.from_rgb(0xE8, 0xA3, 0x3D)

        embed = discord.Embed(description=text, color=color)
        if title_tpl:
            embed.title = title_tpl.format(**fmt_kwargs)
        embed.set_author(
            name=(author_tpl.format(**fmt_kwargs) if author_tpl else member.guild.name),
            icon_url=member.guild.icon.url if member.guild.icon else discord.utils.MISSING,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if banner_url:
            embed.set_image(url=banner_url)

        if footer_tpl:
            footer = footer_tpl.format(**fmt_kwargs)
        else:
            joined = discord.utils.format_dt(member.joined_at or datetime.now(timezone.utc), style="F")
            footer = f"Joined on {joined}"
            if show_count:
                footer = f"You are our {_ordinal(count)} member • " + footer
        embed.set_footer(text=footer)
        await ch.send(embed=embed)
    else:
        if member.mention not in text:
            text = f"{member.mention} {text}"
        await ch.send(text)


# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME / LEAVE
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_member_join(member: discord.Member):
    cfg = await get_guild_config(member.guild.id)
    await _send_welcome(member, cfg)

    e = discord.Embed(title="📥 Member Joined", color=discord.Color.green())
    e.add_field(name="Member", value=f"{member.mention} (`{member}`)", inline=True)
    e.add_field(name="Account Created", value=discord.utils.format_dt(
        member.created_at, style="R"), inline=True)
    e.set_footer(text=f"User ID: {member.id}")
    await _send_server_log(member.guild, e)
    # Autorole
    if cfg[8]:
        role = member.guild.get_role(cfg[8])
        if role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.Forbidden:
                pass
    # Anti-raid join tracking handled separately in on_member_join (see below)
    await antiraid_check(member)


@bot.event
async def on_member_remove(member: discord.Member):
    cfg = await get_guild_config(member.guild.id)
    if cfg[3]:
        ch = member.guild.get_channel(cfg[3])
        if ch:
            text = (cfg[4] or "{member} has left {server}.").format(
                member=str(member), server=member.guild.name)
            await ch.send(text)

    kick_entry = await _find_recent_audit_entry(
        member.guild, discord.AuditLogAction.kick, member.id)
    if kick_entry:
        actor = kick_entry.user
        e = discord.Embed(title="👢 Member Kicked", color=discord.Color.orange())
        e.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=True)
        e.add_field(name="Moderator", value=str(actor), inline=True)
        e.add_field(name="Reason", value=kick_entry.reason or "*No reason provided*", inline=False)
        await _send_server_log(member.guild, e)


welcome_group = app_commands.Group(name="welcome", description="Configure welcome messages")
leave_group   = app_commands.Group(name="leave",   description="Configure leave messages")


@welcome_group.command(name="set", description="Set the welcome channel and message")
@app_commands.describe(channel="Channel", message="Use {member} and {server}")
async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel,
                      message: str = "Welcome {member} to {server}!"):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_channel_id=?, welcome_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Welcome → {channel.mention}")


@welcome_group.command(name="style", description="Plain text or a rich embed card")
@app_commands.describe(style="How the welcome message looks")
@app_commands.choices(style=[
    app_commands.Choice(name="Plain text", value="plain"),
    app_commands.Choice(name="Embed card", value="embed"),
])
async def welcome_style(interaction: discord.Interaction, style: app_commands.Choice[str]):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_style=? WHERE guild_id=?",
        (style.value, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Welcome style set to **{style.name}**.")


@welcome_group.command(name="color", description="Set the embed card's accent color (embed style only)")
@app_commands.describe(hex_color="A hex color, e.g. #E8A33D")
async def welcome_color(interaction: discord.Interaction, hex_color: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    hex_color = hex_color.strip()
    if not hex_color.startswith("#"):
        hex_color = f"#{hex_color}"
    try:
        int(hex_color.lstrip("#"), 16)
    except ValueError:
        return await interaction.response.send_message(
            "That doesn't look like a hex color — try something like `#E8A33D`.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_color=? WHERE guild_id=?",
        (hex_color, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Welcome color set to `{hex_color}`.")


@welcome_group.command(name="banner", description="Set (or clear) the embed card's banner image")
@app_commands.describe(image_url="Direct image URL — leave empty to remove the banner")
async def welcome_banner(interaction: discord.Interaction, image_url: str = ""):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_banner_url=? WHERE guild_id=?",
        (image_url or None, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        "✅ Banner cleared." if not image_url else "✅ Banner image updated.")


@welcome_group.command(name="title", description="Set the embed card's title (embed style only)")
@app_commands.describe(text="Use {member}, {server}, {count}, {ordinal} — leave empty to remove the title")
async def welcome_title(interaction: discord.Interaction, text: str = ""):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_title=? WHERE guild_id=?",
        (text or None, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message("✅ Title cleared." if not text else "✅ Title updated.")


@welcome_group.command(name="author", description="Set the small line above the title (embed style only)")
@app_commands.describe(text="Use {member}, {server}, {count}, {ordinal} — leave empty to use the server name")
async def welcome_author(interaction: discord.Interaction, text: str = ""):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_author_text=? WHERE guild_id=?",
        (text or None, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        "✅ Author line reset to server name." if not text else "✅ Author line updated.")


@welcome_group.command(name="footer", description="Set the embed card's footer text (embed style only)")
@app_commands.describe(text="Use {member}, {server}, {count}, {ordinal} — leave empty for the default")
async def welcome_footer(interaction: discord.Interaction, text: str = ""):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_footer_text=? WHERE guild_id=?",
        (text or None, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        "✅ Footer reset to default." if not text else "✅ Footer updated.")


@welcome_group.command(name="count", description="Show or hide the 'you are our Nth member' line")
@app_commands.describe(show="Show the member count in the embed footer")
async def welcome_count(interaction: discord.Interaction, show: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET welcome_show_count=? WHERE guild_id=?",
        (1 if show else 0, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Member count in welcome card is now **{'on' if show else 'off'}**.")


@welcome_group.command(name="preview", description="Preview the welcome message as it looks right now")
async def welcome_preview(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    cfg = await get_guild_config(interaction.guild.id)
    if not cfg[1]:
        return await interaction.response.send_message(
            "No welcome channel set yet — run `/welcome set` first.", ephemeral=True)
    await interaction.response.send_message("Here's a preview 👇", ephemeral=True)
    await _send_welcome(interaction.user, cfg)


@leave_group.command(name="set", description="Set the leave channel and message")
@app_commands.describe(channel="Channel", message="Use {member} and {server}")
async def leave_set(interaction: discord.Interaction, channel: discord.TextChannel,
                    message: str = "{member} has left {server}."):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET leave_channel_id=?, leave_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Leave → {channel.mention}")


bot.tree.add_command(welcome_group)
bot.tree.add_command(leave_group)


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="setlogchannel", description="Set channel for server event logs")
@app_commands.describe(channel="Log channel")
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET log_channel_id=? WHERE guild_id=?",
        (channel.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Logs → {channel.mention}")


async def _send_server_log(guild: discord.Guild, embed: discord.Embed):
    """Send an event embed to the configured server log channel."""
    cfg = await get_guild_config(guild.id)
    if not cfg[5]:
        return
    channel = guild.get_channel(cfg[5])
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass


async def _find_recent_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
):
    """Find a recent audit-log entry for a specific target."""
    try:
        now = datetime.now(timezone.utc)
        async for entry in guild.audit_logs(limit=10, action=action):
            if getattr(entry.target, "id", None) != target_id:
                continue
            if entry.created_at and (now - entry.created_at).total_seconds() <= 15:
                return entry
    except discord.Forbidden:
        return None
    return None


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    e = discord.Embed(title="🗑️ Message Deleted", color=discord.Color.red())
    e.add_field(name="Author", value=str(message.author), inline=True)
    e.add_field(name="Channel", value=message.channel.mention, inline=True)
    e.add_field(name="Content", value=message.content or "*[no text]*", inline=False)
    e.set_footer(text=f"User ID: {message.author.id} • Message ID: {message.id}")
    await _send_server_log(message.guild, e)


@bot.event
async def on_bulk_message_delete(messages: list[discord.Message]):
    if not messages:
        return
    guild = messages[0].guild
    if not guild:
        return
    channel = messages[0].channel
    e = discord.Embed(title="🧹 Messages Bulk Deleted", color=discord.Color.dark_red())
    e.add_field(name="Channel", value=channel.mention, inline=True)
    e.add_field(name="Count", value=str(len(messages)), inline=True)
    e.set_footer(text=f"Channel ID: {channel.id}")
    await _send_server_log(guild, e)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    e = discord.Embed(title="✏️ Message Edited", color=discord.Color.orange())
    e.add_field(name="Author", value=str(before.author), inline=True)
    e.add_field(name="Channel", value=before.channel.mention, inline=True)
    e.add_field(name="Before", value=before.content or "*[empty]*", inline=False)
    e.add_field(name="After", value=after.content or "*[empty]*", inline=False)
    e.set_footer(text=f"User ID: {before.author.id} • Message ID: {before.id}")
    await _send_server_log(before.guild, e)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_roles = {role.id: role for role in before.roles}
    after_roles = {role.id: role for role in after.roles}
    added_roles = [role for role_id, role in after_roles.items() if role_id not in before_roles]
    removed_roles = [role for role_id, role in before_roles.items() if role_id not in after_roles]

    if not added_roles and not removed_roles:
        return

    entry = await _find_recent_audit_entry(
        after.guild, discord.AuditLogAction.member_role_update, after.id)
    actor = entry.user if entry and entry.user else None
    actor_text = str(actor) if actor else "Unknown / automatic action"

    for role in added_roles:
        e = discord.Embed(title="➕ Role Added", color=discord.Color.green())
        e.add_field(name="Member", value=f"{after.mention} (`{after}`)", inline=True)
        e.add_field(name="Role", value=f"{role.mention} (`{role.name}`)", inline=True)
        e.add_field(name="Changed By", value=actor_text, inline=False)
        await _send_server_log(after.guild, e)

    for role in removed_roles:
        e = discord.Embed(title="➖ Role Removed", color=discord.Color.orange())
        e.add_field(name="Member", value=f"{after.mention} (`{after}`)", inline=True)
        e.add_field(name="Role", value=f"{role.name}", inline=True)
        e.add_field(name="Changed By", value=actor_text, inline=False)
        await _send_server_log(after.guild, e)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-MODERATION
# ═══════════════════════════════════════════════════════════════════════════════
automod_group = app_commands.Group(name="automod", description="Configure auto-moderation")

MASS_MENTION_THRESHOLD = 5


@automod_group.command(name="toggle", description="Enable or disable auto-moderation")
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET automod_enabled=?, block_staff_mentions=? WHERE guild_id=?",
        (int(enabled), int(enabled), interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Auto-mod {'**ON**' if enabled else '**OFF**'} "
        f"(including staff-ping blocking)."
    )


@automod_group.command(name="block-invites", description="Block Discord invite links")
async def automod_block_invites(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET block_invites=? WHERE guild_id=?",
        (int(enabled), interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Invite blocking {'**ON**' if enabled else '**OFF**'}.")


@automod_group.command(
    name="block-staff-mentions",
    description="Delete messages that ping moderators or administrators",
)
async def automod_block_staff_mentions(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET block_staff_mentions=? WHERE guild_id=?",
        (int(enabled), interaction.guild.id),
    )
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Staff mention blocking {'**ON**' if enabled else '**OFF**'}."
    )


@automod_group.command(name="addword", description="Add one or more banned words (comma-separated)")
async def automod_addword(interaction: discord.Interaction, word: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)

    words = list(dict.fromkeys(
        item.strip().lower()
        for item in word.split(",")
        if item.strip()
    ))
    if not words:
        return await interaction.response.send_message(
            "Please provide at least one word.", ephemeral=True)

    await bot.db.executemany(
        "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
        [(interaction.guild.id, item) for item in words])
    await bot.db.commit()
    formatted_words = ", ".join(f"`{item}`" for item in words)
    await interaction.response.send_message(
        f"✅ Added {len(words)} banned word(s): {formatted_words}.",
        ephemeral=True)


@automod_group.command(name="removeword", description="Remove a banned word")
async def automod_removeword(interaction: discord.Interaction, word: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM banned_words WHERE guild_id=? AND word=?",
        (interaction.guild.id, word.lower()))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Unbanned `{word}`.", ephemeral=True)


@automod_group.command(name="listwords", description="List all banned words")
async def automod_listwords(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    async with bot.db.execute(
        "SELECT word FROM banned_words WHERE guild_id=? ORDER BY word",
        (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No banned words yet.", ephemeral=True)
    await interaction.response.send_message(
        "🚫 **Banned words:** " + ", ".join(f"`{r[0]}`" for r in rows), ephemeral=True)


bot.tree.add_command(automod_group)


async def run_automod(message: discord.Message):
    cfg = await get_guild_config(message.guild.id)
    automod_on    = cfg[6]
    block_invites = cfg[7] if len(cfg) > 7 else 0
    block_staff_mentions = cfg[9] if len(cfg) > 9 else 0
    staff_mentions_on = bool(automod_on or block_staff_mentions)

    if not automod_on and not block_invites:
        return

    async def delete_warn(text: str):
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention}, {text}", delete_after=6)
        except discord.Forbidden:
            print(
                f"AutoMod could not delete message {message.id} in "
                f"{message.guild.id}/{message.channel.id}: missing Manage Messages"
            )
        except discord.NotFound:
            pass

    if automod_on and len(message.mentions) >= MASS_MENTION_THRESHOLD:
        return await delete_warn("please don't mass-mention members.")

    if block_invites and INVITE_RE.search(message.content):
        return await delete_warn("posting invite links is not allowed here.")

    if staff_mentions_on and not is_staff_member(message.author):
        mentioned_staff_member = any(
            is_staff_member(member)
            for member in message.mentions
        )
        mentioned_staff_role = any(
            is_staff_role(role)
            for role in message.role_mentions
        )
        if mentioned_staff_member or mentioned_staff_role:
            return await delete_warn(
                "please don't ping moderators or administrators."
            )

    if automod_on:
        async with bot.db.execute(
            "SELECT word FROM banned_words WHERE guild_id=?", (message.guild.id,)
        ) as cur:
            rows = await cur.fetchall()
        content_lower = message.content.lower()
        for (word,) in rows:
            if re.search(rf"\b{re.escape(word)}\b", content_lower):
                return await delete_warn("that message contained a banned word.")


# ═══════════════════════════════════════════════════════════════════════════════
# ANTI-NUKE
# ═══════════════════════════════════════════════════════════════════════════════
antinuke_group = app_commands.Group(name="antinuke", description="Anti-nuke protection")


@antinuke_group.command(name="setup", description="Set the anti-nuke log channel")
@app_commands.describe(channel="Where to send nuke alerts")
async def antinuke_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, log_channel_id) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (interaction.guild.id, channel.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Anti-nuke alerts → {channel.mention}")


@antinuke_group.command(name="toggle", description="Enable or disable anti-nuke")
async def antinuke_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, enabled) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
        (interaction.guild.id, int(enabled)))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Anti-nuke {'**ON**' if enabled else '**OFF**'}.")


@antinuke_group.command(name="action",
    description="Action when nuke detected: kick or ban")
@app_commands.choices(action=[
    app_commands.Choice(name="Kick the nuker", value="kick"),
    app_commands.Choice(name="Ban the nuker",  value="ban"),
])
async def antinuke_action(interaction: discord.Interaction,
                          action: app_commands.Choice[str]):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antinuke_config (guild_id, action) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET action=excluded.action",
        (interaction.guild.id, action.value))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Nuke action set to **{action.value}**.")


@antinuke_group.command(name="thresholds",
    description="Set how many actions in 10s trigger anti-nuke")
@app_commands.describe(bans="Ban threshold", channel_deletes="Channel-delete threshold",
                       role_deletes="Role-delete threshold")
async def antinuke_thresholds(interaction: discord.Interaction,
                              bans: int = 3, channel_deletes: int = 3,
                              role_deletes: int = 3):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        """INSERT INTO antinuke_config
               (guild_id, ban_threshold, channel_delete_threshold, role_delete_threshold)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET
               ban_threshold=excluded.ban_threshold,
               channel_delete_threshold=excluded.channel_delete_threshold,
               role_delete_threshold=excluded.role_delete_threshold""",
        (interaction.guild.id, bans, channel_deletes, role_deletes))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Thresholds — bans: {bans} · channel deletes: {channel_deletes} · role deletes: {role_deletes}")


@antinuke_group.command(name="whitelist-add",
    description="Exempt a user/bot from anti-nuke checks")
@app_commands.describe(user="User to whitelist")
async def antinuke_wl_add(interaction: discord.Interaction, user: discord.User):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id) VALUES (?, ?)",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ {user.mention} whitelisted from anti-nuke.")


@antinuke_group.command(name="whitelist-remove",
    description="Remove a user from the anti-nuke whitelist")
@app_commands.describe(user="User to remove")
async def antinuke_wl_remove(interaction: discord.Interaction, user: discord.User):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ {user.mention} removed from anti-nuke whitelist.")


bot.tree.add_command(antinuke_group)


async def _get_antinuke_cfg(guild_id: int):
    async with bot.db.execute(
        "SELECT * FROM antinuke_config WHERE guild_id=?", (guild_id,)
    ) as cur:
        return await cur.fetchone()


async def _is_antinuke_whitelisted(guild_id: int, user_id: int) -> bool:
    async with bot.db.execute(
        "SELECT 1 FROM antinuke_whitelist WHERE guild_id=? AND user_id=?",
        (guild_id, user_id)
    ) as cur:
        return await cur.fetchone() is not None


def _track_nuke_action(guild_id: int, user_id: int, action: str, threshold: int) -> bool:
    """Add an event timestamp and return True if the threshold is exceeded."""
    now = time.time()
    ts_list = nuke_tracker[guild_id][user_id][action]
    ts_list.append(now)
    # Keep only events within the window
    nuke_tracker[guild_id][user_id][action] = [t for t in ts_list if now - t <= NUKE_WINDOW]
    return len(nuke_tracker[guild_id][user_id][action]) >= threshold


async def _antinuke_punish(guild: discord.Guild, offender: discord.Member,
                           reason: str, cfg):
    """Strip roles, then kick or ban the offender, and log the event."""
    action      = cfg[3] if cfg else "kick"
    log_chan_id = cfg[2] if cfg else None

    # Strip roles first (neutralises the threat immediately)
    try:
        removable = [r for r in offender.roles if r != guild.default_role and r.is_assignable()]
        if removable:
            await offender.remove_roles(*removable, reason="Anti-Nuke: threat neutralised")
    except discord.Forbidden:
        pass

    # Kick or ban
    try:
        if action == "ban":
            await guild.ban(offender, reason=f"Anti-Nuke: {reason}", delete_message_days=0)
        else:
            await guild.kick(offender, reason=f"Anti-Nuke: {reason}")
    except discord.Forbidden:
        pass

    # Log
    if log_chan_id:
        ch = guild.get_channel(log_chan_id)
        if ch:
            e = discord.Embed(
                title="🚨 Anti-Nuke Triggered",
                description=f"**Offender:** {offender.mention} (`{offender}`)\n"
                            f"**Reason:** {reason}\n"
                            f"**Action:** {action.upper()}",
                color=discord.Color.red(),
            )
            e.set_footer(text=f"User ID: {offender.id}")
            await ch.send(embed=e)


async def _resolve_audit_actor(guild: discord.Guild,
                               action: discord.AuditLogAction) -> discord.Member | None:
    """Fetch the most recent audit log entry for the given action and return the responsible member."""
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            if entry.user and not entry.user.bot:
                return guild.get_member(entry.user.id)
            return None
    except discord.Forbidden:
        return None


# Anti-nuke events
@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    ban_entry = await _find_recent_audit_entry(
        guild, discord.AuditLogAction.ban, user.id)
    if ban_entry:
        actor = ban_entry.user
        e = discord.Embed(title="🔨 Member Banned", color=discord.Color.dark_red())
        e.add_field(name="Member", value=f"{user} (`{user.id}`)", inline=True)
        e.add_field(name="Moderator", value=str(actor), inline=True)
        e.add_field(name="Reason", value=ban_entry.reason or "*No reason provided*", inline=False)
        await _send_server_log(guild, e)

    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[4]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.ban)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "ban", threshold):
        await _antinuke_punish(guild, actor, f"Mass ban detected ({threshold}+ bans in 10s)", cfg)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[5]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.channel_delete)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "channel_delete", threshold):
        await _antinuke_punish(
            guild, actor, f"Mass channel deletion detected ({threshold}+ in 10s)", cfg)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    cfg = await _get_antinuke_cfg(guild.id)
    if not cfg or not cfg[1]:
        return
    threshold = cfg[6]
    actor = await _resolve_audit_actor(guild, discord.AuditLogAction.role_delete)
    if not actor or actor.id == bot.user.id:
        return
    if await _is_antinuke_whitelisted(guild.id, actor.id):
        return
    if actor.id == guild.owner_id:
        return
    if _track_nuke_action(guild.id, actor.id, "role_delete", threshold):
        await _antinuke_punish(
            guild, actor, f"Mass role deletion detected ({threshold}+ in 10s)", cfg)


# ═══════════════════════════════════════════════════════════════════════════════
# ANTI-RAID
# ═══════════════════════════════════════════════════════════════════════════════
antiraid_group = app_commands.Group(name="antiraid", description="Anti-raid protection")


@antiraid_group.command(name="setup", description="Set the anti-raid log channel")
@app_commands.describe(channel="Where to send raid alerts")
async def antiraid_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, log_channel_id) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (interaction.guild.id, channel.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Anti-raid alerts → {channel.mention}")


@antiraid_group.command(name="toggle", description="Enable or disable anti-raid")
async def antiraid_toggle(interaction: discord.Interaction, enabled: bool):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, enabled) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
        (interaction.guild.id, int(enabled)))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Anti-raid {'**ON**' if enabled else '**OFF**'}.")


@antiraid_group.command(name="action",
    description="Action taken on raiders: kick, ban, or lockdown")
@app_commands.choices(action=[
    app_commands.Choice(name="Kick raiders",     value="kick"),
    app_commands.Choice(name="Ban raiders",      value="ban"),
    app_commands.Choice(name="Lockdown server",  value="lockdown"),
])
async def antiraid_action(interaction: discord.Interaction,
                          action: app_commands.Choice[str]):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO antiraid_config (guild_id, action) VALUES (?, ?)"
        " ON CONFLICT(guild_id) DO UPDATE SET action=excluded.action",
        (interaction.guild.id, action.value))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Raid action set to **{action.value}**.")


@antiraid_group.command(name="thresholds",
    description="Configure join flood and account age settings")
@app_commands.describe(
    joins="Joins in the time window to trigger (default 10)",
    window="Time window in seconds (default 10)",
    min_age_days="Minimum account age in days (default 7)")
async def antiraid_thresholds(interaction: discord.Interaction,
                              joins: int = 10, window: int = 10,
                              min_age_days: int = 7):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        """INSERT INTO antiraid_config (guild_id, join_threshold, join_window, min_account_age_days)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET
               join_threshold=excluded.join_threshold,
               join_window=excluded.join_window,
               min_account_age_days=excluded.min_account_age_days""",
        (interaction.guild.id, joins, window, min_age_days))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Raid thresholds — {joins} joins / {window}s · min account age {min_age_days}d")


bot.tree.add_command(antiraid_group)


async def antiraid_check(member: discord.Member):
    """Called on every member join to check for a raid."""
    guild = member.guild
    async with bot.db.execute(
        "SELECT * FROM antiraid_config WHERE guild_id=?", (guild.id,)
    ) as cur:
        cfg = await cur.fetchone()
    if not cfg or not cfg[1]:
        return

    join_threshold  = cfg[3]
    join_window     = cfg[4]
    action          = cfg[5]
    min_age_days    = cfg[6]
    log_chan_id     = cfg[2]

    now = time.time()

    # --- New-account check (independent of flood) ---
    account_age_days = (datetime.now(timezone.utc) - member.created_at).days
    if account_age_days < min_age_days:
        await _raid_act(guild, member, action, log_chan_id,
                        f"New account (age {account_age_days}d < {min_age_days}d minimum)")
        return

    # --- Join flood check ---
    ts_list = raid_tracker[guild.id]
    ts_list.append(now)
    raid_tracker[guild.id] = [t for t in ts_list if now - t <= join_window]

    if len(raid_tracker[guild.id]) >= join_threshold:
        raid_tracker[guild.id].clear()  # reset to avoid acting on every subsequent join
        await _raid_act(guild, member, action, log_chan_id,
                        f"Join flood ({join_threshold}+ joins in {join_window}s)")


async def _raid_act(guild: discord.Guild, member: discord.Member,
                    action: str, log_chan_id: int | None, reason: str):
    try:
        if action == "ban":
            await guild.ban(member, reason=f"Anti-Raid: {reason}", delete_message_days=0)
        elif action == "lockdown":
            # Remove Send Messages from @everyone in all text channels
            overwrite = discord.PermissionOverwrite(send_messages=False)
            for ch in guild.text_channels:
                try:
                    await ch.set_permissions(guild.default_role, overwrite=overwrite,
                                             reason="Anti-Raid lockdown")
                except discord.Forbidden:
                    pass
            await guild.kick(member, reason=f"Anti-Raid: {reason}")
        else:
            await guild.kick(member, reason=f"Anti-Raid: {reason}")
    except discord.Forbidden:
        pass

    if log_chan_id:
        ch = guild.get_channel(log_chan_id)
        if ch:
            e = discord.Embed(
                title="🚨 Anti-Raid Triggered",
                description=f"**Member:** {member.mention} (`{member}`)\n"
                            f"**Reason:** {reason}\n"
                            f"**Action:** {action.upper()}",
                color=discord.Color.dark_red(),
            )
            e.set_footer(text=f"Account created: {member.created_at.strftime('%Y-%m-%d')}")
            await ch.send(embed=e)


# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOM TAGS
# ═══════════════════════════════════════════════════════════════════════════════
tag_group = app_commands.Group(name="tag", description="Custom tags / canned responses")


@tag_group.command(name="create", description="Create a custom tag (Manage Messages required)")
@app_commands.describe(name="Tag name", content="Reply content")
async def tag_create(interaction: discord.Interaction, name: str, content: str):
    if not is_mod(interaction):
        return await interaction.response.send_message(
            "Need Manage Messages permission.", ephemeral=True)
    try:
        await bot.db.execute(
            "INSERT INTO tags (guild_id, name, content, creator_id) VALUES (?, ?, ?, ?)",
            (interaction.guild.id, name.lower(), content, interaction.user.id))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Tag `{name}` created.")
    except aiosqlite.IntegrityError:
        await interaction.response.send_message(f"Tag `{name}` already exists.", ephemeral=True)


@tag_group.command(name="delete", description="Delete a custom tag (Manage Messages required)")
@app_commands.describe(name="Tag name")
async def tag_delete(interaction: discord.Interaction, name: str):
    if not is_mod(interaction):
        return await interaction.response.send_message(
            "Need Manage Messages permission.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM tags WHERE guild_id=? AND name=?", (interaction.guild.id, name.lower()))
    await bot.db.commit()
    await interaction.response.send_message(f"🗑️ Tag `{name}` deleted.")


@tag_group.command(name="get", description="Show a tag's content")
@app_commands.describe(name="Tag name")
async def tag_get(interaction: discord.Interaction, name: str):
    async with bot.db.execute(
        "SELECT content FROM tags WHERE guild_id=? AND name=?",
        (interaction.guild.id, name.lower())
    ) as cur:
        row = await cur.fetchone()
    if row:
        await interaction.response.send_message(row[0])
    else:
        await interaction.response.send_message(f"No tag `{name}` found.", ephemeral=True)


@tag_group.command(name="list", description="List all tags in this server")
async def tag_list(interaction: discord.Interaction):
    async with bot.db.execute(
        "SELECT name FROM tags WHERE guild_id=? ORDER BY name", (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No tags yet.")
    await interaction.response.send_message(
        "**Tags:** " + ", ".join(f"`{r[0]}`" for r in rows))


bot.tree.add_command(tag_group)


# ═══════════════════════════════════════════════════════════════════════════════
# ON MESSAGE  (automod + tag prefix)
# ═══════════════════════════════════════════════════════════════════════════════
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await run_automod(message)
    await record_message_activity(message.guild.id, message.author.id)

    # ── AFK: remove AFK status when the AFK user speaks ──────────────────────
    async with bot.db.execute(
        "SELECT reason, old_nick FROM afk WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id)
    ) as cur:
        afk_self = await cur.fetchone()

    if afk_self:
        await bot.db.execute(
            "DELETE FROM afk WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id))
        await bot.db.commit()
        # Restore original nickname
        try:
            await message.author.edit(nick=afk_self[1], reason="AFK removed")
        except discord.Forbidden:
            pass
        await message.channel.send(
            f"👋 Welcome back, {message.author.mention}! Your AFK has been removed.",
            delete_after=8)

    # ── AFK: notify when someone mentions an AFK user ────────────────────────
    if message.mentions:
        alerts = []
        for mentioned in message.mentions:
            if mentioned.id == message.author.id:
                continue
            async with bot.db.execute(
                "SELECT reason, set_at FROM afk WHERE guild_id=? AND user_id=?",
                (message.guild.id, mentioned.id)
            ) as cur:
                afk_row = await cur.fetchone()
            if afk_row:
                alerts.append(
                    f"💤 **{mentioned.display_name}** is AFK: *{afk_row[0]}* (since {afk_row[1]} UTC)")
        if alerts:
            await message.channel.send("\n".join(alerts), delete_after=10)

    # ── Tag prefix trigger ────────────────────────────────────────────────────
    if message.content.startswith("!") and len(message.content) > 1:
        name = message.content[1:].split()[0].lower()
        async with bot.db.execute(
            "SELECT content FROM tags WHERE guild_id=? AND name=?",
            (message.guild.id, name)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await message.channel.send(row[0])

    await bot.process_commands(message)


# ═══════════════════════════════════════════════════════════════════════════════
# REACTION ROLES
# ═══════════════════════════════════════════════════════════════════════════════
rr_group = app_commands.Group(name="reactionrole", description="Manage reaction roles")


@rr_group.command(name="add", description="Link a reaction emoji on a message to a role")
@app_commands.describe(channel="Channel with the message", message_id="Message ID",
                       emoji="Emoji to use", role="Role to assign")
async def rr_add(interaction: discord.Interaction, channel: discord.TextChannel,
                 message_id: str, emoji: str, role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Message ID must be numeric.", ephemeral=True)
    try:
        target = await channel.fetch_message(msg_id)
    except (discord.NotFound, discord.Forbidden):
        return await interaction.response.send_message(
            f"Couldn't find that message in {channel.mention}.", ephemeral=True)
    try:
        await target.add_reaction(emoji)
    except discord.HTTPException:
        return await interaction.response.send_message(
            "Invalid emoji.", ephemeral=True)
    await bot.db.execute(
        "INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id, guild_id) VALUES (?,?,?,?)",
        (msg_id, emoji, role.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Reacting {emoji} → {role.mention}")


@rr_group.command(name="remove", description="Remove a reaction role link")
@app_commands.describe(message_id="Message ID", emoji="Emoji to unlink")
async def rr_remove(interaction: discord.Interaction, message_id: str, emoji: str):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    try:
        msg_id = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Message ID must be numeric.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM reaction_roles WHERE message_id=? AND emoji=?", (msg_id, emoji))
    await bot.db.commit()
    await interaction.response.send_message(f"🗑️ Removed reaction role for {emoji}.")


bot.tree.add_command(rr_group)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
        return
    async with bot.db.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji))
    ) as cur:
        row = await cur.fetchone()
    if row:
        guild = bot.get_guild(payload.guild_id)
        role  = guild.get_role(row[0])
        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    guild  = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return
    async with bot.db.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji))
    ) as cur:
        row = await cur.fetchone()
    if row:
        role = guild.get_role(row[0])
        if role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.Forbidden:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# WHITELIST  (temporary /warn + /mute access)
# ═══════════════════════════════════════════════════════════════════════════════
whitelist_group = app_commands.Group(name="whitelist",
    description="Grant users temporary /warn + /mute access")


@whitelist_group.command(name="add",
    description="Give a user temporary mod powers (warn + mute)")
@app_commands.describe(user="User to grant powers to",
                       minutes="How long (in minutes) the access lasts")
async def wl_add(interaction: discord.Interaction, user: discord.Member, minutes: int):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    if minutes < 1:
        return await interaction.response.send_message(
            "Duration must be at least 1 minute.", ephemeral=True)

    expires_sql = f"datetime('now', '+{minutes} minutes')"
    await bot.db.execute(
        f"INSERT INTO whitelist (guild_id, user_id, expires_at, granted_by)"
        f" VALUES (?, ?, {expires_sql}, ?)"
        f" ON CONFLICT(guild_id, user_id) DO UPDATE SET expires_at={expires_sql}, granted_by=excluded.granted_by",
        (interaction.guild.id, user.id, interaction.user.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ {user.mention} can now use `/warn` and `/mute` for **{minutes} minute(s)**.")


@whitelist_group.command(name="remove",
    description="Revoke a user's temporary mod access early")
@app_commands.describe(user="User to remove from whitelist")
async def wl_remove(interaction: discord.Interaction, user: discord.Member):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM whitelist WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, user.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Removed {user.mention}'s temporary access.")


@whitelist_group.command(name="list", description="Show all active temporary mod grants")
async def wl_list(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    async with bot.db.execute(
        """SELECT user_id, expires_at, granted_by
           FROM whitelist
           WHERE guild_id=? AND expires_at > datetime('now')
           ORDER BY expires_at""",
        (interaction.guild.id,)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message("No active whitelist entries.")
    lines = []
    for user_id, expires_at, granted_by in rows:
        member = interaction.guild.get_member(user_id)
        name   = member.mention if member else f"`{user_id}`"
        lines.append(f"{name} — expires `{expires_at}` (granted by <@{granted_by}>)")
    embed = discord.Embed(title="🔐 Active Whitelist", description="\n".join(lines),
                          color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(whitelist_group)


# ═══════════════════════════════════════════════════════════════════════════════
# MODERATION  (warn / mute check whitelist; all others require real permission)
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="kick", description="Kick a member")
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick(interaction: discord.Interaction, member: discord.Member,
               reason: str = "No reason provided"):
    if not has_perm(interaction, "kick_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "Can't kick someone with equal or higher role.", ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 **{member}** kicked. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member")
@app_commands.describe(member="Member to ban", reason="Reason")
async def ban(interaction: discord.Interaction, member: discord.Member,
              reason: str = "No reason provided"):
    if not has_perm(interaction, "ban_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    if member.top_role >= interaction.user.top_role:
        return await interaction.response.send_message(
            "Can't ban someone with equal or higher role.", ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 **{member}** banned. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="unban", description="Unban a user by ID")
@app_commands.describe(user_id="User ID to unban")
async def unban(interaction: discord.Interaction, user_id: str):
    if not has_perm(interaction, "ban_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        await interaction.response.send_message(f"✅ **{user}** unbanned.")
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message("User isn't banned.", ephemeral=True)


@bot.tree.command(name="mute", description="Timeout a member (mods or whitelisted users)")
@app_commands.describe(member="Member to mute", minutes="Duration in minutes", reason="Reason")
async def mute(interaction: discord.Interaction, member: discord.Member,
               minutes: int, reason: str = "No reason provided"):
    can_act = (has_perm(interaction, "moderate_members") or
               await is_whitelisted(interaction.guild.id, interaction.user.id))
    if not can_act:
        return await interaction.response.send_message(
            "You don't have permission to mute members.", ephemeral=True)
    if member.top_role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "Can't mute someone with equal or higher role.", ephemeral=True)
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(
            f"🔇 **{member}** muted for {minutes} min. Reason: {reason}")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="unmute", description="Remove a timeout from a member")
@app_commands.describe(member="Member to unmute")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "moderate_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    try:
        await member.timeout(None)
        await interaction.response.send_message(f"🔊 **{member}** unmuted.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@bot.tree.command(name="warn", description="Warn a member (mods or whitelisted users)")
@app_commands.describe(member="Member to warn", reason="Reason")
async def warn(interaction: discord.Interaction, member: discord.Member,
               reason: str = "No reason provided"):
    can_act = (has_perm(interaction, "kick_members") or
               await is_whitelisted(interaction.guild.id, interaction.user.id))
    if not can_act:
        return await interaction.response.send_message(
            "You don't have permission to warn members.", ephemeral=True)
    await bot.db.execute(
        "INSERT INTO warnings (guild_id, user_id, reason) VALUES (?, ?, ?)",
        (interaction.guild.id, member.id, reason))
    await bot.db.commit()
    async with bot.db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, member.id)
    ) as cur:
        (count,) = await cur.fetchone()
    await interaction.response.send_message(
        f"⚠️ **{member}** warned ({count} total). Reason: {reason}")


@bot.tree.command(name="warnings", description="View a member's warning history")
@app_commands.describe(member="Member to check")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    async with bot.db.execute(
        "SELECT reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY created_at",
        (interaction.guild.id, member.id)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message(f"**{member}** has no warnings.")
    lines = "\n".join(
        f"{i+1}. {reason}  *(at {ts})*" for i, (reason, ts) in enumerate(rows))
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}",
                          description=lines, color=discord.Color.yellow())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnings-clear", description="Clear all warnings for a member")
@app_commands.describe(member="Member to clear")
async def warnings_clear(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "kick_members"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    await bot.db.execute(
        "DELETE FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, member.id))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Cleared all warnings for **{member}**.")


@bot.tree.command(name="clear", description="Delete recent messages in this channel")
@app_commands.describe(amount="Number of messages to delete (max 100)")
async def clear(interaction: discord.Interaction, amount: int):
    if not has_perm(interaction, "manage_messages"):
        return await interaction.response.send_message("No permission.", ephemeral=True)
    amount = max(1, min(amount, 100))
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RPG SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
rpg_group = app_commands.Group(name="rpg", description="RPG adventure game")


# ── /rpg start ────────────────────────────────────────────────────────────────
@rpg_group.command(name="start", description="Create your RPG character")
@app_commands.describe(cls="Your character class")
@app_commands.choices(cls=[
    app_commands.Choice(name="⚔️ Warrior — high HP & defense",           value="warrior"),
    app_commands.Choice(name="🔮 Mage — highest attack, glass cannon",    value="mage"),
    app_commands.Choice(name="🏹 Archer — balanced with high crit",       value="archer"),
    app_commands.Choice(name="🗡️ Rogue — high attack & crit chance",     value="rogue"),
])
async def rpg_start(interaction: discord.Interaction, cls: app_commands.Choice[str]):
    if await get_character(interaction.user.id, interaction.guild.id):
        return await interaction.response.send_message(
            "You already have a character! Use `/rpg profile` to view it.", ephemeral=True)
    base = RPG_CLASSES[cls.value]
    await bot.db.execute(
        """INSERT INTO rpg_characters
               (user_id, guild_id, class, level, xp, hp, max_hp, attack, defense, gold, equipment)
           VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, 100, '{}')""",
        (interaction.user.id, interaction.guild.id, cls.value,
         base["hp"], base["hp"], base["attack"], base["defense"]))
    await bot.db.commit()
    embed = discord.Embed(
        title=f"{base['emoji']} Character Created!",
        description=f"Welcome, **{interaction.user.display_name}** the **{cls.value.title()}**!",
        color=discord.Color.green())
    embed.add_field(name="❤️ HP",      value=str(base["hp"]),     inline=True)
    embed.add_field(name="⚔️ Attack",  value=str(base["attack"]), inline=True)
    embed.add_field(name="🛡️ Defense", value=str(base["defense"]),inline=True)
    embed.add_field(name="✨ Crit",    value=f"{base['crit']}%",  inline=True)
    embed.add_field(name="💰 Gold",    value="100",               inline=True)
    embed.set_footer(text="Use /rpg hunt to start your adventure!")
    await interaction.response.send_message(embed=embed)


# ── /rpg profile ──────────────────────────────────────────────────────────────
@rpg_group.command(name="profile", description="View your (or another user's) character")
@app_commands.describe(user="User to inspect (leave blank for yourself)")
async def rpg_profile(interaction: discord.Interaction,
                      user: discord.Member = None):
    target = user or interaction.user
    row = await get_character(target.id, interaction.guild.id)
    if not row:
        who = "You don't" if target == interaction.user else f"{target.display_name} doesn't"
        return await interaction.response.send_message(
            f"{who} have a character yet. Use `/rpg start`.", ephemeral=True)
    c    = row_to_char(row)
    base = RPG_CLASSES[c["class"]]
    atk_b, def_b = equipment_bonuses(c["equipment"])
    xp_need = xp_for_level(c["level"])

    embed = discord.Embed(
        title=f"{base['emoji']} {target.display_name}'s Character",
        color=discord.Color.blurple())
    embed.add_field(name="Class",    value=c["class"].title(), inline=True)
    embed.add_field(name="Level",    value=str(c["level"]),    inline=True)
    embed.add_field(name="XP",       value=f"{c['xp']}/{xp_need}", inline=True)
    embed.add_field(name="❤️ HP",    value=f"{c['hp']}/{c['max_hp']}", inline=True)
    embed.add_field(name="⚔️ ATK",   value=f"{c['attack']} (+{atk_b})", inline=True)
    embed.add_field(name="🛡️ DEF",   value=f"{c['defense']} (+{def_b})", inline=True)
    embed.add_field(name="💰 Gold",  value=str(c["gold"]),    inline=True)
    if c["equipment"]:
        items = ", ".join(
            SHOP_ITEMS[k]["name"] for k in c["equipment"].values() if k in SHOP_ITEMS
        ) or "None"
        embed.add_field(name="🎒 Equipment", value=items, inline=False)
    await interaction.response.send_message(embed=embed)


# ── /rpg hunt ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="hunt", description="Go hunt a monster for XP and gold")
async def rpg_hunt(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "You don't have a character yet. Use `/rpg start`.", ephemeral=True)

    # Cooldown check
    last = HUNT_COOLDOWNS.get(interaction.user.id, 0)
    remaining = HUNT_COOLDOWN_SECS - (time.time() - last)
    if remaining > 0:
        return await interaction.response.send_message(
            f"⏳ Hunt cooldown: **{remaining:.0f}s** remaining.", ephemeral=True)
    HUNT_COOLDOWNS[interaction.user.id] = time.time()

    c = row_to_char(row)
    if c["hp"] <= 0:
        return await interaction.response.send_message(
            "❤️ You're too injured to hunt! Use `/rpg rest` to recover.", ephemeral=True)

    atk_b, def_b = equipment_bonuses(c["equipment"])
    p_atk = c["attack"] + atk_b
    p_def = c["defense"] + def_b
    crit_pct = RPG_CLASSES[c["class"]]["crit"]

    # Pick monster (harder monsters appear as player levels up)
    max_idx = min(len(MONSTERS) - 1, c["level"] // 2 + 2)
    monster = dict(MONSTERS[random.randint(0, max_idx)])
    m_hp = monster["hp"]

    log: list[str] = []
    turns = 0
    player_hp = c["hp"]

    while player_hp > 0 and m_hp > 0 and turns < 30:
        turns += 1
        # Player attacks
        dmg = max(1, p_atk - monster["def"])
        if random.randint(1, 100) <= crit_pct:
            dmg = int(dmg * 1.5)
            log.append(f"💥 CRIT! You hit {monster['name']} for **{dmg}**")
        else:
            log.append(f"You hit {monster['name']} for **{dmg}**")
        m_hp -= dmg
        if m_hp <= 0:
            break
        # Monster attacks
        m_dmg = max(1, monster["atk"] - p_def)
        player_hp -= m_dmg
        log.append(f"{monster['name']} hits you for **{m_dmg}**")

    won  = m_hp <= 0
    gold = random.randint(monster["gmin"], monster["gmax"]) if won else 0
    xp   = monster["xp"] if won else monster["xp"] // 4

    c["hp"]   = max(0, player_hp)
    c["xp"]  += xp
    c["gold"] = c["gold"] + gold if won else max(0, c["gold"] - gold // 2)

    lvl_msgs = await check_level_up(interaction.user.id, interaction.guild.id, c)
    await save_character(interaction.user.id, interaction.guild.id, c)

    embed = discord.Embed(
        title=f"{'🏆 Victory!' if won else '💀 Defeated!'} vs {monster['name']}",
        color=discord.Color.green() if won else discord.Color.red())
    embed.add_field(name="Combat Log", value="\n".join(log[-6:]) or "—", inline=False)
    embed.add_field(name="Result",
        value=(f"+{xp} XP · +{gold} 💰" if won else f"+{xp} XP · -{gold // 2} 💰"),
        inline=True)
    embed.add_field(name="❤️ HP", value=f"{c['hp']}/{c['max_hp']}", inline=True)
    if lvl_msgs:
        embed.add_field(name="⬆️ Level Up!", value="\n".join(lvl_msgs), inline=False)
    embed.set_footer(text=f"Hunt again in {HUNT_COOLDOWN_SECS}s · Use /rpg rest to heal")
    await interaction.response.send_message(embed=embed)


# ── /rpg rest ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="rest", description="Restore HP by spending gold (10 gold = 20 HP)")
async def rpg_rest(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    c = row_to_char(row)
    if c["hp"] >= c["max_hp"]:
        return await interaction.response.send_message("❤️ You're already at full HP!")
    heal_cost = 10
    if c["gold"] < heal_cost:
        return await interaction.response.send_message(
            f"💰 You need at least {heal_cost} gold to rest.", ephemeral=True)
    c["gold"] -= heal_cost
    healed     = min(20, c["max_hp"] - c["hp"])
    c["hp"]   += healed
    await save_character(interaction.user.id, interaction.guild.id, c)
    await interaction.response.send_message(
        f"🛌 Rested! Restored **{healed} HP** for **{heal_cost} gold**.\n"
        f"HP: {c['hp']}/{c['max_hp']} · Gold: {c['gold']}")


# ── /rpg shop ─────────────────────────────────────────────────────────────────
@rpg_group.command(name="shop", description="View the item shop")
async def rpg_shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🏪 Item Shop", color=discord.Color.gold())
    weapons = [(k, v) for k, v in SHOP_ITEMS.items() if v["type"] == "weapon"]
    armor   = [(k, v) for k, v in SHOP_ITEMS.items() if v["type"] == "armor"]
    embed.add_field(
        name="⚔️ Weapons",
        value="\n".join(
            f"`{k}` — **{v['name']}** | +{v['atk']} ATK | {v['cost']} 💰"
            for k, v in weapons),
        inline=False)
    embed.add_field(
        name="🛡️ Armor",
        value="\n".join(
            f"`{k}` — **{v['name']}** | +{v['def']} DEF | {v['cost']} 💰"
            for k, v in armor),
        inline=False)
    embed.set_footer(text="Use /rpg buy <item_id> to purchase")
    await interaction.response.send_message(embed=embed)


# ── /rpg buy ──────────────────────────────────────────────────────────────────
@rpg_group.command(name="buy", description="Buy an item from the shop")
@app_commands.describe(item_id="Item ID shown in /rpg shop")
async def rpg_buy(interaction: discord.Interaction, item_id: str):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    item = SHOP_ITEMS.get(item_id.lower())
    if not item:
        return await interaction.response.send_message(
            "Unknown item. Check `/rpg shop` for valid item IDs.", ephemeral=True)
    c = row_to_char(row)
    if c["gold"] < item["cost"]:
        return await interaction.response.send_message(
            f"💰 Need **{item['cost']}** gold, you have **{c['gold']}**.", ephemeral=True)
    slot = item["type"]  # "weapon" or "armor"
    old  = c["equipment"].get(slot)
    c["equipment"][slot] = item_id.lower()
    c["gold"] -= item["cost"]
    await save_character(interaction.user.id, interaction.guild.id, c)
    msg = f"✅ Bought **{item['name']}** for {item['cost']} 💰 and equipped it!"
    if old:
        msg += f"\n*(Replaced **{SHOP_ITEMS[old]['name']}**)*"
    await interaction.response.send_message(msg)


# ── /rpg inventory ────────────────────────────────────────────────────────────
@rpg_group.command(name="inventory", description="View your equipped items")
async def rpg_inventory(interaction: discord.Interaction):
    row = await get_character(interaction.user.id, interaction.guild.id)
    if not row:
        return await interaction.response.send_message(
            "No character yet. Use `/rpg start`.", ephemeral=True)
    c = row_to_char(row)
    equip = c["equipment"]
    if not equip:
        return await interaction.response.send_message("🎒 Your inventory is empty.")
    lines = []
    for slot, item_id in equip.items():
        item = SHOP_ITEMS.get(item_id, {})
        bonus = f"+{item.get('atk',0)} ATK" if slot == "weapon" else f"+{item.get('def',0)} DEF"
        lines.append(f"**{slot.title()}:** {item.get('name', item_id)} ({bonus})")
    await interaction.response.send_message("🎒 **Inventory**\n" + "\n".join(lines))


# ── /rpg duel ─────────────────────────────────────────────────────────────────
class DuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, target: discord.Member):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.target     = target

    @discord.ui.button(label="⚔️ Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the challenged player can accept!", ephemeral=True)
        self.stop()
        await self._run_duel(interaction)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the challenged player can decline!", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ {self.target.display_name} declined the duel.", view=None)

    async def on_timeout(self):
        try:
            await self._message.edit(content="⏰ Duel challenge expired.", view=None)
        except Exception:
            pass

    async def _run_duel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        c_row    = await get_character(self.challenger.id, guild_id)
        t_row    = await get_character(self.target.id,     guild_id)

        if not c_row or not t_row:
            return await interaction.response.edit_message(
                content="One of the players doesn't have a character.", view=None)

        c_char = row_to_char(c_row)
        t_char = row_to_char(t_row)

        if c_char["hp"] <= 0 or t_char["hp"] <= 0:
            return await interaction.response.edit_message(
                content="One of the players is too injured to duel.", view=None)

        c_atk_b, c_def_b = equipment_bonuses(c_char["equipment"])
        t_atk_b, t_def_b = equipment_bonuses(t_char["equipment"])

        c_atk = c_char["attack"]  + c_atk_b
        c_def = c_char["defense"] + c_def_b
        t_atk = t_char["attack"]  + t_atk_b
        t_def = t_char["defense"] + t_def_b
        c_crit = RPG_CLASSES[c_char["class"]]["crit"]
        t_crit = RPG_CLASSES[t_char["class"]]["crit"]

        c_hp = c_char["hp"]
        t_hp = t_char["hp"]
        log  = []
        turn = 0

        while c_hp > 0 and t_hp > 0 and turn < 30:
            turn += 1
            # Challenger attacks
            dmg = max(1, c_atk - t_def)
            if random.randint(1, 100) <= c_crit:
                dmg = int(dmg * 1.5)
                log.append(f"💥 {self.challenger.display_name} CRITS for **{dmg}**!")
            else:
                log.append(f"{self.challenger.display_name} hits for **{dmg}**")
            t_hp -= dmg
            if t_hp <= 0:
                break
            # Target attacks back
            dmg2 = max(1, t_atk - c_def)
            if random.randint(1, 100) <= t_crit:
                dmg2 = int(dmg2 * 1.5)
                log.append(f"💥 {self.target.display_name} CRITS for **{dmg2}**!")
            else:
                log.append(f"{self.target.display_name} hits for **{dmg2}**")
            c_hp -= dmg2

        winner = self.challenger if t_hp <= 0 else self.target
        loser  = self.target     if t_hp <= 0 else self.challenger
        w_char = c_char if winner == self.challenger else t_char
        l_char = t_char if winner == self.challenger else c_char
        w_uid  = self.challenger.id if winner == self.challenger else self.target.id
        l_uid  = self.target.id     if winner == self.challenger else self.challenger.id

        gold_loot = max(1, l_char["gold"] // 10)
        xp_gain   = 50 + w_char["level"] * 10

        w_char["gold"] += gold_loot
        w_char["xp"]   += xp_gain
        w_char["hp"]    = max(1, int(w_char["hp"] * 0.8))  # winner loses a bit of HP
        lvl_msgs = await check_level_up(w_uid, guild_id, w_char)
        await save_character(w_uid, guild_id, w_char)

        l_char["hp"]   = max(1, int(l_char["hp"] * 0.6))
        l_char["gold"] = max(0, l_char["gold"] - gold_loot)
        await save_character(l_uid, guild_id, l_char)

        embed = discord.Embed(
            title=f"⚔️ Duel: {self.challenger.display_name} vs {self.target.display_name}",
            color=discord.Color.gold())
        embed.add_field(name="Combat Log", value="\n".join(log[-8:]) or "—", inline=False)
        embed.add_field(name="🏆 Winner",  value=winner.mention,                  inline=True)
        embed.add_field(name="💰 Spoils",  value=f"+{gold_loot} gold · +{xp_gain} XP", inline=True)
        if lvl_msgs:
            embed.add_field(name="⬆️ Level Up!", value="\n".join(lvl_msgs), inline=False)
        embed.set_footer(text=f"Duel ended in {turn} turn(s)")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


@rpg_group.command(name="duel", description="Challenge another player to a duel")
@app_commands.describe(user="Player to challenge")
async def rpg_duel(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("You can't duel yourself!", ephemeral=True)
    if user.bot:
        return await interaction.response.send_message("You can't duel a bot!", ephemeral=True)

    c_row = await get_character(interaction.user.id, interaction.guild.id)
    t_row = await get_character(user.id, interaction.guild.id)
    if not c_row:
        return await interaction.response.send_message(
            "You don't have a character. Use `/rpg start`.", ephemeral=True)
    if not t_row:
        return await interaction.response.send_message(
            f"{user.display_name} doesn't have a character yet.", ephemeral=True)

    view = DuelView(interaction.user, user)
    msg  = await interaction.response.send_message(
        f"⚔️ {interaction.user.mention} challenges {user.mention} to a duel!\n"
        f"{user.mention}, do you accept?",
        view=view)
    view._message = await interaction.original_response()


bot.tree.add_command(rpg_group)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOROLE
# ═══════════════════════════════════════════════════════════════════════════════
autorole_group = app_commands.Group(name="autorole",
    description="Automatically assign a role to every new member")


@autorole_group.command(name="set", description="Set the role new members receive on join")
@app_commands.describe(role="Role to auto-assign")
async def autorole_set(interaction: discord.Interaction, role: discord.Role):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    if role.managed or role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "I can't assign that role — it's either managed by an integration "
            "or above my highest role.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET autorole_id=? WHERE guild_id=?",
        (role.id, interaction.guild.id))
    await bot.db.commit()
    await interaction.response.send_message(
        f"✅ Autorole set to {role.mention} — new members will receive it on join.")


@autorole_group.command(name="disable", description="Disable autorole")
async def autorole_disable(interaction: discord.Interaction):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    await ensure_guild_config(interaction.guild.id)
    await bot.db.execute(
        "UPDATE guild_config SET autorole_id=NULL WHERE guild_id=?",
        (interaction.guild.id,))
    await bot.db.commit()
    await interaction.response.send_message("✅ Autorole disabled.")


@autorole_group.command(name="view", description="Show the current autorole")
async def autorole_view(interaction: discord.Interaction):
    cfg = await get_guild_config(interaction.guild.id)
    autorole_id = cfg[8] if len(cfg) > 8 else None
    if not autorole_id:
        return await interaction.response.send_message("No autorole is set.", ephemeral=True)
    role = interaction.guild.get_role(autorole_id)
    if role:
        await interaction.response.send_message(f"🎭 Autorole: {role.mention}", ephemeral=True)
    else:
        await interaction.response.send_message(
            "⚠️ The saved autorole no longer exists. Use `/autorole set` to update it.",
            ephemeral=True)


bot.tree.add_command(autorole_group)


# ═══════════════════════════════════════════════════════════════════════════════
# ROLE MANAGEMENT  (give / take / removeall)
# ═══════════════════════════════════════════════════════════════════════════════
role_group = app_commands.Group(name="role", description="Manage member roles")


@role_group.command(name="give", description="Give a role to a member")
@app_commands.describe(member="Member to give the role to", role="Role to assign")
async def role_give(interaction: discord.Interaction, member: discord.Member,
                    role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if role.managed:
        return await interaction.response.send_message(
            "That role is managed by an integration and can't be assigned manually.",
            ephemeral=True)
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "That role is above my highest role — I can't assign it.", ephemeral=True)
    if role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't assign a role equal to or above your own.", ephemeral=True)
    if role in member.roles:
        return await interaction.response.send_message(
            f"{member.mention} already has {role.mention}.", ephemeral=True)
    try:
        await member.add_roles(role, reason=f"Role given by {interaction.user}")
        await interaction.response.send_message(
            f"✅ Gave {role.mention} to {member.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@role_group.command(name="take", description="Remove a role from a member")
@app_commands.describe(member="Member to remove the role from", role="Role to remove")
async def role_take(interaction: discord.Interaction, member: discord.Member,
                    role: discord.Role):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if role.managed:
        return await interaction.response.send_message(
            "That role is managed by an integration and can't be removed manually.",
            ephemeral=True)
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            "That role is above my highest role — I can't remove it.", ephemeral=True)
    if role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't remove a role equal to or above your own.", ephemeral=True)
    if role not in member.roles:
        return await interaction.response.send_message(
            f"{member.mention} doesn't have {role.mention}.", ephemeral=True)
    try:
        await member.remove_roles(role, reason=f"Role taken by {interaction.user}")
        await interaction.response.send_message(
            f"✅ Removed {role.mention} from {member.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)


@role_group.command(name="removeall", description="Remove all assignable roles from a member")
@app_commands.describe(member="Member to strip roles from")
async def role_removeall(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction, "manage_roles"):
        return await interaction.response.send_message("Need Manage Roles.", ephemeral=True)
    if member.top_role >= interaction.user.top_role and not has_perm(interaction, "administrator"):
        return await interaction.response.send_message(
            "You can't remove roles from someone with an equal or higher role.", ephemeral=True)

    removable = [
        r for r in member.roles
        if r != interaction.guild.default_role
        and not r.managed
        and r < interaction.guild.me.top_role
        and r < interaction.user.top_role
    ]

    if not removable:
        return await interaction.response.send_message(
            f"{member.mention} has no roles I can remove.", ephemeral=True)

    await interaction.response.defer()
    try:
        await member.remove_roles(*removable, reason=f"All roles removed by {interaction.user}")
        names = ", ".join(r.mention for r in removable)
        await interaction.followup.send(
            f"✅ Removed **{len(removable)}** role(s) from {member.mention}: {names}")
    except discord.Forbidden:
        await interaction.followup.send("Missing permissions to remove some roles.", ephemeral=True)


bot.tree.add_command(role_group)


# ═══════════════════════════════════════════════════════════════════════════════
# GIVEAWAY
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_duration(s: str) -> timedelta | None:
    """Parse a duration string like '1d', '2h30m', '90s' into a timedelta."""
    total = timedelta()
    found = False
    for amount, unit in re.findall(r'(\d+)\s*([dhms])', s.lower()):
        found = True
        amount = int(amount)
        if unit == 'd':   total += timedelta(days=amount)
        elif unit == 'h': total += timedelta(hours=amount)
        elif unit == 'm': total += timedelta(minutes=amount)
        elif unit == 's': total += timedelta(seconds=amount)
    return total if found else None


def _giveaway_entry_weight(member: discord.Member, extra_role_id: int | None,
                           extra_entries: int) -> int:
    if extra_role_id and any(role.id == extra_role_id for role in member.roles):
        return 1 + max(0, extra_entries)
    return 1


def _weighted_entry_count(entries: list, guild: discord.Guild | None,
                           extra_role_id: int | None, extra_entries: int) -> int:
    if not guild:
        return len(entries)
    total = 0
    for user_id in entries:
        member = guild.get_member(user_id)
        if member:
            total += _giveaway_entry_weight(member, extra_role_id, extra_entries)
    return total


def _pick_weighted_winners(entries: list, guild: discord.Guild | None,
                           winners_count: int, extra_role_id: int | None,
                           extra_entries: int) -> list[int]:
    """Pick unique winners while giving eligible role holders extra chances."""
    if not guild:
        return []
    remaining = []
    seen = set()
    for user_id in entries:
        if user_id in seen:
            continue
        member = guild.get_member(user_id)
        if member:
            seen.add(user_id)
            remaining.append(
                [user_id, _giveaway_entry_weight(member, extra_role_id, extra_entries)]
            )

    winners = []
    for _ in range(min(winners_count, len(remaining))):
        total_weight = sum(item[1] for item in remaining)
        pick = random.uniform(0, total_weight)
        selected_index = 0
        for index, (_, weight) in enumerate(remaining):
            pick -= weight
            if pick <= 0:
                selected_index = index
                break
        winners.append(remaining.pop(selected_index)[0])
    return winners


def _giveaway_embed(prize: str, description: str, host_id: int, winners_count: int,
                    ends_at: datetime, entries: list, ended: bool = False,
                    winner_ids: list | None = None, guild: discord.Guild | None = None,
                    daily_messages: int = 0, weekly_messages: int = 0,
                    monthly_messages: int = 0, bypass_role_id: int | None = None,
                    extra_entries_role_id: int | None = None,
                    extra_entries: int = 1, image_filename: str | None = None) -> discord.Embed:
    unix = int(ends_at.timestamp())
    color = discord.Color.from_rgb(88, 101, 242)  # Discord blurple, matching the image
    if ended:
        color = discord.Color.greyple()

    embed = discord.Embed(
        title=prize,
        description=(description + "\n\u200b") if description else "\u200b",
        color=color,
    )
    if ended:
        embed.add_field(name="Ended",      value=f"<t:{unix}:F>",                  inline=False)
    else:
        embed.add_field(name="Ends",       value=f"<t:{unix}:R>  (<t:{unix}:F>)",  inline=False)
    embed.add_field(name="Hosted by",      value=f"<@{host_id}>",                  inline=True)
    entry_count = _weighted_entry_count(
        entries, guild, extra_entries_role_id, extra_entries)
    embed.add_field(name="Entries",        value=str(entry_count),                 inline=True)
    embed.add_field(name="Winners",        value=str(winners_count),               inline=True)

    requirements = []
    if daily_messages:
        requirements.append(f"{daily_messages} messages today")
    if weekly_messages:
        requirements.append(f"{weekly_messages} messages this week")
    if monthly_messages:
        requirements.append(f"{monthly_messages} messages this month")
    if requirements:
        requirement_text = "\n".join(f"• {item}" for item in requirements)
        if bypass_role_id:
            requirement_text += f"\n• Bypass: <@&{bypass_role_id}>"
        embed.add_field(name="Entry requirements", value=requirement_text, inline=False)
    if extra_entries_role_id and extra_entries > 0:
        embed.add_field(
            name="Bonus entries",
            value=f"<@&{extra_entries_role_id}> members receive +{extra_entries} extra "
                  f"chance{'s' if extra_entries != 1 else ''}.",
            inline=False,
        )

    if ended:
        if winner_ids:
            embed.add_field(
                name="🏆 Winner(s)",
                value=" ".join(f"<@{w}>" for w in winner_ids),
                inline=False,
            )
        else:
            embed.add_field(name="🏆 Winner(s)", value="No valid entries.", inline=False)

    embed.set_footer(text=ends_at.strftime("%d/%m/%Y"))
    if image_filename:
        embed.set_image(url=f"attachment://{image_filename}")
    return embed


class GiveawayEndedView(discord.ui.View):
    """Shown on the embed after a giveaway ends — has a Reroll button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 Reroll", style=discord.ButtonStyle.secondary,
                       custom_id="giveaway_reroll")
    async def reroll(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "You need Manage Server permission to reroll.", ephemeral=True)

        msg_id = interaction.message.id
        async with bot.db.execute(
            "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?",
            (msg_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "Giveaway not found.", ephemeral=True)
        if not row[2]:
            return await interaction.response.send_message(
                "This giveaway hasn't ended yet.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # Temporarily unflag so _end_giveaway can re-pick
        await bot.db.execute(
            "UPDATE giveaways SET ended=0 WHERE message_id=?", (msg_id,))
        await bot.db.commit()
        await _end_giveaway(row[1], row[0], msg_id, reroll=True)
        await interaction.followup.send("🎲 Winner rerolled!", ephemeral=True)


class GiveawayView(discord.ui.View):
    """Persistent view — survives bot restarts because custom_id is fixed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉", style=discord.ButtonStyle.primary,
                       custom_id="giveaway_enter")
    async def enter(self, interaction: discord.Interaction,
                    button: discord.ui.Button):
        msg_id = interaction.message.id
        async with bot.db.execute(
            """SELECT entries, ended, ends_at, daily_messages, weekly_messages,
                      monthly_messages, bypass_role_id
               FROM giveaways WHERE message_id=?""",
            (msg_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return await interaction.response.send_message(
                "This giveaway no longer exists.", ephemeral=True)

        (entries_list, ended, ends_at_str, daily_required, weekly_required,
         monthly_required, bypass_role_id) = row
        if ended:
            return await interaction.response.send_message(
                "This giveaway has already ended!", ephemeral=True)

        # Check expiry
        ends_at = datetime.fromisoformat(ends_at_str)
        if datetime.utcnow() > ends_at:
            return await interaction.response.send_message(
                "This giveaway has already ended!", ephemeral=True)

        entries = json.loads(entries_list or "[]")
        uid = interaction.user.id

        if uid in entries:
            # Toggle — let them leave the giveaway
            entries.remove(uid)
            await bot.db.execute(
                "UPDATE giveaways SET entries=? WHERE message_id=?",
                (json.dumps(entries), msg_id))
            await bot.db.commit()
            await _refresh_giveaway_embed(interaction.message, msg_id)
            return await interaction.response.send_message(
                "You have left the giveaway.", ephemeral=True)

        has_bypass_role = (
            bypass_role_id
            and any(role.id == bypass_role_id for role in interaction.user.roles)
        )
        if not has_bypass_role:
            daily_count, weekly_count, monthly_count = await get_message_activity(
                interaction.guild.id, uid)
            missing = []
            if daily_count < daily_required:
                missing.append(f"{daily_required} messages today ({daily_count}/{daily_required})")
            if weekly_count < weekly_required:
                missing.append(f"{weekly_required} messages this week ({weekly_count}/{weekly_required})")
            if monthly_count < monthly_required:
                missing.append(f"{monthly_required} messages this month ({monthly_count}/{monthly_required})")
            if missing:
                return await interaction.response.send_message(
                    "You don't meet the giveaway requirements:\n" +
                    "\n".join(f"• {item}" for item in missing) +
                    (f"\n\nThe <@&{bypass_role_id}> role bypasses these requirements."
                     if bypass_role_id else ""),
                    ephemeral=True,
                )

        entries.append(uid)
        await bot.db.execute(
            "UPDATE giveaways SET entries=? WHERE message_id=?",
            (json.dumps(entries), msg_id))
        await bot.db.commit()
        await _refresh_giveaway_embed(interaction.message, msg_id)
        await interaction.response.send_message(
            "🎉 You've entered the giveaway! Good luck!", ephemeral=True)


async def _refresh_giveaway_embed(message: discord.Message, msg_id: int):
    """Re-draw the giveaway embed with the current entry count."""
    async with bot.db.execute(
        """SELECT prize, description, host_id, winners, ends_at, entries, ended,
                  daily_messages, weekly_messages, monthly_messages,
                  bypass_role_id, extra_entries_role_id, extra_entries, image_filename
           FROM giveaways WHERE message_id=?""",
        (msg_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return
    (prize, desc, host_id, winners, ends_at_str, entries_json, ended,
     daily_required, weekly_required, monthly_required, bypass_role_id,
     extra_role_id, extra_entries, image_filename) = row
    entries  = json.loads(entries_json or "[]")
    ends_at  = datetime.fromisoformat(ends_at_str)
    embed    = _giveaway_embed(
        prize, desc, host_id, winners, ends_at, entries, bool(ended),
        guild=message.guild, daily_messages=daily_required,
        weekly_messages=weekly_required, monthly_messages=monthly_required,
        bypass_role_id=bypass_role_id, extra_entries_role_id=extra_role_id,
        extra_entries=extra_entries, image_filename=image_filename)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass


async def _end_giveaway(guild_id: int, channel_id: int, message_id: int,
                        reroll: bool = False):
    """Pick winner(s), update the embed, announce in channel."""
    async with bot.db.execute(
        """SELECT prize, description, host_id, winners, ends_at, entries,
                  daily_messages, weekly_messages, monthly_messages,
                  bypass_role_id, extra_entries_role_id, extra_entries, image_filename
           FROM giveaways WHERE message_id=?""",
        (message_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return

    (prize, desc, host_id, winners_count, ends_at_str, entries_json,
     daily_required, weekly_required, monthly_required, bypass_role_id,
     extra_role_id, extra_entries, image_filename) = row
    entries  = json.loads(entries_json or "[]")
    ends_at  = datetime.fromisoformat(ends_at_str)
    guild    = bot.get_guild(guild_id)
    channel  = guild.get_channel(channel_id) if guild else None

    # Pick winners
    winner_ids   = _pick_weighted_winners(
        entries, guild, winners_count, extra_role_id, extra_entries)

    # Mark ended
    await bot.db.execute(
        "UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
    await bot.db.commit()

    # Edit original message
    if channel:
        try:
            msg   = await channel.fetch_message(message_id)
            embed = _giveaway_embed(
                prize, desc, host_id, winners_count, ends_at, entries,
                ended=True, winner_ids=winner_ids, guild=guild,
                daily_messages=daily_required, weekly_messages=weekly_required,
                monthly_messages=monthly_required, bypass_role_id=bypass_role_id,
                extra_entries_role_id=extra_role_id, extra_entries=extra_entries,
                image_filename=image_filename)
            await msg.edit(embed=embed, view=GiveawayEndedView())
        except (discord.NotFound, discord.HTTPException):
            pass

        # Announce
        if winner_ids:
            mentions = " ".join(f"<@{w}>" for w in winner_ids)
            action   = "rerolled" if reroll else "ended"
            await channel.send(
                f"🎉 **Giveaway {action}!** Congratulations {mentions}! "
                f"You won **{prize}**!")
        else:
            await channel.send(
                f"😔 The **{prize}** giveaway ended but there were no valid entries.")


@tasks.loop(seconds=30)
async def giveaway_ticker():
    """Auto-end giveaways whose timer has expired."""
    await bot.wait_until_ready()
    now = datetime.utcnow().isoformat()
    async with bot.db.execute(
        "SELECT message_id, channel_id, guild_id FROM giveaways "
        "WHERE ended=0 AND ends_at <= ?", (now,)
    ) as cur:
        rows = await cur.fetchall()
    for msg_id, ch_id, g_id in rows:
        await _end_giveaway(g_id, ch_id, msg_id)


# ── Commands ──────────────────────────────────────────────────────────────────
giveaway_group = app_commands.Group(name="giveaway", description="Manage giveaways")


@giveaway_group.command(name="create", description="Start a new giveaway")
@app_commands.describe(
    prize="What are you giving away?",
    duration="Duration, e.g. 1d / 2h30m / 90m / 7d",
    winners="Number of winners (default 1)",
    channel="Channel to post in (defaults to current channel)",
    description="Optional extra description shown in the embed",
    daily_messages="Minimum messages sent today (0 disables this requirement)",
    weekly_messages="Minimum messages sent this week (0 disables this requirement)",
    monthly_messages="Minimum messages sent this month (0 disables this requirement)",
    bypass_role="Role that bypasses message requirements",
    extra_entries_role="Role that receives bonus entry chances",
    extra_entries="Bonus chances for members with the extra-entry role",
    image="Optional image attachment for the giveaway",
)
async def gw_create(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    winners: int = 1,
    channel: discord.TextChannel = None,
    description: str = "",
    daily_messages: int = 0,
    weekly_messages: int = 0,
    monthly_messages: int = 0,
    bypass_role: discord.Role = None,
    extra_entries_role: discord.Role = None,
    extra_entries: int = 1,
    image: discord.Attachment = None,
):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)

    delta = _parse_duration(duration)
    if delta is None or delta.total_seconds() < 10:
        return await interaction.response.send_message(
            "Invalid duration. Use values like `10m`, `2h`, `1d`, `7d12h`.",
            ephemeral=True)
    if daily_messages < 0 or weekly_messages < 0 or monthly_messages < 0:
        return await interaction.response.send_message(
            "Message requirements cannot be negative.", ephemeral=True)
    if extra_entries < 0:
        return await interaction.response.send_message(
            "Extra entries cannot be negative.", ephemeral=True)
    if image and not (
        (image.content_type and image.content_type.startswith("image/"))
        or image.filename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        )
    ):
        return await interaction.response.send_message(
            "The giveaway attachment must be an image file.", ephemeral=True)

    winners = max(1, winners)
    dest    = channel or interaction.channel
    ends_at = datetime.utcnow() + delta
    image_filename = image.filename if image else None

    embed = _giveaway_embed(
        prize, description, interaction.user.id, winners, ends_at, [],
        guild=interaction.guild, daily_messages=daily_messages,
        weekly_messages=weekly_messages, monthly_messages=monthly_messages,
        bypass_role_id=bypass_role.id if bypass_role else None,
        extra_entries_role_id=extra_entries_role.id if extra_entries_role else None,
        extra_entries=extra_entries, image_filename=image_filename)

    await interaction.response.defer(ephemeral=True)
    msg = await dest.send(
        embed=embed,
        view=GiveawayView(),
        file=await image.to_file() if image else None,
    )

    await bot.db.execute(
        """INSERT INTO giveaways
               (message_id, channel_id, guild_id, host_id, prize, description,
                winners, ends_at, ended, entries, daily_messages, weekly_messages,
                monthly_messages, bypass_role_id, extra_entries_role_id,
                extra_entries, image_filename)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', ?, ?, ?, ?, ?, ?, ?)""",
        (msg.id, dest.id, interaction.guild.id, interaction.user.id,
         prize, description, winners, ends_at.isoformat(),
         daily_messages, weekly_messages, monthly_messages,
         bypass_role.id if bypass_role else None,
         extra_entries_role.id if extra_entries_role else None,
         extra_entries, image_filename))
    await bot.db.commit()

    await interaction.followup.send(
        f"✅ Giveaway successfully created! **ID:** `{msg.id}`\n"
        f"Posted in {dest.mention}.",
        ephemeral=True)


@giveaway_group.command(name="end", description="End a giveaway early and pick a winner now")
@app_commands.describe(message_id="ID of the giveaway message")
async def gw_end(interaction: discord.Interaction, message_id: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

    async with bot.db.execute(
        "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?", (mid,)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return await interaction.response.send_message(
            "No giveaway found with that ID.", ephemeral=True)
    if row[2]:
        return await interaction.response.send_message(
            "That giveaway has already ended.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    await _end_giveaway(row[1], row[0], mid)
    await interaction.followup.send("✅ Giveaway ended and winner(s) selected!", ephemeral=True)


@giveaway_group.command(name="reroll", description="Reroll the winner of an ended giveaway")
@app_commands.describe(message_id="ID of the giveaway message")
async def gw_reroll(interaction: discord.Interaction, message_id: str):
    if not has_perm(interaction, "manage_guild"):
        return await interaction.response.send_message("Need Manage Server.", ephemeral=True)
    try:
        mid = int(message_id)
    except ValueError:
        return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

    async with bot.db.execute(
        "SELECT channel_id, guild_id, ended FROM giveaways WHERE message_id=?", (mid,)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return await interaction.response.send_message(
            "No giveaway found with that ID.", ephemeral=True)
    if not row[2]:
        return await interaction.response.send_message(
            "That giveaway hasn't ended yet. Use `/giveaway end` first.", ephemeral=True)

    # Allow reroll — temporarily unflag ended so _end_giveaway can re-pick
    await bot.db.execute("UPDATE giveaways SET ended=0 WHERE message_id=?", (mid,))
    await bot.db.commit()

    await interaction.response.defer(ephemeral=True)
    await _end_giveaway(row[1], row[0], mid, reroll=True)
    await interaction.followup.send("🎲 Winner rerolled!", ephemeral=True)


bot.tree.add_command(giveaway_group)


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════
@bot.tree.command(name="dashboard", description="Show a snapshot of this server's stats and a link to the web dashboard")
async def dashboard_cmd(interaction: discord.Interaction):
    guild = interaction.guild

    async with bot.db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=?", (guild.id,)
    ) as cur:
        warning_count = (await cur.fetchone())[0]

    async with bot.db.execute(
        "SELECT COUNT(*) FROM rpg_characters WHERE guild_id=?", (guild.id,)
    ) as cur:
        rpg_count = (await cur.fetchone())[0]

    async with bot.db.execute(
        "SELECT COUNT(*) FROM giveaways WHERE guild_id=? AND ended=0", (guild.id,)
    ) as cur:
        active_giveaways = (await cur.fetchone())[0]

    config   = await get_guild_config(guild.id)
    automod  = bool(config[6]) if config else False

    async with bot.db.execute(
        "SELECT enabled FROM antinuke_config WHERE guild_id=?", (guild.id,)
    ) as cur:
        row = await cur.fetchone()
        antinuke_on = bool(row[0]) if row else False

    async with bot.db.execute(
        "SELECT enabled FROM antiraid_config WHERE guild_id=?", (guild.id,)
    ) as cur:
        row = await cur.fetchone()
        antiraid_on = bool(row[0]) if row else False

    embed = discord.Embed(
        title=f"📊 {guild.name} — Dashboard",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Warnings issued", value=str(warning_count), inline=True)
    embed.add_field(name="RPG characters", value=str(rpg_count), inline=True)
    embed.add_field(name="Active giveaways", value=str(active_giveaways), inline=True)
    embed.add_field(name="Automod", value="🟢 On" if automod else "⚪ Off", inline=True)
    embed.add_field(name="Anti-nuke", value="🟢 On" if antinuke_on else "⚪ Off", inline=True)
    embed.add_field(name="Anti-raid", value="🟢 On" if antiraid_on else "⚪ Off", inline=True)
    embed.add_field(
        name="Full dashboard",
        value=f"[Open the web console]({DASHBOARD_URL}/guild/{guild.id})",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
# AFK
# ═══════════════════════════════════════════════════════════════════════════════
AFK_PREFIX = "[AFK] "


@bot.tree.command(name="afk", description="Set yourself as AFK — adds [AFK] in front of your name")
@app_commands.describe(reason="Why you're going AFK (shown when someone mentions you)")
async def afk_cmd(interaction: discord.Interaction, reason: str = "AFK"):
    member = interaction.user
    guild  = interaction.guild

    # Check not already AFK
    async with bot.db.execute(
        "SELECT 1 FROM afk WHERE guild_id=? AND user_id=?",
        (guild.id, member.id)
    ) as cur:
        if await cur.fetchone():
            return await interaction.response.send_message(
                "You're already AFK! Send any message to remove it.", ephemeral=True)

    # Save current nick before we touch it
    old_nick = member.nick  # None means using username

    # Build new nickname — cap at 32 chars (Discord limit)
    base_name = member.nick or member.name
    new_nick   = (AFK_PREFIX + base_name)[:32]

    # Save to DB
    await bot.db.execute(
        "INSERT OR REPLACE INTO afk (guild_id, user_id, reason, old_nick) VALUES (?, ?, ?, ?)",
        (guild.id, member.id, reason, old_nick))
    await bot.db.commit()

    # Try to update nickname
    nick_updated = False
    try:
        await member.edit(nick=new_nick, reason="User went AFK")
        nick_updated = True
    except discord.Forbidden:
        pass  # Can't rename server owner or someone above bot

    msg = f"💤 You're now AFK: *{reason}*"
    if not nick_updated:
        msg += "\n*(I couldn't update your nickname — you may be above my role or the server owner)*"
    await interaction.response.send_message(msg, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════════
def run_dashboard_in_thread():
    """Serve the Flask dashboard (dashboard.py) inside this same process,
    on a background thread, using waitress (a plain-Python production WSGI
    server — safe to embed, unlike Flask's dev server or gunicorn's
    multi-process model)."""
    from waitress import serve
    from dashboard import app as dashboard_app

    port = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "5000"))
    serve(dashboard_app, host="0.0.0.0", port=port, _quiet=True)


if __name__ == "__main__":
    threading.Thread(target=run_dashboard_in_thread, daemon=True).start()
    bot.run(TOKEN)
