"""Liveness endpoint.

Ported from the org's fastapi-boilerplate. Only the basic (dependency-free)
variant is included — the boilerplate's `/detailed` check pings a SQL
database, which this app doesn't have.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    """Return a static healthy status if the process can handle requests.

    Liveness probe: 200 with `{"status": "healthy"}`, no external
    dependencies are checked.
    """
    return {"status": "healthy"}
