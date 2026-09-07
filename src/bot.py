"""Teams bot adapter.

Detects @mentions and delegates to the Phase A pipeline (src/workflow.py)
for extraction/resolution/card creation.
"""

import os

from fastapi import FastAPI
from microsoft_teams.api import MessageActivity
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.apps.http import FastAPIAdapter

from src import workflow
from src.core.exceptions import setup_exception_handlers
from src.core.health import router as health_router
from src.core.logging import setup_logging
from src.core.middleware import setup_middleware

setup_logging()

# Build our own FastAPI instance (rather than letting FastAPIAdapter() default
# to a bare one) so we can register the org's logging/security middleware,
# typed-exception handlers, and a /health route before handing it to the
# Teams SDK's adapter.
_fastapi_app = FastAPI()
setup_middleware(_fastapi_app)
setup_exception_handlers(_fastapi_app)
_fastapi_app.include_router(health_router)

# client_id/client_secret/tenant_id are NOT auto-read from the environment by
# App() — only DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS, SERVICE_URL, and
# CLOUD are. When real credentials are present in .env, pass them explicitly;
# otherwise fall back to the anonymous-testing path (Emulator, Phase 1).
app = App(
    client_id=os.getenv("MICROSOFT_APP_ID"),
    client_secret=os.getenv("MICROSOFT_APP_PASSWORD"),
    tenant_id=os.getenv("MICROSOFT_APP_TENANT_ID"),
    http_server_adapter=FastAPIAdapter(app=_fastapi_app),
)


@app.on_message
async def on_message(ctx: ActivityContext[MessageActivity]) -> None:
    """Handle every inbound Teams message activity.

    Acts only when the bot was directly @mentioned, per the design (see
    ../demo-scope.md).

    Registered as the `microsoft_teams.apps` message handler via the
    `@app.on_message` decorator, so it's invoked by the SDK for every
    "message"-type activity the bot receives — not called directly
    elsewhere in this codebase.

    Args:
        ctx: SDK-provided activity context wrapping the inbound
            `MessageActivity` plus helpers (`ctx.send`, `ctx.reply`) for
            replying on the same conversation.

    Returns:
        None. All output happens as a side effect (see below); there's no
        return value the SDK inspects.

    Side effects:
        If the bot was not @mentioned, does nothing and returns early.
        Otherwise delegates to `workflow.handle(ctx)`, which runs the
        extraction/resolution/card-creation pipeline and sends the
        resulting text back on the same conversation itself (via
        `ctx.send`, not `ctx.reply` — `ctx.reply()` prepends a Teams-only
        "quoted message" placeholder that the Bot Framework Emulator's
        chat window doesn't render).
    """
    activity = ctx.activity

    if not activity.is_recipient_mentioned():
        # Not mentioned — per the design, the bot should only act when
        # directly @mentioned. Ignore everything else.
        return

    await workflow.handle(ctx)
