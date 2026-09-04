# -*- coding: utf-8 -*-
"""
NeirAni_Bot — Kod orqali anime qidiruv + har bir qism uchun alohida Premium
+ muddatli Premium tizimi + to'liq Admin Panel.

Talab: Python 3.8+, pyTelegramBotAPI
O'rnatish:  pip install pyTelegramBotAPI
Ishga tushirish:  python bot.py

Long-polling ishlatiladi — server, domen yoki SSL shart emas, Termux'da
to'g'ridan-to'g'ri ishlaydi.
"""

import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta

import telebot
from telebot import types

# ============================================================
#                        KONFIGURATSIYA
# ============================================================
# XAVFSIZLIK: TOKEN kodga hech qachon qattiq yozilmaydi (GitHub'ga xavfsiz
# joylash uchun). Token ikki manbadan olinadi (birinchi topilgani ishlatiladi):
#   1) TOKEN environment o'zgaruvchisi   ->  export TOKEN="..."
#   2) shu papkadagi token.txt fayli     ->  bitta qatorda tokenning o'zi
# token.txt fayli .gitignore orqali GitHub'ga hech qachon yuklanmaydi.
def _load_token():
    env_token = os.getenv("TOKEN")
    if env_token:
        return env_token.strip()
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return None


BOT_TOKEN = _load_token()
MAIN_ADMIN_ID = 7991544389          # Asosiy admin — o'chirib bo'lmaydi (spetsifikatsiyada berilgan)
DB_PATH = "neirani_bot.db"

if not BOT_TOKEN:
    raise SystemExit(
        "❌ XATOLIK: BOT_TOKEN topilmadi.\n\n"
        "Quyidagilardan birini qiling:\n"
        "  1) export TOKEN=\"sizning_tokeningiz\"   (Termux/Linux)\n"
        "  2) shu papkada token.txt fayl yarating va ichiga faqat tokenni yozing\n"
    )

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

DATE_FMT = "%d.%m.%Y"
DT_FMT = "%Y-%m-%d %H:%M:%S"
MAX_ANIME_VIDEO_SECONDS = 100  # 1 daqiqa 40 soniya — anime qo'shishdagi rasm/video bosqichi uchun

_BOT_USERNAME_CACHE = {"value": None}


def get_bot_username():
    """Bot username'ini Telegramdan dinamik oladi va keshlaydi (kod ichida qattiq yozilmaydi)."""
    if _BOT_USERNAME_CACHE["value"]:
        return _BOT_USERNAME_CACHE["value"]
    try:
        me = bot.get_me()
        _BOT_USERNAME_CACHE["value"] = me.username
        return me.username
    except Exception as e:
        log_action(0, "Bot username olishda xato", str(e))
        return None


# ============================================================
#                    MA'LUMOTLAR BAZASI
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now().strftime(DT_FMT)


