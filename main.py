import os, time, asyncio, logging, tempfile
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent, BotCommand
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, InlineQueryHandler, filters
)
from store import GitHubJSONBackend, Store
from converter import convert_to_audio, extract_thumbnail, probe_duration

# ---------------- CONFIG ----------------
BOT_TOKEN       = os.getenv("BOT_TOKEN")
GH_TOKEN        = os.getenv("GH_TOKEN")
GH_REPO         = os.getenv("GH_REPO", "hivaasadi8/HiVo-MP3")
GH_PATH         = os.getenv("GH_PATH", "data/db.json")
GH_BRANCH       = os.getenv("GH_BRANCH", "main")
GATE_CHANNEL    = os.getenv("GATE_CHANNEL", "")
ADMINS          = set(int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip())

# ⚠️ سقف تلگرام Bot API برای دانلود = 20MB (غیرقابل تغییر بدون Local Server)
TG_HARD_LIMIT_MB = 19
FREE_MAX_MB      = int(os.getenv("FREE_MAX_MB", "10"))
PREMIUM_MAX_MB   = int(os.getenv("PREMIUM_MAX_MB", str(TG_HARD_LIMIT_MB)))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("HiVo-MP3")

backend = GitHubJSONBackend(GH_TOKEN, GH_REPO, GH_PATH, GH_BRANCH)
store   = Store(backend)
ADMIN_STATE = {}


