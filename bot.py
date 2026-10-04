import os
import re
import sqlite3
import logging
import asyncio
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_FILE = "monitor.db"
CHECK_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            last_status TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, username)
        )
        """
    )
    conn.commit()
    conn.close()


def clean_username(value: str) -> str:
    value = value.strip()

    value = re.sub(
        r"^https?://(www\.)?instagram\.com/",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = value.lstrip("@").strip("/")
    value = value.split("?")[0].split("/")[0]

    return value.strip()


async def check_instagram(username: str) -> dict:
    """
    Conservative public-profile checker.

    ACTIVE: Instagram returns a normal public profile page.
    NOT_FOUND: Instagram clearly indicates the profile/page does not exist.
    Ambiguous/protected responses are treated as NOT_FOUND to preserve the
    bot's existing two-state behavior. This does not bypass login, CAPTCHA,
    rate limits, or other Instagram protections.
    """
    username = clean_username(username)
    profile_url = f"https://www.instagram.com/{username}/"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }

    def fetch():
        request = Request(profile_url, headers=headers, method="GET")
        with urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore").lower()
            return response.status, response.geturl().lower(), body

    try:
        status_code, final_url, body = await asyncio.to_thread(fetch)

        if status_code == 404:
            status = "NOT_FOUND"
        elif any(
            phrase in body
            for phrase in (
                "sorry, this page isn't available",
                "page isn't available",
                "the link you followed may be broken",
                "page may have been removed",
            )
        ):
            status = "NOT_FOUND"
        elif (
            status_code == 200
            and not any(x in final_url for x in ("login", "challenge", "checkpoint"))
        ):
            status = "ACTIVE"
        else:
            status = "NOT_FOUND"

        return {
            "status": status,
            "username": username,
            "profile_url": profile_url,
        }

    except (HTTPError, URLError, TimeoutError, asyncio.TimeoutError):
        return {
            "status": "NOT_FOUND",
            "username": username,
            "profile_url": profile_url,
        }
    except Exception as e:
        logger.exception("Check failed for %s: %s", username, e)
        return {
            "status": "NOT_FOUND",
            "username": username,
            "profile_url": profile_url,
        }


def format_result(result: dict) -> str:
    if result["status"] == "ACTIVE":
        emoji = "🟢"
    else:
        emoji = "⚪"

    return (
        f"{emoji} <b>{result['status']}</b>\n\n"
        f"Username: <code>@{result['username']}</code>\n"
        f"Profile: {result['profile_url']}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Instagram Monitor Bot</b>\n\n"
        "Send an Instagram username or profile URL to check it.\n\n"
        "<b>Commands:</b>\n"
        "/check username\n"
        "/watch username\n"
        "/unwatch username\n"
        "/list\n"
        "/help",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Help</b>\n\n"
        "/check username\n"
        "/watch username\n"
        "/unwatch username\n"
        "/list\n\n"
        "You can also send an Instagram username or full profile URL directly.",
        parse_mode="HTML",
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n/check username")
        return

    username = clean_username(context.args[0])

    if not username:
        await update.message.reply_text("Invalid Instagram username.")
        return

    message = await update.message.reply_text("🔎 Checking Instagram...")

    result = await check_instagram(username)

    await message.edit_text(
        format_result(result),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n/watch username")
        return

    username = clean_username(context.args[0])

    if not username:
        await update.message.reply_text("Invalid Instagram username.")
        return

    chat_id = update.effective_chat.id
    result = await check_instagram(username)

    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO monitors
            (chat_id, username, last_status, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (chat_id, username, result["status"]),
        )
        conn.commit()

    except sqlite3.IntegrityError:
        conn.close()
        await update.message.reply_text(
            f"⚠️ <code>@{username}</code> is already being monitored.",
            parse_mode="HTML",
        )
        return

    conn.close()

    await update.message.reply_text(
        f"👁 <b>Monitoring started</b>\n\n"
        f"Username: <code>@{username}</code>\n"
        f"Current status: <b>{result['status']}</b>\n\n"
        f"Checks run every 5 minutes.",
        parse_mode="HTML",
    )


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n/unwatch username")
        return

    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id

    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM monitors
        WHERE chat_id = ? AND username = ?
        """,
        (chat_id, username),
    )

    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await update.message.reply_text(
            f"🛑 Stopped monitoring <code>@{username}</code>.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"⚠️ <code>@{username}</code> is not being monitored.",
            parse_mode="HTML",
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    conn = get_db()
    rows = conn.execute(
        """
        SELECT username, last_status
        FROM monitors
        WHERE chat_id = ?
        ORDER BY id DESC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "📭 You are not monitoring any accounts."
        )
        return

    lines = ["📋 <b>Monitored Accounts</b>\n"]

    for row in rows:
        emoji = "🟢" if row["last_status"] == "ACTIVE" else "⚪"
        lines.append(
            f"{emoji} <code>@{row['username']}</code> — "
            f"<b>{row['last_status']}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, chat_id, username, last_status FROM monitors"
    ).fetchall()
    conn.close()

    for row in rows:
        monitor_id = row["id"]
        chat_id = row["chat_id"]
        username = row["username"]
        old_status = row["last_status"]

        try:
            result = await check_instagram(username)
            new_status = result["status"]

            conn = get_db()
            conn.execute(
                "UPDATE monitors SET last_status = ? WHERE id = ?",
                (new_status, monitor_id),
            )
            conn.commit()
            conn.close()

            if old_status != new_status:
                old_emoji = "🟢" if old_status == "ACTIVE" else "⚪"
                new_emoji = "🟢" if new_status == "ACTIVE" else "⚪"

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🔔 <b>Status Changed</b>\n\n"
                        f"Username: <code>@{username}</code>\n\n"
                        f"{old_emoji} Previous: <b>{old_status}</b>\n"
                        f"{new_emoji} Current: <b>{new_status}</b>\n\n"
                        f"Profile: https://www.instagram.com/{username}/"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

        except Exception:
            logger.exception("Monitoring error for %s", username)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text.strip()

    if "instagram.com/" not in text.lower() and not text.startswith("@"):
        return

    username = clean_username(text)

    if not username:
        return

    result = await check_instagram(username)

    await update.message.reply_text(
        format_result(result),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Instagram Monitor Bot is running.")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server listening on 0.0.0.0:%s", port)
    server.serve_forever()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing from .env")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("watch", watch_command))
    application.add_handler(CommandHandler("unwatch", unwatch_command))
    application.add_handler(CommandHandler("list", list_command))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    if application.job_queue:
        application.job_queue.run_repeating(
            monitor_job,
            interval=CHECK_INTERVAL,
            first=10,
        )

    logger.info("Instagram Monitor Bot started.")

    threading.Thread(target=run_health_server, daemon=True).start()

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
