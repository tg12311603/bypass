import asyncio
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from urllib.request import urlopen

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatMemberStatus, KeyboardButtonStyle
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "120"))
RETRY_INTERVAL = 20
CAPTCHA_RETRY_INTERVAL = 20
MAX_URL_LENGTH = int(os.getenv("MAX_URL_LENGTH", "2048"))


def format_channel_id(channel: str):
    channel = channel.strip()
    if not channel:
        return ""
    if "t.me/" in channel:
        channel = channel.split("t.me/")[-1].split("/")[0].split("?")[0]
    if channel.startswith("-100") and channel[1:].isdigit():
        return int(channel)
    if channel.lstrip("-").isdigit():
        return int(channel)
    if not channel.startswith("@"):
        channel = f"@{channel}"
    return channel


FORCE_SUB_CHANNEL_RAW = os.getenv("FORCE_SUB_CHANNEL", "@agents_Verse").strip()
FORCE_SUB_CHANNEL = (
    None
    if FORCE_SUB_CHANNEL_RAW.lower() in ("", "false", "none", "off", "0")
    else format_channel_id(FORCE_SUB_CHANNEL_RAW)
)
FORCE_SUB_CHANNEL_URL = os.getenv(
    "FORCE_SUB_CHANNEL_URL",
    f"https://t.me/{str(FORCE_SUB_CHANNEL).lstrip('@')}"
    if str(FORCE_SUB_CHANNEL).startswith("@")
    else "https://t.me/agents_Verse",
).strip()

FORCE_SUB_MESSAGE = (
    "⚠️ You must join our channel to use this bot!\n\n"
    "Please click 'Join Channel' below, join the channel, and then click 'Verify'."
)


def get_force_sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=FORCE_SUB_CHANNEL_URL,
                    style=KeyboardButtonStyle.PRIMARY,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Verify",
                    callback_data="verify_channel",
                    style=KeyboardButtonStyle.SUCCESS,
                )
            ],
        ]
    )


