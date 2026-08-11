"""
Web dashboard for the Discord bot.

Everything lives in this ONE file — HTML templates and CSS are embedded
as Python strings below, served through a Jinja2 DictLoader instead of a
templates/ folder on disk. This is deliberate: it means the entire
dashboard is a single file to upload, with no folder structure to
preserve — handy if you're setting this up from a phone where uploading
a nested folder tree isn't really possible.

This module is imported by main.py and served in a background thread of
the SAME process as the bot — one Python process, one command to run,
one container to deploy. It can also still be run standalone with
`python dashboard.py` for local development.

It never touches Discord's gateway — only the plain REST API, to list the
guilds the bot is in, their names/icons, and their roles. It talks to the
bot's SQLite file (bot.db) through its own plain sqlite3 connection —
kept separate from the bot's aiosqlite connection, since Flask is
synchronous and the bot's connection lives on the asyncio event loop.
WAL mode (set in main.py) is what makes both sides sharing that file safe.

Env vars (set in .env at the project root):
    DISCORD_TOKEN        same bot token main.py uses
    DASHBOARD_PASSWORD   password required to log in to the dashboard
    DASHBOARD_SECRET_KEY random string used to sign the session cookie
    BOT_DB_PATH          path to bot.db (default: bot.db, next to main.py)
    DASHBOARD_PORT        port to serve on when run standalone (default: 5000)
"""
import os
import sqlite3
import time
import json
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g, Response
)
from jinja2 import DictLoader
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "dev-only-change-this")
DB_PATH = os.getenv("BOT_DB_PATH", os.path.join(os.path.dirname(__file__), "bot.db"))

API_BASE = "https://discord.com/api/v10"

# Keep this in sync with the command groups registered in main.py.
COMMAND_GROUPS = [
    {"name": "help",           "label": "/help",           "desc": "List all available commands",           "category": "Utility"},
    {"name": "dashboard",      "label": "/dashboard",      "desc": "Server stats snapshot + console link",  "category": "Utility"},
    {"name": "afk",            "label": "/afk",            "desc": "Set yourself as AFK",                    "category": "Utility"},
    {"name": "setlogchannel",  "label": "/setlogchannel",  "desc": "Set the server event log channel",       "category": "Server config"},
    {"name": "welcome",        "label": "/welcome",        "desc": "Configure welcome messages",             "category": "Server config"},
    {"name": "leave",          "label": "/leave",          "desc": "Configure leave messages",               "category": "Server config"},
    {"name": "automod",        "label": "/automod",        "desc": "Auto-moderation rules",                  "category": "Server config"},
    {"name": "autorole",       "label": "/autorole",       "desc": "Role given to new members",              "category": "Server config"},
    {"name": "antinuke",       "label": "/antinuke",       "desc": "Anti-nuke protection",                   "category": "Security"},
    {"name": "antiraid",       "label": "/antiraid",       "desc": "Anti-raid protection",                   "category": "Security"},
    {"name": "whitelist",      "label": "/whitelist",      "desc": "Temporary moderator whitelist",          "category": "Security"},
    {"name": "kick",           "label": "/kick",           "desc": "Kick a member",                          "category": "Moderation"},
    {"name": "ban",            "label": "/ban",            "desc": "Ban a member",                           "category": "Moderation"},
    {"name": "unban",          "label": "/unban",          "desc": "Unban a user by ID",                     "category": "Moderation"},
    {"name": "mute",           "label": "/mute",           "desc": "Timeout a member",                       "category": "Moderation"},
    {"name": "unmute",         "label": "/unmute",         "desc": "Remove a timeout",                       "category": "Moderation"},
    {"name": "warn",           "label": "/warn",           "desc": "Warn a member",                          "category": "Moderation"},
    {"name": "warnings",       "label": "/warnings",       "desc": "View a member's warning history",        "category": "Moderation"},
    {"name": "warnings-clear", "label": "/warnings-clear", "desc": "Clear a member's warnings",              "category": "Moderation"},
    {"name": "clear",          "label": "/clear",          "desc": "Bulk-delete recent messages",            "category": "Moderation"},
    {"name": "role",           "label": "/role",           "desc": "Manually give/take roles",               "category": "Moderation"},
    {"name": "tag",            "label": "/tag",            "desc": "Custom tags / canned responses",         "category": "Fun & engagement"},
    {"name": "reactionrole",   "label": "/reactionrole",   "desc": "Reaction-role assignment",                "category": "Fun & engagement"},
    {"name": "rpg",            "label": "/rpg",            "desc": "RPG adventure game",                     "category": "Fun & engagement"},
    {"name": "giveaway",       "label": "/giveaway",       "desc": "Giveaways",                              "category": "Fun & engagement"},
]
COMMAND_CATEGORIES = ["Utility", "Server config", "Security", "Moderation", "Fun & engagement"]

