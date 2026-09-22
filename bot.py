import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()


def env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {name} must be a number, but got: {value}") from exc


TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = env_int("ADMIN_CHAT_ID")
TARGET_CHAT_ID = env_int("TARGET_CHAT_ID")
GROUP_NAME = os.getenv("GROUP_NAME", "صقور السوق | Saudi • US")
INTERVAL_HOURS = int(os.getenv("INTERVAL_HOURS", "3"))
MAX_WARNINGS = int(os.getenv("MAX_WARNINGS", "3"))
BLOCK_FORWARDS = os.getenv("BLOCK_FORWARDS", "true").lower() == "true"
DB_PATH = os.getenv("DB_PATH", "guard.db")

LINK_RE = re.compile(
    r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|wa\.me/|discord\.gg/|"
    r"(?:[a-z0-9-]+\.)+(?:com|net|org|io|me|co|ly|app|site|online|info|xyz)(?:/|\b))"
)

PROMO_MESSAGE = f"""📊 <b>{GROUP_NAME}</b>

عايز تحليل سهم؟ ابعت اسم السهم واكتب جنبه:
🇸🇦 <b>سعودي</b> أو 🇺🇸 <b>أمريكي</b>

📩 ولو عايز تتواصل مع الإدارة، اكتب كلمة <b>تم</b> فقط، وهيتم التواصل معاك في أقرب وقت.

⚠️ ممنوع إرسال الروابط أو الإعلانات داخل الجروب حفاظًا على الأعضاء."""

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("saqour-guard")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS contact_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


def warning_count(chat_id: int, user_id: int) -> int:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return int(row["count"]) if row else 0


def add_warning(chat_id: int, user_id: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO warnings(chat_id, user_id, count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                count = count + 1,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, now),
        )
        conn.commit()
    return warning_count(chat_id, user_id)