def init_db():
    conn = db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        first_name TEXT,
        joined_at TEXT,
        last_activity TEXT,
        is_premium INTEGER DEFAULT 0,
        premium_expire TEXT
    );

    CREATE TABLE IF NOT EXISTS animes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        image_file_id TEXT,
        image_type TEXT DEFAULT 'photo',
        genre TEXT,
        language TEXT,
        is_ongoing INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        downloads INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS episodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_id INTEGER NOT NULL,
        episode_number INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        name TEXT,
        is_premium INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS saved_animes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        anime_id INTEGER,
        saved_at TEXT,
        UNIQUE(user_id, anime_id)
    );

    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        username TEXT UNIQUE
    );

    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        added_by INTEGER,
        added_at TEXT
    );

    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        message TEXT,
        answered INTEGER DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        details TEXT,
        created_at TEXT
    );
    """)
    conn.commit()

    # --- Migratsiya: mavjud (eski) bazada yo'q bo'lgan ustunlarni ma'lumot yo'qotmasdan qo'shish ---
    try:
        c.execute("ALTER TABLE animes ADD COLUMN image_type TEXT DEFAULT 'photo'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # ustun allaqachon mavjud

    c.execute("INSERT OR IGNORE INTO admins (telegram_id, added_by, added_at) VALUES (?, ?, ?)",
              (MAIN_ADMIN_ID, MAIN_ADMIN_ID, now()))

    defaults = {
        "maintenance_mode": "0",
        "start_message": "👋 Assalomu alaykum, {name}!\n\nAnineo ga xush kelibsiz.\n\n🎬 Kerakli bo'limni tanlang.",
        "force_sub_message": "🔒 Botdan foydalanish uchun kanalga obuna bo'ling:",
        "not_found_message": "❌ Anime topilmadi.\n\n🔢 Kodni tekshirib qayta yuboring.",
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(key):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""


def set_setting(key, value):
    conn = db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


def log_action(admin_id, action, details=""):
    conn = db()
    conn.execute("INSERT INTO logs (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
                 (admin_id, action, details, now()))
    conn.commit()
    conn.close()


def touch_user(tg_user):
    conn = db()
    row = conn.execute("SELECT id FROM users WHERE telegram_id=?", (tg_user.id,)).fetchone()
    if row:
        conn.execute("UPDATE users SET last_activity=?, username=?, first_name=? WHERE telegram_id=?",
                     (now(), tg_user.username, tg_user.first_name, tg_user.id))
    else:
        conn.execute("INSERT INTO users (telegram_id, username, first_name, joined_at, last_activity) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (tg_user.id, tg_user.username, tg_user.first_name, now(), now()))
    conn.commit()
    conn.close()


def get_user_row(telegram_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row


def is_admin(telegram_id):
    conn = db()
    row = conn.execute("SELECT 1 FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()
    conn.close()
    return row is not None


def is_main_admin(telegram_id):
    return telegram_id == MAIN_ADMIN_ID


# ---------- PREMIUM MUDDAT LOGIKASI ----------

def check_and_expire_premium(telegram_id):
    """Premium muddati tugagan bo'lsa avtomatik o'chiradi. True/False qaytaradi (hozir premium faolmi)."""
    row = get_user_row(telegram_id)
    if not row or not row["is_premium"]:
        return False
    if not row["premium_expire"]:
        return True  # muddatsiz (kamdan-kam holat)
    expire_dt = datetime.strptime(row["premium_expire"], DATE_FMT)
    if datetime.now() > expire_dt:
        conn = db()
        conn.execute("UPDATE users SET is_premium=0, premium_expire=NULL WHERE telegram_id=?", (telegram_id,))
        conn.commit()
        conn.close()
        return False
    return True


def is_premium(telegram_id):
    return check_and_expire_premium(telegram_id)


def add_premium_months(telegram_id, months):
    row = get_user_row(telegram_id)
    now_dt = datetime.now()
    base = now_dt
    if row and row["is_premium"] and row["premium_expire"]:
        try:
            existing = datetime.strptime(row["premium_expire"], DATE_FMT)
            if existing > now_dt:
                base = existing
        except Exception:
            pass
    new_expire = base + timedelta(days=30 * months)
    cap = now_dt + timedelta(days=360)
    if new_expire > cap:
        new_expire = cap
    conn = db()
    conn.execute("UPDATE users SET is_premium=1, premium_expire=? WHERE telegram_id=?",
                 (new_expire.strftime(DATE_FMT), telegram_id))
    conn.commit()
    conn.close()
    return new_expire.strftime(DATE_FMT)


def premium_status_text(telegram_id):
    row = get_user_row(telegram_id)
    active = check_and_expire_premium(telegram_id)
    if not active:
        return "💎 Premium: Tugagan"
    expire_dt = datetime.strptime(row["premium_expire"], DATE_FMT)
    remaining = (expire_dt - datetime.now()).days
    return f"💎 Premium: Faol\n📅 Tugash sanasi: {row['premium_expire']}\n⏳ Qolgan muddat: {remaining} kun"


# ============================================================
#                    HOLAT (WIZARD) BOSHQARUVI
# ============================================================
WIZ = {}      # admin_id -> {...}
USTATE = {}   # user_id -> {...}

ADD_ANIME_STEPS = [
    ("code", "🔢 Anime kodini kiriting (masalan: 125):"),
    ("name", "🎬 Anime nomini kiriting:"),
    ("image", "🖼🎬 Anime rasmi yoki videosini yuboring (ixtiyoriy — /skip):"),
    ("genre", "🎭 Janrini kiriting (masalan: Action, Adventure):"),
    ("language", "🌐 Tilini kiriting (masalan: O'zbekcha):"),
    ("is_ongoing", "⏳ Bu anime hozir davom etayaptimi? (ha / yo'q):"),
]


# ============================================================
#                        KLAVIATURALAR
# ============================================================

def user_main_menu(uid=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🎬 Anime qidirish", "🔢 Kod orqali qidirish")
    kb.row("🔥 Mashhur animelar", "🆕 Yangi animelar")
    kb.row("📺 Davom etayotganlar", "🔖 Saqlanganlar")
    kb.row("📥 Yuklab olish")
    kb.row("💎 Premium", "👤 Profil")
    kb.row("📞 Murojaat", "ℹ️ Yordam")
    kb.row("🔄 Botni qayta ishga tushirish", "☰ Menyu")
    if uid and is_admin(uid):
        kb.row("👨‍💻 Admin panel")
    return kb


def admin_main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🎬 Anime boshqarish", "📺 Qismlar boshqarish")
    kb.row("💎 Premium boshqarish")
    kb.row("👥 Foydalanuvchilar", "👑 Adminlar")
    kb.row("📢 Kanallar", "📣 Reklama")
    kb.row("📤 Kanalga post yuborish")
    kb.row("📊 Statistika", "💾 Backup")
    kb.row("⚙️ Sozlamalar")
    kb.row("🔄 Botni qayta ishga tushirish")
    kb.row("⬅️ Orqaga")
    return kb


def cancel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("❌ Bekor qilish")
    return kb


def channels_inline_kb(missing=None):
    """missing berilmasa — barcha kanallarni ko'rsatadi (masalan birinchi ochilishda).
    missing ro'yxati berilsa — faqat foydalanuvchi hali obuna bo'lmagan kanallarni ko'rsatadi."""
    if missing is None:
        conn = db()
        rows = conn.execute("SELECT * FROM channels").fetchall()
        conn.close()
    else:
        rows = missing
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        username = r["username"]
        if username.startswith("@"):
            kb.add(types.InlineKeyboardButton(f"📢 {r['name']}", url=f"https://t.me/{username.lstrip('@')}"))
        else:
            # Raqamli chat_id uchun to'g'ridan-to'g'ri havola qurib bo'lmaydi — faqat nom ko'rsatiladi
            kb.add(types.InlineKeyboardButton(f"📢 {r['name']}", callback_data="noop"))
    kb.add(types.InlineKeyboardButton("✅ OBUNANI TEKSHIRISH", callback_data="checksub"))
    return kb


