import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

async def main():
    phone = input("Enter your phone number (with country code, e.g. +12345678900): ").strip()
    if not phone.startswith("+"):
        if len(phone) == 10 and phone.isdigit():
            phone = "+91" + phone
        else:
            phone = "+" + phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("Sending login code to your Telegram account...")
        sent_code = await client.send_code_request(phone)
        code = input("Enter the login code you received in Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
        except Exception as e:
            if "SessionPasswordNeeded" in type(e).__name__:
                password = input("Enter your 2-step verification (2FA) password: ").strip()
                await client.sign_in(password=password)
            else:
                raise e

    me = await client.get_me()
    print("\nLogged in successfully!")
    print("Name:", me.first_name)
    print("Username:", me.username)
    print("User ID:", me.id)

    print("\n" + "=" * 60)
    print("SESSION STRING - COPY AND PASTE INTO .env")
    print("=" * 60)
    print(client.session.save())
    print("=" * 60)
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