# ── Embedded templates & stylesheets ────────────────────────────────────
# Everything lives in this one file on purpose: no templates/ or static/
# folder to keep track of, so the whole app is a single upload.
TEMPLATES = {
    "base.html": """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}Console{% endblock %} · Bot Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
{% block head_extra %}{% endblock %}
</head>
<body>
{% block body %}
{% if session.get('authed') %}
<header class="topbar">
  <a class="brand" href="{{ url_for('guilds_list') }}"><span class="brand-mark">◈</span> Console</a>
  <a class="logout" href="{{ url_for('logout') }}">Log out</a>
</header>
{% endif %}

{% include "_flash.html" %}

<main class="page">
{% block content %}{% endblock %}
</main>
{% endblock %}
</body>
</html>
""",
    "_flash.html": """{% with messages = get_flashed_messages() %}
  {% if messages %}
    <div class="flash-wrap">
      {% for msg in messages %}<div class="flash">{{ msg }}</div>{% endfor %}
    </div>
  {% endif %}
{% endwith %}
""",
    "guild_shell.html": """{% extends "base.html" %}
{% block title %}{{ name }} · {{ section }}{% endblock %}

{% block body %}
<div class="shell">
  <aside class="sidebar">
    <a class="brand" href="{{ url_for('guilds_list') }}"><span class="brand-mark">◈</span> Console</a>

    <div class="sidebar-guild">
      <div class="sidebar-guild-icon">{{ name[:1] }}</div>
      <div class="sidebar-guild-name">{{ name }}</div>
    </div>

    <nav class="sidebar-nav">
      <a class="sidebar-link {{ 'active' if section == 'Overview' }}" href="{{ url_for('guild_overview', guild_id=gid) }}">
        <span class="sidebar-ico">◐</span> Overview
      </a>
      <a class="sidebar-link {{ 'active' if section == 'Commands' }}" href="{{ url_for('guild_commands', guild_id=gid) }}">
        <span class="sidebar-ico">⌘</span> Commands
      </a>
      <a class="sidebar-link {{ 'active' if section == 'Settings' }}" href="{{ url_for('guild_settings', guild_id=gid) }}">
        <span class="sidebar-ico">⚙</span> Settings
      </a>
    </nav>

    <div class="sidebar-foot">
      <a class="sidebar-link" href="{{ url_for('guilds_list') }}"><span class="sidebar-ico">←</span> All servers</a>
      <a class="sidebar-link" href="{{ url_for('logout') }}"><span class="sidebar-ico">⏻</span> Log out</a>
    </div>
  </aside>

  <div class="shell-main">
    {% include "_flash.html" %}
    <main class="page page-in-shell">
      {% block page_content %}{% endblock %}
    </main>
  </div>
</div>
{% endblock %}
""",
    "login.html": """{% extends "base.html" %}
{% block title %}Sign in{% endblock %}
{% block content %}
<div class="login-screen">
  <div class="login-card">
    <div class="login-mark">◈</div>
    <h1>Guild Console</h1>

    {% if discord_login_available %}
    <p class="muted">Log in with Discord to manage the servers you moderate.</p>
    <a class="btn btn-primary" href="{{ url_for('login_discord') }}" style="display:block; margin-bottom:18px;">
      Log in with Discord
    </a>
    <div style="display:flex; align-items:center; gap:10px; margin:4px 0 18px; color:var(--muted); font-size:0.78rem;">
      <span style="flex:1; height:1px; background:var(--line);"></span>
      or, owner access
      <span style="flex:1; height:1px; background:var(--line);"></span>
    </div>
    {% else %}
    <p class="muted">Enter the dashboard password to continue.</p>
    {% endif %}

    <form method="post">
      <input type="password" name="password" placeholder="Owner password" autofocus required>
      <button type="submit">Enter</button>
    </form>
    {% if discord_login_available %}
    <p class="muted" style="font-size:0.76rem; margin-top:14px; margin-bottom:0;">
      Owner password sees every server the bot is in. Discord login only
      shows servers where you have Manage Server permission.
    </p>
    {% endif %}
  </div>
</div>
{% endblock %}
""",
    "landing.html": """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guild Console — the command center for your Discord server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,680&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
<link rel="stylesheet" href="/static/landing.css">
</head>
<body class="landing">

<header class="l-nav">
  <div class="l-nav-inner">
    <a class="brand" href="{{ url_for('landing') }}"><span class="brand-mark">◈</span> Guild Console</a>
    <a class="btn btn-outline" href="{{ url_for('login') }}">Log in</a>
  </div>
</header>

<section class="hero">
  <div class="hero-inner">
    <div class="hero-copy">
      <p class="eyebrow">For server owners &amp; moderators</p>
      <h1>Everything your bot tracks,<br>in one console.</h1>
      <p class="hero-sub">
        Warnings, raids, giveaways, RPG progress, automod state — your bot already
        knows all of it. Guild Console just gives you a place to look at it, and
        change it, without touching a slash command.
      </p>
      <div class="hero-actions">
        <a class="btn btn-primary btn-lg" href="{{ url_for('login') }}">Open the console</a>
        <a class="btn btn-ghost btn-lg" href="#features">See what's inside</a>
      </div>
    </div>

    <div class="hero-preview" aria-hidden="true">
      <div class="mock-window">
        <div class="mock-titlebar">
          <span class="mock-dot"></span><span class="mock-dot"></span><span class="mock-dot"></span>
          <span class="mock-url">console.yourbot.gg/guild/482910…</span>
        </div>
        <div class="mock-body">
          <div class="mock-stat-row">
            <div class="mock-stat"><div class="mock-stat-num">128</div><div class="mock-stat-label">Warnings</div></div>
            <div class="mock-stat"><div class="mock-stat-num">64</div><div class="mock-stat-label">RPG chars</div></div>
            <div class="mock-stat"><div class="mock-stat-num">3</div><div class="mock-stat-label">Giveaways</div></div>
          </div>
          <div class="mock-pills">
            <span class="mock-pill on">Automod on</span>
            <span class="mock-pill on">Anti-raid on</span>
            <span class="mock-pill">Invites blocked</span>
          </div>
          <div class="mock-table">
            <div class="mock-row mock-head"><span>User</span><span>Class</span><span>Lvl</span></div>
            <div class="mock-row"><span>Ardent_Wolf</span><span>Ranger</span><span>41</span></div>
            <div class="mock-row"><span>mossy.exe</span><span>Warlock</span><span>38</span></div>
            <div class="mock-row"><span>k4rina</span><span>Paladin</span><span>35</span></div>
          </div>
        </div>
      </div>
      <div class="mock-glow"></div>
    </div>
  </div>
</section>

<section class="l-band">
  <div class="l-band-inner">
    <div class="band-item"><span class="band-num">01</span> Sign in with your dashboard password</div>
    <div class="band-item"><span class="band-num">02</span> Pick a server from the ones your bot is in</div>
    <div class="band-item"><span class="band-num">03</span> View stats, or edit settings — changes apply instantly</div>
  </div>
</section>

<section class="features" id="features">
  <div class="features-inner">
    <h2>What's inside</h2>
    <div class="feature-grid">

      <div class="feature-card">
        <div class="feature-icon">◐</div>
        <h3>Live server stats</h3>
        <p>Warning counts, active giveaways, and message activity at a glance — the numbers your bot has been quietly logging all along.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon">⚔</div>
        <h3>RPG leaderboard</h3>
        <p>See who's actually playing your economy system: top characters by level, XP, and gold, ranked automatically.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon">◈</div>
        <h3>Moderation log</h3>
        <p>The most recent warnings, who issued them, and why — searchable history instead of scrolling through a mod-log channel.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon">▲</div>
        <h3>Anti-nuke &amp; anti-raid</h3>
        <p>Check protection status at a glance, and tune thresholds — ban limits, join-rate windows, minimum account age — without a command.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon">✦</div>
        <h3>Giveaways</h3>
        <p>Every giveaway currently running, entry counts, and when it ends — plus a running total of everything you've given away.</p>
      </div>

      <div class="feature-card">
        <div class="feature-icon">⚙</div>
        <h3>One-page settings</h3>
        <p>Welcome and leave messages, autorole, automod toggles, log channels — edit them all in a form instead of a chain of slash commands.</p>
      </div>

    </div>
  </div>
</section>

<section class="cta">
  <div class="cta-inner">
    <h2>Your server's already generating the data.</h2>
    <p>Go take a look at it.</p>
    <a class="btn btn-primary btn-lg" href="{{ url_for('login') }}">Open the console</a>
  </div>
</section>

<footer class="l-footer">
  <div class="l-footer-inner">
    <span>◈ Guild Console</span>
    <a href="{{ url_for('login') }}">Log in</a>
  </div>
</footer>

</body>
</html>
""",
    "guilds.html": """{% extends "base.html" %}
{% block title %}Guilds{% endblock %}
{% block content %}
<div class="page-head">
  <h1>Your servers</h1>
  <p class="muted">
    Select a server to view stats or edit its configuration.
    {% if session.get('oauth_username') %}
      Logged in as <strong>{{ session['oauth_username'] }}</strong> — showing servers where you have Manage Server.
    {% elif session.get('authed') %}
      Logged in with owner access — showing every server the bot is in.
    {% endif %}
  </p>
</div>

{% if guilds %}
<div class="guild-grid">
  {% for guild in guilds %}
  <a class="guild-card" href="{{ url_for('guild_overview', guild_id=guild.id) }}">
    {% if guild.icon_url %}
      <img class="guild-icon" src="{{ guild.icon_url }}" alt="">
    {% else %}
      <div class="guild-icon guild-icon-fallback">{{ guild.name[:1] }}</div>
    {% endif %}
    <div class="guild-card-name">{{ guild.name }}</div>
  </a>
  {% endfor %}
</div>
{% elif session.get('oauth_username') %}
<div class="empty-state">
  <p>No servers here yet — you need <strong>Manage Server</strong> permission in a server the bot is already in for it to show up.</p>
</div>
{% else %}
<div class="empty-state">
  <p>No servers found. Check that <code>DISCORD_TOKEN</code> is set, or that the bot has joined at least one server.</p>
</div>
{% endif %}
{% endblock %}
""",
    "guild.html": """{% extends "guild_shell.html" %}
{% set section = "Overview" %}
{% block page_content %}
<div class="page-head">
  <h1>Overview</h1>
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="stat-value">{{ warning_count }}</div>
    <div class="stat-label">Warnings issued</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{{ rpg_count }}</div>
    <div class="stat-label">RPG characters</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{{ active_giveaways|length }}</div>
    <div class="stat-label">Active giveaways</div>
  </div>
  <div class="stat-card">
    <div class="stat-value">{{ ended_giveaway_count }}</div>
    <div class="stat-label">Giveaways completed</div>
  </div>
</div>

<div class="status-strip">
  <div class="status-pill {{ 'on' if config and config['automod_enabled'] else 'off' }}">
    Automod {{ 'on' if config and config['automod_enabled'] else 'off' }}
  </div>
  <div class="status-pill {{ 'on' if antinuke and antinuke['enabled'] else 'off' }}">
    Anti-nuke {{ 'on' if antinuke and antinuke['enabled'] else 'off' }}
  </div>
  <div class="status-pill {{ 'on' if antiraid and antiraid['enabled'] else 'off' }}">
    Anti-raid {{ 'on' if antiraid and antiraid['enabled'] else 'off' }}
  </div>
  <div class="status-pill {{ 'on' if config and config['block_invites'] else 'off' }}">
    Block invites {{ 'on' if config and config['block_invites'] else 'off' }}
  </div>
</div>

<div class="panel-grid">

  <section class="panel">
    <h2>Warnings, last 14 days</h2>
    {% if warnings_by_day|selectattr('count', 'gt', 0)|list %}
    <canvas id="warningsChart" height="140"></canvas>
    {% else %}
    <p class="muted">No warnings in this window.</p>
    {% endif %}
  </section>

  <section class="panel">
    <h2>RPG leaderboard</h2>
    {% if leaderboard %}
    <canvas id="rpgChart" height="140"></canvas>
    {% else %}
    <p class="muted">No characters created yet.</p>
    {% endif %}
  </section>

  <section class="panel">
    <h2>Most active this week</h2>
    {% if top_active %}
    <canvas id="activityChart" height="140"></canvas>
    {% else %}
    <p class="muted">No message activity recorded yet.</p>
    {% endif %}
  </section>

  <section class="panel">
    <h2>Active giveaways</h2>
    {% if active_giveaways %}
    <table>
      <thead><tr><th>Prize</th><th>Winners</th><th>Entries</th><th>Ends</th></tr></thead>
      <tbody>
        {% for gw in active_giveaways %}
        <tr>
          <td>{{ gw['prize'] }}</td>
          <td>{{ gw['winners'] }}</td>
          <td>{{ gw['entry_count'] }}</td>
          <td class="mono">{{ gw['ends_at'][:16] }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="muted">No giveaways running.</p>
    {% endif %}
  </section>

  <section class="panel panel-wide">
    <h2>Recent warnings</h2>
    {% if recent_warnings %}
    <table>
      <thead><tr><th>User ID</th><th>Reason</th><th>When</th></tr></thead>
      <tbody>
        {% for w in recent_warnings %}
        <tr>
          <td class="mono">{{ w['user_id'] }}</td>
          <td>{{ w['reason'] }}</td>
          <td class="mono">{{ w['created_at'] }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="muted">No warnings on record.</p>
    {% endif %}
  </section>

</div>

<script>
const warningsByDay = {{ warnings_by_day | tojson }};
const rpgData = {{ leaderboard_chart | tojson }};
const activityData = {{ top_active_chart | tojson }};

const axisColor = "#8B8FA3";
const gridColor = "rgba(46, 50, 68, 0.5)";
Chart.defaults.color = axisColor;
Chart.defaults.font.family = "Inter, sans-serif";

const warningsCanvas = document.getElementById("warningsChart");
if (warningsCanvas) {
  new Chart(warningsCanvas, {
    type: "line",
    data: {
      labels: warningsByDay.map(d => d.day.slice(5)),
      datasets: [{
        label: "Warnings",
        data: warningsByDay.map(d => d.count),
        borderColor: "#E8A33D",
        backgroundColor: "rgba(232, 163, 61, 0.15)",
        tension: 0.3,
        fill: true,
        pointRadius: 2,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: gridColor } },
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: gridColor } },
      },
    },
  });
}

const rpgCanvas = document.getElementById("rpgChart");
if (rpgCanvas) {
  new Chart(rpgCanvas, {
    type: "bar",
    data: {
      labels: rpgData.map(c => c.user_id),
      datasets: [{
        label: "Level",
        data: rpgData.map(c => c.level),
        backgroundColor: "#5FB8A6",
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: gridColor } },
        y: { grid: { display: false }, ticks: { font: { family: "JetBrains Mono", size: 10 } } },
      },
    },
  });
}

const activityCanvas = document.getElementById("activityChart");
if (activityCanvas) {
  new Chart(activityCanvas, {
    type: "bar",
    data: {
      labels: activityData.map(a => a.user_id),
      datasets: [{
        label: "Messages this week",
        data: activityData.map(a => a.weekly_count),
        backgroundColor: "#E8A33D",
        borderRadius: 4,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { family: "JetBrains Mono", size: 10 } } },
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: gridColor } },
      },
    },
  });
}
</script>
{% endblock %}

{% block head_extra %}
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
{% endblock %}
""",
    "settings.html": """{% extends "guild_shell.html" %}
{% set section = "Settings" %}
{% block page_content %}
<div class="page-head">
  <h1>Settings</h1>
</div>

<form method="post" class="settings-form">

  <section class="panel">
    <h2>General</h2>
    <div class="field-grid">
      <label>Welcome channel ID
        <input type="text" name="welcome_channel_id" class="mono"
               value="{{ config['welcome_channel_id'] if config and config['welcome_channel_id'] else '' }}"
               placeholder="channel ID">
      </label>
      <label>Leave channel ID
        <input type="text" name="leave_channel_id" class="mono"
               value="{{ config['leave_channel_id'] if config and config['leave_channel_id'] else '' }}"
               placeholder="channel ID">
      </label>
      <label>Log channel ID
        <input type="text" name="log_channel_id" class="mono"
               value="{{ config['log_channel_id'] if config and config['log_channel_id'] else '' }}"
               placeholder="channel ID">
      </label>
      <label>Autorole ID
        <input type="text" name="autorole_id" class="mono"
               value="{{ config['autorole_id'] if config and config['autorole_id'] else '' }}"
               placeholder="role ID, given to new members">
      </label>
    </div>
    <label class="full">Welcome message
      <textarea name="welcome_message" rows="2" placeholder="Use {member} and {guild} as placeholders">{{ config['welcome_message'] if config and config['welcome_message'] else '' }}</textarea>
    </label>
    <label class="full">Leave message
      <textarea name="leave_message" rows="2">{{ config['leave_message'] if config and config['leave_message'] else '' }}</textarea>
    </label>
    <div class="toggle-row">
      <label class="toggle"><input type="checkbox" name="automod_enabled" {% if config and config['automod_enabled'] %}checked{% endif %}> Automod enabled</label>
      <label class="toggle"><input type="checkbox" name="block_invites" {% if config and config['block_invites'] %}checked{% endif %}> Block invite links</label>
      <label class="toggle"><input type="checkbox" name="block_staff_mentions" {% if config and config['block_staff_mentions'] %}checked{% endif %}> Block mass staff mentions</label>
    </div>
  </section>

  <section class="panel">
    <h2>Welcome card</h2>
    <p class="muted" style="margin-top:-8px;">
      Plain text is just the welcome message above. Embed card adds a
      title, author line, banner image, accent color, and footer — all
      support <code>{member}</code>, <code>{server}</code>,
      <code>{count}</code>, <code>{ordinal}</code> (e.g. "412th").
    </p>
    <div class="field-grid">
      <label>Style
        <select name="welcome_style">
          {% set current_style = config['welcome_style'] if config and config['welcome_style'] else 'plain' %}
          <option value="plain" {% if current_style == 'plain' %}selected{% endif %}>Plain text</option>
          <option value="embed" {% if current_style == 'embed' %}selected{% endif %}>Embed card</option>
        </select>
      </label>
      <label>Accent color
        <input type="color" name="welcome_color" style="height:38px; padding:4px;"
               value="{{ config['welcome_color'] if config and config['welcome_color'] else '#E8A33D' }}">
      </label>
      <label>Banner image URL
        <input type="text" name="welcome_banner_url" class="mono"
               value="{{ config['welcome_banner_url'] if config and config['welcome_banner_url'] else '' }}"
               placeholder="https://…">
      </label>
    </div>
    <div class="field-grid">
      <label>Author line (small text above the title)
        <input type="text" name="welcome_author_text"
               value="{{ config['welcome_author_text'] if config and config['welcome_author_text'] else '' }}"
               placeholder="e.g. 🌌 THE COSMOS WELCOMES YOU">
      </label>
      <label>Title
        <input type="text" name="welcome_title"
               value="{{ config['welcome_title'] if config and config['welcome_title'] else '' }}"
               placeholder="e.g. ✨ A new star enters {server} ✨">
      </label>
    </div>
    <label class="full">Footer text
      <input type="text" name="welcome_footer_text"
             value="{{ config['welcome_footer_text'] if config and config['welcome_footer_text'] else '' }}"
             placeholder="e.g. ⋆ {ordinal} soul to join the cosmos ⋆">
    </label>
    <label class="toggle"><input type="checkbox" name="welcome_show_count"
      {% if config is none or config['welcome_show_count'] %}checked{% endif %}> Show "you are our Nth member" (only used if footer text above is left empty)</label>
  </section>

  <section class="panel">
    <h2>Anti-nuke</h2>
    <label class="toggle"><input type="checkbox" name="antinuke_enabled" {% if antinuke and antinuke['enabled'] %}checked{% endif %}> Enabled</label>
    <div class="field-grid">
      <label>Log channel ID
        <input type="text" name="antinuke_log_channel_id" class="mono"
               value="{{ antinuke['log_channel_id'] if antinuke and antinuke['log_channel_id'] else '' }}">
      </label>
      <label>Action
        <select name="antinuke_action">
          {% set current = antinuke['action'] if antinuke else 'kick' %}
          {% for opt in ['kick', 'ban', 'strip_roles'] %}
            <option value="{{ opt }}" {% if current == opt %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>
      </label>
      <label>Ban threshold
        <input type="number" min="1" name="ban_threshold"
               value="{{ antinuke['ban_threshold'] if antinuke else 3 }}">
      </label>
      <label>Channel-delete threshold
        <input type="number" min="1" name="channel_delete_threshold"
               value="{{ antinuke['channel_delete_threshold'] if antinuke else 3 }}">
      </label>
      <label>Role-delete threshold
        <input type="number" min="1" name="role_delete_threshold"
               value="{{ antinuke['role_delete_threshold'] if antinuke else 3 }}">
      </label>
    </div>
  </section>

  <section class="panel">
    <h2>Anti-raid</h2>
    <label class="toggle"><input type="checkbox" name="antiraid_enabled" {% if antiraid and antiraid['enabled'] %}checked{% endif %}> Enabled</label>
    <div class="field-grid">
      <label>Log channel ID
        <input type="text" name="antiraid_log_channel_id" class="mono"
               value="{{ antiraid['log_channel_id'] if antiraid and antiraid['log_channel_id'] else '' }}">
      </label>
      <label>Action
        <select name="antiraid_action">
          {% set current = antiraid['action'] if antiraid else 'kick' %}
          {% for opt in ['kick', 'ban'] %}
            <option value="{{ opt }}" {% if current == opt %}selected{% endif %}>{{ opt }}</option>
          {% endfor %}
        </select>
      </label>
      <label>Join threshold
        <input type="number" min="1" name="join_threshold"
               value="{{ antiraid['join_threshold'] if antiraid else 10 }}">
      </label>
      <label>Join window (seconds)
        <input type="number" min="1" name="join_window"
               value="{{ antiraid['join_window'] if antiraid else 10 }}">
      </label>
      <label>Min account age (days)
        <input type="number" min="0" name="min_account_age_days"
               value="{{ antiraid['min_account_age_days'] if antiraid else 7 }}">
      </label>
    </div>
  </section>

  <div class="form-actions">
    <button type="submit" class="btn btn-primary">Save changes</button>
  </div>
</form>
{% endblock %}
""",
    "commands.html": """{% extends "guild_shell.html" %}
{% set section = "Commands" %}
{% block page_content %}
<div class="page-head">
  <h1>Commands</h1>
</div>
<p class="muted" style="margin-top: -14px; margin-bottom: 24px; max-width: 60ch;">
  Turn command groups on or off for this server, and optionally restrict a
  group to specific roles. Leave the role box empty to allow anyone the
  command's own permissions allow (server admins can always use every
  command).
</p>

<form method="post" class="settings-form">
  {% for category, cmds in categories.items() %}
    {% if cmds %}
    <section class="panel">
      <h2>{{ category }}</h2>
      <div class="cmd-list">
        {% for cmd in cmds %}
        {% set s = settings_map.get(cmd.name) %}
        {% set is_enabled = (s.enabled if s else True) %}
        {% set selected_roles = (s.roles if s else []) %}
        <div class="cmd-row">
          <label class="toggle cmd-toggle">
            <input type="checkbox" name="enabled_{{ cmd.name }}" {% if is_enabled %}checked{% endif %}>
            <span class="cmd-name">{{ cmd.label }}</span>
          </label>
          <span class="cmd-desc">{{ cmd.desc }}</span>
          {% if roles %}
          <select name="roles_{{ cmd.name }}" multiple class="cmd-roles" size="1">
            {% for role in roles %}
              <option value="{{ role.id }}" {% if role.id|int in selected_roles %}selected{% endif %}>{{ role.name }}</option>
            {% endfor %}
          </select>
          {% else %}
          <span class="muted cmd-roles-empty">No roles found</span>
          {% endif %}
        </div>
        {% endfor %}
      </div>
    </section>
    {% endif %}
  {% endfor %}

  <div class="form-actions">
    <button type="submit" class="btn btn-primary">Save changes</button>
  </div>
</form>
{% endblock %}
""",
}

