# -*- coding: utf-8 -*-
"""Tokyo_Updates Auto-Delete Bot | Credits: Tokyo_Updates
No MongoDB needed: everything is stored in a local SQLite file.
"""
__credits__ = 'Tokyo_Updates'
import asyncio
import functools
import gzip
import html
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from contextlib import closing
from datetime import datetime, timezone

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from info import (BACKUP_TO_LOG, BOT_TOKEN, CHECK_INTERVAL, DB_PATH, DEFAULT_DELETE_SECONDS,
                  LOG_CHANNEL, OWNER_IDS, START_PIC, START_PIC_LOCAL, UPDATES_URL)

# ───────────────────────── small caps ─────────────────────────
_ASCII = 'abcdefghijklmnopqrstuvwxyz'
_SC_LOWER = 'ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ'          # (x has no real small-cap glyph, it stays x)
assert len(_SC_LOWER) == 26
_SC_TABLE = {ord(a): b for a, b in zip(_ASCII, _SC_LOWER)}
_SC_TABLE.update({ord(a.upper()): b for a, b in zip(_ASCII, _SC_LOWER)})
_SC_BACK = {ord(b): a for a, b in zip(_ASCII, _SC_LOWER) if a != 'x'}   # small caps -> plain (so pasted "2ʜ" still works)
# Never converted: HTML tags, <code>/<pre> blocks, user links, entities, URLs, @usernames, /commands and Tokyo_d(...) data
_SC_SKIP = re.compile(r'(<code>.*?</code>|<pre>.*?</pre>|<a\s[^>]*>.*?</a>|<[^>]+>|&#?\w+;|\x02.*?\x03|https?://[^\s<>"]+|@\w+|(?<![\w/])/[A-Za-z_]+)', re.S)


def Tokyo_sc(text):
    """Small caps for everything the bot shows. Convert the final string ONCE (not twice)."""
    if not text:
        return text
    out, pos = [], 0
    for match in _SC_SKIP.finditer(text):
        out.append(text[pos:match.start()].translate(_SC_TABLE))
        token = match.group(0)
        out.append(token[1:-1] if token.startswith('\x02') else token)
        pos = match.end()
    out.append(text[pos:].translate(_SC_TABLE))
    return ''.join(out).replace('\x02', '').replace('\x03', '')


def Tokyo_d(value):
    """User data (chat / channel names): HTML-escaped and kept exactly as written (not small-capped)."""
    return '\x02' + html.escape(str(value)) + '\x03'


BRAND = Tokyo_sc('Tokyo_Updates')
CREDIT = Tokyo_sc('Credits: Tokyo_Updates')
HTML = ParseMode.HTML
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
AUTO_TYPES = GROUP_TYPES + (ChatType.CHANNEL,)   # chats where auto-delete works
ANON_ADMIN_ID, CHANNEL_BOT_ID = 1087968824, 136817688
TEMP_SECONDS = 60          # bot's own temporary replies in groups are deleted after this
WARN_COOLDOWN = 20         # seconds between join-warnings for the same user in a chat

# Tokyo_Updates: hide the bot token from logs (httpx prints full Telegram URLs that contain it)
Tokyo_TOKEN_RE = re.compile(r'\d{6,}:[A-Za-z0-9_-]{30,}')


class Tokyo_SafeFormatter(logging.Formatter):
    def format(self, record):
        return Tokyo_TOKEN_RE.sub('<BOT_TOKEN_HIDDEN>', super().format(record))


Tokyo_STATE = {'ram': None, 'conflict': False, 'dirty': False, 'fresh_db': False, 'backup_msg': None, 'data': 'disk', 'pin_warned': 0, 'lock': None}
BACKUP_NAME = 'tokyo_backup.sqlite3.gz'
BACKUP_MIN_GAP = 30        # seconds between two backups (changes are batched)
Tokyo_DATA_LABEL = {'restored': 'Settings restored from backup ✅', 'disk': 'Settings loaded from disk ✅',
                    'fresh': 'Fresh start (no backup found) 🆕'}


class Tokyo_ConflictWatch(logging.Filter):
    """Notices 'Conflict: terminated by other getUpdates' (same token running twice) so the bot can wait + restart."""
    def filter(self, record):
        error = record.exc_info[1] if record.exc_info else None
        if (error is not None and type(error).__name__ == 'Conflict') or 'terminated by other getUpdates' in record.getMessage():
            Tokyo_STATE['conflict'] = True
        return True


Tokyo_handler = logging.StreamHandler()
Tokyo_handler.addFilter(Tokyo_ConflictWatch())
Tokyo_handler.setFormatter(Tokyo_SafeFormatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[Tokyo_handler])
for Tokyo_noisy in ('httpx', 'httpcore'):
    logging.getLogger(Tokyo_noisy).setLevel(logging.WARNING)   # no more per-request getUpdates spam
log = logging.getLogger('Tokyo_Updates.AutoDelete')


# ───────────────────────── database ─────────────────────────
def Tokyo_connect_db():
    return sqlite3.connect(DB_PATH, timeout=30)


