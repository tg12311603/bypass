import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        me = await client.get_me()
        print("\nLogged in successfully!")
        print("Name:", me.first_name)
        print("Username:", me.username)
        print("User ID:", me.id)

        print("\n" + "=" * 60)
        print("SESSION STRING - KEEP THIS SECRET")
        print("=" * 60)
        print(client.session.save())
        print("=" * 60)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
