import asyncio
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(f"Health check HTTP server listening on port {port}")
    except Exception as e:
        print(f"Failed to start health server: {e}")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ.get("SESSION", "").strip()
BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_BOT = os.getenv("TARGET_BOT", "DDxBypass_Bot").lstrip("@")
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_URL_LENGTH = int(os.getenv("MAX_URL_LENGTH", "2048"))

if not SESSION:
    raise RuntimeError("SESSION is missing. Run session_generator.py first.")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

# A URL-like pattern. We deliberately return the first URL in the target
# bot's response instead of trying to interpret arbitrary commands.
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

telethon_client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# Serialize requests. This avoids accidentally matching another user's
# response when the target bot does not provide a request ID/correlation ID.
target_lock = asyncio.Lock()


def clean_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}>")


def validate_url(value: str) -> bool:
    if not value or len(value) > MAX_URL_LENGTH:
        return False
    try:
        p = urlparse(value)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


def extract_bypassed_url(text: str, original_url: str) -> str | None:
    # 1. Match explicit "Bypassed ➙ <url>" or "Destination / Result" pattern
    # Handles single hop and multi-hop (↻ x1, ↻ x2) by taking the final bypassed URL
    bypassed_matches = re.findall(
        r"(?:Bypassed|bypassed|Destination|Result|Unlocked|Final)[^\n\r]*?[➙:>=→]\s*(https?://[^\s<>\"']+)",
        text,
        re.IGNORECASE,
    )
    if bypassed_matches:
        return clean_url(bypassed_matches[-1])

    # 2. Fallback: Find all URLs in response and exclude the original URL and promo/bot links
    all_urls = [clean_url(x) for x in URL_RE.findall(text)]
    norm_orig = original_url.strip().rstrip("/")
    candidate_urls = [
        u
        for u in all_urls
        if u.rstrip("/") != norm_orig
        and "t.me/DD_Bypass" not in u
        and "DDxBypass_Bot" not in u
    ]
    if candidate_urls:
        return candidate_urls[-1]

    return None


async def bypass_url(url: str) -> str:
    async with target_lock:
        target = await telethon_client.get_entity(TARGET_BOT)

        # Mark the current end of the conversation so we only inspect
        # messages arriving after our request.
        before_id = 0
        recent = await telethon_client.get_messages(target, limit=1)
        if recent:
            before_id = recent[0].id

        await telethon_client.send_message(target, url)

        deadline = asyncio.get_running_loop().time() + TIMEOUT

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Target bot did not reply in time.")

            messages = await telethon_client.get_messages(target, limit=10)
            # get_messages returns newest first.
            for msg in reversed(messages):
                if msg.id <= before_id:
                    continue
                text = msg.raw_text or ""
                if not text:
                    continue

                bypassed = extract_bypassed_url(text, url)
                if bypassed:
                    return bypassed

                # If the target bot sent an explicit failure or unsupported message
                if any(keyword in text.lower() for keyword in ["unable to bypass", "invalid url", "unsupported", "failed"]):
                    return text.strip()

            await asyncio.sleep(0.75)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a short URL and I'll try to process it.\n\n"
        "Example:\nhttps://example.com/short-link"
    )


async def bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (update.message.text or "").strip()

    if not text:
        await update.message.reply_text("Send a URL after /bypass.")
        return

    parts = text.split(maxsplit=1)
    url = parts[1].strip() if len(parts) == 2 else ""

    if not validate_url(url):
        await update.message.reply_text(
            "❌ Please use a valid http:// or https:// URL.\n"
            "Example: /bypass https://example.com/link"
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    status = await update.message.reply_text("⏳ Processing...")

    try:
        result = await bypass_url(url)
        await status.edit_text(f"✅ Result:\n{result}")
    except TimeoutError:
        await status.edit_text(
            "⌛ The target bot did not reply within the timeout. Try again later."
        )
    except Exception as exc:
        print("Bridge error:", repr(exc))
        await status.edit_text(
            "❌ Unable to process this URL right now. Check the terminal logs."
        )


async def plain_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    url = (update.message.text or "").strip()

    if not validate_url(url):
        await update.message.reply_text(
            "Please send a valid http:// or https:// URL."
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    status = await update.message.reply_text("⏳ Processing...")

    try:
        result = await bypass_url(url)
        await status.edit_text(f"✅ Result:\n{result}")
    except TimeoutError:
        await status.edit_text("⌛ Target bot timed out. Please try again.")
    except Exception as exc:
        print("Bridge error:", repr(exc))
        await status.edit_text("❌ Processing failed. Check the terminal logs.")


async def main():
    start_health_server()
    print("Connecting Telethon user session...")
    await telethon_client.connect()
    if not await telethon_client.is_user_authorized():
        raise RuntimeError("Invalid or expired Telethon session string.")
    me = await telethon_client.get_me()
    print(f"Telethon logged in as @{me.username or me.id}")

    target = await telethon_client.get_entity(TARGET_BOT)
    print(f"Target bot: @{getattr(target, 'username', TARGET_BOT)}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bypass", bypass))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_url))

    print("Bot is running. Press Ctrl+C to stop.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await telethon_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