# ---------------- KEYBOARDS ----------------
def main_menu_kb(is_admin=False, is_premium=False):
    rows = [
        [InlineKeyboardButton("🎬  ارسال ویدیو  ", callback_data="how_to")],
        [
            InlineKeyboardButton("📊 آمار من", callback_data="my_stats"),
            InlineKeyboardButton(
                "⭐️ پریمیوم" + (" ✅" if is_premium else ""),
                callback_data="premium"
            ),
        ],
        [InlineKeyboardButton("ℹ️ راهنما و قوانین", callback_data="help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠  پنل مدیریت  ", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def format_kb(is_premium=False):
    rows = [
        [InlineKeyboardButton("🎵 MP3 • 128kbps", callback_data="cv|mp3|128k")],
        [InlineKeyboardButton("🎵 MP3 • 192kbps  ⭐️پیشنهادی", callback_data="cv|mp3|192k")],
    ]
    if is_premium:
        rows.append([InlineKeyboardButton(
            "💎 MP3 • 320kbps  [ویژه پریمیوم]", callback_data="cv|mp3|320k"
        )])
    rows += [
        [InlineKeyboardButton("🎼 M4A • AAC 192kbps", callback_data="cv|m4a|192k")],
        [InlineKeyboardButton("🎤 ویس تلگرام (OGG)", callback_data="cv|voice|48k")],
        [InlineKeyboardButton("◀️  بازگشت به منو", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(rows)


def back_kb(to="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️  بازگشت", callback_data=to)]])


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار کلی", callback_data="ad|stats")],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="ad|broadcast")],
        [InlineKeyboardButton("⭐️ دادن پریمیوم", callback_data="ad|grant")],
        [InlineKeyboardButton("🚫 لغو پریمیوم", callback_data="ad|revoke")],
        [InlineKeyboardButton("⚙️ تنظیمات", callback_data="ad|settings")],
        [InlineKeyboardButton("◀️  بازگشت", callback_data="back_main")],
    ])


# ---------------- GATE ----------------
async def check_gate(context, user_id):
    if not GATE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(GATE_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("gate error: %s", e)
        return True


# ---------------- HANDLERS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(user.id, username=user.username or "", first_name=user.first_name or "")

    if not await check_gate(context, user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📣 عضویت در کانال",
                                  url=f"https://t.me/{GATE_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="gate_check")],
        ])
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🔒  <b>قفل عضویت</b>  🔒\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "برای استفاده از ربات، ابتدا در کانال زیر عضو شو 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🎵  <b>H I V O - M P 3</b>  🎵\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 سلام <b>{user.first_name}</b> عزیز\n"
        "به کارگاه موزیک خوش اومدی ✨\n\n"
        "┌──────────────────────┐\n"
        "│ 🎬 <b>ویدیو بفرست</b>\n"
        "│ 🎧 <b>موزیک تحویل بگیر</b>\n"
        "│ ⚡️ سریع • دقیق • باکیفیت\n"
        "└──────────────────────┘\n\n"
        "💡 <i>فرمت‌ها: MP3 • M4A • ویس تلگرام</i>"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id in ADMINS, store.is_premium(user.id)),
    )


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(user.id, username=user.username or "")

    if not await check_gate(context, user.id):
        await update.message.reply_text("🔒 اول عضو کانال شو 🙏")
        return

    msg = update.message
    media = msg.video or msg.document or msg.audio or msg.voice
    if not media:
        return

    is_prem = store.is_premium(user.id) or user.id in ADMINS
    max_mb = PREMIUM_MAX_MB if is_prem else FREE_MAX_MB
    size_mb = (media.file_size or 0) / (1024 * 1024)

    # ⛔ چک سقف سخت تلگرام
    if size_mb > TG_HARD_LIMIT_MB:
        await msg.reply_text(
            "╭──────────────────────╮\n"
            "│  ⚠️  <b>حجم فایل زیاده</b>\n"
            "╰──────────────────────╯\n\n"
            f"📦 حجم فایل شما: <b>{size_mb:.1f} MB</b>\n"
            f"🚧 سقف تلگرام: <b>{TG_HARD_LIMIT_MB} MB</b>\n\n"
            "❗️ <b>دلیل:</b> تلگرام به ربات‌ها اجازه دانلود فایل\n"
            "بیشتر از ۲۰ مگابایت رو نمیده.\n\n"
            "💡 <b>راه‌حل:</b>\n"
            "• ویدیو رو با کیفیت پایین‌تر دانلود کن\n"
            "• یا با یه نرم‌افزار حجمش رو کم کن\n"
            "• یا فایل رو تیکه‌تیکه بفرست",
            parse_mode=ParseMode.HTML,
        )
        return

    # چک سقف داخلی برنامه
    if size_mb > max_mb:
        await msg.reply_text(
            "╭──────────────────────╮\n"
            "│  🔒  <b>سقف پلن شما</b>\n"
            "╰──────────────────────╯\n\n"
            f"📦 حجم فایل: <b>{size_mb:.1f} MB</b>\n"
            f"🎯 سقف پلن شما: <b>{max_mb} MB</b>\n\n"
            "💎 <b>با ارتقا به پریمیوم:</b>\n"
            f"• سقف تا <b>{PREMIUM_MAX_MB} MB</b>\n"
            "• کیفیت ۳۲۰kbps\n"
            "• اولویت در پردازش",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["pending_file"] = {
        "file_id": media.file_id,
        "file_name": getattr(media, "file_name", None) or "video.mp4",
        "size": media.file_size or 0,
        "is_premium": is_prem,
    }

    await msg.reply_text(
        "╭──────────────────────╮\n"
        "│  🎚  <b>انتخاب کیفیت</b>\n"
        "╰──────────────────────╯\n\n"
        f"📁 فایل: <code>{context.user_data['pending_file']['file_name'][:40]}</code>\n"
        f"📦 حجم: <b>{size_mb:.2f} MB</b>\n\n"
        "👇 یکی از گزینه‌ها رو انتخاب کن:",
        parse_mode=ParseMode.HTML,
        reply_markup=format_kb(is_prem),
    )


async def do_convert(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    user = q.from_user
    _, fmt, bitrate = data.split("|")

    pending = context.user_data.get("pending_file")
    if not pending:
        await q.edit_message_text("❌ فایلی پیدا نشد. دوباره ویدیو بفرست.")
        return

    is_prem = store.is_premium(user.id) or user.id in ADMINS
    if bitrate == "320k" and not is_prem:
        await q.answer("💎 این کیفیت ویژه اعضای پریمیومه!", show_alert=True)
        return

    bar = "▓▓▓▓▓░░░░░░░░░░░░░░░"
    await q.edit_message_text(
        "╭──────────────────────╮\n"
        "│  ⚙️  <b>در حال پردازش...</b>\n"
        "╰──────────────────────╯\n\n"
        f"🎬 <b>ورودی</b>  : <code>{pending['file_name'][:30]}</code>\n"
        f"🎧 <b>خروجی</b>  : <b>{fmt.upper()} • {bitrate}</b>\n\n"
        f"<code>{bar}</code>  ۰%\n\n"
        "⏳ <i>لطفاً چند لحظه صبر کن...</i>",
        parse_mode=ParseMode.HTML,
    )
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.UPLOAD_VOICE)

    src = audio = thumb = None
    try:
        # گرفتن فایل با مدیریت خطای سقف تلگرام
        try:
            tg_file = await context.bot.get_file(pending["file_id"])
        except Exception as e:
            if "too big" in str(e).lower():
                await q.edit_message_text(
                    "╭──────────────────────╮\n"
                    "│  ❌  <b>فایل خیلی بزرگه</b>\n"
                    "╰──────────────────────╯\n\n"
                    "تلگرام به ربات‌ها اجازه دانلود فایل‌های\n"
                    "بیشتر از ۲۰ مگابایت رو نمیده.\n\n"
                    "💡 لطفاً فایل کوچک‌تری بفرست.",
                    parse_mode=ParseMode.HTML,
                )
                return
            raise

        src = os.path.join(tempfile.gettempdir(),
                           f"src_{user.id}_{int(time.time())}")
        await tg_file.download_to_drive(src)

        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, convert_to_audio, src, fmt, bitrate)
        thumb = await loop.run_in_executor(None, extract_thumbnail, src)
        dur   = await loop.run_in_executor(None, probe_duration, audio)
        size  = os.path.getsize(audio)

        with open(audio, "rb") as f:
            if fmt == "voice":
                await context.bot.send_voice(
                    chat_id=q.message.chat_id, voice=f,
                    duration=int(dur) if dur else None,
                    reply_to_message_id=q.message.message_id,
                )
            else:
                thumb_f = open(thumb, "rb") if thumb and os.path.exists(thumb) else None
                await context.bot.send_audio(
                    chat_id=q.message.chat_id, audio=f,
                    title=os.path.splitext(pending["file_name"])[0][:64],
                    performer="HiVo-MP3",
                    duration=int(dur) if dur else None,
                    thumbnail=thumb_f,
                    reply_to_message_id=q.message.message_id,
                )
                if thumb_f:
                    thumb_f.close()

        store.inc_user_conversions(user.id)
        store.inc_stat("total_conversions")

        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=(
                "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "      ✅ <b>آماده شد!</b> ✅\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"🎚 فرمت  : <b>{fmt.upper()}</b>\n"
                f"🔊 کیفیت : <b>{bitrate}</b>\n"
                f"📦 حجم   : <b>{size/1024/1024:.2f} MB</b>\n"
                f"⏱ مدت   : <b>{int(dur//60):02d}:{int(dur%60):02d}</b>\n\n"
                "🎵 <i>نوش جان! موزیکت آماده‌ست</i>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 تبدیل جدید", callback_data="how_to"),
                InlineKeyboardButton("⭐️ پریمیوم", callback_data="premium"),
            ]]),
        )
        context.user_data.pop("pending_file", None)

    except Exception as e:
        log.exception("convert failed")
        await context.bot.send_message(
            q.message.chat_id,
            "❌ <b>خطا در تبدیل</b>\n\n"
            f"<code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        for p in (src, audio, thumb):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


async def handle_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    user = q.from_user
    action = data.split("|", 1)[1]

    if action == "stats":
        d = backend.load()
        users = d.get("users", {})
        premium_count = sum(1 for u in users.values() if u.get("premium_until", 0) > time.time())
        total_conv = d.get("stats", {}).get("total_conversions", 0)
        await q.edit_message_text(
            "╭──────────────────────╮\n"
            "│  📊  <b>آمار کلی ربات</b>\n"
            "╰──────────────────────╯\n\n"
            f"👥 کاربران    : <b>{len(users)}</b>\n"
            f"⭐️ پریمیوم    : <b>{premium_count}</b>\n"
            f"🎧 تبدیل‌ها    : <b>{total_conv}</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb("admin"),
        )
        return

    if action == "broadcast":
        ADMIN_STATE[user.id] = {"action": "broadcast"}
        await q.edit_message_text(
            "📢 <b>پیام همگانی</b>\n\n"
            "متن پیام رو بفرست.\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "grant":
        ADMIN_STATE[user.id] = {"action": "grant"}
        await q.edit_message_text(
            "⭐️ <b>دادن پریمیوم</b>\n\n"
            "آیدی عددی کاربر رو بفرست.\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "revoke":
        ADMIN_STATE[user.id] = {"action": "revoke"}
        await q.edit_message_text(
            "🚫 <b>لغو پریمیوم</b>\n\n"
            "آیدی عددی کاربر رو بفرست.\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "settings":
        await q.edit_message_text(
            "⚙️ <b>تنظیمات ربات</b>\n\n"
            f"🔒 قفل کانال       : <code>{GATE_CHANNEL or 'خاموش'}</code>\n"
            f"📦 سقف رایگان     : <b>{FREE_MAX_MB} MB</b>\n"
            f"💎 سقف پریمیوم   : <b>{PREMIUM_MAX_MB} MB</b>\n"
            f"🚧 سقف تلگرام     : <b>{TG_HARD_LIMIT_MB} MB</b>\n"
            f"👮 ادمین‌ها         : <b>{len(ADMINS)}</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb("admin"),
        )
        return


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data or ""

    if data == "gate_check":
        if await check_gate(context, user.id):
            await q.answer("✅ تایید شد", show_alert=False)
            await q.edit_message_text("✅ عضویتت تایید شد.\nحالا /start رو بزن.")
        else:
            await q.answer("❌ هنوز عضو نشدی!", show_alert=True)
        return

    await q.answer()

    if data == "back_main":
        await q.edit_message_text(
            "🏠 <b>منوی اصلی</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(user.id in ADMINS, store.is_premium(user.id)),
        )
        return

    if data == "how_to":
        await q.edit_message_text(
            "🎬 <b>راهنمای تبدیل</b>\n\n"
            "۱. یه ویدیو (MP4 / MKV / ...) بفرست\n"
            "۲. فرمت و کیفیت رو انتخاب کن\n"
            "۳. آهنگ آماده رو تحویل بگیر 🎵\n\n"
            "⚡️ <i>تبدیل معمولاً کمتر از ۳۰ ثانیه طول می‌کشه</i>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data == "help":
        await q.edit_message_text(
            "ℹ️ <b>راهنما و قوانین</b>\n\n"
            "🎧 <b>فرمت‌های پشتیبانی‌شده:</b>\n"
            "• MP3 (128k / 192k / 320k)\n"
            "• M4A با کدک AAC\n"
            "• ویس تلگرام (OGG)\n\n"
            "📦 <b>حد مجاز حجم:</b>\n"
            f"• رایگان: {FREE_MAX_MB} MB\n"
            f"• پریمیوم: {PREMIUM_MAX_MB} MB\n"
            f"• سقف تلگرام: {TG_HARD_LIMIT_MB} MB\n\n"
            "⚠️ <b>توجه:</b> تلگرام به ربات‌ها اجازه دانلود\n"
            "فایل‌های بالای ۲۰ مگابایت رو نمیده.\n\n"
            "⭐️ <b>پریمیوم چه مزایایی داره؟</b>\n"
            "• کیفیت 320kbps\n"
            "• حجم بیشتر\n"
            "• اولویت در پردازش",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data == "premium":
        await q.edit_message_text(
            "╭──────────────────────╮\n"
            "│  ⭐️  <b>اشتراک ویژه HiVo</b>\n"
            "╰──────────────────────╯\n\n"
            f"✅ حجم تا <b>{PREMIUM_MAX_MB} MB</b> (سقف تلگرام)\n"
            "✅ کیفیت <b>320kbps</b> (استودیویی)\n"
            "✅ اولویت در صف پردازش\n"
            "✅ پشتیبانی اختصاصی\n\n"
            "💬 برای خرید به ادمین پیام بده.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data == "my_stats":
        u = store.get_user(user.id)
        prem = store.is_premium(user.id)
        await q.edit_message_text(
            "╭──────────────────────╮\n"
            "│  📊  <b>آمار شما</b>\n"
            "╰──────────────────────╯\n\n"
            f"🆔 آیدی عددی : <code>{user.id}</code>\n"
            f"🎧 تبدیل‌ها     : <b>{u.get('conversions', 0)}</b>\n"
            f"⭐️ پریمیوم    : {'<b>فعال ✅</b>' if prem else '<b>غیرفعال</b>'}",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data.startswith("cv|"):
        await do_convert(update, context, data)
        return

    if data == "admin" and user.id in ADMINS:
        await q.edit_message_text(
            "╭──────────────────────╮\n"
            "│  🛠  <b>پنل مدیریت</b>\n"
            "╰──────────────────────╯",
            parse_mode=ParseMode.HTML, reply_markup=admin_kb(),
        )
        return

    if data.startswith("ad|") and user.id in ADMINS:
        await handle_admin_cb(update, context, data)
        return


async def admin_state_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id not in ADMINS:
        return False
    st = ADMIN_STATE.get(user.id)
    if not st:
        return False

    text = (update.message.text or "").strip()
    action = st["action"]
    ADMIN_STATE.pop(user.id, None)

    if action == "broadcast":
        d = backend.load()
        ids = list(d.get("users", {}).keys())
        ok = fail = 0
        for uid in ids:
            try:
                await context.bot.send_message(int(uid), text)
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await update.message.reply_text(
            "📢 <b>ارسال شد</b>\n\n"
            f"✅ موفق: <b>{ok}</b>\n"
            f"❌ ناموفق: <b>{fail}</b>",
            parse_mode=ParseMode.HTML,
        )
        return True

    if action in ("grant", "revoke"):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ آیدی معتبر نیست.")
            return True
        uid = int(text)
        if action == "grant":
            store.grant_premium(uid, 30)
            await update.message.reply_text(f"✅ پریمیوم ۳۰ روزه به <code>{uid}</code> داده شد.",
                                            parse_mode=ParseMode.HTML)
            try:
                await context.bot.send_message(uid, "⭐️ اشتراک پریمیوم فعال شد (۳۰ روز).")
            except Exception:
                pass
        else:
            with store.lock:
                d = backend.load()
                u = d.setdefault("users", {}).setdefault(str(uid), {})
                u["premium_until"] = 0
                backend.save(d)
            await update.message.reply_text(f"🚫 پریمیوم <code>{uid}</code> لغو شد.",
                                            parse_mode=ParseMode.HTML)
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await admin_state_handler(update, context):
        return
    await update.message.reply_text(
        "🎬 یه <b>ویدیو</b> بفرست تا تبدیلش کنم.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMINS:
        return
    await update.message.reply_text(
        "🛠 <b>پنل مدیریت</b>",
        parse_mode=ParseMode.HTML, reply_markup=admin_kb(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_STATE.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ لغو شد.")


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    results = [
        InlineQueryResultArticle(
            id="share",
            title="🎵 HiVo-MP3 — تبدیل ویدیو به موزیک",
            description="ارسال ویدیو ← دریافت MP3",
            input_message_content=InputTextMessageContent(
                "🎵 <b>HiVo-MP3</b>\n"
                "ویدیو بفرست، موزیک تحویل بگیر.\n"
                "👉 @HiVoMP3Bot",
                parse_mode=ParseMode.HTML,
            ),
        )
    ]
    await q.answer(results, cache_time=300)


# ---------------- BOOT ----------------
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "شروع"),
        BotCommand("help", "راهنما"),
        BotCommand("admin", "پنل ادمین"),
        BotCommand("cancel", "لغو عملیات"),
    ])


async def error_handler(update, context):
    log.error("Exception:", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.Document.VIDEO | filters.Document.AUDIO,
        on_video,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("🎵 HiVo-MP3 is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