STYLE_CSS = """:root {
  --bg:        #12141C;
  --panel:     #1B1E2B;
  --panel-2:   #222537;
  --line:      #2E3244;
  --text:      #E9E6DF;
  --muted:     #8B8FA3;
  --amber:     #E8A33D;
  --amber-dim: #7A5A26;
  --teal:      #5FB8A6;
  --red:       #E0574F;

  --display: "Fraunces", Georgia, serif;
  --body: "Inter", -apple-system, BlinkMacSystemFont, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  background-image:
    radial-gradient(circle at 15% 0%, rgba(232, 163, 61, 0.06), transparent 40%),
    radial-gradient(circle at 85% 15%, rgba(95, 184, 166, 0.05), transparent 45%);
  color: var(--text);
  font-family: var(--body);
  min-height: 100vh;
}

a { color: inherit; text-decoration: none; }

.topbar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 32px;
  border-bottom: 1px solid var(--line);
  background: rgba(27, 30, 43, 0.6);
}
.brand {
  font-family: var(--display);
  font-weight: 600;
  font-size: 1.15rem;
  letter-spacing: 0.01em;
}
.brand-mark { color: var(--amber); margin-right: 4px; }
.crumbs { color: var(--muted); font-size: 0.92rem; display: flex; gap: 8px; align-items: center; }
.crumbs a:hover { color: var(--amber); }
.crumb-sep { opacity: 0.5; }
.logout {
  margin-left: auto;
  color: var(--muted);
  font-size: 0.88rem;
  border: 1px solid var(--line);
  padding: 6px 12px;
  border-radius: 6px;
}
.logout:hover { border-color: var(--amber-dim); color: var(--amber); }

.flash-wrap { max-width: 960px; margin: 16px auto 0; padding: 0 32px; }
.flash {
  background: rgba(95, 184, 166, 0.12);
  border: 1px solid var(--teal);
  color: var(--teal);
  padding: 10px 16px;
  border-radius: 8px;
  font-size: 0.9rem;
}

.page {
  max-width: 1080px;
  margin: 0 auto;
  padding: 40px 32px 80px;
}

.page-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 28px;
  flex-wrap: wrap;
  gap: 12px;
}
h1 {
  font-family: var(--display);
  font-weight: 600;
  font-size: 2rem;
  margin: 0;
}
h2 {
  font-family: var(--display);
  font-weight: 600;
  font-size: 1.15rem;
  margin: 0 0 16px;
  color: var(--amber);
}
.muted { color: var(--muted); }
code {
  font-family: var(--mono);
  background: var(--panel-2);
  padding: 2px 6px;
  border-radius: 4px;
}

/* Login */
.login-screen {
  min-height: 90vh;
  display: flex;
  align-items: center;
  justify-content: center;
}
.login-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 40px;
  width: 320px;
  text-align: center;
}
.login-mark { color: var(--amber); font-size: 2rem; margin-bottom: 8px; }
.login-card h1 { font-size: 1.3rem; margin-bottom: 4px; }
.login-card p { margin-top: 0; margin-bottom: 20px; font-size: 0.88rem; }
.login-card form { display: flex; flex-direction: column; gap: 10px; }
.login-card input {
  background: var(--panel-2);
  border: 1px solid var(--line);
  color: var(--text);
  padding: 10px 12px;
  border-radius: 8px;
  font-family: var(--body);
}
.login-card button {
  background: var(--amber);
  color: #221606;
  border: none;
  padding: 10px 12px;
  border-radius: 8px;
  font-weight: 600;
  cursor: pointer;
}
.login-card button:hover { background: #f0af4c; }

/* Guild grid */
.guild-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 16px;
}
.guild-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  transition: border-color 0.15s, transform 0.15s;
}
.guild-card:hover { border-color: var(--amber-dim); transform: translateY(-2px); }
.guild-icon { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; }
.guild-icon-fallback {
  display: flex; align-items: center; justify-content: center;
  background: var(--panel-2);
  font-family: var(--display);
  color: var(--amber);
  font-size: 1.3rem;
}
.guild-card-name { font-size: 0.92rem; text-align: center; }

.empty-state {
  border: 1px dashed var(--line);
  border-radius: 12px;
  padding: 32px;
  text-align: center;
  color: var(--muted);
}

/* Stat cards */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.stat-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 18px 20px;
}
.stat-value {
  font-family: var(--mono);
  font-size: 1.8rem;
  color: var(--amber);
  font-weight: 500;
}
.stat-label { color: var(--muted); font-size: 0.82rem; margin-top: 4px; }

.status-strip {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 28px;
}
.status-pill {
  font-size: 0.78rem;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--muted);
}
.status-pill.on { border-color: var(--teal); color: var(--teal); }
.status-pill.off { border-color: var(--line); color: var(--muted); }

/* Panels */
.panel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 18px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 22px 24px;
}

table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th {
  text-align: left;
  color: var(--muted);
  font-weight: 500;
  font-size: 0.76rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
td { padding: 8px 0; border-bottom: 1px solid rgba(46, 50, 68, 0.5); }
tr:last-child td { border-bottom: none; }
.mono { font-family: var(--mono); font-size: 0.82rem; color: var(--muted); }

/* Settings form */
.settings-form { display: flex; flex-direction: column; gap: 18px; }
.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.82rem;
  color: var(--muted);
}
label.full { margin-bottom: 14px; }
input, select, textarea {
  background: var(--panel-2);
  border: 1px solid var(--line);
  color: var(--text);
  padding: 9px 10px;
  border-radius: 8px;
  font-family: var(--body);
  font-size: 0.92rem;
}
textarea { resize: vertical; font-family: var(--body); }
.toggle-row { display: flex; gap: 20px; flex-wrap: wrap; margin: 6px 0 4px; }
.toggle { flex-direction: row; align-items: center; gap: 8px; font-size: 0.88rem; color: var(--text); }
.toggle input { width: auto; }

.form-actions { display: flex; justify-content: flex-end; }

/* Command setup page */
.cmd-list { display: flex; flex-direction: column; gap: 4px; }
.cmd-row {
  display: grid;
  grid-template-columns: 160px 1fr 180px;
  align-items: center;
  gap: 14px;
  padding: 10px 0;
  border-bottom: 1px solid rgba(46, 50, 68, 0.5);
}
.cmd-row:last-child { border-bottom: none; }
.cmd-toggle { flex-direction: row; }
.cmd-name { font-family: var(--mono); font-size: 0.86rem; color: var(--text); }
.cmd-desc { color: var(--muted); font-size: 0.82rem; }
.cmd-roles { min-width: 160px; max-height: 32px; font-size: 0.78rem; }
.cmd-roles-empty { font-size: 0.78rem; justify-self: end; }
@media (max-width: 700px) {
  .cmd-row { grid-template-columns: 1fr; gap: 6px; }
}

.btn {
  display: inline-block;
  padding: 9px 18px;
  border-radius: 8px;
  font-size: 0.88rem;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
}
.btn-primary { background: var(--amber); color: #221606; }
.btn-primary:hover { background: #f0af4c; }
.btn-outline { border-color: var(--line); color: var(--text); }
.btn-outline:hover { border-color: var(--amber-dim); color: var(--amber); }

@media (max-width: 640px) {
  .topbar { padding: 14px 18px; }
  .page { padding: 28px 18px 60px; }
}

/* ── Sidebar shell (guild-scoped pages) ─────────────────────────────── */
.shell {
  display: flex;
  min-height: 100vh;
}
.sidebar {
  width: 232px;
  flex-shrink: 0;
  background: var(--panel);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  padding: 22px 16px;
  position: sticky;
  top: 0;
  height: 100vh;
}
.sidebar .brand { padding: 0 8px; margin-bottom: 24px; }
.sidebar-guild {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 8px;
  margin-bottom: 18px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 18px;
}
.sidebar-guild-icon {
  width: 34px; height: 34px;
  border-radius: 50%;
  background: var(--panel-2);
  color: var(--amber);
  font-family: var(--display);
  display: flex; align-items: center; justify-content: center;
  font-size: 1rem;
  flex-shrink: 0;
}
.sidebar-guild-name {
  font-size: 0.9rem;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
}
.sidebar-link {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: 8px;
  font-size: 0.88rem;
  color: var(--muted);
}
.sidebar-link:hover { background: var(--panel-2); color: var(--text); }
.sidebar-link.active { background: rgba(232, 163, 61, 0.12); color: var(--amber); }
.sidebar-ico { width: 16px; text-align: center; opacity: 0.85; font-size: 0.9rem; }
.sidebar-foot {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-top: 1px solid var(--line);
  padding-top: 10px;
}
.shell-main { flex: 1; min-width: 0; }
.page-in-shell { max-width: 980px; padding: 36px 40px 80px; }

.panel-wide { grid-column: 1 / -1; }

canvas { max-width: 100%; }

@media (max-width: 900px) {
  .shell { flex-direction: column; }
  .sidebar { width: 100%; height: auto; position: static; flex-direction: row; align-items: center; padding: 12px 16px; gap: 16px; overflow-x: auto; }
  .sidebar .brand { margin-bottom: 0; }
  .sidebar-guild { border-bottom: none; padding-bottom: 0; margin-bottom: 0; }
  .sidebar-nav { flex-direction: row; flex: none; }
  .sidebar-foot { flex-direction: row; border-top: none; padding-top: 0; }
  .page-in-shell { padding: 24px 18px 60px; }
}
"""

