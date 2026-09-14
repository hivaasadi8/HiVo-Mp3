"""
HiVo-MP3 — Telegram Bot
-----------------------
ربات تبدیل ویدیو به موزیک با معماری تمیز، پایدار و مقیاس‌پذیر.

ویژگی‌ها:
  • معماری چندلایه (main / store / converter)
  • Graceful shutdown با SIGTERM/SIGINT
  • Rate limiting درون‌حافظه‌ای برای هر کاربر
  • محدودیت تبدیل همزمان با Semaphore
  • Progress bar زنده در حین تبدیل
  • مدیریت خطای گروه‌بندی‌شده با پیام فارسی
  • پنل ادمین کامل (آمار، لیدربورد، جستجو، بن، بکاپ)
  • Gate عضویت اجباری
  • قابلیت Inline Mode برای اشتراک‌گذاری
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, InlineQueryHandler, MessageHandler, filters,
)

from converter import (
    PRESETS, cleanup, convert, extract_embedded_art, extract_thumbnail,
    ffmpeg_available, probe, probe_duration,
)
from store import GitHubJSONBackend, Store

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
GH_TOKEN      = os.getenv("GH_TOKEN", "").strip()
GH_REPO       = os.getenv("GH_REPO", "hivaasadi8/HiVo-MP3")
GH_PATH       = os.getenv("GH_PATH", "data/db.json")
GH_BRANCH     = os.getenv("GH_BRANCH", "main")
GATE_CHANNEL  = os.getenv("GATE_CHANNEL", "").strip()

ADMINS = {
    int(x) for x in os.getenv("ADMINS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

# سقف‌های حجم
TG_HARD_LIMIT_MB = 19
FREE_MAX_MB      = int(os.getenv("FREE_MAX_MB", "10"))
PREMIUM_MAX_MB   = int(os.getenv("PREMIUM_MAX_MB", str(TG_HARD_LIMIT_MB)))

# Rate limits (به ترتیب: تعداد/پنجره‌ی زمانی بر حسب ثانیه)
FREE_RATE    = (5,  300)    # ۵ تبدیل در هر ۵ دقیقه
PREMIUM_RATE = (30, 300)    # ۳۰ تبدیل در هر ۵ دقیقه

MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("HiVo.Main")


# ═══════════════════════════════════════════════════════════════════════
# SINGLETONS
# ═══════════════════════════════════════════════════════════════════════

backend = GitHubJSONBackend(GH_TOKEN, GH_REPO, GH_PATH, GH_BRANCH)
store   = Store(backend)

ADMIN_STATE: dict[int, dict] = {}           # state مخصوص ادمین
RATE_BUCKETS: dict[int, deque] = defaultdict(deque)
CONVERT_SEM = asyncio.Semaphore(MAX_CONCURRENT)


# ═══════════════════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════════════════

def box(title: str, body: str = "") -> str:
    """جعبه‌ی مینیمال."""
    top = "╭──────────────────────╮"
    bot = "╰──────────────────────╯"
    line = f"│  {title}"
    return f"{top}\n{line}\n{bot}" + (f"\n\n{body}" if body else "")


def esc(text: str) -> str:
    """Escape برای HTML."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md(text: str) -> str:
    """کوتاه‌سازی متن برای نمایش در دکمه/پیام."""
    text = (text or "").strip().replace("\n", " ")
    return text[:40] + ("…" if len(text) > 40 else "")


def fmt_size(b: int) -> str:
    return f"{b / (1024*1024):.2f} MB"


def fmt_dur(sec: float) -> str:
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def progress_bar(pct: int, width: int = 18) -> str:
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "▓" * filled + "░" * (width - filled)


def rate_limited(uid: int, is_premium: bool) -> tuple[bool, int]:
    """بررسی rate limit. برمی‌گردونه (limited, seconds_left)."""
    limit, window = PREMIUM_RATE if is_premium else FREE_RATE
    bucket = RATE_BUCKETS[uid]
    now = time.time()

    while bucket and now - bucket[0] > window:
        bucket.popleft()

    if len(bucket) >= limit:
        wait = int(window - (now - bucket[0])) + 1
        return True, wait

    bucket.append(now)
    return False, 0