def anime_card_text(a):
    return (f"🎬 <b>{a['name']}</b>\n\n"
            f"🎭 Janr: {a['genre'] or '-'}\n"
            f"📺 Qismlar soni: {episode_count(a['id'])}\n"
            f"🌐 Til: {a['language'] or '-'}\n"
            f"🔢 Kod: <code>{a['code']}</code>")


def episode_count(anime_id):
    conn = db()
    n = conn.execute("SELECT COUNT(*) c FROM episodes WHERE anime_id=?", (anime_id,)).fetchone()["c"]
    conn.close()
    return n


def anime_user_kb(anime_id, saved=False):
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("📺 Qismlarni ko'rish", callback_data=f"eplist_{anime_id}"),
           types.InlineKeyboardButton("📥 Yuklab olish", callback_data=f"eplist_{anime_id}"))
    save_label = "✅ Saqlangan" if saved else "🔖 Saqlash"
    kb.row(types.InlineKeyboardButton(save_label, callback_data=f"save_{anime_id}"))
    kb.row(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="backmenu"))
    return kb


def find_anime_by_code(code):
    conn = db()
    row = conn.execute("SELECT * FROM animes WHERE code=?", (code.strip(),)).fetchone()
    conn.close()
    return row


def build_watch_deep_link_kb(code):
    """Anime kodiga bog'langan deep-link tugmasini yaratadi: https://t.me/<bot>?start=<code>.
    Bot username Telegramdan dinamik olinadi — kodga qattiq yozilmagan."""
    kb = types.InlineKeyboardMarkup()
    username = get_bot_username()
    if username:
        kb.add(types.InlineKeyboardButton("🔹 Tomosha qilish 🔹",
                                           url=f"https://t.me/{username}?start={code}"))
    else:
        # Bot username hozircha olinmadi (masalan tarmoq xatosi) — tugmasiz caption bilan yuboramiz,
        # lekin xatoni yashirmaymiz, logga yozilgan
        kb = None
    return kb