async def check_user_membership(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    if not FORCE_SUB_CHANNEL:
        return True

    try:
        member = await context.bot.get_chat_member(
            chat_id=FORCE_SUB_CHANNEL, user_id=user_id
        )
        if member.status in {
            ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        }:
            return True
        if member.status == ChatMemberStatus.RESTRICTED and getattr(
            member, "is_member", False
        ):
            return True

        return False
    except BadRequest as e:
        err_msg = str(e).lower()
        if any(term in err_msg for term in ["participant", "user not found"]):
            return False
        if "chat admin required" in err_msg or "not enough rights" in err_msg:
            print(
                f"[WARNING] Bot must be an administrator in {FORCE_SUB_CHANNEL} to check user membership!"
            )
            return False
        print(f"Error checking channel membership for user {user_id}: {e}")
        return False
    except Exception as e:
        print(f"Unexpected error checking channel membership for user {user_id}: {e}")
        return False


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not update.effective_user:
        return

    user_id = update.effective_user.id
    is_member = await check_user_membership(user_id, context)

    if is_member:
        await query.answer("✅ Verified! Welcome aboard.", show_alert=False)
        try:
            await query.edit_message_text(
                "✅ Verification successful!\n\n"
                "👋 Send me a short URL and I'll try to process it.\n\n"
                "Example:\nhttps://example.com/short-link"
            )
        except Exception as e:
            print(f"Failed to edit message after verification: {e}")
    else:
        await query.answer(
            "❌ You have not joined the channel yet!\n\n"
            "Please join the channel and click Verify again.",
            show_alert=True,
        )

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


class BypassError(Exception):
    """Raised when the target bypass bot returns an explicit error."""
    pass


def is_captcha_challenge(text: str) -> bool:
    if not text:
        return False
    keywords = [
        "captcha",
        "solve the",
        "verify",
        "verification",
        "recaptcha",
        "turnstile",
        "human verification",
        "please verify",
    ]
    text_lower = text.lower()
    return any(k in text_lower for k in keywords)


def check_for_error(text: str) -> str | None:
    if not text or is_captcha_challenge(text):
        return None

    # Check for explicit error label like "Error : ...", "❌ Error: ...", "⚠️ Warning: ..."
    err_match = re.search(r"(?:▸\s*|[❌⚠️]\s*)?(?:Error|Warning)\s*[:➙>=→]\s*([^\n\r]+)", text, re.IGNORECASE)
    if err_match:
        return err_match.group(1).strip().lstrip("❌⚠️: ")

    error_keywords = [
        "no script found",
        "script not found",
        "no bypass found",
        "not supported",
        "unsupported",
        "unable to bypass",
        "cannot bypass",
        "invalid url",
        "invalid link",
        "failed to bypass",
        "bypass failed",
        "could not bypass",
        "error bypassing",
        "link expired",
        "limit reached",
        "slow down",
        "flood wait",
    ]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line_str in enumerate(lines):
        if any(k in line_str.lower() for k in error_keywords):
            clean_err = line_str.lstrip("▸•-❌⚠️ \t")
            # If the error line ends with ':' and the next line has details (e.g. the url), include it
            if clean_err.endswith(":") and i + 1 < len(lines):
                next_line = lines[i + 1]
                if not next_line.lower().startswith("powered by") and not next_line.startswith("|"):
                    return f"{clean_err} {next_line}"
            return clean_err

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


def extract_bypassed_url(text: str, original_url: str) -> str | None:
    norm_orig = original_url.strip().rstrip("/")
    bot_lower = TARGET_BOT.lower().lstrip("@")

    # 1. Match explicit "Bypassed Link :✅ <url>", "Bypassed ➙ <url>", "Destination: <url>", etc.
    bypassed_matches = re.findall(
        r"(?:Bypassed(?:\s*Link)?|Destination|Result|Unlocked|Final)[^\n\r]*?(https?://[^\s<>\"']+)",
        text,
        re.IGNORECASE,
    )
    for match in reversed(bypassed_matches):
        cleaned = clean_url(match)
        if cleaned.rstrip("/") != norm_orig and f"t.me/{bot_lower}" not in cleaned.lower():
            return cleaned

    # 2. Fallback: Find all URLs in response and exclude the original URL and target bot links
    all_urls = [clean_url(x) for x in URL_RE.findall(text)]
    candidate_urls = [
        u
        for u in all_urls
        if u.rstrip("/") != norm_orig
        and f"t.me/{bot_lower}" not in u.lower()
        and f"@{bot_lower}" not in u.lower()
    ]
    if candidate_urls:
        return candidate_urls[-1]

    return None


async def bypass_url(url: str, on_status_update=None) -> str:
    async with target_lock:
        target = await telethon_client.get_entity(TARGET_BOT)

        # Mark the current end of the conversation so we only inspect
        # messages arriving after our request.
        before_id = 0
        recent = await telethon_client.get_messages(target, limit=1)
        if recent:
            before_id = recent[0].id

        await telethon_client.send_message(target, url)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        last_resend_time = loop.time()
        captcha_active = False

        while True:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError("We are unable to bypass link, try again later.")

            messages = await telethon_client.get_messages(target, limit=15)
            # Inspect messages from newest to oldest
            for msg in messages:
                if msg.id <= before_id:
                    continue
                text = msg.raw_text or ""
                if not text:
                    continue

                # 1. Try to extract bypassed destination URL
                bypassed = extract_bypassed_url(text, url)
                if bypassed:
                    return bypassed

                # 2. Check for explicit error messages from the bot first (e.g. "No Script Found")
                err = check_for_error(text)
                if err:
                    raise BypassError(err)

                # 3. Detect Captcha challenge
                if is_captcha_challenge(text):
                    if not captcha_active:
                        captcha_active = True
                        deadline = max(deadline, now + CAPTCHA_TIMEOUT)
                        if on_status_update:
                            await on_status_update(
                                "⏳ Please wait, processing..."
                            )

            # Check if any incoming message from target has arrived after before_id
            bot_replied = any(
                msg.id > before_id and not getattr(msg, "out", False)
                for msg in messages
            )

            # Resend every 20 seconds:
            # - While waiting on captcha resolution
            # - Or if the bot hasn't replied at all
            if (captcha_active or not bot_replied) and (now - last_resend_time >= RETRY_INTERVAL):
                await telethon_client.send_message(target, url)
                last_resend_time = now

            await asyncio.sleep(1.0)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    is_member = await check_user_membership(update.effective_user.id, context)
    if not is_member:
        await update.message.reply_text(
            "👋 Welcome!\n\n" + FORCE_SUB_MESSAGE,
            reply_markup=get_force_sub_keyboard(),
        )
        return

    await update.message.reply_text(
        "👋 Send me a short URL and I'll try to process it.\n\n"
        "Example:\nhttps://example.com/short-link"
    )


async def bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    is_member = await check_user_membership(update.effective_user.id, context)
    if not is_member:
        await update.message.reply_text(
            FORCE_SUB_MESSAGE,
            reply_markup=get_force_sub_keyboard(),
        )
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

    async def update_status(msg: str):
        try:
            await status.edit_text(msg)
        except Exception:
            pass

    try:
        result = await bypass_url(url, on_status_update=update_status)
        await status.edit_text(f"✅ Result:\n{result}")
    except BypassError as err:
        await status.edit_text(f"❌ Error: {err}")
    except TimeoutError as err:
        await status.edit_text(f"⌛ {err}")
    except Exception as exc:
        print("Bridge error:", repr(exc))
        await status.edit_text(
            "❌ Unable to process this URL right now. Check the terminal logs."
        )


async def plain_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    is_member = await check_user_membership(update.effective_user.id, context)
    if not is_member:
        await update.message.reply_text(
            FORCE_SUB_MESSAGE,
            reply_markup=get_force_sub_keyboard(),
        )
        return

    url = (update.message.text or "").strip()

    if not validate_url(url):
        await update.message.reply_text(
            "Please send a valid http:// or https:// URL."
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    status = await update.message.reply_text("⏳ Processing...")

    async def update_status(msg: str):
        try:
            await status.edit_text(msg)
        except Exception:
            pass

    try:
        result = await bypass_url(url, on_status_update=update_status)
        await status.edit_text(f"✅ Result:\n{result}")
    except BypassError as err:
        await status.edit_text(f"❌ Error: {err}")
    except TimeoutError as err:
        await status.edit_text(f"⌛ {err}")
    except Exception as exc:
        print("Bridge error:", repr(exc))
        await status.edit_text("❌ Processing failed. Check the terminal logs.")


async def self_ping_loop():
    """Ping our own health endpoint every 13 minutes to prevent Render spin-down."""
    # Render sets RENDER_EXTERNAL_URL automatically; fall back to localhost.
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    port_str = os.getenv("PORT", "")
    if render_url:
        ping_url = f"{render_url}/"
    elif port_str:
        ping_url = f"http://localhost:{port_str}/"
    else:
        print("Self-ping: no URL available, skipping.")
        return

    print(f"Self-ping enabled → {ping_url} (every 13 min)")
    while True:
        await asyncio.sleep(13 * 60)  # wait 13 minutes
        try:
            urlopen(ping_url, timeout=10)
            print("Self-ping: OK")
        except Exception as e:
            print(f"Self-ping: failed ({e})")


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
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_channel$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_url))

    print("Bot is running. Press Ctrl+C to stop.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Start self-ping in background
    asyncio.ensure_future(self_ping_loop())

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