def Tokyo_init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with closing(Tokyo_connect_db()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY, seconds INTEGER NOT NULL, fsub INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pending(chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, delete_at INTEGER NOT NULL, PRIMARY KEY(chat_id, message_id));
        CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS fsub_channels(channel_id INTEGER PRIMARY KEY, title TEXT, username TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, username TEXT, first_seen INTEGER, last_seen INTEGER);
        CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, type TEXT, added_by INTEGER, added_by_name TEXT, added_at INTEGER, active INTEGER NOT NULL DEFAULT 1);
        CREATE INDEX IF NOT EXISTS pending_due ON pending(delete_at);
        ''')
        try:
            db.execute('ALTER TABLE users ADD COLUMN started INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # column already exists
        db.commit()


def Tokyo_touch_chat(context, chat):
    """Register a group/channel the first time we see it (once per run), so /stats is complete."""
    seen = context.bot_data.setdefault('seen_chats', set())
    if chat.id in seen:
        return
    seen.add(chat.id)
    with closing(Tokyo_connect_db()) as db:
        row = db.execute('SELECT active FROM chats WHERE chat_id=?', (chat.id,)).fetchone()
        db.execute('''INSERT INTO chats(chat_id,title,username,type,added_at,active) VALUES(?,?,?,?,?,1)
                      ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, type=excluded.type, active=1''',
                   (chat.id, chat.title, chat.username, chat.type, int(time.time())))
        db.commit()
    if not row or not row[0]:
        Tokyo_mark_dirty()


def Tokyo_mark_dirty():
    """Something changed -> the backup worker uploads a fresh backup to the log channel."""
    Tokyo_STATE['dirty'] = True


def Tokyo_get_config(key, default=None):
    with closing(Tokyo_connect_db()) as db:
        row = db.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def Tokyo_set_config(key, value):
    with closing(Tokyo_connect_db()) as db:
        if value is None:
            db.execute('DELETE FROM config WHERE key=?', (key,))
        else:
            db.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
        db.commit()
    Tokyo_mark_dirty()


def Tokyo_get_seconds(chat_id, is_channel=False):
    """Delete delay in seconds. 0 = OFF. Groups AND channels stay OFF until an admin runs /set_timer."""
    with closing(Tokyo_connect_db()) as db:
        row = db.execute('SELECT seconds FROM settings WHERE chat_id=?', (chat_id,)).fetchone()
    if row:
        return row[0]
    return 0


def Tokyo_set_seconds(chat_id, seconds):
    with closing(Tokyo_connect_db()) as db:
        db.execute('INSERT INTO settings(chat_id,seconds) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET seconds=excluded.seconds', (chat_id, seconds))
        db.commit()
    Tokyo_mark_dirty()


def Tokyo_fsub_enabled():
    return Tokyo_get_config('fsub_enabled', '0') == '1'


def Tokyo_get_fsub_channels():
    with closing(Tokyo_connect_db()) as db:
        rows = db.execute('SELECT channel_id,title,username,link FROM fsub_channels ORDER BY rowid').fetchall()
    return [{'id': r[0], 'title': r[1], 'username': r[2], 'link': r[3]} for r in rows]


def Tokyo_get_log_channel():
    value = Tokyo_get_config('log_channel')
    if value and value.lstrip('-').isdigit():
        return int(value)
    return LOG_CHANNEL or 0


def Tokyo_schedule_delete(chat_id, message_id, seconds):
    with closing(Tokyo_connect_db()) as db:
        db.execute('INSERT OR REPLACE INTO pending(chat_id,message_id,delete_at) VALUES(?,?,?)', (chat_id, message_id, int(time.time()) + int(seconds)))
        db.commit()


def Tokyo_upsert_user(user):
    """Returns True if this user was never seen before."""
    now = int(time.time())
    with closing(Tokyo_connect_db()) as db:
        exists = db.execute('SELECT 1 FROM users WHERE user_id=?', (user.id,)).fetchone()
        db.execute('''INSERT INTO users(user_id,name,username,first_seen,last_seen) VALUES(?,?,?,?,?)
                      ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, last_seen=excluded.last_seen''',
                   (user.id, user.full_name, user.username, now, now))
        db.commit()
    if not exists:
        Tokyo_mark_dirty()
    return not exists


def Tokyo_mark_started(user):
    """True only the FIRST time this user ever presses /start (so the log channel gets one entry per user)."""
    Tokyo_upsert_user(user)
    with closing(Tokyo_connect_db()) as db:
        changed = db.execute('UPDATE users SET started=1 WHERE user_id=? AND started=0', (user.id,)).rowcount
        db.commit()
    return bool(changed)


# ───────────────────────── helpers ─────────────────────────
def Tokyo_parse_time(value):
    value = value.translate(_SC_BACK)   # pasted small caps (2ʜ, ᴏꜰꜰ) still work
    if value.lower().strip() == 'off':
        return 0
    match = re.fullmatch(r'(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)?', value.lower().strip())
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2) or 's'
    factor = 1 if unit.startswith('s') else 60 if unit.startswith('m') else 3600 if unit.startswith('h') else 86400 if unit.startswith('d') else 604800
    seconds = number * factor
    return seconds if 10 <= seconds <= 365 * 86400 else None


def Tokyo_pretty_time(seconds):
    if not seconds:
        return 'OFF'
    parts = []
    for suffix, size in [('d', 86400), ('h', 3600), ('min', 60), ('s', 1)]:
        number, seconds = divmod(seconds, size)
        if number:
            parts.append(f'{number}{suffix}')
    return ' '.join(parts) or '0s'


def Tokyo_mention(user):
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'


def Tokyo_user_info(user):
    uname = f'@{user.username}' if user.username else 'N/A'
    return (f'👤 <b>Name:</b> {Tokyo_mention(user)}\n'
            f'🔖 <b>Username:</b> {uname}\n'
            f'🆔 <b>ID:</b> <code>{user.id}</code>')


def Tokyo_chat_info(chat):
    uname = f'@{chat.username}' if chat.username else 'Private (no username)'
    kind = {'group': 'Group', 'supergroup': 'Supergroup', 'channel': 'Channel'}.get(chat.type, chat.type)
    return (f'💬 <b>Name:</b> {Tokyo_d(chat.title or "N/A")}\n'
            f'🔗 <b>Username:</b> {uname}\n'
            f'🆔 <b>ID:</b> <code>{chat.id}</code>\n'
            f'📂 <b>Type:</b> {kind}')


async def Tokyo_send_log(context, text):
    chat_id = Tokyo_get_log_channel()
    if not chat_id:
        return
    try:
        await context.bot.send_message(chat_id, Tokyo_sc(f'{text}\n\n<i>{CREDIT}</i>'), parse_mode=HTML, disable_web_page_preview=True)
    except TelegramError as error:
        log.warning('Log channel send failed (%s): %s', chat_id, error)


async def Tokyo_reply(update, text, markup=None, temp=True):
    """Reply in HTML with Tokyo_Updates credit. Bot replies in groups/channels are auto-deleted to keep them clean."""
    message = update.effective_message
    sent = await message.reply_text(Tokyo_sc(f'{text}\n\n<i>{CREDIT}</i>'), parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    if temp and update.effective_chat.type in AUTO_TYPES:
        Tokyo_schedule_delete(sent.chat_id, sent.message_id, TEMP_SECONDS)
    return sent


# ───────────────────────── force-sub core ─────────────────────────
async def Tokyo_missing_channels(context, user_id, force=False):
    """Channels (global list) the user has NOT joined. Owners are always exempt."""
    if user_id in OWNER_IDS or not Tokyo_fsub_enabled():
        return []
    channels = Tokyo_get_fsub_channels()
    if not channels:
        return []
    cache = context.bot_data.setdefault('fsub_ok', {})
    if not force and cache.get(user_id, 0) > time.time():
        return []
    missing = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(channel['id'], user_id)
            joined = member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) \
                or (member.status == ChatMemberStatus.RESTRICTED and member.is_member)
            if not joined:
                missing.append(channel)
        except TelegramError as error:
            # Bot can't check this channel (removed / not admin). Don't lock everyone out; tell the owner instead.
            log.warning('Force-sub check failed for %s: %s', channel['id'], error)
            last = context.bot_data.setdefault('fsub_alert', {})
            if last.get(channel['id'], 0) < time.time() - 3600:
                last[channel['id']] = time.time()
                await Tokyo_send_log(context, f'⚠️ <b>Force-Sub problem</b>\nChannel <code>{channel["id"]}</code> ({Tokyo_d(channel["title"] or "")}) check nahi ho pa raha.\nBot ko us channel me admin banao.\n<code>{html.escape(str(error))}</code>')
    if not missing:
        if len(cache) > 5000:
            cache.clear()
        cache[user_id] = time.time() + 120
    return missing


def Tokyo_join_markup(missing):
    rows = []
    for channel in missing:
        if channel['link']:
            rows.append([InlineKeyboardButton('📢 ' + Tokyo_sc('Join ') + (channel['title'] or Tokyo_sc('Channel')), url=channel['link'])])
    rows.append([InlineKeyboardButton(Tokyo_sc('✅ I Have Joined'), callback_data='fsub_verify')])
    return InlineKeyboardMarkup(rows)


async def Tokyo_send_start_media(message, caption, markup):
    """Send the start picture with caption; fall back to text if the picture can't be sent."""
    caption = Tokyo_sc(caption)
    photo = Tokyo_get_config('start_pic') or START_PIC
    try:
        if photo:
            return await message.reply_photo(photo, caption=caption, parse_mode=HTML, reply_markup=markup)
        if os.path.exists(START_PIC_LOCAL):
            with open(START_PIC_LOCAL, 'rb') as handle:
                return await message.reply_photo(handle, caption=caption, parse_mode=HTML, reply_markup=markup)
    except TelegramError as error:
        log.warning('Start picture failed: %s', error)
    return await message.reply_text(caption, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)


async def Tokyo_join_prompt(update, context, missing):
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    markup = Tokyo_join_markup(missing)
    if chat.type == ChatType.PRIVATE:
        text = (f'🔒 <b>Access Locked</b>\n\nHey {Tokyo_mention(user)}, bot use karne se pehle neeche diye gaye '
                f'channel(s) join karo, phir <b>✅ I Have Joined</b> dabao.\n\n<i>{CREDIT}</i>')
        await Tokyo_send_start_media(message, text, markup)
        return
    # group: delete offending message + throttled temp warning
    try:
        await message.delete()
    except TelegramError:
        pass
    warned = context.bot_data.setdefault('warned', {})
    key = (chat.id, user.id)
    if warned.get(key, 0) > time.time() - WARN_COOLDOWN:
        return
    warned[key] = time.time()
    if len(warned) > 5000:
        warned.clear()
    text = (f'⚠️ {Tokyo_mention(user)}, group me message/command use karne ke liye pehle neeche ke channel(s) join karo, '
            f'phir <b>✅ I Have Joined</b> dabao.\n\n<i>{CREDIT}</i>')
    sent = await context.bot.send_message(chat.id, Tokyo_sc(text), parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    Tokyo_schedule_delete(sent.chat_id, sent.message_id, TEMP_SECONDS)


def Tokyo_fsub_gate(func):
    """Command runs only after the user has joined every force-sub channel."""
    @functools.wraps(func)
    async def Tokyo_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user, chat = update.effective_user, update.effective_chat
        if not update.effective_message or not chat:
            return
        if chat.type == ChatType.CHANNEL:   # channel posts can only come from the channel's admins
            await func(update, context)
            return
        if not user:
            return
        missing = await Tokyo_missing_channels(context, user.id)
        if missing:
            await Tokyo_join_prompt(update, context, missing)
            return
        await func(update, context)
    return Tokyo_wrapper


def Tokyo_owner_only(func):
    @functools.wraps(func)
    async def Tokyo_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not update.effective_message:
            return
        if not user or user.id not in OWNER_IDS:
            hint = '' if OWNER_IDS else '\n(OWNER_IDS environment variable abhi set nahi hai.)'
            await Tokyo_reply(update, '❌ Ye command sirf <b>bot owner</b> ke liye hai.' + hint)
            return
        await func(update, context)
    return Tokyo_wrapper


async def Tokyo_is_group_admin(update, context):
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if chat.type == ChatType.CHANNEL:   # only channel admins can post there
        return True
    if user and user.id in OWNER_IDS:
        return True
    if chat.type not in GROUP_TYPES:
        await Tokyo_reply(update, '❌ Ye command sirf group/channel me chalti hai.')
        return False
    if message.sender_chat and message.sender_chat.id == chat.id:   # anonymous group admin
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
    except TelegramError as error:
        log.warning('Group-admin check failed: %s', error)
    await Tokyo_reply(update, '❌ Sirf group admins ye command use kar sakte hain.')
    return False


# ───────────────────────── start / help ─────────────────────────
def Tokyo_start_text(user):
    return (f'🌸 <b>{BRAND} | ᴀᴜᴛᴏ ᴅᴇʟᴇᴛᴇ ʙᴏᴛ</b>\n\n'
            f'Hey {Tokyo_mention(user)}! 👋\n'
            'Main group ke messages ko ek set time ke baad automatically delete kar deta hoon.\n\n'
            '✨ <b>Features</b>\n'
            '• ⏳ Custom auto-delete timer (/set_timer)\n'
            '• 🧹 Group hamesha clean & spam-free\n'
            '• 🔒 Force-subscribe protection\n\n'
            '➕ Mujhe group me add karo, admin banao (<b>Delete Messages</b> permission) aur <code>/set_timer 2h</code> chalao.\n\n'
            f'ᴄʀᴇᴅɪᴛs: {BRAND}')


def Tokyo_start_markup(bot_username):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(Tokyo_sc('➕ Add Me To Your Group'), url=f'https://t.me/{bot_username}?startgroup=true&admin=delete_messages')],
        [InlineKeyboardButton(Tokyo_sc('📖 Help'), callback_data='help'), InlineKeyboardButton(Tokyo_sc('📢 Updates'), url=UPDATES_URL)],
    ])


def Tokyo_help_text(user_id):
    text = ('📖 <b>Help & Commands</b>\n\n'
            '<b>Group / Channel admins:</b>\n'
            '• <code>/set_timer 2h</code> — delete timer set (<code>30s</code>, <code>3min</code>, <code>2h</code>, <code>1d</code>, <code>7200</code>, <code>off</code>)\n'
            '• /show_time — current timer\n'
            '• /get_id — chat / user ID\n\n'
            '⏳ Min 10 sec • Max 365 days • <code>off</code> = band. Naya timer sirf naye messages par.\n'
            '📢 Channel me bot ko admin (Delete Messages) banao aur channel me <code>/set_timer 2h</code> post karo (jab tak timer set na ho, auto-delete OFF rehta hai).\n\n'
            '⚙️ <b>Setup:</b> bot ko group admin banao (Delete Messages) aur BotFather me /setprivacy → Disable karo.\n\n' + CREDIT)
    if user_id in OWNER_IDS:
        text += ('\n\n👑 <b>Owner:</b>\n'
                 '• <code>/add_fsub -1001234567890</code>\n• <code>/remove_fsub -1001234567890</code>\n• /list_fsub\n• <code>/set_fsub on</code> | <code>/set_fsub off</code>\n'
                 '• <code>/set_log -1001234567890</code> | <code>/set_log off</code>\n• /set_pic (photo par reply)\n• /stats\n\n' + CREDIT)
    return text


async def Tokyo_edit(query, text, markup):
    text = Tokyo_sc(text)
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, parse_mode=HTML, reply_markup=markup)
        else:
            await query.edit_message_text(text, parse_mode=HTML, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as error:
        if 'not modified' not in str(error).lower():
            log.warning('Edit failed: %s', error)


async def Tokyo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, chat, message = update.effective_user, update.effective_chat, update.effective_message
    if not user or not message:
        return
    if Tokyo_mark_started(user):   # log only once per user, never again on repeated /start
        if chat.type == ChatType.PRIVATE:
            await Tokyo_send_log(context, f'🚀 <b>New User Started The Bot</b>\n\n{Tokyo_user_info(user)}')
        else:
            await Tokyo_send_log(context, f'🚀 <b>New User Started The Bot (In Group)</b>\n\n{Tokyo_user_info(user)}\n\n{Tokyo_chat_info(chat)}')
    missing = await Tokyo_missing_channels(context, user.id)
    if missing:
        await Tokyo_join_prompt(update, context, missing)
        return
    sent = await Tokyo_send_start_media(message, Tokyo_start_text(user), Tokyo_start_markup(context.bot.username))
    if chat.type in GROUP_TYPES:
        Tokyo_schedule_delete(sent.chat_id, sent.message_id, TEMP_SECONDS)


@Tokyo_fsub_gate
async def Tokyo_help(update, context):
    await Tokyo_reply(update, Tokyo_help_text(update.effective_user.id))


async def Tokyo_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    missing = await Tokyo_missing_channels(context, user.id)
    if missing:
        await query.answer(Tokyo_sc('❌ Pehle sab channel join karo!'), show_alert=True)
        return
    await query.answer()
    if query.data == 'help':
        back = InlineKeyboardMarkup([[InlineKeyboardButton(Tokyo_sc('⬅️ Back'), callback_data='home')]])
        await Tokyo_edit(query, Tokyo_help_text(user.id), back)
    else:
        await Tokyo_edit(query, Tokyo_start_text(user), Tokyo_start_markup(context.bot.username))


async def Tokyo_verify_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    missing = await Tokyo_missing_channels(context, user.id, force=True)
    if missing:
        await query.answer(Tokyo_sc('❌ Abhi tak sab channel join nahi kiye!'), show_alert=True)
        return
    await query.answer(Tokyo_sc('✅ Verified! Ab bot use kar sakte ho.'), show_alert=False)
    if query.message.chat.type == ChatType.PRIVATE:
        await Tokyo_edit(query, Tokyo_start_text(user), Tokyo_start_markup(context.bot.username))
    else:
        try:
            await query.message.delete()
        except TelegramError:
            pass


# ───────────────────────── group commands ─────────────────────────
@Tokyo_fsub_gate
async def Tokyo_set_timer(update, context):
    if not await Tokyo_is_group_admin(update, context):
        return
    chat = update.effective_chat
    if not context.args:
        await Tokyo_reply(update, '❌ Example: <code>/set_timer 2h</code> | <code>/set_timer 3min</code> | <code>/set_timer 7200</code> | <code>/set_timer off</code>')
        return
    seconds = Tokyo_parse_time(context.args[0])
    if seconds is None:
        await Tokyo_reply(update, '❌ Invalid time. Example: <code>/set_timer 30s</code>, <code>/set_timer 3min</code>, <code>/set_timer 2h</code> (10s–365d) ya <code>off</code>.')
        return
    Tokyo_set_seconds(chat.id, seconds)
    if seconds:
        text = f'✅ <b>Set Time Successfully!</b>\n⏳ Auto-delete: <b>{Tokyo_pretty_time(seconds)}</b>\nNaye messages is timer ke hisab se delete honge.'
    else:
        text = '✅ <b>Auto-delete OFF Successfully!</b>'
    await Tokyo_reply(update, text)


@Tokyo_fsub_gate
async def Tokyo_show_time(update, context):
    chat = update.effective_chat
    seconds = Tokyo_get_seconds(chat.id, chat.type == ChatType.CHANNEL)
    if seconds:
        await Tokyo_reply(update, f'⏱ Current timer: <b>{Tokyo_pretty_time(seconds)}</b> ✅')
    else:
        await Tokyo_reply(update, '⏱ Current timer: <b>OFF</b> ❌')


@Tokyo_fsub_gate
async def Tokyo_get_id(update, context):
    chat, user = update.effective_chat, update.effective_user
    lines = []
    if chat.type != ChatType.PRIVATE:
        lines.append(f'🆔 Chat ID: <code>{chat.id}</code>')
    if user:
        lines.append(f'🆔 Your ID: <code>{user.id}</code>')
    await Tokyo_reply(update, '\n'.join(lines))


# ───────────────────────── owner commands ─────────────────────────
def Tokyo_clear_fsub_cache(context):
    context.bot_data['fsub_ok'] = {}
    Tokyo_mark_dirty()   # called whenever the force-sub list / switch changes -> back it up


@Tokyo_owner_only
async def Tokyo_add_fsub(update, context):
    if not context.args or not re.fullmatch(r'-?\d+', context.args[0]):
        await Tokyo_reply(update, 'Example: <code>/add_fsub -1001234567890</code>\n(Channel ID chahiye, username nahi.)')
        return
    channel_id = int(context.args[0])
    try:
        chat = await context.bot.get_chat(channel_id)
        me = await context.bot.get_chat_member(channel_id, context.bot.id)
    except TelegramError as error:
        await Tokyo_reply(update, f'❌ Channel nahi mila / bot us me nahi hai.\n<code>{html.escape(str(error))}</code>')
        return
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        await Tokyo_reply(update, '❌ Pehle bot ko us channel me <b>admin</b> banao, phir dobara add karo (warna membership check nahi hoga).')
        return
    link = f'https://t.me/{chat.username}' if chat.username else (chat.invite_link or None)
    if not link:
        try:
            link = await context.bot.export_chat_invite_link(channel_id)
        except TelegramError as error:
            log.warning('Invite link failed for %s: %s', channel_id, error)
    with closing(Tokyo_connect_db()) as db:
        db.execute('''INSERT INTO fsub_channels(channel_id,title,username,link) VALUES(?,?,?,?)
                      ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, username=excluded.username, link=excluded.link''',
                   (channel_id, chat.title, chat.username, link))
        db.commit()
    Tokyo_clear_fsub_cache(context)
    note = '' if link else '\n⚠️ Invite link nahi ban paya. Bot ko "Invite Users" permission do, warna join button nahi dikhega.'
    await Tokyo_reply(update, f'✅ <b>Force-Sub Channel Added Successfully!</b> (sab groups/private me lagu).\n\n{Tokyo_chat_info(chat)}\n🔗 <b>Link:</b> {html.escape(link or "N/A")}{note}\n\nOn karne ke liye: <code>/set_fsub on</code>')
    await Tokyo_send_log(context, f'📢 <b>Force-Sub Channel Added</b>\n\n{Tokyo_chat_info(chat)}\n\nBy: {Tokyo_user_info(update.effective_user)}')


@Tokyo_owner_only
async def Tokyo_remove_fsub(update, context):
    if not context.args or not re.fullmatch(r'-?\d+', context.args[0]):
        await Tokyo_reply(update, 'Example: <code>/remove_fsub -1001234567890</code>')
        return
    with closing(Tokyo_connect_db()) as db:
        removed = db.execute('DELETE FROM fsub_channels WHERE channel_id=?', (int(context.args[0]),)).rowcount
        db.commit()
    Tokyo_clear_fsub_cache(context)
    if removed and not Tokyo_get_fsub_channels():
        Tokyo_set_config('fsub_enabled', '0')
    await Tokyo_reply(update, '✅ <b>Force-Sub Channel Removed Successfully!</b>' if removed else '❌ Ye channel ID list me nahi hai.')


@Tokyo_owner_only
async def Tokyo_list_fsub(update, context):
    channels = Tokyo_get_fsub_channels()
    lines = [f'{i}. {Tokyo_d(c["title"] or "Channel")} — <code>{c["id"]}</code>' for i, c in enumerate(channels, 1)]
    state = 'ON ✅' if Tokyo_fsub_enabled() else 'OFF ❌'
    await Tokyo_reply(update, f'📢 <b>Force-Sub (global): {state}</b>\n\n' + ('\n'.join(lines) if lines else 'Koi channel add nahi hai.') + '\n\nExample: <code>/add_fsub -1001234567890</code>')


@Tokyo_owner_only
async def Tokyo_set_fsub(update, context):
    if not context.args or context.args[0].translate(_SC_BACK).lower() not in ('on', 'off'):
        await Tokyo_reply(update, 'Example: <code>/set_fsub on</code> | <code>/set_fsub off</code>')
        return
    enabled = context.args[0].translate(_SC_BACK).lower() == 'on'
    if enabled and not Tokyo_get_fsub_channels():
        await Tokyo_reply(update, '❌ Pehle <code>/add_fsub -1001234567890</code> se channel add karo.')
        return
    Tokyo_set_config('fsub_enabled', '1' if enabled else '0')
    Tokyo_clear_fsub_cache(context)
    await Tokyo_reply(update, f'✅ <b>Force-Sub {"Enabled" if enabled else "Disabled"} Successfully!</b> (sab groups/private me lagu)')
    await Tokyo_send_log(context, f'⚙️ <b>Force-Sub {"Enabled" if enabled else "Disabled"}</b>\nBy: {Tokyo_user_info(update.effective_user)}')


@Tokyo_owner_only
async def Tokyo_set_log(update, context):
    if not context.args:
        current = Tokyo_get_log_channel()
        shown = f'<code>{current}</code>' if current else 'not set'
        await Tokyo_reply(update, f'📝 Log channel: {shown}\nExample: <code>/set_log -1001234567890</code> | <code>/set_log off</code>')
        return
    arg = context.args[0].translate(_SC_BACK).lower()
    if arg == 'off':
        Tokyo_set_config('log_channel', None)
        await Tokyo_reply(update, '✅ <b>Log Channel Disabled Successfully!</b>')
        return
    if not re.fullmatch(r'-?\d+', arg):
        await Tokyo_reply(update, 'Example: <code>/set_log -1001234567890</code>')
        return
    try:
        chat = await context.bot.get_chat(int(arg))
        await context.bot.send_message(chat.id, Tokyo_sc(f'✅ <b>{BRAND}</b> log channel connected.'), parse_mode=HTML)
    except TelegramError as error:
        await Tokyo_reply(update, f'❌ Log channel me message nahi bhej paya. Bot ko channel me admin banao.\n<code>{html.escape(str(error))}</code>')
        return
    Tokyo_set_config('log_channel', chat.id)
    await Tokyo_reply(update, f'✅ <b>Log Channel Set Successfully!</b>\n🆔 <code>{chat.id}</code>')


@Tokyo_owner_only
async def Tokyo_set_pic(update, context):
    message = update.effective_message
    if context.args and context.args[0].translate(_SC_BACK).lower() == 'reset':
        Tokyo_set_config('start_pic', None)
        await Tokyo_reply(update, '✅ <b>Start Picture Reset Successfully!</b>')
        return
    target = message.reply_to_message
    if not target or not target.photo:
        await Tokyo_reply(update, 'Kisi <b>photo par reply</b> karke <code>/set_pic</code> bhejo. Reset: <code>/set_pic reset</code>')
        return
    Tokyo_set_config('start_pic', target.photo[-1].file_id)
    await Tokyo_reply(update, '✅ <b>Start Picture Updated Successfully!</b> /start se check karo.')


@Tokyo_owner_only
async def Tokyo_stats(update, context):
    with closing(Tokyo_connect_db()) as db:
        users = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        groups = db.execute("SELECT COUNT(*) FROM chats WHERE active=1 AND type IN ('group','supergroup')").fetchone()[0]
        channels = db.execute("SELECT COUNT(*) FROM chats WHERE active=1 AND type='channel'").fetchone()[0]
        pending = db.execute('SELECT COUNT(*) FROM pending').fetchone()[0]
    await Tokyo_reply(update, f'📊 <b>Stats</b>\n👤 Users: <b>{users}</b>\n💬 Active groups: <b>{groups}</b>\n📢 Active channels: <b>{channels}</b>\n🗑 Pending deletions: <b>{pending}</b>\n📢 Force-Sub: {"ON" if Tokyo_fsub_enabled() else "OFF"} ({len(Tokyo_get_fsub_channels())} channel)')


# ───────────────────────── bot added / removed (group + channel) ─────────────────────────
async def Tokyo_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.my_chat_member
    chat, adder = event.chat, event.from_user
    if chat.type == ChatType.PRIVATE:
        return
    old, new = event.old_chat_member.status, event.new_chat_member.status
    inside = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if old not in inside and new in inside:
        action, title = 'added', '➕ <b>Bot Added To {kind}</b>'
    elif old in inside and new not in inside:
        action, title = 'removed', '➖ <b>Bot Removed From {kind}</b>'
    elif old == ChatMemberStatus.MEMBER and new == ChatMemberStatus.ADMINISTRATOR:
        action, title = 'promoted', '⭐ <b>Bot Promoted To Admin In {kind}</b>'
    elif old == ChatMemberStatus.ADMINISTRATOR and new == ChatMemberStatus.MEMBER:
        action, title = 'demoted', '⬇️ <b>Bot Demoted In {kind}</b>'
    else:
        return
    kind = 'Channel' if chat.type == ChatType.CHANNEL else 'Group'
    now = int(time.time())
    with closing(Tokyo_connect_db()) as db:
        if action == 'added':
            db.execute('''INSERT INTO chats(chat_id,title,username,type,added_by,added_by_name,added_at,active) VALUES(?,?,?,?,?,?,?,1)
                          ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, type=excluded.type,
                          added_by=excluded.added_by, added_by_name=excluded.added_by_name, added_at=excluded.added_at, active=1''',
                       (chat.id, chat.title, chat.username, chat.type, adder.id, adder.full_name, now))
        elif action == 'removed':
            db.execute('UPDATE chats SET active=0 WHERE chat_id=?', (chat.id,))
        db.commit()
    Tokyo_mark_dirty()
    Tokyo_upsert_user(adder)
    extra = ''
    if action != 'removed':
        can_delete = getattr(event.new_chat_member, 'can_delete_messages', None)
        extra = f'\n\n🤖 <b>Bot Status:</b> {"Admin" if new == ChatMemberStatus.ADMINISTRATOR else "Member"}'
        if chat.type != ChatType.CHANNEL:
            extra += f'\n🗑 <b>Delete Permission:</b> {"Yes ✅" if can_delete else "No ❌"}'
        try:
            extra += f'\n👥 <b>Members:</b> {await context.bot.get_chat_member_count(chat.id)}'
        except TelegramError:
            pass
    label = 'Removed By' if action == 'removed' else 'Added By' if action == 'added' else 'Changed By'
    await Tokyo_send_log(context, f'{title.format(kind=kind)}\n\n{Tokyo_chat_info(chat)}{extra}\n\n👤 <b>{label}</b>\n{Tokyo_user_info(adder)}')
    if action in ('added', 'promoted') and chat.type in AUTO_TYPES:
        try:
            if new == ChatMemberStatus.ADMINISTRATOR and getattr(event.new_chat_member, 'can_delete_messages', False):
                text = f'✅ Thanks {Tokyo_mention(adder)}! Ab {"channel me" if chat.type == ChatType.CHANNEL else "yahan"} <code>/set_timer 2h</code> se auto-delete timer set karo.'
            else:
                text = f'👋 Thanks {Tokyo_mention(adder)}! Mujhe <b>admin</b> banao (Delete Messages permission) taaki main messages delete kar sakun.'
            text += f'\n\n<i>{CREDIT}</i>'
            sent = await context.bot.send_message(chat.id, Tokyo_sc(text), parse_mode=HTML)
            Tokyo_schedule_delete(sent.chat_id, sent.message_id, 120)
        except TelegramError:
            pass


# ───────────────────────── message tracking / auto delete ─────────────────────────
async def Tokyo_track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not message or not chat or chat.type not in GROUP_TYPES:
        return
    Tokyo_touch_chat(context, chat)
    is_bot = bool(user and user.is_bot and user.id not in (ANON_ADMIN_ID, CHANNEL_BOT_ID))
    if is_bot:
        return
    is_command = bool(message.text and message.text.startswith('/'))
    is_service = bool(filters.StatusUpdate.ALL.check_update(update))
    anonymous = message.sender_chat is not None or (user and user.id in (ANON_ADMIN_ID, CHANNEL_BOT_ID))
    if user and not anonymous and not is_command and not is_service:
        missing = await Tokyo_missing_channels(context, user.id)
        if missing:
            await Tokyo_join_prompt(update, context, missing)
            return
    seconds = Tokyo_get_seconds(chat.id)
    if seconds:
        Tokyo_schedule_delete(chat.id, message.message_id, seconds)


async def Tokyo_track_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel posts are auto-deleted too (bot must be channel admin with Delete Messages). Default OFF until /set_timer."""
    message, chat = update.effective_message, update.effective_chat
    if not message or not chat or chat.type != ChatType.CHANNEL:
        return
    Tokyo_touch_chat(context, chat)
    seconds = Tokyo_get_seconds(chat.id, is_channel=True)
    if seconds:
        Tokyo_schedule_delete(chat.id, message.message_id, seconds)


async def Tokyo_deletion_worker(app):
    while True:
        try:
            with closing(Tokyo_connect_db()) as db:
                due = db.execute('SELECT chat_id,message_id FROM pending WHERE delete_at<=? ORDER BY delete_at LIMIT 100', (int(time.time()),)).fetchall()
            for chat_id, message_id in due:
                try:
                    await app.bot.delete_message(chat_id, message_id)
                except (BadRequest, Forbidden) as error:
                    log.info('Delete skipped %s/%s: %s', chat_id, message_id, error)
                except TelegramError as error:
                    log.warning('Delete failed %s/%s: %s', chat_id, message_id, error)
                    continue
                with closing(Tokyo_connect_db()) as db:
                    db.execute('DELETE FROM pending WHERE chat_id=? AND message_id=?', (chat_id, message_id))
                    db.commit()
        except Exception:
            log.exception('Tokyo deletion worker error')
        await asyncio.sleep(CHECK_INTERVAL)


async def Tokyo_error_handler(update, context):
    error = context.error
    if type(error).__name__ == 'Conflict':
        Tokyo_STATE['conflict'] = True
        return
    log.error('Unhandled error: %s', error, exc_info=error)
    text = Tokyo_TOKEN_RE.sub('<hidden>', f'{type(error).__name__}: {error}')[:300]
    where = ''
    if isinstance(update, Update) and update.effective_chat:
        where = f'\n💬 Chat: <code>{update.effective_chat.id}</code>'
    await Tokyo_send_log(context, f'⚠️ <b>Error</b>{where}\n<code>{html.escape(text)}</code>')
    try:
        message = update.effective_message if isinstance(update, Update) else None
        if message and message.text and message.text.startswith('/') and update.effective_chat.type != ChatType.CHANNEL:
            sent = await message.reply_text(Tokyo_sc(f'⚠️ Kuch gadbad ho gayi, owner ko bata diya gaya.\n\n<i>{CREDIT}</i>'), parse_mode=HTML)
            if update.effective_chat.type in GROUP_TYPES:
                Tokyo_schedule_delete(sent.chat_id, sent.message_id, TEMP_SECONDS)
    except TelegramError:
        pass


Tokyo_FONT = {
    'T': ['█████', '  █  ', '  █  ', '  █  ', '  █  '], 'O': [' ███ ', '█   █', '█   █', '█   █', ' ███ '],
    'K': ['█   █', '█  █ ', '███  ', '█  █ ', '█   █'], 'Y': ['█   █', ' █ █ ', '  █  ', '  █  ', '  █  '],
    'U': ['█   █', '█   █', '█   █', '█   █', ' ███ '], 'P': ['████ ', '█   █', '████ ', '█    ', '█    '],
    'D': ['████ ', '█   █', '█   █', '█   █', '████ '], 'A': [' ███ ', '█   █', '█████', '█   █', '█   █'],
    'E': ['█████', '█    ', '████ ', '█    ', '█████'], 'S': [' ████', '█    ', ' ███ ', '    █', '████ '],
    ' ': ['   ', '   ', '   ', '   ', '   '],
}


def Tokyo_big_text(word):
    return '\n'.join('   ' + '  '.join(Tokyo_FONT[ch][row] for ch in word) for row in range(5))


def Tokyo_build_banner(bot_name, username, bot_id):
    S = Tokyo_sc
    channels = Tokyo_get_fsub_channels()
    log_channel = Tokyo_get_log_channel()
    line = '═' * 62
    return '\n'.join([
        '', f'╔{line}╗', Tokyo_big_text('TOKYO'), '', Tokyo_big_text('UPDATES'), f'╚{line}╝', '',
        '   🔄🔄🔄   ' + S('BOT RESTARTED & RUNNING') + '   🔄🔄🔄', '',
        f'   🤖 {S("Bot")}        : {bot_name} (@{username})  [ID: {bot_id}]',
        f'   👑 {S("Owners")}     : {len(OWNER_IDS)} {S("set")}' + ('' if OWNER_IDS else '  ⚠️ ' + S('OWNER_IDS missing!')),
        f'   📢 {S("Force-Sub")}  : {S("ON" if Tokyo_fsub_enabled() else "OFF")} ({len(channels)} {S("channel")})',
        f'   📝 {S("Log channel")}: {log_channel or S("not set")}',
        f'   💾 {S("Data")}       : {S(Tokyo_DATA_LABEL[Tokyo_STATE["data"]])}',
        f'   🗑 {S("Auto-delete")}: {S("OFF until an admin sets /set_timer")}  |  {S("DB")}: {DB_PATH}',
        '', f'   🌸 {BRAND}  |  {CREDIT}', '   🔄 ' + S('Restart complete — bot is online & polling Telegram.'), '',
    ])


async def Tokyo_health_server():
    """Tiny HTTP responder for hosts that need an open $PORT (e.g. Render Web Service). Skipped when PORT is not set."""
    port = os.getenv('PORT', '')
    if not port.isdigit():
        return None

    async def Tokyo_handle(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), timeout=5)
            body = b'Tokyo_Updates Auto-Delete Bot is running | Credits: Tokyo_Updates'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(Tokyo_handle, '0.0.0.0', int(port))
    log.info('Tokyo_Updates health server listening on port %s', port)
    return server


# ───────────────────────── backup / restore (survives hosts that wipe the disk on restart) ─────────────────────────
def Tokyo_make_snapshot(folder):
    """Consistent gzip copy of the live database (safe while the bot is running)."""
    snap = os.path.join(folder, 'snapshot.sqlite3')
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as src_db, closing(sqlite3.connect(snap)) as dst_db:
        src_db.backup(dst_db)
    gz_path = os.path.join(folder, BACKUP_NAME)
    with open(snap, 'rb') as raw, gzip.open(gz_path, 'wb', compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed)
    os.remove(snap)
    return gz_path


def Tokyo_unpack_backup(gz_path, folder):
    """Unzip + sanity-check a backup, then put it in place of the (empty) database. True on success."""
    restored = os.path.join(folder, 'restored.sqlite3')
    with gzip.open(gz_path, 'rb') as packed, open(restored, 'wb') as raw:
        shutil.copyfileobj(packed, raw)
    with closing(sqlite3.connect(restored)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            return False
        db.execute('SELECT COUNT(*) FROM settings').fetchone()   # raises if this is not our database
    for suffix in ('-wal', '-shm'):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    shutil.move(restored, DB_PATH)
    return True


async def Tokyo_find_backup(bot):
    """(document, message_id) of the newest backup pinned in the log channel, else (None, None)."""
    chat_id = Tokyo_get_log_channel()
    if not chat_id:
        return None, None
    try:
        pinned = (await bot.get_chat(chat_id)).pinned_message
    except TelegramError as error:
        log.warning('Backup lookup failed (%s): %s', chat_id, error)
        return None, None
    document = pinned.document if pinned else None
    if document and document.file_name == BACKUP_NAME:
        return document, pinned.message_id
    return None, None


async def Tokyo_restore_backup(bot):
    """Empty database after a restart? Pull the newest backup from the log channel. True if restored."""
    document, message_id = await Tokyo_find_backup(bot)
    if not document:
        log.info('No database backup found in the log channel (fresh start).')
        return False
    folder = tempfile.mkdtemp(prefix='tokyo_restore_')
    try:
        gz_path = os.path.join(folder, BACKUP_NAME)
        await (await bot.get_file(document.file_id)).download_to_drive(gz_path)
        if not await asyncio.to_thread(Tokyo_unpack_backup, gz_path, folder):
            log.error('Backup in the log channel is damaged - starting fresh.')
            return False
        Tokyo_init_db()   # apply schema upgrades if the backup is from an older version
        Tokyo_STATE['backup_msg'] = message_id
        log.info('✅ Settings restored from the log-channel backup.')
        return True
    except (sqlite3.DatabaseError, OSError, EOFError) as error:   # not gzip / not our database / cut-off file
        log.error('Backup in the log channel is damaged (%s) - starting fresh.', error)
        return False
    except Exception:
        log.exception('Restore from backup failed')
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def Tokyo_backup_locked(bot):
    chat_id = Tokyo_get_log_channel()
    if not (BACKUP_TO_LOG and chat_id):
        return None
    folder = tempfile.mkdtemp(prefix='tokyo_backup_')
    try:
        gz_path = await asyncio.to_thread(Tokyo_make_snapshot, folder)
        if os.path.getsize(gz_path) > 19 * 1024 * 1024:
            log.warning('Backup is above 19 MB - bots can only download 20 MB, so a restore may fail.')
        with open(gz_path, 'rb') as handle:
            sent = await bot.send_document(chat_id, handle, filename=BACKUP_NAME, disable_notification=True,
                                           caption=Tokyo_sc('Database backup - restored automatically after a restart. Do not delete or unpin.'))
        try:
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except TelegramError as error:
            log.warning('Backup pin failed: %s', error)
            try:
                await bot.delete_message(chat_id, sent.message_id)
            except TelegramError:
                pass
            if time.time() - Tokyo_STATE['pin_warned'] > 3600:
                Tokyo_STATE['pin_warned'] = time.time()
                await Tokyo_send_log(types.SimpleNamespace(bot=bot), '⚠️ <b>Backup Problem</b>\nBot ko log channel me <b>Pin Messages</b> (channel me: <b>Edit Messages</b>) permission do, warna restart ke baad settings restore nahi hongi.')
            return False
        previous, Tokyo_STATE['backup_msg'] = Tokyo_STATE['backup_msg'], sent.message_id
        if previous and previous != sent.message_id:
            try:
                await bot.delete_message(chat_id, previous)
            except TelegramError:
                pass
        return True
    except TelegramError as error:
        log.warning('Backup upload failed: %s', error)
        return False
    except Exception:
        log.exception('Backup failed')
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def Tokyo_backup_now(bot):
    """Upload + pin a fresh backup in the log channel. True = done, False = failed, None = nothing to do."""
    async with Tokyo_STATE['lock']:
        return await Tokyo_backup_locked(bot)


async def Tokyo_backup_worker(app):
    last = 0.0
    while True:
        await asyncio.sleep(10)
        try:
            if Tokyo_STATE['dirty'] and time.time() - last >= BACKUP_MIN_GAP:
                Tokyo_STATE['dirty'] = False
                result = await Tokyo_backup_now(app.bot)
                last = time.time()
                if result is False:
                    Tokyo_STATE['dirty'] = True   # try again after the gap
        except Exception:
            log.exception('Tokyo backup worker error')


RAM_DAYS = 7


def Tokyo_db_healthy():
    try:
        with closing(Tokyo_connect_db()) as db:
            db.execute('SELECT 1 FROM settings LIMIT 1').fetchall()
        return True
    except sqlite3.Error:
        return False


def Tokyo_ram_snapshot():
    folder = tempfile.mkdtemp(prefix='tokyo_ram_')
    try:
        with open(Tokyo_make_snapshot(folder), 'rb') as handle:
            return handle.read()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def Tokyo_ram_unpack(data):
    folder = tempfile.mkdtemp(prefix='tokyo_ram_')
    try:
        gz_path = os.path.join(folder, BACKUP_NAME)
        with open(gz_path, 'wb') as handle:
            handle.write(data)
        return Tokyo_unpack_backup(gz_path, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def Tokyo_ram_check(bot):
    """Keep one database copy in RAM, replaced by a new one every 7 days (the old one is dropped).
    If the database file is lost/emptied while the bot is running, bring it back from that copy."""
    ram = Tokyo_STATE['ram']
    if ram and not Tokyo_db_healthy():
        if await asyncio.to_thread(Tokyo_ram_unpack, ram[1]):
            Tokyo_init_db()
            log.warning('Database was missing - restored from the RAM copy.')
            await Tokyo_send_log(types.SimpleNamespace(bot=bot), '♻️ <b>Database Restored From RAM Copy</b>\nDatabase file gone mila, RAM backup se wapas laaya gaya.')
        return
    if ram is None or time.time() - ram[0] >= RAM_DAYS * 86400:
        data = await asyncio.to_thread(Tokyo_ram_snapshot)
        Tokyo_STATE['ram'], old = (time.time(), data), Tokyo_STATE['ram']
        del old   # the previous copy is released from RAM
        log.info('RAM database copy refreshed (%d KB).', len(data) // 1024)


async def Tokyo_ram_worker(app):
    while True:
        try:
            await Tokyo_ram_check(app.bot)
        except Exception:
            log.exception('Tokyo RAM backup error')
        await asyncio.sleep(30)


async def Tokyo_post_stop(app):
    """Graceful shutdown (redeploy / SIGTERM): save the very latest data before the host wipes the disk."""
    if not BACKUP_TO_LOG or Tokyo_STATE['conflict']:
        return
    try:
        await asyncio.wait_for(Tokyo_backup_now(app.bot), timeout=25)
    except Exception:
        log.warning('Final backup on shutdown failed')


async def Tokyo_post_shutdown(app):
    for name in ('worker', 'backup_worker', 'ram_worker'):
        task = app.bot_data.get(name)
        if task:
            task.cancel()
    server = app.bot_data.get('health')
    if server:
        server.close()


def Tokyo_cmd(name, description):
    return BotCommand(name, Tokyo_sc(description))


async def Tokyo_post_init(app):
    Tokyo_STATE['lock'] = asyncio.Lock()
    user_cmds = [Tokyo_cmd('start', 'Start the bot'), Tokyo_cmd('help', 'Help & commands'),
                 Tokyo_cmd('set_timer', 'Set auto-delete time (admins)'), Tokyo_cmd('show_time', 'Show current timer'),
                 Tokyo_cmd('get_id', 'Show chat / user ID')]
    owner_cmds = user_cmds + [Tokyo_cmd('add_fsub', 'Add force-sub channel ID'), Tokyo_cmd('remove_fsub', 'Remove force-sub channel'),
                              Tokyo_cmd('list_fsub', 'List force-sub channels'), Tokyo_cmd('set_fsub', 'Force-sub on/off'),
                              Tokyo_cmd('set_log', 'Set log channel'), Tokyo_cmd('set_pic', 'Set start picture'), Tokyo_cmd('stats', 'Bot stats')]
    try:
        await app.bot.set_my_commands(user_cmds)
        for owner_id in OWNER_IDS:
            try:
                await app.bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(owner_id))
            except TelegramError:
                pass  # owner hasn't started the bot yet
    except TelegramError as error:
        log.warning('set_my_commands failed: %s', error)
    # Host wiped the disk (fresh database)? Bring the settings back from the log-channel backup BEFORE anything runs.
    if BACKUP_TO_LOG and Tokyo_STATE['fresh_db'] and await Tokyo_restore_backup(app.bot):
        Tokyo_STATE['data'] = 'restored'
    else:
        Tokyo_STATE['data'] = 'fresh' if Tokyo_STATE['fresh_db'] else 'disk'
        if BACKUP_TO_LOG:
            _, Tokyo_STATE['backup_msg'] = await Tokyo_find_backup(app.bot)   # so the next backup replaces the old one
    Tokyo_STATE['dirty'] = bool(BACKUP_TO_LOG) and Tokyo_STATE['data'] != 'restored'
    loop = asyncio.get_running_loop()
    app.bot_data['worker'] = loop.create_task(Tokyo_deletion_worker(app))
    app.bot_data['ram_worker'] = loop.create_task(Tokyo_ram_worker(app))
    if BACKUP_TO_LOG:
        app.bot_data['backup_worker'] = loop.create_task(Tokyo_backup_worker(app))
    app.bot_data['health'] = await Tokyo_health_server()
    try:
        print(Tokyo_build_banner(app.bot.first_name, app.bot.username, app.bot.id), flush=True)
        log.info('🔄 Bot restarted | data: %s', Tokyo_STATE['data'])
        when = datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')
        await Tokyo_send_log(types.SimpleNamespace(bot=app.bot),
                             f'🔄 <b>Bot Restarted</b>\n🤖 @{app.bot.username}\n🌸 {BRAND}\n💾 {Tokyo_DATA_LABEL[Tokyo_STATE["data"]]}\n🕒 {when}')
    except Exception:
        log.exception('Startup banner failed')
    if not OWNER_IDS:
        log.warning('OWNER_IDS is empty: nobody can use owner commands (fsub / log / pic). Set OWNER_IDS env.')


def Tokyo_main():
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN missing. Set it in environment variables; do not upload your token to GitHub.')
    Tokyo_STATE['fresh_db'] = not os.path.exists(DB_PATH)   # missing file = host wiped the disk -> restore from the log channel
    Tokyo_init_db()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(Tokyo_post_init).post_stop(Tokyo_post_stop).post_shutdown(Tokyo_post_shutdown).build())
    handlers = [
        ('start', Tokyo_start), ('help', Tokyo_help),
        ('set_timer', Tokyo_set_timer), ('set_time', Tokyo_set_timer), ('show_time', Tokyo_show_time), ('get_id', Tokyo_get_id),
        ('add_fsub', Tokyo_add_fsub), ('remove_fsub', Tokyo_remove_fsub), ('list_fsub', Tokyo_list_fsub),
        ('set_fsub', Tokyo_set_fsub), ('set_log', Tokyo_set_log), ('set_pic', Tokyo_set_pic), ('stats', Tokyo_stats),
    ]
    for command, callback in handlers:
        app.add_handler(CommandHandler(command, callback, filters=filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST))
    app.add_handler(CallbackQueryHandler(Tokyo_verify_cb, pattern='^fsub_verify$'))
    app.add_handler(CallbackQueryHandler(Tokyo_menu_cb, pattern='^(help|home)$'))
    app.add_handler(ChatMemberHandler(Tokyo_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & ~filters.ChatType.PRIVATE & ~filters.ChatType.CHANNEL, Tokyo_track_message), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, Tokyo_track_channel_post), group=1)
    app.add_error_handler(Tokyo_error_handler)
    log.info('%s Auto-Delete Bot is starting. Credits: Tokyo_Updates', BRAND)
    try:
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CHANNEL_POST, Update.CALLBACK_QUERY, Update.MY_CHAT_MEMBER])
    finally:
        if Tokyo_STATE['conflict']:
            retry = int(os.environ.get('TOKYO_CONFLICT_RETRY', '0'))
            delay = min(20 * (2 ** retry), 300)
            os.environ['TOKYO_CONFLICT_RETRY'] = str(retry + 1)
            log.error('⚠️ CONFLICT: ye BOT_TOKEN kahin aur bhi chal raha hai (purana deploy / dusri Render service / PC / Termux). '
                      'Us dusre instance ko band karo. %s sec baad restart kar raha hoon... | Credits: Tokyo_Updates', delay)
            time.sleep(delay)
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == '__main__':
    Tokyo_main()
