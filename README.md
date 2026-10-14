# ᴛᴏᴋʏᴏ_ᴜᴘᴅᴀᴛᴇs | Auto-Delete Bot

**Credits / Branding: Tokyo_Updates**

Telegram Bot API bot (python-telegram-bot + local SQLite, **no MongoDB**). Group messages auto-delete after a timer, with **global owner-controlled Force-Sub**, a **log channel**, and a `/start` menu with picture.

## Commands
**Everyone (only after joining the Force-Sub channels)**

| Command | What it does |
|---|---|
| `/start` | Start menu with picture + buttons |
| `/help` | Command list |
| `/show_time` | Current delete timer (time + ✅ ON / ❌ OFF) |
| `/get_id` | Chat ID / your ID |

**Group / Channel admins:** `/set_timer 2h` (also `30s`, `3min`, `1d`, `7200`; min 10s, max 365d; `/set_timer off` stops auto-delete). Replies like **Set Time Successfully ✅** are shown and auto-deleted too.

**Owner only** (`OWNER_IDS`)

| Command | Example |
|---|---|
| `/add_fsub` | `/add_fsub -1001234567890` (channel **ID**, bot must be admin there) |
| `/remove_fsub` | `/remove_fsub -1001234567890` |
| `/list_fsub` | shows channels + ON/OFF |
| `/set_fsub` | `/set_fsub on` / `off` |
| `/set_log` | `/set_log -1001234567890` / `off` |
| `/set_pic` | reply to a photo to change the start picture (`/set_pic reset` to restore) |
| `/stats` | users / groups / pending deletions |

## Channel auto-delete
- Add the bot as **admin with Delete Messages** in your channel, then post `/set_timer 2h` in the channel. Every new channel post is deleted after that time.
- Groups and channels stay **OFF by default** (nothing is deleted) until an admin uses `/set_timer`. `/show_time` and `/set_timer off` work there too.

## Force-Sub (global)
- Only the owner can add/remove channels or switch it on/off; it applies to **every** group and private chat.
- Channels are stored by **channel ID**. Join buttons use the public link, or an invite link the bot creates (give it *Invite Users* permission for private channels).
- Users who haven't joined: commands are blocked, and in groups their messages are deleted with a temporary join warning. The **✅ I Have Joined** button re-checks instantly.
- Owners are exempt. If the bot can't check a channel (removed / not admin), users are **not** locked out; a warning goes to the log channel.

## Log channel
Add the bot as admin in a channel and run `/set_log <channel id>`. It logs:
- Someone starts the bot (name, username, ID; plus group info if started in a group)
- Bot added / removed / promoted in a group or channel: **who did it** (name, username, ID) and the group/channel name, username, ID, type, member count, delete permission
- Force-Sub changes and Force-Sub problems

## Restart: settings & timers stay
Many hosts (Render free, Koyeb, Heroku, Railway without a volume...) wipe the disk on every restart/redeploy, which used to reset every timer to the default.
Now the bot keeps a **backup of its database in the log channel** (one pinned file, `tokyo_backup.sqlite3.gz`) and restores it automatically when it starts with an empty disk.
- Needs: a log channel (`LOG_CHANNEL` env / `info.py`, or `/set_log`) and the bot as **admin** there with **Post Messages** + **Pin Messages** (channels: **Edit Messages**). Don't pin other messages in that channel.
- Backups run ~30 s after any change (timer, force-sub, log channel, bot added/removed) and once more on a graceful shutdown.
- On every start the log channel gets **🔄 Bot Restarted** (with *restored from backup / loaded from disk / fresh start*), and the console banner says **BOT RESTARTED**.
- The bot also keeps one copy of the database in **RAM**, replaced by a fresh one every **7 days** (old copy dropped). If the database file is lost/emptied while the bot runs, it is restored from that copy.
- If your host has a persistent disk, mount it and set `DB_PATH` to it (then the backup is just a safety copy). `BACKUP_TO_LOG=0` turns the backup off.

## Output style
All bot messages, buttons and command descriptions are shown in **small caps**. Commands, `<code>` examples, @usernames, links and chat/channel names are left as they are. `/set_timer` and `/set_log` replies no longer show the chat name.

## Pictures
- `assets/start.jpg` is the `/start` banner (1280×720). Replace the file, set `START_PIC`, or use `/set_pic`.
- `assets/bot_profile.jpg` is a ready bot profile picture: upload it in **@BotFather → /setuserpic**.
- `python make_assets.py` regenerates both (needs Pillow, optional).

## Setup
1. Create a bot with `@BotFather`, copy the token; `/setprivacy` → **Disable**.
2. Set env vars: `BOT_TOKEN`, `OWNER_IDS` (your Telegram ID).
3. Add bot to the group as admin with **Delete Messages**.
4. Add bot as admin in each Force-Sub channel and in the log channel, then use the owner commands above.

## Run / Deploy
```bash
pip install -r requirements.txt
python bot.py
```
Render: Background Worker via `render.yaml`/`Dockerfile`; mount a persistent disk at `/var/data` so settings and pending deletions survive restarts. Never run two copies with the same token.

**Made for Tokyo_Updates. Credits: Tokyo_Updates.**
