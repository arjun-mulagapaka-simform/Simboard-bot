import asyncio

from dotenv import load_dotenv

load_dotenv()

from bot import app  # noqa: E402 (must load .env before importing bot)


async def main() -> None:
    await app.start(port=3978)


if __name__ == "__main__":
    asyncio.run(main())
