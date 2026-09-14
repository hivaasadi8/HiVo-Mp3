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

# ---------- CONFIG ----------
BOT_TOKEN       = os.getenv("BOT_TOKEN")
GH_TOKEN        = os.getenv("GH_TOKEN")
GH_REPO         = os.getenv("GH_REPO", "hivaasadi8/HiVo-MP3")
GH_PATH         = os.getenv("GH_PATH", "data/db.json")
GH_BRANCH       = os.getenv("GH_BRANCH", "main")
GATE_CHANNEL    = os.getenv("GATE_CHANNEL", "")     # مثل @HiVoChannel
ADMINS          = set(int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip())
FREE_MAX_MB     = int(os.getenv("FREE_MAX_MB", "20"))
PREMIUM_MAX_MB  = int(os.getenv("PREMIUM_MAX_MB", "50"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("HiVo-MP3")

backend = GitHubJSONBackend(GH_TOKEN, GH_REPO, GH_PATH, GH_BRANCH)
store   = Store(backend)
ADMIN_STATE = {}   # uid -> {"action": "..."}


# ---------- UI ----------
def main_menu_kb(is_admin=False, is_premium=False):
    rows = [
        [InlineKeyboardButton("🎬 ارسال ویدیو", callback_data="how_to")],
        [InlineKeyboardButton("📊 آمار من", callback_data="my_stats"),
         InlineKeyboardButton("⭐️ پریمیوم", callback_data="premium")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 پنل ادمین", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def format_kb(is_premium=False):
    rows = [
        [InlineKeyboardButton("MP3 • 128k", callback_data="cv|mp3|128k"),
         InlineKeyboardButton("MP3 • 192k", callback_data="cv|mp3|192k")],
    ]
    if is_premium:
        rows.append([InlineKeyboardButton("MP3 • 320k ⭐️", callback_data="cv|mp3|320k")])
    rows += [
        [InlineKeyboardButton("M4A • AAC", callback_data="cv|m4a|192k")],
        [InlineKeyboardButton("🎤 ویس تلگرام", callback_data="cv|voice|48k")],
        [InlineKeyboardButton("◀ بازگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(rows)


def back_kb(to="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ بازگشت", callback_data=to)]])


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار کلی", callback_data="ad|stats")],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="ad|broadcast")],
        [InlineKeyboardButton("⭐️ دادن پریمیوم", callback_data="ad|grant")],
        [InlineKeyboardButton("🚫 لغو پریمیوم", callback_data="ad|revoke")],
        [InlineKeyboardButton("⚙️ تنظیمات", callback_data="ad|settings")],
        [InlineKeyboardButton("◀ بازگشت", callback_data="back_main")],
    ])


# ---------- GATE ----------
async def check_gate(context, user_id):
    if not GATE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(GATE_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("gate error: %s", e)
        return True  # fail-open


# ---------- HANDLERS ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(user.id, username=user.username or "", first_name=user.first_name or "")

    if not await check_gate(context, user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📣 عضویت در کانال",
                                  url=f"https://t.me/{GATE_CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton("✅ عضو شدم", callback_data="gate_check")],
        ])
        await update.message.reply_text("برای استفاده ابتدا در کانال عضو شو 👇", reply_markup=kb)
        return

    text = (
        f"👋 سلام <b>{user.first_name}</b>\n\n"
        "🎵 <b>HiVo-MP3</b>\n"
        "ویدیوهات رو بفرست تا به موزیک باکیفیت تبدیل کنم.\n\n"
        "🎧 MP3 / M4A / ویس تلگرام\n"
        "⚡️ سریع، دقیق، بدون افت کیفیت"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(user.id in ADMINS, store.is_premium(user.id)),
    )


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(user.id, username=user.username or "")

    if not await check_gate(context, user.id):
        await update.message.reply_text("اول عضو کانال شو 🙏")
        return

    msg = update.message
    media = msg.video or msg.document or msg.audio or msg.voice
    if not media:
        return

    is_prem = store.is_premium(user.id) or user.id in ADMINS
    max_mb = PREMIUM_MAX_MB if is_prem else FREE_MAX_MB
    size_mb = (media.file_size or 0) / (1024 * 1024)

    if size_mb > max_mb:
        await msg.reply_text(
            f"⚠️ حجم فایل {size_mb:.1f}MB بیشتر از حد مجاز ({max_mb}MB) هست.\n"
            + ("⭐️ با پریمیوم حجم بیشتری میتونی بفرستی." if not is_prem else "")
        )
        return

    context.user_data["pending_file"] = {
        "file_id": media.file_id,
        "file_name": getattr(media, "file_name", None) or "video.mp4",
        "size": media.file_size or 0,
        "is_premium": is_prem,
    }

    await msg.reply_text("🎚 کیفیت و فرمت خروجی رو انتخاب کن:", reply_markup=format_kb(is_prem))


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
        await q.answer("این کیفیت ویژه اعضای پریمیومه ⭐️", show_alert=True)
        return

    await q.edit_message_text(f"⏳ در حال تبدیل به <b>{fmt.upper()}</b> ({bitrate})...",
                              parse_mode=ParseMode.HTML)
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.RECORD_AUDIO)

    src = audio = thumb = None
    try:
        tg_file = await context.bot.get_file(pending["file_id"])
        src = os.path.join(tempfile.gettempdir(),
                           f"src_{user.id}_{int(time.time())}")
        await tg_file.download_to_drive(src)

        loop = asyncio.get_running_loop()
        audio  = await loop.run_in_executor(None, convert_to_audio, src, fmt, bitrate)
        thumb  = await loop.run_in_executor(None, extract_thumbnail, src)
        dur    = await loop.run_in_executor(None, probe_duration, audio)
        size   = os.path.getsize(audio)

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
            text=f"✅ آماده شد!\n🎚 {fmt.upper()} • {bitrate} • {size/1024/1024:.1f}MB",
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
            f"❌ خطا در تبدیل:\n<code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        for p in (src, audio, thumb):
            if p and os.path.exists(p):
                try: os.remove(p)
                except Exception: pass


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
            f"📊 <b>آمار کلی</b>\n\n"
            f"👥 کاربران: <b>{len(users)}</b>\n"
            f"⭐️ پریمیوم: <b>{premium_count}</b>\n"
            f"🎧 تبدیلها: <b>{total_conv}</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_kb("admin"),
        )
        return

    if action == "broadcast":
        ADMIN_STATE[user.id] = {"action": "broadcast"}
        await q.edit_message_text("📢 متن پیام همگانی رو بفرست (یا /cancel):")
        return

    if action == "grant":
        ADMIN_STATE[user.id] = {"action": "grant"}
        await q.edit_message_text("⭐️ آیدی عددی کاربر رو بفرست (یا /cancel):")
        return

    if action == "revoke":
        ADMIN_STATE[user.id] = {"action": "revoke"}
        await q.edit_message_text("🚫 آیدی عددی کاربر رو بفرست (یا /cancel):")
        return

    if action == "settings":
        await q.edit_message_text(
            f"⚙️ <b>تنظیمات</b>\n\n"
            f"Gate: <code>{GATE_CHANNEL or 'خاموش'}</code>\n"
            f"حد مجاز رایگان: {FREE_MAX_MB}MB\n"
            f"حد مجاز پریمیوم: {PREMIUM_MAX_MB}MB",
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
            await q.edit_message_text("✅ عضویتت تایید شد. /start رو بزن.")
        else:
            await q.answer("هنوز عضو نشدی!", show_alert=True)
        return

    await q.answer()

    if data == "back_main":
        await q.edit_message_text(
            "🏠 منوی اصلی",
            reply_markup=main_menu_kb(user.id in ADMINS, store.is_premium(user.id)),
        )
        return

    if data == "how_to":
        await q.edit_message_text(
            "🎬 کافیه یه ویدیو (MP4 / MKV / ...) بفرستی.\n"
            "من آهنگش رو جدا میکنم و برات میفرستم.",
            reply_markup=back_kb(),
        )
        return

    if data == "help":
        await q.edit_message_text(
            "ℹ️ <b>راهنما</b>\n\n"
            "۱. یه ویدیو بفرست\n"
            "۲. فرمت و کیفیت رو انتخاب کن\n"
            "۳. آهنگ آماده رو تحویل بگیر\n\n"
            "🎧 فرمتها: MP3، M4A، ویس\n"
            "⭐️ پریمیوم: کیفیت 320k و حجم بیشتر",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data == "premium":
        await q.edit_message_text(
            "⭐️ <b>اشتراک ویژه HiVo</b>\n\n"
            "✅ حجم تا 50MB\n"
            "✅ کیفیت 320kbps\n"
            "✅ اولویت در پردازش\n\n"
            "برای خرید به ادمین پیام بده.",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data == "my_stats":
        u = store.get_user(user.id)
        prem = store.is_premium(user.id)
        await q.edit_message_text(
            f"📊 <b>آمار شما</b>\n\n"
            f"🆔 <code>{user.id}</code>\n"
            f"🎧 تبدیلها: <b>{u.get('conversions', 0)}</b>\n"
            f"⭐️ پریمیوم: {'فعال ✅' if prem else 'غیرفعال'}",
            parse_mode=ParseMode.HTML, reply_markup=back_kb(),
        )
        return

    if data.startswith("cv|"):
        await do_convert(update, context, data)
        return

    if data == "admin" and user.id in ADMINS:
        await q.edit_message_text("🛠 <b>پنل ادمین</b>",
                                  parse_mode=ParseMode.HTML, reply_markup=admin_kb())
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
        await update.message.reply_text(f"📢 ارسال شد\n✅ {ok} | ❌ {fail}")
        return True

    if action in ("grant", "revoke"):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ آیدی معتبر نیست.")
            return True
        uid = int(text)
        if action == "grant":
            store.grant_premium(uid, 30)
            await update.message.reply_text(f"✅ پریمیوم ۳۰ روزه به {uid} داده شد.")
            try: await context.bot.send_message(uid, "⭐️ اشتراک پریمیوم فعال شد (۳۰ روز).")
            except Exception: pass
        else:
            with store.lock:
                d = backend.load()
                u = d.setdefault("users", {}).setdefault(str(uid), {})
                u["premium_until"] = 0
                backend.save(d)
            await update.message.reply_text(f"🚫 پریمیوم {uid} لغو شد.")
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await admin_state_handler(update, context):
        return
    await update.message.reply_text("🎬 یه ویدیو بفرست تا تبدیلش کنم.")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMINS:
        return
    await update.message.reply_text("🛠 <b>پنل ادمین</b>",
                                    parse_mode=ParseMode.HTML, reply_markup=admin_kb())


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
                "🎵 <b>HiVo-MP3</b>\nویدیو بفرست، موزیک تحویل بگیر.\n👉 @HiVoMP3Bot",
                parse_mode=ParseMode.HTML,
            ),
        )
    ]
    await q.answer(results, cache_time=300)


# ---------- BOOT ----------
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "شروع"),
        BotCommand("help", "راهنما"),
        BotCommand("admin", "پنل ادمین"),
        BotCommand("cancel", "لغو عملیات"),
    ])


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO | filters.VOICE |
        (filters.Document.VIDEO | filters.Document.AUDIO),
        on_video,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("🎵 HiVo-MP3 is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