LANDING_CSS = """/* Landing page — reuses tokens from style.css, adds marketing-page layout */

body.landing { overflow-x: hidden; }

.l-nav {
  border-bottom: 1px solid var(--line);
  background: rgba(18, 20, 28, 0.7);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 10;
}
.l-nav-inner {
  max-width: 1120px;
  margin: 0 auto;
  padding: 18px 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

/* Hero */
.hero {
  padding: 96px 32px 60px;
  background-image:
    radial-gradient(circle at 20% 0%, rgba(232, 163, 61, 0.10), transparent 45%),
    radial-gradient(circle at 90% 10%, rgba(95, 184, 166, 0.08), transparent 40%);
}
.hero-inner {
  max-width: 1120px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  gap: 56px;
  align-items: center;
}
.eyebrow {
  font-family: var(--mono);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--amber);
  margin: 0 0 14px;
}
.hero h1 {
  font-family: var(--display);
  font-weight: 680;
  font-size: 3.1rem;
  line-height: 1.08;
  margin: 0 0 20px;
  letter-spacing: -0.01em;
}
.hero-sub {
  color: var(--muted);
  font-size: 1.08rem;
  line-height: 1.6;
  max-width: 46ch;
  margin: 0 0 32px;
}
.hero-actions { display: flex; gap: 14px; flex-wrap: wrap; }

.btn-lg { padding: 13px 26px; font-size: 0.95rem; }
.btn-ghost { color: var(--text); }
.btn-ghost:hover { color: var(--amber); }

/* Mock preview window */
.hero-preview { position: relative; }
.mock-glow {
  position: absolute;
  inset: -30px;
  background: radial-gradient(circle, rgba(232, 163, 61, 0.14), transparent 65%);
  filter: blur(10px);
  z-index: 0;
}
.mock-window {
  position: relative;
  z-index: 1;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 30px 60px -20px rgba(0,0,0,0.6);
  transform: rotate(0.4deg);
}
.mock-titlebar {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--panel-2);
}
.mock-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line); }
.mock-url {
  margin-left: 10px;
  font-family: var(--mono);
  font-size: 0.72rem;
  color: var(--muted);
}
.mock-body { padding: 20px; }
.mock-stat-row { display: flex; gap: 10px; margin-bottom: 16px; }
.mock-stat {
  flex: 1;
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
}
.mock-stat-num { font-family: var(--mono); color: var(--amber); font-size: 1.4rem; }
.mock-stat-label { font-size: 0.72rem; color: var(--muted); margin-top: 2px; }
.mock-pills { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
.mock-pill {
  font-size: 0.7rem;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--muted);
}
.mock-pill.on { border-color: var(--teal); color: var(--teal); }
.mock-table { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.mock-row {
  display: grid;
  grid-template-columns: 1.4fr 1fr 0.6fr;
  padding: 9px 14px;
  font-size: 0.78rem;
  font-family: var(--mono);
  border-bottom: 1px solid var(--line);
  color: var(--text);
}
.mock-row:last-child { border-bottom: none; }
.mock-row.mock-head {
  color: var(--muted);
  text-transform: uppercase;
  font-size: 0.66rem;
  letter-spacing: 0.05em;
  background: var(--panel-2);
}

/* Step band */
.l-band {
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: var(--panel);
}
.l-band-inner {
  max-width: 1120px;
  margin: 0 auto;
  padding: 22px 32px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 18px;
}
.band-item {
  font-size: 0.86rem;
  color: var(--muted);
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.band-num {
  font-family: var(--mono);
  color: var(--amber);
  font-size: 0.78rem;
}

/* Features */
.features { padding: 88px 32px; }
.features-inner { max-width: 1120px; margin: 0 auto; }
.features h2 {
  font-family: var(--display);
  font-size: 2rem;
  font-weight: 600;
  color: var(--text);
  margin: 0 0 40px;
}
.feature-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 18px;
}
.feature-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 26px 24px;
}
.feature-icon {
  font-size: 1.3rem;
  color: var(--amber);
  margin-bottom: 14px;
}
.feature-card h3 {
  font-family: var(--display);
  font-size: 1.05rem;
  font-weight: 600;
  margin: 0 0 8px;
}
.feature-card p {
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.55;
  margin: 0;
}

/* CTA */
.cta {
  padding: 90px 32px;
  text-align: center;
  border-top: 1px solid var(--line);
  background-image: radial-gradient(circle at 50% 0%, rgba(232, 163, 61, 0.08), transparent 55%);
}
.cta h2 {
  font-family: var(--display);
  font-size: 1.8rem;
  font-weight: 600;
  margin: 0 0 6px;
}
.cta p { color: var(--muted); margin: 0 0 28px; }

/* Footer */
.l-footer { border-top: 1px solid var(--line); padding: 26px 32px; }
.l-footer-inner {
  max-width: 1120px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  color: var(--muted);
  font-size: 0.84rem;
}
.l-footer-inner a:hover { color: var(--amber); }

@media (max-width: 860px) {
  .hero-inner { grid-template-columns: 1fr; }
  .hero h1 { font-size: 2.3rem; }
  .l-band-inner { grid-template-columns: 1fr; }
  .feature-grid { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
}
"""

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.jinja_loader = DictLoader(TEMPLATES)