# ═══════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════

def kb_main(is_admin: bool = False, is_premium: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎬  ارسال ویدیو  ", callback_data="how_to")],
        [
            InlineKeyboardButton("📊 آمار من",     callback_data="my_stats"),
            InlineKeyboardButton("⚙️ تنظیمات",    callback_data="settings"),
        ],
        [
            InlineKeyboardButton(
                "⭐️ پریمیوم" + (" ✅" if is_premium else ""),
                callback_data="premium",
            ),
            InlineKeyboardButton("ℹ️ راهنما",       callback_data="help"),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠  پنل مدیریت  ", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def kb_format(is_premium: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("MP3 · 128", callback_data="cv|mp3_128"),
            InlineKeyboardButton("MP3 · 192 ⭐️", callback_data="cv|mp3_192"),
        ],
        [InlineKeyboardButton("MP3 · 320 💎", callback_data="cv|mp3_320")],
        [
            InlineKeyboardButton("M4A · 192", callback_data="cv|m4a_192"),
            InlineKeyboardButton("🎤 ویس",    callback_data="cv|voice"),
        ],
    ]
    if is_premium:
        rows.append([
            InlineKeyboardButton("🎼 FLAC", callback_data="cv|flac"),
            InlineKeyboardButton("MP3 · V0 💎", callback_data="cv|mp3_v0"),
        ])
    rows.append([InlineKeyboardButton("◀️  بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_back(to: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️  بازگشت", callback_data=to)]])


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار",       callback_data="ad|stats"),
            InlineKeyboardButton("📈 ۷ روز اخیر", callback_data="ad|weekly"),
        ],
        [InlineKeyboardButton("🏆 لیدربورد",     callback_data="ad|top")],
        [InlineKeyboardButton("📢 پیام همگانی",  callback_data="ad|broadcast")],
        [
            InlineKeyboardButton("⭐️ افزودن",    callback_data="ad|grant"),
            InlineKeyboardButton("🚫 لغو",         callback_data="ad|revoke"),
        ],
        [
            InlineKeyboardButton("🔍 جستجو",       callback_data="ad|search"),
            InlineKeyboardButton("⛔️ بن/آنبن",     callback_data="ad|ban"),
        ],
        [
            InlineKeyboardButton("💾 بکاپ",         callback_data="ad|backup"),
            InlineKeyboardButton("⚙️ تنظیمات",      callback_data="ad|settings"),
        ],
        [InlineKeyboardButton("◀️  بازگشت",        callback_data="back_main")],
    ])


def kb_post_convert() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 تبدیل جدید", callback_data="how_to"),
        InlineKeyboardButton("⭐️ پریمیوم",   callback_data="premium"),
    ]])


def kb_gate() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📣 عضویت در کانال",
            url=f"https://t.me/{GATE_CHANNEL.lstrip('@')}",
        )],
        [InlineKeyboardButton("✅ عضو شدم", callback_data="gate_check")],
    ])


# ═══════════════════════════════════════════════════════════════════════
# GATE
# ═══════════════════════════════════════════════════════════════════════

