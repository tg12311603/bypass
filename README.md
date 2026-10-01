# Telegram Bypass Wrapper

A Telegram bot wrapper that receives a URL from your users, sends it through a logged-in Telegram user account using Telethon to `@DDxBypass_Bot`, waits for the reply, extracts a URL, and returns it to the user.

## Important
This project requires you to have legitimate access to the target bot and to follow Telegram's terms and the target bot's rules. Do not use it for spam, flooding, or unauthorized access.

## Setup

1. Create a Telegram API application at https://my.telegram.org/apps
2. Put your API ID and API hash in `.env`.
3. Put your own Telegram bot token in `.env`.
4. Set `TARGET_CHAT` in `.env` to your target group invite link, group username, or bot username (e.g. `https://t.me/+1EPuo1hBdd02Njlh`).
5. (Optional) Set `MESSAGE_TEMPLATE={url}` or `MESSAGE_TEMPLATE=/bypass {url}` depending on what format the target expects.
6. Generate a Telethon session:
   ```bash
   python3 session_generator.py
   ```
   Copy the printed session string into `.env` as `SESSION`.
7. Start:
   ```bash
   python3 bridge.py
   ```

## First run

The session generator asks for your phone number, Telegram login code, and 2FA password if enabled.

Never publish `.env`, the API hash, bot token, or session string.

## Architecture

User -> Your Bot -> Telethon user session -> Target group/bot -> Telethon response (reply-matched) -> Your Bot -> User

The bridge uses a per-request lock and reply-matching so concurrent group chat messages do not get mixed up.