def reset_warning(chat_id: int, user_id: int) -> None:
    with closing(db()) as conn:
        conn.execute(
            "DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        conn.commit()


def save_contact(update: Update) -> None:
    user = update.effective_user
    chat = update.effective_chat
    with closing(db()) as conn:
        conn.execute(
            """INSERT INTO contact_requests
            (chat_id, user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                chat.id,
                user.id,
                user.username,
                user.full_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


async def is_admin(update: Update, user_id: int) -> bool:
    member = await update.effective_chat.get_member(user_id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


def contains_link(message) -> bool:
    text = message.text or message.caption or ""
    if LINK_RE.search(text):
        return True
    entities = list(message.entities or []) + list(message.caption_entities or [])
    return any(e.type in {"url", "text_link"} for e in entities)


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.is_bot:
        return
    if TARGET_CHAT_ID and update.effective_chat.id != TARGET_CHAT_ID:
        return
    if await is_admin(update, user.id):
        return

    forwarded = bool(message.forward_origin)
    if not contains_link(message) and not (BLOCK_FORWARDS and forwarded):
        return

    try:
        await message.delete()
    except Exception:
        log.exception("Could not delete message")
        return

    count = add_warning(update.effective_chat.id, user.id)
    mention = user.mention_html()
    reason = "إعادة توجيه محتوى" if forwarded and not contains_link(message) else "إرسال رابط"

    if count >= MAX_WARNINGS:
        permissions = ChatPermissions.no_permissions()
        try:
            await update.effective_chat.restrict_member(user.id, permissions=permissions)
            await context.bot.send_message(
                update.effective_chat.id,
                f"⛔ {mention}\nتم التقييد النهائي بعد {MAX_WARNINGS} مخالفات.\nالسبب الأخير: {reason}.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.exception("Could not restrict member")
            await context.bot.send_message(
                update.effective_chat.id,
                f"⚠️ وصل {mention} إلى المخالفة الثالثة، لكن تعذر تقييده. تأكد أن البوت مشرف وله صلاحية تقييد الأعضاء.",
                parse_mode=ParseMode.HTML,
            )
    else:
        await context.bot.send_message(
            update.effective_chat.id,
            f"⚠️ تحذير {count}/{MAX_WARNINGS} إلى {mention}\nممنوع {reason} داخل الجروب. بعد التحذير الثالث سيتم تقييد الحساب نهائيًا.",
            parse_mode=ParseMode.HTML,
        )


async def contact_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if TARGET_CHAT_ID and update.effective_chat.id != TARGET_CHAT_ID:
        return
    user = update.effective_user
    save_contact(update)
    await update.effective_message.reply_html(
        f"✅ تم تسجيل طلبك يا {user.mention_html()}، وهيتم التواصل معاك في أقرب وقت."
    )
    if ADMIN_CHAT_ID:
        username = f"@{user.username}" if user.username else "لا يوجد يوزرنيم"
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            "📩 <b>طلب تواصل جديد</b>\n\n"
            f"الاسم: {user.full_name}\n"
            f"اليوزر: {username}\n"
            f"ID: <code>{user.id}</code>\n"
            f"الجروب: {update.effective_chat.title}",
            parse_mode=ParseMode.HTML,
        )


async def periodic_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    if TARGET_CHAT_ID:
        await context.bot.send_message(
            TARGET_CHAT_ID, PROMO_MESSAGE, parse_mode=ParseMode.HTML
        )


async def admin_only(update: Update) -> bool:
    if not update.effective_user or not update.effective_chat:
        return False
    return await is_admin(update, update.effective_user.id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    await update.effective_message.reply_text(
        f"✅ البوت يعمل\nالجروب: {update.effective_chat.id}\n"
        f"الفاصل: كل {INTERVAL_HOURS} ساعات\nالحد: {MAX_WARNINGS} تحذيرات"
    )


async def post_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    await context.bot.send_message(
        update.effective_chat.id, PROMO_MESSAGE, parse_mode=ParseMode.HTML
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    target = update.effective_message.reply_to_message
    if not target or not target.from_user:
        await update.effective_message.reply_text("استخدم الأمر بالرد على رسالة العضو: /resetwarn")
        return
    reset_warning(update.effective_chat.id, target.from_user.id)
    await update.effective_message.reply_html(
        f"✅ تم تصفير تحذيرات {target.from_user.mention_html()}."
    )


async def unrestrict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_only(update):
        return
    target = update.effective_message.reply_to_message
    if not target or not target.from_user:
        await update.effective_message.reply_text("استخدم الأمر بالرد على رسالة العضو: /unrestrict")
        return
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )
    await update.effective_chat.restrict_member(target.from_user.id, permissions=permissions)
    reset_warning(update.effective_chat.id, target.from_user.id)
    await update.effective_message.reply_html(
        f"✅ تم فك تقييد {target.from_user.mention_html()} وتصفير تحذيراته."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)


def main() -> None:
    if not TOKEN or TOKEN == "ضع_توكن_البوت_هنا":
        raise SystemExit(
            "ERROR: BOT_TOKEN is missing. Run setup_windows.bat first or edit the .env file."
        )
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("postnow", post_now))
    app.add_handler(CommandHandler("resetwarn", reset_cmd))
    app.add_handler(CommandHandler("unrestrict", unrestrict_cmd))
    app.add_handler(
        MessageHandler(filters.Regex(r"^\s*تم\s*[.!؟]*\s*$") & ~filters.COMMAND, contact_request),
        group=0,
    )
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, moderate), group=1)
    app.add_error_handler(error_handler)

    if TARGET_CHAT_ID:
        app.job_queue.run_repeating(
            periodic_post,
            interval=INTERVAL_HOURS * 3600,
            first=30,
            name="periodic_group_message",
        )
    else:
        log.warning("TARGET_CHAT_ID is not set; periodic messages are disabled")

    log.info("Saqour Group Guard started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
