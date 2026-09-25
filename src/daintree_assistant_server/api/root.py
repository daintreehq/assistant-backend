"""Root redirect: a human who visits the API hostname lands on the product page."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()

# Hardcoded like every other product decision here (CLAUDE.md, "Config & readiness"):
# where a human visitor goes is not something an operator should be able to retarget.
MARKETING_URL = "https://daintree.org/assistant"


# HEAD is listed explicitly: a FastAPI APIRoute registers only the methods named
# here, so a bare @router.get would answer HEAD / with 405. Link previewers and
# uptime monitors do send HEAD, and a redirect is a better answer than a refusal.
@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root() -> RedirectResponse:
    """Send a browser that reached the API origin to the product page.

    Scoped to ``/`` alone: this same origin serves the MCP endpoint, and root is
    the one path with no API meaning.

    302 rather than 301 because browsers cache a permanent redirect for a long
    time and honour it without re-asking, which is difficult to walk back if root
    ever needs to serve something itself.
    """
    return RedirectResponse(url=MARKETING_URL, status_code=302)
