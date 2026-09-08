"""Process entrypoint — loads env vars, then starts the Teams app server."""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from src.bot import app  # noqa: E402 (must load .env before importing bot)


async def main() -> None:
    """Start the Teams app's HTTP server and block until it stops."""
    await app.start(port=3978)


if __name__ == "__main__":
    asyncio.run(main())
