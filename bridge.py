import asyncio
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User, Chat, Channel, ChatInviteAlready, ChatInvite
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.errors import UserAlreadyParticipantError
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
TARGET_CHAT_CONFIG = (
    os.getenv("TARGET_CHAT")
    or os.getenv("TARGET_BOT")
    or "https://t.me/+1EPuo1hBdd02Njlh"
).strip()
MESSAGE_TEMPLATE = os.getenv("MESSAGE_TEMPLATE", "{url}").strip()
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
MAX_URL_LENGTH = int(os.getenv("MAX_URL_LENGTH", "2048"))
TARGET_ENTITY = None

if not SESSION:
    raise RuntimeError("SESSION is missing. Run session_generator.py first.")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")

# A URL-like pattern.
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

telethon_client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

# Serialize requests to avoid mixing up multiple concurrent requests
target_lock = asyncio.Lock()


class BypassError(Exception):
    """Raised when the target bypass service returns an explicit error."""
    pass


def check_for_error(text: str) -> str | None:
    if not text:
        return None
    err_match = re.search(r"(?:▸\s*)?Error\s*[➙:>=→:]\s*([^|\n\r⧖]+)", text, re.IGNORECASE)
    if err_match:
        return err_match.group(1).strip()

    error_keywords = [
        "not supported",
        "unsupported",
        "unable to bypass",
        "invalid url",
        "failed to bypass",
        "bypass failed",
        "could not bypass",
        "error bypassing",
        "link expired",
        "limit reached",
        "slow down",
        "flood wait",
    ]
    text_lower = text.lower()
    if any(k in text_lower for k in error_keywords):
        for line in text.splitlines():
            line_str = line.strip()
            if any(k in line_str.lower() for k in error_keywords):
                return line_str.lstrip("▸•- \t")
        return text.strip()

    return None


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


def is_candidate_url(url: str, original_url: str) -> bool:
    clean_u = clean_url(url)
    if not clean_u:
        return False
    norm_u = clean_u.rstrip("/")
    norm_orig = original_url.strip().rstrip("/")
    if norm_u == norm_orig:
        return False

    # Exclude internal telegram promos, bots, and group invite links
    blocked_patterns = [
        "t.me/dd_bypass",
        "ddxbypass_bot",
        "t.me/dd_botz",
        "1epuo1hbdd02njlh",
        "t.me/share",
        "t.me/addstickers",
        "t.me/joinchat",
        "t.me/+",
    ]
    u_lower = norm_u.lower()
    for b in blocked_patterns:
        if b in u_lower:
            return False
    return True


def extract_bypassed_url(msg_or_text, original_url: str) -> str | None:
    # 1. If a Telethon Message object was passed, inspect inline buttons first
    if hasattr(msg_or_text, "buttons") and msg_or_text.buttons:
        for row in msg_or_text.buttons:
            for btn in row:
                btn_url = getattr(btn, "url", None)
                if btn_url and is_candidate_url(btn_url, original_url):
                    return clean_url(btn_url)

    # 2. Inspect text entities (hyperlinks / text URLs)
    if hasattr(msg_or_text, "entities") and msg_or_text.entities:
        for ent in msg_or_text.entities:
            ent_url = getattr(ent, "url", None)
            if ent_url and is_candidate_url(ent_url, original_url):
                return clean_url(ent_url)

    # 3. Extract plain text
    text = msg_or_text.raw_text if hasattr(msg_or_text, "raw_text") else str(msg_or_text or "")

    # 4. Match explicit bypass label pattern (e.g. "Bypassed ➙ <url>", "Destination: <url>")
    bypassed_matches = re.findall(
        r"(?:Bypassed|bypassed|Destination|Result|Unlocked|Final|Link)[^\n\r]*?[➙:>=→]\s*(https?://[^\s<>\"']+)",
        text,
        re.IGNORECASE,
    )
    for match in reversed(bypassed_matches):
        if is_candidate_url(match, original_url):
            return clean_url(match)

    # 5. Fallback: Find all URLs in message text
    all_urls = [clean_url(x) for x in URL_RE.findall(text)]
    candidate_urls = [u for u in all_urls if is_candidate_url(u, original_url)]
    if candidate_urls:
        return candidate_urls[-1]

    return None


