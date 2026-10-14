# -*- coding: utf-8 -*-
"""Tokyo_Updates Auto-Delete Bot configuration | Credits: Tokyo_Updates"""
__credits__ = 'Tokyo_Updates'
import os

# Set BOT_TOKEN in your host's Environment settings. Never publish the token.
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()

# Bot OWNER user IDs (comma separated), e.g. OWNER_IDS=123456789,987654321
# Only owners can manage Force-Sub (global), the log channel and the start picture.
OWNER_IDS = {int(v.strip()) for v in os.getenv('OWNER_IDS', '5308609720').split(',') if v.strip().isdigit()}

DB_PATH = os.getenv('DB_PATH', 'data/tokyo_autodelete.sqlite3')
CHECK_INTERVAL = max(5, int(os.getenv('CHECK_INTERVAL', '10')))
DEFAULT_DELETE_SECONDS = max(10, int(os.getenv('DEFAULT_DELETE_SECONDS', '7200')))

# Optional: log channel ID (e.g. -1001234567890). Can also be set later with /set_log (owner only).
_log = os.getenv('LOG_CHANNEL', '-1002477743748').strip()
LOG_CHANNEL = int(_log) if _log.lstrip('-').isdigit() else 0

# Start picture: leave empty to use assets/start.jpg. Can be a URL or a Telegram file_id.
# Owner can also change it from chat with /set_pic (reply to a photo).
START_PIC = os.getenv('START_PIC', '').strip()
START_PIC_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'start.jpg')

UPDATES_URL = os.getenv('UPDATES_URL', 'https://t.me/+JBAI-hnBgcY2ODY1').strip()

# Settings (timers, force-sub, log channel...) are backed up to the LOG CHANNEL (pinned file) and restored automatically
# when the host wipes the disk on restart. Needs: log channel set + bot admin there (Post + Pin/Edit Messages). 0 = off.
BACKUP_TO_LOG = os.getenv('BACKUP_TO_LOG', '1').strip() != '0'