async def check_gate(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if not GATE_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(GATE_CHANNEL, user_id)
        return m.status in ("member", "administrator", "creator")
    except TelegramError as e:
        log.warning("gate check failed: %s", e)
        return True    # fail-open


# ═══════════════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(
        user.id,
        username=user.username or "",
        first_name=user.first_name or "",
    )

    if store.is_banned(user.id):
        await update.message.reply_text("⛔️ دسترسی شما محدود شده.")
        return

    if not await check_gate(context, user.id):
        await update.message.reply_text(
            box("🔒  قفل عضویت", "برای استفاده، ابتدا در کانال عضو شو 👇"),
            parse_mode=ParseMode.HTML, reply_markup=kb_gate(),
        )
        return

    is_prem = store.is_premium(user.id)
    text = (
        box(
            "🎵  H I V O - M P 3",
            f"👋 سلام <b>{esc(user.first_name)}</b>\n"
            "به کارگاه موزیک خوش اومدی ✨\n\n"
            "🎬 <b>ویدیو بفرست</b>\n"
            "🎧 <b>موزیک تحویل بگیر</b>\n"
            "⚡️ سریع • دقیق • باکیفیت"
        )
        + f"\n\n💎 وضعیت: {'<b>پریمیوم ✅</b>' if is_prem else 'رایگان'}"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=kb_main(user.id in ADMINS, is_prem),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        box(
            "ℹ️  راهنما",
            "🎧 <b>فرمت‌ها:</b>\n"
            "• MP3 (128 / 192 / 320 / V0)\n"
            "• M4A · AAC\n"
            "• 🎤 ویس تلگرام\n"
            "• 🎼 FLAC (پریمیوم)\n\n"
            f"📦 سقف رایگان: <b>{FREE_MAX_MB} MB</b>\n"
            f"💎 سقف پریمیوم: <b>{PREMIUM_MAX_MB} MB</b>\n\n"
            f"⏱ محدودیت رایگان: ۵ تبدیل / ۵ دقیقه\n"
            f"⏱ محدودیت پریمیوم: ۳۰ تبدیل / ۵ دقیقه"
        ),
        parse_mode=ParseMode.HTML, reply_markup=kb_back(),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        return
    await update.message.reply_text(
        box("🛠  پنل مدیریت"),
        parse_mode=ParseMode.HTML, reply_markup=kb_admin(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_STATE.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ لغو شد.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"🆔 <code>{u.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════════════
# VIDEO HANDLER
# ═══════════════════════════════════════════════════════════════════════

async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    store.upsert_user(user.id, username=user.username or "")

    if store.is_banned(user.id):
        await update.message.reply_text("⛔️ دسترسی شما محدود شده.")
        return

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

    # سقف سخت تلگرام
    if size_mb > TG_HARD_LIMIT_MB:
        await msg.reply_text(
            box(
                "⚠️  حجم فایل زیاده",
                f"📦 حجم فایل: <b>{size_mb:.1f} MB</b>\n"
                f"🚧 سقف تلگرام: <b>{TG_HARD_LIMIT_MB} MB</b>\n\n"
                "❗️ تلگرام به ربات‌ها اجازه دانلود فایل\n"
                "بیشتر از ۲۰ مگابایت رو نمیده.\n\n"
                "💡 ویدیو رو با کیفیت پایین‌تر بگیر"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # سقف پلن
    if size_mb > max_mb:
        await msg.reply_text(
            box(
                "🔒  سقف پلن شما",
                f"📦 حجم: <b>{size_mb:.1f} MB</b>\n"
                f"🎯 سقف پلن: <b>{max_mb} MB</b>\n\n"
                "💎 با پریمیوم: کیفیت بالاتر + سقف بیشتر"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐️ ارتقا به پریمیوم", callback_data="premium")
            ]]),
        )
        return

    context.user_data["pending_file"] = {
        "file_id":   media.file_id,
        "file_name": getattr(media, "file_name", None) or "video.mp4",
        "size":      media.file_size or 0,
        "is_prem":   is_prem,
        "added_at":  time.time(),
    }

    await msg.reply_text(
        box(
            "🎚  انتخاب کیفیت",
            f"📁 <code>{esc(md(context.user_data['pending_file']['file_name']))}</code>\n"
            f"📦 <b>{size_mb:.2f} MB</b>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb_format(is_prem),
    )


# ═══════════════════════════════════════════════════════════════════════
# CONVERSION
# ═══════════════════════════════════════════════════════════════════════

async def do_convert(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    user = q.from_user

    _, preset_key = data.split("|", 1)
    if preset_key not in PRESETS:
        await q.answer("❌ فرمت نامعتبر", show_alert=True)
        return

    pending = context.user_data.get("pending_file")
    if not pending:
        await q.edit_message_text("❌ فایلی پیدا نشد. دوباره ویدیو بفرست.")
        return

    is_prem = store.is_premium(user.id) or user.id in ADMINS
    preset_meta = PRESETS[preset_key]

    # چک دسترسی به preset های ویژه
    premium_only = preset_key in ("mp3_320", "mp3_v0", "flac", "m4a_256")
    if premium_only and not is_prem:
        await q.answer("💎 این گزینه ویژه پریمیومه!", show_alert=True)
        return

    # Rate limit
    limited, wait = rate_limited(user.id, is_prem)
    if limited:
        await q.answer(f"⏱ کمی صبر کن — {wait}s", show_alert=True)
        return

    # ─── progress message ───
    file_label = md(pending["file_name"])
    preset_label = preset_key.replace("_", " · ").upper()
    await q.edit_message_text(
        box(
            "⚙️  در حال پردازش",
            f"🎬 <code>{esc(file_label)}</code>\n"
            f"🎧 <b>{preset_label}</b>\n\n"
            f"<code>{progress_bar(0)}</code>  0%"
        ),
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_chat_action(q.message.chat_id, ChatAction.UPLOAD_VOICE)
    except TelegramError:
        pass

    src = out_path = thumb = embedded = None
    started = time.time()

    async with CONVERT_SEM:
        try:
            # ── دانلود فایل ──
            try:
                tg_file = await context.bot.get_file(pending["file_id"])
            except TelegramError as e:
                if "too big" in str(e).lower():
                    await q.edit_message_text(
                        box("❌  فایل خیلی بزرگه",
                            "تلگرام اجازه دانلود فایل‌های\nبیشتر از ۲۰ مگابایت رو نمیده.")
                    )
                    return
                raise

            src = os.path.join(tempfile.gettempdir(),
                               f"hivo_src_{user.id}_{int(time.time())}")
            await tg_file.download_to_drive(src)

            # ── آپدیت ۳۰٪ ──
            try:
                await q.edit_message_text(
                    box("⚙️  در حال پردازش",
                        f"🎬 <code>{esc(file_label)}</code>\n"
                        f"🎧 <b>{preset_label}</b>\n\n"
                        f"<code>{progress_bar(30)}</code>  30%"),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass

            # ── تبدیل ──
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: convert(
                    src, preset_key,
                    metadata={
                        "title":  os.path.splitext(pending["file_name"])[0][:64],
                        "artist": "HiVo-MP3",
                    },
                ),
            )
            out_path = result.path

            # ── کاور: اول امبد، بعد فریم ──
            embedded = await loop.run_in_executor(None, extract_embedded_art, src)
            thumb    = embedded or await loop.run_in_executor(None, extract_thumbnail, src)

            # ── آپدیت ۸۰٪ ──
            try:
                await q.edit_message_text(
                    box("⚙️  در حال ارسال",
                        f"🎬 <code>{esc(file_label)}</code>\n"
                        f"🎧 <b>{preset_label}</b>\n\n"
                        f"<code>{progress_bar(80)}</code>  80%"),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass

            # ── ارسال ──
            with open(out_path, "rb") as f:
                thumb_f = open(thumb, "rb") if thumb and os.path.exists(thumb) else None
                try:
                    if result.fmt == "voice":
                        await context.bot.send_voice(
                            chat_id=q.message.chat_id, voice=f,
                            duration=int(result.duration) or None,
                            reply_to_message_id=q.message.message_id,
                        )
                    else:
                        await context.bot.send_audio(
                            chat_id=q.message.chat_id, audio=f,
                            title=os.path.splitext(pending["file_name"])[0][:64],
                            performer="HiVo-MP3",
                            duration=int(result.duration) or None,
                            thumbnail=thumb_f,
                            reply_to_message_id=q.message.message_id,
                        )
                finally:
                    if thumb_f:
                        thumb_f.close()

            # ── آمار ──
            store.inc_user_conversions(user.id)
            store.inc_stat(f"conversions_{result.fmt}")

            # ── پیام نهایی ──
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=box(
                    "✅  آماده شد",
                    f"🎚 فرمت  · <b>{result.fmt.upper()}</b>\n"
                    f"🔊 کیفیت · <b>{result.bitrate}</b>\n"
                    f"📦 حجم   · <b>{fmt_size(result.size)}</b>\n"
                    f"⏱ مدت   · <b>{fmt_dur(result.duration)}</b>\n"
                    f"⚡️ زمان  · <b>{result.elapsed:.1f}s</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_post_convert(),
            )
            context.user_data.pop("pending_file", None)

        except Exception as e:
            log.exception("convert failed for user %s", user.id)
            err = str(e)[:180]
            try:
                await context.bot.send_message(
                    q.message.chat_id,
                    box("❌  خطا در تبدیل", f"<code>{esc(err)}</code>"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_post_convert(),
                )
            except TelegramError:
                pass
        finally:
            cleanup(src, out_path, thumb, embedded)


# ═══════════════════════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data or ""

    # ── gate ──
    if data == "gate_check":
        if await check_gate(context, user.id):
            await q.answer("✅ تایید شد")
            await q.edit_message_text("✅ عضویتت تایید شد.\nحالا /start بزن.")
        else:
            await q.answer("❌ هنوز عضو نشدی!", show_alert=True)
        return

    await q.answer()

    # ── تبدیل ──
    if data.startswith("cv|"):
        await do_convert(update, context, data)
        return

    # ── ادمین ──
    if data.startswith("ad|"):
        if user.id not in ADMINS:
            return
        await handle_admin_cb(update, context, data)
        return

    is_prem = store.is_premium(user.id)
    is_admin = user.id in ADMINS

    # ── مسیرها ──
    if data == "back_main":
        await q.edit_message_text(
            box("🏠  منوی اصلی"),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin, is_prem),
        )
        return

    if data == "how_to":
        await q.edit_message_text(
            box("🎬  راهنمای تبدیل",
                "۱. یه ویدیو بفرست\n"
                "۲. کیفیت رو انتخاب کن\n"
                "۳. موزیک آماده رو تحویل بگیر 🎵\n\n"
                "<i>معمولاً کمتر از ۳۰ ثانیه</i>"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back(),
        )
        return

    if data == "help":
        await cmd_help(update, context) if False else await q.edit_message_text(
            box("ℹ️  راهنما",
                "🎧 MP3 · M4A · 🎤 ویس · 🎼 FLAC\n\n"
                f"📦 رایگان: <b>{FREE_MAX_MB} MB</b>\n"
                f"💎 پریمیوم: <b>{PREMIUM_MAX_MB} MB</b>\n\n"
                "⏱ رایگان: ۵ تبدیل / ۵ دقیقه\n"
                "⏱ پریمیوم: ۳۰ تبدیل / ۵ دقیقه"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back(),
        )
        return

    if data == "premium":
        await q.edit_message_text(
            box("⭐️  اشتراک ویژه",
                f"✅ سقف تا <b>{PREMIUM_MAX_MB} MB</b>\n"
                "✅ کیفیت 320 / V0 / FLAC\n"
                "✅ ۳۰ تبدیل در ۵ دقیقه\n"
                "✅ اولویت در پردازش\n\n"
                "💬 برای خرید به ادمین پیام بده."),
            parse_mode=ParseMode.HTML, reply_markup=kb_back(),
        )
        return

    if data == "my_stats":
        rec = store.get_user_record(user.id)
        days = store.premium_days_left(user.id)
        await q.edit_message_text(
            box("📊  آمار شما",
                f"🆔 <code>{user.id}</code>\n"
                f"🎧 تبدیل‌ها: <b>{rec.conversions}</b>\n"
                f"⭐️ پریمیوم: "
                + (f"<b>{days} روز مونده</b>" if rec.is_premium else "<b>غیرفعال</b>")),
            parse_mode=ParseMode.HTML, reply_markup=kb_back(),
        )
        return

    if data == "settings":
        await q.edit_message_text(
            box("⚙️  تنظیمات",
                "به‌زودی: انتخاب کیفیت پیش‌فرض،\nزبان، و اعلان‌ها."),
            parse_mode=ParseMode.HTML, reply_markup=kb_back(),
        )
        return

    if data == "admin" and is_admin:
        await q.edit_message_text(
            box("🛠  پنل مدیریت"),
            parse_mode=ParseMode.HTML, reply_markup=kb_admin(),
        )
        return


# ═══════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════

async def handle_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    q = update.callback_query
    user = q.from_user
    action = data.split("|", 1)[1]

    if action == "stats":
        d = backend.load()
        users = d.get("users", {})
        stats = d.get("stats", {})
        prem = sum(1 for u in users.values() if u.get("premium_until", 0) > time.time())
        total = stats.get("total_conversions", 0)
        banned = sum(1 for u in users.values() if u.get("banned"))

        await q.edit_message_text(
            box("📊  آمار کلی",
                f"👥 کاربران    · <b>{len(users)}</b>\n"
                f"⭐️ پریمیوم    · <b>{prem}</b>\n"
                f"⛔️ بن‌شده    · <b>{banned}</b>\n"
                f"🎧 تبدیل‌ها    · <b>{total}</b>"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin"),
        )
        return

    if action == "weekly":
        days = store.stats_range(7)
        lines = []
        for day in sorted(days.keys()):
            cnt = days[day].get("total_conversions", 0)
            bar = "▮" * min(cnt, 15) or "·"
            lines.append(f"<code>{day[5:]}</code>  {bar}  <b>{cnt}</b>")
        await q.edit_message_text(
            box("📈  ۷ روز اخیر", "\n".join(lines) or "—"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin"),
        )
        return

    if action == "top":
        tops = store.top_users(by="conversions", limit=10)
        lines = []
        medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 7
        for i, u in enumerate(tops):
            name = u.get("first_name") or u.get("username") or str(u["uid"])
            lines.append(f"{medals[i]} {esc(name)[:18]} · <b>{u['value']}</b>")
        await q.edit_message_text(
            box("🏆  لیدربورد", "\n".join(lines) or "—"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin"),
        )
        return

    if action == "broadcast":
        ADMIN_STATE[user.id] = {"action": "broadcast"}
        await q.edit_message_text(
            box("📢  پیام همگانی", "متن پیام رو بفرست.\nبرای لغو: /cancel"),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "grant":
        ADMIN_STATE[user.id] = {"action": "grant"}
        await q.edit_message_text(
            box("⭐️  افزودن پریمیوم", "آیدی عددی کاربر:\nبرای لغو: /cancel"),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "revoke":
        ADMIN_STATE[user.id] = {"action": "revoke"}
        await q.edit_message_text(
            box("🚫  لغو پریمیوم", "آیدی عددی کاربر:\nبرای لغو: /cancel"),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "search":
        ADMIN_STATE[user.id] = {"action": "search"}
        await q.edit_message_text(
            box("🔍  جستجوی کاربر",
                "قسمتی از یوزرنیم / اسم / آیدی رو بفرست.\nبرای لغو: /cancel"),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "ban":
        ADMIN_STATE[user.id] = {"action": "ban"}
        await q.edit_message_text(
            box("⛔️  بن / آنبن",
                "آیدی عددی کاربر:\nبرای لغو: /cancel"),
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "backup":
        raw = store.export()
        fname = f"hivo_backup_{int(time.time())}.json"
        path = os.path.join(tempfile.gettempdir(), fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=q.message.chat_id, document=f,
                    filename=fname, caption="💾 بکاپ دیتابیس",
                )
        finally:
            cleanup(path)
        return

    if action == "settings":
        await q.edit_message_text(
            box("⚙️  تنظیمات",
                f"🔒 Gate       · <code>{esc(GATE_CHANNEL or 'خاموش')}</code>\n"
                f"📦 رایگان     · <b>{FREE_MAX_MB} MB</b>\n"
                f"💎 پریمیوم   · <b>{PREMIUM_MAX_MB} MB</b>\n"
                f"🚧 تلگرام     · <b>{TG_HARD_LIMIT_MB} MB</b>\n"
                f"👮 ادمین‌ها   · <b>{len(ADMINS)}</b>\n"
                f"🔀 همزمان     · <b>{MAX_CONCURRENT}</b>"),
            parse_mode=ParseMode.HTML, reply_markup=kb_back("admin"),
        )
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
        users = list(store.all_users().keys())
        ok = fail = 0
        for uid_str in users:
            try:
                await context.bot.send_message(int(uid_str), text)
                ok += 1
                await asyncio.sleep(0.05)
            except (Forbidden, BadRequest):
                fail += 1
            except TelegramError:
                fail += 1
        await update.message.reply_text(
            box("📢  ارسال شد", f"✅ موفق · <b>{ok}</b>\n❌ ناموفق · <b>{fail}</b>"),
            parse_mode=ParseMode.HTML,
        )
        return True

    if action in ("grant", "revoke", "ban"):
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ آیدی معتبر نیست.")
            return True
        uid = int(text)

        if action == "grant":
            store.grant_premium(uid, 30)
            await update.message.reply_text(
                f"✅ پریمیوم ۳۰ روزه به <code>{uid}</code> داده شد.",
                parse_mode=ParseMode.HTML,
            )
            try:
                await context.bot.send_message(uid, "⭐️ اشتراک پریمیوم فعال شد (۳۰ روز).")
            except TelegramError:
                pass

        elif action == "revoke":
            store.revoke_premium(uid)
            await update.message.reply_text(
                f"🚫 پریمیوم <code>{uid}</code> لغو شد.",
                parse_mode=ParseMode.HTML,
            )

        elif action == "ban":
            if store.is_banned(uid):
                store.unban_user(uid)
                await update.message.reply_text(f"✅ آنبن شد: <code>{uid}</code>",
                                                parse_mode=ParseMode.HTML)
            else:
                store.ban_user(uid, reason="admin action")
                await update.message.reply_text(f"⛔️ بن شد: <code>{uid}</code>",
                                                parse_mode=ParseMode.HTML)
        return True

    if action == "search":
        results = store.search_users(text, limit=15)
        if not results:
            await update.message.reply_text("❌ چیزی پیدا نشد.")
            return True
        lines = []
        for r in results:
            name = r.get("first_name") or r.get("username") or "—"
            badge = "💎" if r.get("premium_until", 0) > time.time() else "▫️"
            banned = " ⛔️" if r.get("banned") else ""
            lines.append(f"{badge} <code>{r['uid']}</code> · {esc(name)[:20]}{banned}")
        await update.message.reply_text(
            box("🔍  نتایج", "\n".join(lines)),
            parse_mode=ParseMode.HTML,
        )
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════
# TEXT + INLINE
# ═══════════════════════════════════════════════════════════════════════

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await admin_state_handler(update, context):
        return
    await update.message.reply_text(
        "🎬 یه <b>ویدیو</b> بفرست تا تبدیلش کنم.",
        parse_mode=ParseMode.HTML,
    )


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    results = [
        InlineQueryResultArticle(
            id="share",
            title="🎵 HiVo-MP3",
            description="تبدیل ویدیو به موزیک",
            input_message_content=InputTextMessageContent(
                "🎵 <b>HiVo-MP3</b>\n"
                "ویدیو بفرست، موزیک تحویل بگیر.\n"
                "👉 @HiVoMP3Bot",
                parse_mode=ParseMode.HTML,
            ),
        )
    ]
    await q.answer(results, cache_time=300, is_personal=False)


# ═══════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "شروع"),
        BotCommand("help",   "راهنما"),
        BotCommand("id",     "آیدی من"),
        BotCommand("admin",  "پنل ادمین"),
        BotCommand("cancel", "لغو"),
    ])

    if not ffmpeg_available():
        log.error("⚠️ FFmpeg not available — conversions will fail!")
    else:
        log.info("✅ FFmpeg detected")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=context.error)
    # اگه کسی خواست پیام خطا ببینه، فقط ادمین‌ها
    if isinstance(update, Update) and update.effective_user:
        if update.effective_user.id in ADMINS:
            try:
                msg = f"🚨 <code>{esc(str(context.error)[:300])}</code>"
                await context.bot.send_message(update.effective_user.id, msg,
                                               parse_mode=ParseMode.HTML)
            except TelegramError:
                pass


def _install_signal_handlers(app: Application):
    """Graceful shutdown روی SIGTERM/SIGINT."""
    def _shutdown(signum, frame):
        log.info("signal %s received — shutting down", signum)
        try:
            store.flush()
            log.info("db flushed")
        except Exception as e:
            log.warning("flush failed: %s", e)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing")
    if not GH_TOKEN:
        log.warning("⚠️ GH_TOKEN missing — persistence will fail")

    log.info("🚀 HiVo-MP3 starting — %s", now_utc())

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("id",     cmd_id))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.AUDIO | filters.VOICE
        | filters.Document.VIDEO | filters.Document.AUDIO,
        on_video,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    _install_signal_handlers(app)

    log.info("🎵 polling started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