async def resolve_target(client: TelegramClient, target_str: str):
    target_str = target_str.strip()
    invite_match = re.search(
        r"(?:t\.me/\+|t\.me/joinchat/|tg://join\?invite=|\+)([a-zA-Z0-9_-]+)",
        target_str,
    )
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            check = await client(CheckChatInviteRequest(invite_hash))
            if isinstance(check, ChatInviteAlready):
                chat_title = getattr(check.chat, "title", str(check.chat.id))
                print(f"Target is group (already joined): {chat_title}")
                return await client.get_entity(check.chat)
            elif isinstance(check, ChatInvite):
                print(f"Target is group '{getattr(check, 'title', invite_hash)}', joining via invite...")
                updates = await client(ImportChatInviteRequest(invite_hash))
                if updates.chats:
                    return await client.get_entity(updates.chats[0])
        except UserAlreadyParticipantError:
            print("Already a participant in target group.")
        except Exception as e:
            print(f"Notice during group invite resolution: {e}")

    # Numeric chat ID
    if target_str.lstrip("-").isdigit():
        return await client.get_entity(int(target_str))

    # Username or link without '+'
    username = (
        target_str.replace("https://t.me/", "")
        .replace("http://t.me/", "")
        .lstrip("@")
        .rstrip("/")
    )
    try:
        return await client.get_entity(username)
    except Exception as exc:
        async for dialog in client.iter_dialogs():
            d_uname = getattr(dialog.entity, "username", None) or ""
            d_title = getattr(dialog.entity, "title", None) or ""
            if username.lower() in [d_uname.lower(), d_title.lower()]:
                return dialog.entity
        raise exc


async def bypass_url(url: str) -> str:
    global TARGET_ENTITY
    async with target_lock:
        if TARGET_ENTITY is None:
            TARGET_ENTITY = await resolve_target(telethon_client, TARGET_CHAT_CONFIG)
        target = TARGET_ENTITY

        is_private = isinstance(target, User)

        msg_text = (
            MESSAGE_TEMPLATE.format(url=url)
            if "{url}" in MESSAGE_TEMPLATE
            else f"{MESSAGE_TEMPLATE} {url}"
        )

        sent_msg = await telethon_client.send_message(target, msg_text)
        sent_id = sent_msg.id
        bot_reply_id = None

        deadline = asyncio.get_running_loop().time() + TIMEOUT

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Target did not reply in time.")

            messages = await telethon_client.get_messages(target, limit=35)
            # Inspect messages from newest to oldest
            for msg in messages:
                if is_private:
                    # In a 1-on-1 private chat, messages with id > sent_id are from target
                    if msg.id <= sent_id:
                        continue
                else:
                    # In a group chat, match replies to our message or replies to the bot's initial response
                    reply_id = getattr(msg, "reply_to_msg_id", None)
                    if reply_id is None and getattr(msg, "reply_to", None):
                        reply_id = getattr(msg.reply_to, "reply_to_msg_id", None)

                    is_match = (
                        (reply_id == sent_id)
                        or (bot_reply_id and reply_id == bot_reply_id)
                    )

                    if not is_match:
                        continue

                # Record the bot reply message ID
                bot_reply_id = msg.id
                text = msg.raw_text or ""

                # 1. Check for explicit error messages from the bot (e.g. "Link not supported !")
                err_desc = check_for_error(text)
                if err_desc:
                    raise BypassError(err_desc)

                # 2. Extract bypassed destination URL
                bypassed = extract_bypassed_url(msg, url)
                if bypassed:
                    return bypassed

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
    except BypassError as err:
        await status.edit_text(f"❌ Error: {err}")
    except TimeoutError:
        await status.edit_text(
            "⌛ The target did not reply within the timeout. Try again later."
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
    except BypassError as err:
        await status.edit_text(f"❌ Error: {err}")
    except TimeoutError:
        await status.edit_text("⌛ Target timed out. Please try again.")
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

    global TARGET_ENTITY
    TARGET_ENTITY = await resolve_target(telethon_client, TARGET_CHAT_CONFIG)
    target_name = (
        getattr(TARGET_ENTITY, "title", None)
        or getattr(TARGET_ENTITY, "username", None)
        or str(getattr(TARGET_ENTITY, "id", TARGET_CHAT_CONFIG))
    )
    print(f"Target resolved: {target_name}")

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