@app.route("/static/style.css")
def _style_css():
    return Response(STYLE_CSS, mimetype="text/css")


@app.route("/static/landing.css")
def _landing_css():
    return Response(LANDING_CSS, mimetype="text/css")

_guild_cache = {"data": None, "fetched_at": 0}
GUILD_CACHE_TTL = 60  # seconds


# ── DB helpers ─────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def q1(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    db = get_db()
    db.execute(sql, params)
    db.commit()


def ensure_schema():
    """Defensive: create/extend tables even if the bot hasn't been
    restarted with the latest main.py yet, so the dashboard never 500s
    on a column that only exists in newer code."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS command_settings (
            guild_id         INTEGER NOT NULL,
            command_name     TEXT    NOT NULL,
            enabled          INTEGER DEFAULT 1,
            allowed_role_ids TEXT    DEFAULT '[]',
            PRIMARY KEY (guild_id, command_name)
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY
        );
    """)
    for stmt in [
        "ALTER TABLE guild_config ADD COLUMN welcome_channel_id INTEGER",
        "ALTER TABLE guild_config ADD COLUMN welcome_message TEXT",
        "ALTER TABLE guild_config ADD COLUMN leave_channel_id INTEGER",
        "ALTER TABLE guild_config ADD COLUMN leave_message TEXT",
        "ALTER TABLE guild_config ADD COLUMN log_channel_id INTEGER",
        "ALTER TABLE guild_config ADD COLUMN automod_enabled INTEGER DEFAULT 0",
        "ALTER TABLE guild_config ADD COLUMN block_invites INTEGER DEFAULT 0",
        "ALTER TABLE guild_config ADD COLUMN autorole_id INTEGER",
        "ALTER TABLE guild_config ADD COLUMN block_staff_mentions INTEGER DEFAULT 0",
        "ALTER TABLE guild_config ADD COLUMN welcome_style TEXT DEFAULT 'plain'",
        "ALTER TABLE guild_config ADD COLUMN welcome_color TEXT DEFAULT '#E8A33D'",
        "ALTER TABLE guild_config ADD COLUMN welcome_banner_url TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_show_count INTEGER DEFAULT 1",
        "ALTER TABLE guild_config ADD COLUMN welcome_title TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_author_text TEXT",
        "ALTER TABLE guild_config ADD COLUMN welcome_footer_text TEXT",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


ensure_schema()


# ── Discord REST helpers ──────────────────────────────────────────────────
def discord_guilds():
    """List of guilds the bot is in, cached briefly. Falls back to DB-only
    guild IDs (no name/icon) if the token isn't set or the API call fails."""
    now = time.time()
    if _guild_cache["data"] is not None and now - _guild_cache["fetched_at"] < GUILD_CACHE_TTL:
        return _guild_cache["data"]

    guilds = []
    if DISCORD_TOKEN:
        try:
            resp = requests.get(
                f"{API_BASE}/users/@me/guilds",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
                timeout=10,
            )
            if resp.ok:
                for guild in resp.json():
                    icon_url = None
                    if guild.get("icon"):
                        icon_url = (
                            f"https://cdn.discordapp.com/icons/"
                            f"{guild['id']}/{guild['icon']}.png?size=64"
                        )
                    guilds.append({
                        "id": guild["id"],
                        "name": guild["name"],
                        "icon_url": icon_url,
                    })
        except requests.RequestException:
            pass

    if not guilds:
        # Fallback: whatever guild_ids show up in guild_config
        rows = q("SELECT guild_id FROM guild_config")
        guilds = [{"id": str(r["guild_id"]), "name": f"Guild {r['guild_id']}", "icon_url": None}
                  for r in rows]

    _guild_cache["data"] = guilds
    _guild_cache["fetched_at"] = now
    return guilds


def guild_name(guild_id: str) -> str:
    for guild in discord_guilds():
        if str(guild["id"]) == str(guild_id):
            return guild["name"]
    return f"Guild {guild_id}"


_role_cache: dict = {}
ROLE_CACHE_TTL = 60


def discord_roles(guild_id: str):
    """Roles for one guild, cached briefly. Empty list if the token isn't
    set or the call fails — the commands page still works, just without
    role restriction options."""
    now = time.time()
    cached = _role_cache.get(guild_id)
    if cached and now - cached["fetched_at"] < ROLE_CACHE_TTL:
        return cached["data"]

    roles = []
    if DISCORD_TOKEN:
        try:
            resp = requests.get(
                f"{API_BASE}/guilds/{guild_id}/roles",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
                timeout=10,
            )
            if resp.ok:
                roles = [
                    {"id": r["id"], "name": r["name"]}
                    for r in resp.json()
                    if r["name"] != "@everyone"
                ]
                roles.sort(key=lambda r: r["name"].lower())
        except requests.RequestException:
            pass

    _role_cache[guild_id] = {"data": roles, "fetched_at": now}
    return roles


# ── Auth ───────────────────────────────────────────────────────────────────
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed") and not session.get("oauth_user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def guild_access_allowed(guild_id) -> bool:
    """Owner-password logins see every server. Discord-login users only
    see servers where Discord itself says they have Manage Server (or
    Administrator, or they own it)."""
    if session.get("authed"):
        return True
    allowed = session.get("oauth_guild_ids")
    return allowed is not None and int(guild_id) in allowed


def guild_scoped(view):
    """Blocks a /guild/<guild_id>/... route unless the logged-in person
    is allowed to see that specific guild."""
    @wraps(view)
    def wrapped(guild_id, *args, **kwargs):
        if not guild_access_allowed(guild_id):
            flash("You don't have access to that server.")
            return redirect(url_for("guilds_list"))
        return view(guild_id, *args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("guilds_list"))
        flash("Wrong password.")
    return render_template("login.html", discord_login_available=bool(DISCORD_CLIENT_ID))


@app.route("/login/discord")
def login_discord():
    if not DISCORD_CLIENT_ID:
        flash("Discord login isn't configured on this server yet.")
        return redirect(url_for("login"))
    redirect_uri = f"{DASHBOARD_URL_BASE()}/callback"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify guilds",
    }
    return redirect(f"{API_BASE}/oauth2/authorize?{urlencode(params)}")


