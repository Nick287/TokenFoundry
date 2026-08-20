"""FastAPI control-plane entrypoint — router assembly + middleware.

Serves BOTH the API and the built React portal from a single container: routers
under the API prefix, and the SPA's static assets (with client-side-routing
fallback) at the root. Because the frontend is same-origin, no CORS is needed in
the single-container deployment; the local-dev allowance stays for `vite dev` on
a separate port. Health endpoint is unauthenticated for Container Apps probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    budgets,
    deploy_config,
    github_accounts,
    keys,
    login,
    routes,
    tenants,
    usage,
    users,
)
from app.config import get_settings

settings = get_settings()

# Configure the ROOT logger before anything else logs. Nothing in this app did
# so previously, which meant Python's default WARNING level silently swallowed
# every `logger.info` the service emits — the usage importer's per-run counters,
# the re-login trail, the device-flow field names. Debugging then relies on
# guessing from an empty log, and absence of a log line gets misread as absence
# of the behaviour.
#
# `force=True` matters under uvicorn: it installs its own handlers on import, so
# a plain basicConfig() would be a no-op and this would silently do nothing.
#
# Azure SDK loggers are pinned to WARNING regardless: at INFO they emit a block
# per HTTP request (every Cosmos query, every Key Vault read), which would drown
# the lines above in traffic nobody reads.
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    force=True,
)
for _noisy in (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "urllib3",
    # httpx logs a line per request at INFO. Observed on dev-15: provisioning one
    # account emitted dozens of ARM URLs, burying the four lines that actually
    # describe what happened. The URLs also carry subscription and resource ids
    # into the log, which is needless exposure for a line nobody reads.
    "httpx",
    "httpcore",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


class _DropHealthProbeAccessLogs(logging.Filter):
    """Silence uvicorn's access line for the container's own health probes.

    Container Apps polls /healthz every few seconds forever, and uvicorn logs one
    access line per poll. Measured on dev-19 over 7 days: 18,442 of 22,492
    control-plane log lines were health probes — 82% of everything this service
    logged, and roughly half of the whole workspace's ingestion. It is a fixed
    cost that does not scale with traffic, so it only gets more dominant the
    quieter the environment is.

    Nothing is lost by dropping it. Whether the probe succeeds is already
    Container Apps' own health state, and a failing container shows up as a
    revision that will not go Running — not as a missing log line. Errors,
    warnings and every real API request still log normally.

    Filters only the ACCESS logger, so a genuine exception raised while serving
    /healthz still surfaces through the error logger.
    """

    _PROBE_PATHS = ("/healthz", "/readyz", "/livez")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access formats as: '%s - "%s %s HTTP/%s" %d' with args
        # (client, method, path, http_version, status). Read the path from args
        # rather than the rendered message: getMessage() formats the whole line
        # on every probe, which is the work being avoided.
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and path in self._PROBE_PATHS:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_DropHealthProbeAccessLogs())

logger = logging.getLogger(__name__)


async def _usage_import_loop() -> None:
    """Drain Event Hub Capture blobs into Cosmos, forever, on a timer.

    The interval mirrors Capture's own flush interval: running faster only
    re-lists the same blobs. The importer is synchronous (the Blob and Cosmos
    SDKs are), so each pass goes to a worker thread — a slow import must not
    stall the event loop serving the portal.

    `run_once` swallows its own failures, so this loop only has to survive
    cancellation. It is safe to have several replicas running it: imports are
    idempotent upserts keyed on the request id.
    """
    from app.services.usage_capture_import import UsageCaptureImporter

    importer = UsageCaptureImporter()
    if not importer.configured:
        logger.info("usage-import: no capture storage configured; importer disabled")
        return

    interval = max(settings.usage_capture_interval_seconds, 60)
    # Stagger the first pass: on a rolling deploy every replica would otherwise
    # start listing the same container in the same second.
    await asyncio.sleep(interval)
    while True:
        await asyncio.to_thread(importer.run_once)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bootstrap: create tables + seed the admin user before serving traffic.

    Kept out of business endpoints; idempotent so restarts/scale-out are safe.
    """
    from app.init_db import init_db

    init_db()
    importer = asyncio.create_task(_usage_import_loop())
    try:
        yield
    finally:
        importer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await importer


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
app.include_router(github_accounts.router, prefix=_prefix, tags=["github-accounts"])
app.include_router(deploy_config.router, prefix=_prefix, tags=["deploy-config"])
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
