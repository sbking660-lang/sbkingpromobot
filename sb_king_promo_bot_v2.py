#!/usr/bin/env python3
"""
SB KING Channel Promotion Bot
Single-file Telegram bot with:
- Main/VIP channel + channel directory
- Owner-only admin management (grant/revoke)
- Add/edit/delete channels
- Click analytics, referral tracking, admin notifications
- JSON backup/restore
- SQLite persistence
- Basic role-based access: owner and admins
- Branded welcome message

Setup:
  python -m pip install "python-telegram-bot>=21,<23"
  export BOT_TOKEN="123456:telegram-bot-token"
  export OWNER_ID="123456789"
  python sb_king_promo_bot.py

Never share your bot token publicly. Create a bot with @BotFather.
The owner must start the bot once before receiving private notifications.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
DB_PATH = os.getenv("BOT_DB", "sb_king_promo.sqlite3")
BACKUP_PATH = os.getenv("BOT_BACKUP", "sb_king_backup.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sb-king-promo")

ADD_NAME, ADD_URL, ADD_KIND = range(3)
EDIT_PICK, EDIT_FIELD, EDIT_VALUE = range(3, 6)
ADMIN_ADD_ID, ADMIN_REMOVE_ID = range(6, 8)
RESTORE_JSON = 8

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'regular',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, added_by INTEGER NOT NULL, added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            referred_by INTEGER, started_at TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL, clicked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL,
            new_user_id INTEGER NOT NULL UNIQUE, joined_at TEXT NOT NULL
        );
        """)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('main_channel_id','')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome','Welcome to SB KING Channel Hub 💎')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('brand','SB KING PROMO HUB 💎')")

def setting(key, default=""):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))

def is_owner(uid):
    return uid == int(OWNER_ID_RAW)

def is_admin(uid):
    if is_owner(uid):
        return True
    with db() as c:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            if update.callback_query:
                await update.callback_query.answer("Admin access only.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("हे मेन्यू फक्त अधिकृत अॅडमिनसाठी आहे.")
            return
        return await fn(update, context)
    return wrapper

def channels(kind=None, active=True):
    with db() as c:
        sql = "SELECT * FROM channels"
        args = []
        where = []
        if active:
            where.append("active=1")
        if kind:
            where.append("kind=?"); args.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE kind WHEN 'vip' THEN 0 WHEN 'main' THEN 1 ELSE 2 END, id DESC"
        return c.execute(sql, args).fetchall()

def get_channel(cid):
    with db() as c:
        return c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()

async def notify_admins(context, message):
    ids = [int(OWNER_ID_RAW)]
    with db() as c:
        ids += [r["user_id"] for r in c.execute("SELECT user_id FROM admins")]
    for uid in set(ids):
        try:
            await context.bot.send_message(uid, message)
        except Exception as e:
            log.info("Could not notify %s: %s", uid, e)

def home_keyboard(uid):
    rows = []
    main_id = setting("main_channel_id")
    main = get_channel(int(main_id)) if main_id.isdigit() else None
    if main and main["active"]:
        rows.append([InlineKeyboardButton(f"👑 {main['name']} · MAIN VIP", callback_data=f"ch:{main['id']}")])
    vip = [x for x in channels("vip") if not main or x["id"] != main["id"]]
    for ch in vip:
        rows.append([InlineKeyboardButton(f"💎 {ch['name']} · VIP", callback_data=f"ch:{ch['id']}")])
    regular = [x for x in channels("regular") if not main or x["id"] != main["id"]]
    if regular:
        rows.append([InlineKeyboardButton("📚 All Channels", callback_data="list:regular")])
    rows.append([InlineKeyboardButton("➕ More Channels", callback_data="list:all")])
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)