def channel_post_channels_kb(anime_id):
    conn = db()
    rows = conn.execute("SELECT * FROM channels").fetchall()
    conn.close()
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.add(types.InlineKeyboardButton(f"📤 {r['username']} ga yuborish",
                                           callback_data=f"postch_{anime_id}_{r['id']}"))
    kb.add(types.InlineKeyboardButton("📡 BARCHA kanallarga yuborish", callback_data=f"postall_{anime_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="postcancel"))
    return kb


def show_post_preview_and_channels(chat_id, uid, a):
    text = anime_card_text(a)
    watch_kb = build_watch_deep_link_kb(a["code"])
    try:
        if a["image_file_id"]:
            image_type = a["image_type"] if "image_type" in a.keys() else "photo"
            if image_type == "video":
                bot.send_video(chat_id, a["image_file_id"], caption=text, reply_markup=watch_kb)
            else:
                bot.send_photo(chat_id, a["image_file_id"], caption=text, reply_markup=watch_kb)
        else:
            bot.send_message(chat_id, text, reply_markup=watch_kb)
    except Exception as e:
        log_action(0, "Post preview xatosi", str(e))
        bot.send_message(chat_id, text)

    conn = db()
    has_channels = conn.execute("SELECT COUNT(*) c FROM channels").fetchone()["c"] > 0
    conn.close()
    if not has_channels:
        WIZ.pop(uid, None)
        bot.send_message(chat_id, "⚠️ Hozircha hech qanday kanal qo'shilmagan. Avval "
                                   "\"📢 Kanallar\" bo'limidan kanal qo'shing.",
                          reply_markup=admin_main_menu())
        return

    WIZ[uid] = {"flow": "post_select", "anime_id": a["id"]}
    bot.send_message(chat_id, "📤 Qaysi kanal(lar)ga yuborilsin?", reply_markup=channel_post_channels_kb(a["id"]))


def send_anime_post_to_channel(admin_chat_id, anime_id, channel_row):
    conn = db()
    a = conn.execute("SELECT * FROM animes WHERE id=?", (anime_id,)).fetchone()
    conn.close()
    if not a:
        bot.send_message(admin_chat_id, "❌ Post yuborishda xatolik yuz berdi.")
        log_action(0, "Post yuborish xatosi", f"anime_id {anime_id} topilmadi")
        return False

    text = anime_card_text(a)
    watch_kb = build_watch_deep_link_kb(a["code"])
    target = channel_row["username"]
    try:
        image_type = a["image_type"] if "image_type" in a.keys() else "photo"
        if a["image_file_id"]:
            if image_type == "video":
                bot.send_video(target, a["image_file_id"], caption=text, reply_markup=watch_kb)
            else:
                bot.send_photo(target, a["image_file_id"], caption=text, reply_markup=watch_kb)
        else:
            bot.send_message(target, text, reply_markup=watch_kb)
        bot.send_message(admin_chat_id, f"✅ Post {channel_row['name']} ({target}) ga muvaffaqiyatli yuborildi.")
        return True
    except Exception as e:
        # Xatoni yashirmaymiz — texnik sababini logga yozamiz va adminga tushunarli xabar beramiz
        log_action(0, "Post yuborish xatosi", f"{target}: {e}")
        bot.send_message(admin_chat_id,
                          f"❌ Post yuborishda xatolik yuz berdi ({channel_row['name']} — {target}).\n"
                          f"Ehtimol bot shu kanalda admin emas yoki yozish huquqiga ega emas.")
        return False


def send_anime_card(chat_id, a, user_id=None):
    conn = db()
    conn.execute("UPDATE animes SET views = views + 1 WHERE id=?", (a["id"],))
    conn.commit()
    saved = False
    if user_id:
        urow = conn.execute("SELECT id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        if urow:
            saved = conn.execute("SELECT 1 FROM saved_animes WHERE user_id=? AND anime_id=?",
                                  (urow["id"], a["id"])).fetchone() is not None
    conn.close()
    text = anime_card_text(a)
    kb = anime_user_kb(a["id"], saved=saved)
    if a["image_file_id"]:
        try:
            image_type = a["image_type"] if "image_type" in a.keys() else "photo"
        except Exception:
            image_type = "photo"
        if image_type == "video":
            bot.send_video(chat_id, a["image_file_id"], caption=text, reply_markup=kb)
        else:
            bot.send_photo(chat_id, a["image_file_id"], caption=text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


# ============================================================
#                   MAJBURIY OBUNA TEKSHIRISH
# ============================================================

def get_missing_channels(user_id):
    """Foydalanuvchi hali obuna bo'lmagan majburiy kanallar ro'yxatini qaytaradi.
    Admin uchun har doim bo'sh ro'yxat (ozod). API xatosi bo'lsa, o'sha kanal
    'hali tasdiqlanmagan' deb hisoblanadi (fail-closed) va xato logga yoziladi."""
    if is_admin(user_id):
        return []

    conn = db()
    channels = conn.execute("SELECT * FROM channels").fetchall()
    conn.close()
    if not channels:
        return []

    NOT_SUBSCRIBED = ("left", "kicked")
    missing = []
    for ch in channels:
        try:
            member = bot.get_chat_member(ch["username"], user_id)
        except Exception as e:
            log_action(0, "Obuna tekshiruvi xatosi", f"{ch['username']}: {e}")
            missing.append(ch)
            continue

        status = member.status
        if status in NOT_SUBSCRIBED:
            missing.append(ch)
        elif status == "restricted":
            if not hasattr(member, "is_member") or member.is_member is False:
                missing.append(ch)
        elif status not in ("member", "administrator", "creator"):
            missing.append(ch)
    return missing


def check_subscription(user_id):
    """Barcha
