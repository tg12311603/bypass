# Telegram Bypass Wrapper

A Telegram bot wrapper that receives a URL from your users, sends it through a logged-in Telegram user account using Telethon to `@DDxBypass_Bot`, waits for the reply, extracts a URL, and returns it to the user.

## Important
This project requires you to have legitimate access to the target bot and to follow Telegram's terms and the target bot's rules. Do not use it for spam, flooding, or unauthorized access.

## Setup

1. Create a Telegram API application at https://my.telegram.org/apps
2. Put your API ID and API hash in `.env`.
3. Put your own Telegram bot token in `.env`.
4. Set `TARGET_BOT=DDxBypass_Bot` (or another bot you are authorized to use).
5. Generate a Telethon session:
   ```bash
   python3 session_generator.py
   ```
   Copy the printed session string into `.env` as `SESSION`.
6. Start:
   ```bash
   python3 bridge.py
   ```

## First run

The session generator asks for your phone number, Telegram login code, and 2FA password if enabled.

Never publish `.env`, the API hash, bot token, or session string.

## Architecture

User -> Your Bot -> Telethon user session -> Target bot -> Telethon response -> Your Bot -> User

The bridge uses a per-request lock/queue so concurrent requests do not get mixed up.