def admin_keyboard(uid):
    rows = [
        [InlineKeyboardButton("➕ Add Channel", callback_data="admin:add"),
         InlineKeyboardButton("✏️ Edit Channel", callback_data="admin:edit")],
        [InlineKeyboardButton("🗑 Delete Channel", callback_data="admin:delete"),
         InlineKeyboardButton("👑 Set Main VIP", callback_data="admin:main")],
        [InlineKeyboardButton("👥 Admin Management", callback_data="admin:admins")],
        [InlineKeyboardButton("📊 Analytics", callback_data="admin:analytics"),
         InlineKeyboardButton("🔔 Notifications Info", callback_data="admin:notify")],
        [InlineKeyboardButton("💾 Backup", callback_data="admin:backup"),
         InlineKeyboardButton("📥 Restore", callback_data="admin:restore")],
        [InlineKeyboardButton("🏠 User View", callback_data="home")]
    ]
    if is_owner(uid):
        rows.insert(3, [InlineKeyboardButton("➕ Grant Admin", callback_data="admin:grant"),
                        InlineKeyboardButton("➖ Revoke Admin", callback_data="admin:revoke")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("मला वापरण्यासाठी कृपया बॉटला private chat मध्ये Start करा.")
        return
    ref = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref") and arg[3:].isdigit():
            candidate = int(arg[3:])
            if candidate != user.id:
                ref = candidate
    with db() as c:
        existing = c.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
        c.execute("""INSERT INTO users(user_id,username,first_name,referred_by,started_at,last_seen)
          VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name, last_seen=excluded.last_seen""",
          (user.id, user.username, user.first_name, ref, now(), now()))
        if not existing and ref:
            c.execute("INSERT OR IGNORE INTO referrals(referrer_id,new_user_id,joined_at) VALUES(?,?,?)",
                      (ref, user.id, now()))
    if ref and not existing:
        await notify_admins(context, f"🎯 New referral: {user.first_name} (ID {user.id}) joined via {ref}.")
    text = f"✨ {setting('brand', 'SB KING PROMO HUB 💎')}\\n\\n{setting('welcome', 'Welcome to SB KING Channel Hub 💎')}\\n\\nखालील बटणातून VIP आणि इतर चॅनेल्स निवडा."
    await update.effective_message.reply_text(text, reply_markup=home_keyboard(user.id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Commands:\\n/start — Channel hub\\n/help — Help\\n/myref — Your referral link\\n/admin — Admin panel (admins only)"
    )

async def myref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{update.effective_user.id}"
    with db() as c:
        count = c.execute("SELECT COUNT(*) n FROM referrals WHERE referrer_id=?", (update.effective_user.id,)).fetchone()["n"]
    await update.effective_message.reply_text(f"🔗 तुमची referral link:\\n{link}\\n\\n👥 Successful referrals: {count}")

async def show_list(q, uid, kind):
    rows = channels(None if kind == "all" else kind)
    if not rows:
        await q.edit_message_text("सध्या या विभागात कोणतेही चॅनेल नाही.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]))
        return
    kb = []
    main_id = setting("main_channel_id")
    for ch in rows:
        label = ("👑 " if str(ch["id"]) == main_id else ("💎 " if ch["kind"] == "vip" else "📢 ")) + ch["name"]
        kb.append([InlineKeyboardButton(label, callback_data=f"ch:{ch['id']}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    await q.edit_message_text("📚 Channel Directory — निवडण्यासाठी बटण दाबा:", reply_markup=InlineKeyboardMarkup(kb))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data == "home":
        await q.edit_message_text(f"✨ {setting('brand')}\\nचॅनेल निवडा:", reply_markup=home_keyboard(uid))
    elif data.startswith("list:"):
        await show_list(q, uid, data.split(":", 1)[1])
    elif data.startswith("ch:"):
        try: cid = int(data.split(":")[1])
        except ValueError: return
        ch = get_channel(cid)
        if not ch or not ch["active"]:
            await q.edit_message_text("हे चॅनेल उपलब्ध नाही.", reply_markup=home_keyboard(uid)); return
        with db() as c:
            c.execute("INSERT INTO clicks(user_id,channel_id,clicked_at) VALUES(?,?,?)", (uid, cid, now()))
        await q.edit_message_text(f"📢 {ch['name']}\\n\\nचॅनेल उघडण्यासाठी खालील बटण दाबा.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Open Channel", url=ch["url"])],
                [InlineKeyboardButton("⬅️ Channel List", callback_data="list:all")]
            ]))
    elif data == "admin:home":
        if not is_admin(uid): await q.answer("Admin only", show_alert=True); return
        await q.edit_message_text("🛠 Admin Control Center", reply_markup=admin_keyboard(uid))
    elif data == "admin:add":
        if not is_admin(uid): return
        context.user_data.clear()
        await q.edit_message_text("चॅनेलचे नाव पाठवा. रद्द करण्यासाठी /cancel")
        context.user_data["flow"] = "add_name"
    elif data == "admin:edit":
        if not is_admin(uid): return
        items = channels(active=False)
        if not items: await q.edit_message_text("चॅनेल नाहीत.", reply_markup=admin_keyboard(uid)); return
        kb = [[InlineKeyboardButton(f"{x['id']}. {x['name']}", callback_data=f"editpick:{x['id']}")] for x in items]
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:home")])
        await q.edit_message_text("बदलायचे चॅनेल निवडा:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("editpick:"):
        if not is_admin(uid): return
        cid = int(data.split(":")[1]); context.user_data["edit_id"] = cid
        await q.edit_message_text("काय बदलायचे? खालीलपैकी लिहा: name / url / kind / active")
        context.user_data["flow"] = "edit_field"
    elif data == "admin:delete":
        if not is_admin(uid): return
        items = channels(active=False)
        kb = [[InlineKeyboardButton(f"🗑 {x['name']}", callback_data=f"del:{x['id']}")] for x in items]
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:home")])
        await q.edit_message_text("डिलीट करायचे चॅनेल निवडा:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("del:"):
        if not is_admin(uid): return
        cid = int(data.split(":")[1]); ch = get_channel(cid)
        if ch:
            with db() as c: c.execute("DELETE FROM channels WHERE id=?", (cid,))
            if setting("main_channel_id") == str(cid): set_setting("main_channel_id", "")
            await notify_admins(context, f"🗑 Channel deleted: {ch['name']} (by {uid})")
        await q.edit_message_text("चॅनेल डिलीट केले.", reply_markup=admin_keyboard(uid))
    elif data == "admin:main":
        if not is_admin(uid): return
        items = channels(active=False)
        kb = [[InlineKeyboardButton(f"👑 {x['name']}", callback_data=f"mainset:{x['id']}")] for x in items]
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:home")])
        await q.edit_message_text("Main/VIP चॅनेल निवडा:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("mainset:"):
        if not is_admin(uid): return
        cid = int(data.split(":")[1])
        if get_channel(cid): set_setting("main_channel_id", cid)
        await q.edit_message_text("Main/VIP channel सेट केले.", reply_markup=admin_keyboard(uid))
    elif data == "admin:admins":
        if not is_admin(uid): return
        with db() as c: rows = c.execute("SELECT user_id,added_at FROM admins ORDER BY added_at DESC").fetchall()
        body = "👥 Admin list:\\n" + ("\\n".join(f"• {r['user_id']} — {r['added_at']}" for r in rows) if rows else "No additional admins.")
        await q.edit_message_text(body, reply_markup=admin_keyboard(uid))
    elif data == "admin:grant":
        if not is_owner(uid): await q.answer("Owner only", show_alert=True); return
        context.user_data["flow"] = "grant"
        await q.edit_message_text("ज्याला admin करायचे त्याचा Telegram numeric user ID पाठवा. /cancel")
    elif data == "admin:revoke":
        if not is_owner(uid): await q.answer("Owner only", show_alert=True); return
        with db() as c: rows = c.execute("SELECT user_id FROM admins").fetchall()
        kb = [[InlineKeyboardButton(f"➖ {r['user_id']}", callback_data=f"revoke:{r['user_id']}")] for r in rows]
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:home")])
        await q.edit_message_text("Admin काढण्यासाठी निवडा:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("revoke:"):
        if not is_owner(uid): return
        target = int(data.split(":")[1])
        with db() as c: c.execute("DELETE FROM admins WHERE user_id=?", (target,))
        await q.edit_message_text(f"Admin {target} काढला.", reply_markup=admin_keyboard(uid))
        try: await context.bot.send_message(target, "तुमचा admin access काढण्यात आला आहे.")
        except Exception: pass
    elif data == "admin:analytics":
        if not is_admin(uid): return
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            clicks = c.execute("SELECT COUNT(*) n FROM clicks").fetchone()["n"]
            refs = c.execute("SELECT COUNT(*) n FROM referrals").fetchone()["n"]
            top = c.execute("""SELECT channels.name,COUNT(clicks.id) n FROM channels
                LEFT JOIN clicks ON clicks.channel_id=channels.id GROUP BY channels.id
                ORDER BY n DESC LIMIT 5""").fetchall()
            ref_top = c.execute("""SELECT referrer_id,COUNT(*) n FROM referrals GROUP BY referrer_id
                ORDER BY n DESC LIMIT 5""").fetchall()
        body = f"📊 Analytics\\n👥 Users: {users}\\n🖱 Total channel clicks: {clicks}\\n🎯 Referrals: {refs}\\n\\nTop channels:\\n"
        body += "\\n".join(f"• {r['name']}: {r['n']} clicks" for r in top) or "No data"
        body += "\\n\\nTop referrers:\\n" + ("\\n".join(f"• ID {r['referrer_id']}: {r['n']}" for r in ref_top) or "No data")
        await q.edit_message_text(body, reply_markup=admin_keyboard(uid))
    elif data == "admin:notify":
        if not is_admin(uid): return
        await q.edit_message_text("🔔 Notifications: नवीन referral आणि channel add/delete झाल्यावर owner/admin ला संदेश पाठवला जातो. Private notifications मिळण्यासाठी admin ने bot ला Start केलेले असावे.", reply_markup=admin_keyboard(uid))
    elif data == "admin:backup":
        if not is_admin(uid): return
        export_backup()
        await q.message.reply_document(document=open(BACKUP_PATH, "rb"), filename="sb_king_backup.json", caption="💾 Database backup")
    elif data == "admin:restore":
        if not is_owner(uid): await q.answer("Owner only", show_alert=True); return
        context.user_data["flow"] = "restore"
        await q.edit_message_text("Backup JSON file upload करा. फक्त स्वतःचा trusted backup वापरा. /cancel")
    else:
        await q.answer("Unknown action")

def export_backup():
    payload = {}
    with db() as c:
        for table in ("settings", "channels", "admins", "users", "clicks", "referrals"):
            payload[table] = [dict(r) for r in c.execute(f"SELECT * FROM {table}").fetchall()]
    payload["exported_at"] = now()
    Path(BACKUP_PATH).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def import_backup(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"settings", "channels", "admins", "users", "clicks", "referrals"}
    if not required.issubset(payload): raise ValueError("Invalid backup format")
    with db() as c:
        for table in required:
            c.execute(f"DELETE FROM {table}")
            rows = payload.get(table, [])
            for row in rows:
                cols = list(row.keys())
                marks = ",".join("?" for _ in cols)
                c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", [row[x] for x in cols])

async def text_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    uid = update.effective_user.id
    if not flow: return
    # Re-check permissions on every step; never trust a stale conversation state.
    if flow in ("add_name", "add_url", "add_kind", "edit_field", "edit_value") and not is_admin(uid):
        context.user_data.clear()
        await update.effective_message.reply_text("Admin access नाही; ही प्रक्रिया रद्द केली.")
        return
    if flow in ("grant", "restore") and not is_owner(uid):
        await update.effective_message.reply_text("Owner only.")
        context.user_data.clear()
        return
    value = (update.effective_message.text or "").strip()
    if value == "/cancel":
        context.user_data.clear(); await update.effective_message.reply_text("रद्द केले."); return
    if flow == "add_name":
        context.user_data["new_name"] = value
        context.user_data["flow"] = "add_url"
        await update.effective_message.reply_text("आता Telegram channel link पाठवा (https://t.me/...).")
    elif flow == "add_url":
        if not (value.startswith("https://t.me/") or value.startswith("https://telegram.me/")):
            await update.effective_message.reply_text("कृपया वैध https://t.me/... लिंक द्या."); return
        context.user_data["new_url"] = value
        context.user_data["flow"] = "add_kind"
        await update.effective_message.reply_text("प्रकार लिहा: vip किंवा regular")
    elif flow == "add_kind":
        kind = value.lower()
        if kind not in ("vip", "regular"):
            await update.effective_message.reply_text("vip किंवा regular लिहा."); return
        name, url = context.user_data["new_name"], context.user_data["new_url"]
        if not name or len(name) > 60:
            await update.effective_message.reply_text("नाव 1 ते 60 अक्षरांमध्ये द्या.")
            return
        try:
            with db() as c:
                c.execute("INSERT INTO channels(name,url,kind,created_at) VALUES(?,?,?,?)", (name, url, kind, now()))
        except sqlite3.IntegrityError:
            await update.effective_message.reply_text("ही लिंक आधीच जोडलेली आहे."); context.user_data.clear(); return
        context.user_data.clear()
        await update.effective_message.reply_text(f"✅ Channel add झाला: {name}", reply_markup=admin_keyboard(uid))
        await notify_admins(context, f"➕ New channel added: {name} ({kind}) by {uid}")
    elif flow == "edit_field":
        field = value.lower()
        if field not in ("name", "url", "kind", "active"):
            await update.effective_message.reply_text("name / url / kind / active पैकी एक लिहा."); return
        context.user_data["edit_field"] = field; context.user_data["flow"] = "edit_value"
        await update.effective_message.reply_text("नवीन value पाठवा. active साठी 1 किंवा 0.")
    elif flow == "edit_value":
        cid = int(context.user_data["edit_id"]); field = context.user_data["edit_field"]
        if not get_channel(cid):
            context.user_data.clear()
            await update.effective_message.reply_text("हे चॅनेल सापडले नाही. पुन्हा प्रयत्न करा.")
            return
        if field == "kind" and value not in ("vip", "regular"):
            await update.effective_message.reply_text("kind = vip किंवा regular."); return
        if field == "url" and not (value.startswith("https://t.me/") or value.startswith("https://telegram.me/")):
            await update.effective_message.reply_text("वैध Telegram link द्या."); return
        if field == "active" and value not in ("0", "1"):
            await update.effective_message.reply_text("active = 1 किंवा 0."); return
        try:
            with db() as c:
                c.execute(f"UPDATE channels SET {field}=? WHERE id=?", (value, cid))
        except sqlite3.IntegrityError:
            await update.effective_message.reply_text("ही लिंक दुसऱ्या चॅनेलसाठी वापरली आहे.")
            return
        if field == "active" and value == "0" and setting("main_channel_id") == str(cid):
            set_setting("main_channel_id", "")
        context.user_data.clear()
        await update.effective_message.reply_text("✅ Channel updated.", reply_markup=admin_keyboard(uid))
    elif flow == "grant":
        if not value.isdigit():
            await update.effective_message.reply_text("Numeric Telegram user ID द्या."); return
        target = int(value)
        if target == int(OWNER_ID_RAW):
            await update.effective_message.reply_text("Owner ला वेगळा admin बनवण्याची गरज नाही."); return
        with db() as c: c.execute("INSERT OR REPLACE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (target, uid, now()))
        context.user_data.clear()
        await update.effective_message.reply_text(f"✅ {target} admin झाला.", reply_markup=admin_keyboard(uid))
        try: await context.bot.send_message(target, "तुम्हाला SB KING Promo Bot मध्ये admin access दिला आहे. /start करा.")
        except Exception: pass
    elif flow == "restore":
        await update.effective_message.reply_text("Restore साठी JSON document upload करा, text paste करू नका.")

async def document_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("flow") != "restore" or not is_owner(update.effective_user.id):
        return
    doc = update.effective_message.document
    if not doc or not doc.file_name.lower().endswith(".json"):
        await update.effective_message.reply_text("कृपया .json backup file upload करा."); return
    tgfile = await context.bot.get_file(doc.file_id)
    temp = "restore_upload.json"
    await tgfile.download_to_drive(temp)
    try:
        import_backup(temp)
        context.user_data.clear()
        await update.effective_message.reply_text("✅ Backup restore झाला. कृपया data तपासा.")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Restore failed: {e}")
    finally:
        try: os.remove(temp)
        except OSError: pass

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("रद्द केले.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Admin access नाही."); return
    await update.effective_message.reply_text("🛠 Admin Control Center", reply_markup=admin_keyboard(update.effective_user.id))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error", exc_info=context.error)

def main():
    if not BOT_TOKEN or not OWNER_ID_RAW.isdigit():
        raise SystemExit("Set BOT_TOKEN and OWNER_ID environment variables before running.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("myref", myref))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, document_flow))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_flow))
    app.add_error_handler(error_handler)
    log.info("SB KING Promo Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
