"""FastAPI control-plane entrypoint — router assembly + middleware.

Serves BOTH the API and the built React portal from a single container: routers
under the API prefix, and the SPA's static assets (with client-side-routing
fallback) at the root. Because the frontend is same-origin, no CORS is needed in
the single-container deployment; the local-dev allowance stays for `vite dev` on
a separate port. Health endpoint is unauthenticated for Container Apps probes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import budgets, keys, login, routes, tenants, usage, users
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bootstrap: create tables + seed the admin user before serving traffic.

    Kept out of business endpoints; idempotent so restarts/scale-out are safe.
    """
    from app.init_db import init_db

    init_db()
    yield


app = FastAPI(
    title="Token Foundry — Control Plane",
    version="0.1.0",
    description="Azure-native LLM token hub: tenants, virtual keys, model routes, usage.",
    lifespan=lifespan,
)

# Local dev runs `vite dev` on :5173 (cross-origin to :8000), so allow CORS
# there. In the single-container cloud deployment the SPA is same-origin and
# this is a no-op (empty allowlist).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"] if settings.is_local else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "token-foundry"}


_prefix = settings.api_prefix
app.include_router(login.router, prefix=_prefix, tags=["auth"])
app.include_router(users.router, prefix=_prefix, tags=["users"])
app.include_router(tenants.router, prefix=_prefix, tags=["tenants"])
app.include_router(keys.router, prefix=_prefix, tags=["keys"])
app.include_router(routes.router, prefix=_prefix, tags=["routes"])
app.include_router(budgets.router, prefix=_prefix, tags=["budgets"])
app.include_router(usage.router, prefix=_prefix, tags=["usage"])


# --- Serve the built React portal (single-container deployment) ---
# The Docker build copies portal/dist -> ./static. When present, mount it and
# fall back to index.html for client-side routes. Absent locally (api-only run),
# the API still works on its own.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=_STATIC_DIR / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """Serve a static file if it exists, else index.html (SPA routing)."""
        candidate = _STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_STATIC_DIR / "index.html")