@app.route("/callback")
def discord_callback():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        flash("Discord login isn't configured on this server yet.")
        return redirect(url_for("login"))

    code = request.args.get("code")
    if not code:
        flash("Discord login was cancelled or failed.")
        return redirect(url_for("login"))

    redirect_uri = f"{DASHBOARD_URL_BASE()}/callback"
    try:
        token_resp = requests.post(
            f"{API_BASE}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        auth_header = {"Authorization": f"Bearer {access_token}"}
        me = requests.get(f"{API_BASE}/users/@me", headers=auth_header, timeout=10).json()
        my_guilds = requests.get(f"{API_BASE}/users/@me/guilds", headers=auth_header, timeout=10).json()
    except requests.RequestException:
        flash("Couldn't reach Discord to log you in — try again in a moment.")
        return redirect(url_for("login"))

    bot_guild_ids = {int(g["id"]) for g in discord_guilds()}
    allowed_ids = []
    for g in my_guilds:
        perms = int(g.get("permissions", 0))
        has_access = g.get("owner") or (perms & MANAGE_GUILD) or (perms & ADMINISTRATOR)
        if has_access and int(g["id"]) in bot_guild_ids:
            allowed_ids.append(int(g["id"]))

    session["oauth_user_id"] = me.get("id")
    session["oauth_username"] = me.get("username")
    session["oauth_guild_ids"] = allowed_ids
    return redirect(url_for("guilds_list"))


def DASHBOARD_URL_BASE():
    configured = os.getenv("DASHBOARD_URL")
    base = configured if configured else request.url_root
    return base.rstrip("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Public landing page ────────────────────────────────────────────────────
@app.route("/")
def landing():
    if session.get("authed") or session.get("oauth_user_id"):
        return redirect(url_for("guilds_list"))
    return render_template("landing.html")


# ── Guild list ───────────────────────────────────────────────────────────
@app.route("/console")
@login_required
def guilds_list():
    if session.get("authed"):
        guilds = discord_guilds()
    else:
        allowed = set(session.get("oauth_guild_ids") or [])
        guilds = [g for g in discord_guilds() if int(g["id"]) in allowed]
    return render_template("guilds.html", guilds=guilds)


# ── Guild overview ──────────────────────────────────────────────────────
@app.route("/guild/<guild_id>")
@login_required
@guild_scoped
def guild_overview(guild_id):
    gid = int(guild_id)

    warning_count = q1(
        "SELECT COUNT(*) c FROM warnings WHERE guild_id=?", (gid,)
    )["c"]
    recent_warnings = q(
        """SELECT user_id, reason, created_at FROM warnings
           WHERE guild_id=? ORDER BY created_at DESC LIMIT 10""", (gid,)
    )

    rpg_count = q1(
        "SELECT COUNT(*) c FROM rpg_characters WHERE guild_id=?", (gid,)
    )["c"]
    leaderboard = q(
        """SELECT user_id, class, level, xp, gold FROM rpg_characters
           WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 10""", (gid,)
    )

    active_giveaways = q(
        """SELECT message_id, prize, winners, ends_at, entries FROM giveaways
           WHERE guild_id=? AND ended=0 ORDER BY ends_at ASC""", (gid,)
    )
    active_giveaways = [
        dict(row, entry_count=len(json.loads(row["entries"] or "[]")))
        for row in active_giveaways
    ]
    ended_giveaway_count = q1(
        "SELECT COUNT(*) c FROM giveaways WHERE guild_id=? AND ended=1", (gid,)
    )["c"]

    top_active = q(
        """SELECT user_id, weekly_count FROM message_activity
           WHERE guild_id=? ORDER BY weekly_count DESC LIMIT 5""", (gid,)
    )

    warnings_raw = q(
        """SELECT date(created_at) d, COUNT(*) c FROM warnings
           WHERE guild_id=? AND created_at >= date('now', '-13 days')
           GROUP BY d ORDER BY d ASC""", (gid,)
    )
    warnings_by_date = {r["d"]: r["c"] for r in warnings_raw}
    warnings_by_day = []
    for i in range(13, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        warnings_by_day.append({"day": day, "count": warnings_by_date.get(day, 0)})

    config = q1("SELECT * FROM guild_config WHERE guild_id=?", (gid,))
    antinuke = q1("SELECT * FROM antinuke_config WHERE guild_id=?", (gid,))
    antiraid = q1("SELECT * FROM antiraid_config WHERE guild_id=?", (gid,))

    return render_template(
        "guild.html",
        gid=gid,
        name=guild_name(guild_id),
        warning_count=warning_count,
        recent_warnings=recent_warnings,
        rpg_count=rpg_count,
        leaderboard=leaderboard,
        leaderboard_chart=[dict(row) for row in leaderboard],
        active_giveaways=active_giveaways,
        ended_giveaway_count=ended_giveaway_count,
        top_active=top_active,
        top_active_chart=[dict(row) for row in top_active],
        warnings_by_day=warnings_by_day,
        config=config,
        antinuke=antinuke,
        antiraid=antiraid,
        now=datetime.now(timezone.utc),
    )


# ── Settings (edit) ───────────────────────────────────────────────────────
@app.route("/guild/<guild_id>/settings", methods=["GET", "POST"])
@login_required
@guild_scoped
def guild_settings(guild_id):
    gid = int(guild_id)

    if request.method == "POST":
        form = request.form

        def as_int_or_none(key):
            val = form.get(key, "").strip()
            return int(val) if val else None

        execute(
            """INSERT INTO guild_config
                   (guild_id, welcome_channel_id, welcome_message,
                    leave_channel_id, leave_message, log_channel_id,
                    automod_enabled, block_invites, autorole_id,
                    block_staff_mentions, welcome_style, welcome_color,
                    welcome_banner_url, welcome_show_count,
                    welcome_title, welcome_author_text, welcome_footer_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   welcome_channel_id=excluded.welcome_channel_id,
                   welcome_message=excluded.welcome_message,
                   leave_channel_id=excluded.leave_channel_id,
                   leave_message=excluded.leave_message,
                   log_channel_id=excluded.log_channel_id,
                   automod_enabled=excluded.automod_enabled,
                   block_invites=excluded.block_invites,
                   autorole_id=excluded.autorole_id,
                   block_staff_mentions=excluded.block_staff_mentions,
                   welcome_style=excluded.welcome_style,
                   welcome_color=excluded.welcome_color,
                   welcome_banner_url=excluded.welcome_banner_url,
                   welcome_show_count=excluded.welcome_show_count,
                   welcome_title=excluded.welcome_title,
                   welcome_author_text=excluded.welcome_author_text,
                   welcome_footer_text=excluded.welcome_footer_text""",
            (
                gid,
                as_int_or_none("welcome_channel_id"),
                form.get("welcome_message") or None,
                as_int_or_none("leave_channel_id"),
                form.get("leave_message") or None,
                as_int_or_none("log_channel_id"),
                1 if form.get("automod_enabled") else 0,
                1 if form.get("block_invites") else 0,
                as_int_or_none("autorole_id"),
                1 if form.get("block_staff_mentions") else 0,
                form.get("welcome_style") or "plain",
                form.get("welcome_color") or "#E8A33D",
                form.get("welcome_banner_url") or None,
                1 if form.get("welcome_show_count") else 0,
                form.get("welcome_title") or None,
                form.get("welcome_author_text") or None,
                form.get("welcome_footer_text") or None,
            ),
        )

        execute(
            """INSERT INTO antinuke_config
                   (guild_id, enabled, log_channel_id, action,
                    ban_threshold, channel_delete_threshold, role_delete_threshold)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   enabled=excluded.enabled,
                   log_channel_id=excluded.log_channel_id,
                   action=excluded.action,
                   ban_threshold=excluded.ban_threshold,
                   channel_delete_threshold=excluded.channel_delete_threshold,
                   role_delete_threshold=excluded.role_delete_threshold""",
            (
                gid,
                1 if form.get("antinuke_enabled") else 0,
                as_int_or_none("antinuke_log_channel_id"),
                form.get("antinuke_action") or "kick",
                int(form.get("ban_threshold") or 3),
                int(form.get("channel_delete_threshold") or 3),
                int(form.get("role_delete_threshold") or 3),
            ),
        )

        execute(
            """INSERT INTO antiraid_config
                   (guild_id, enabled, log_channel_id, join_threshold,
                    join_window, action, min_account_age_days)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   enabled=excluded.enabled,
                   log_channel_id=excluded.log_channel_id,
                   join_threshold=excluded.join_threshold,
                   join_window=excluded.join_window,
                   action=excluded.action,
                   min_account_age_days=excluded.min_account_age_days""",
            (
                gid,
                1 if form.get("antiraid_enabled") else 0,
                as_int_or_none("antiraid_log_channel_id"),
                int(form.get("join_threshold") or 10),
                int(form.get("join_window") or 10),
                form.get("antiraid_action") or "kick",
                int(form.get("min_account_age_days") or 7),
            ),
        )

        flash("Settings saved.")
        return redirect(url_for("guild_settings", guild_id=guild_id))

    config = q1("SELECT * FROM guild_config WHERE guild_id=?", (gid,))
    antinuke = q1("SELECT * FROM antinuke_config WHERE guild_id=?", (gid,))
    antiraid = q1("SELECT * FROM antiraid_config WHERE guild_id=?", (gid,))

    return render_template(
        "settings.html",
        gid=gid,
        name=guild_name(guild_id),
        config=config,
        antinuke=antinuke,
        antiraid=antiraid,
    )


# ── Command setup ──────────────────────────────────────────────────────────
@app.route("/guild/<guild_id>/commands", methods=["GET", "POST"])
@login_required
@guild_scoped
def guild_commands(guild_id):
    gid = int(guild_id)

    if request.method == "POST":
        for cmd in COMMAND_GROUPS:
            cname = cmd["name"]
            enabled = 1 if request.form.get(f"enabled_{cname}") else 0
            role_ids = [
                int(r) for r in request.form.getlist(f"roles_{cname}") if r.strip()
            ]
            execute(
                """INSERT INTO command_settings
                       (guild_id, command_name, enabled, allowed_role_ids)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, command_name) DO UPDATE SET
                       enabled=excluded.enabled,
                       allowed_role_ids=excluded.allowed_role_ids""",
                (gid, cname, enabled, json.dumps(role_ids)),
            )
        flash("Command settings saved.")
        return redirect(url_for("guild_commands", guild_id=guild_id))

    rows = q(
        "SELECT command_name, enabled, allowed_role_ids FROM command_settings WHERE guild_id=?",
        (gid,),
    )
    settings_map = {
        r["command_name"]: {
            "enabled": r["enabled"],
            "roles": json.loads(r["allowed_role_ids"] or "[]"),
        }
        for r in rows
    }
    roles = discord_roles(guild_id)

    categories = {cat: [] for cat in COMMAND_CATEGORIES}
    for cmd in COMMAND_GROUPS:
        categories[cmd["category"]].append(cmd)

    return render_template(
        "commands.html",
        gid=gid,
        name=guild_name(guild_id),
        categories=categories,
        settings_map=settings_map,
        roles=roles,
    )


if __name__ == "__main__":
    # Standalone dev mode only. In production this app is served by
    # main.py inside a waitress thread — see run_dashboard_in_thread().
    app.run(host="0.0.0.0", port=int(os.getenv("DASHBOARD_PORT", "5000")), debug=True)
