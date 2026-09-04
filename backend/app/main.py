"""MrListener API, and in production the web app itself.

In development the frontend is served by Vite on 5173 and proxies /api and /ws
here. In production (``npm run build`` then ``npm start``) there is only this
process: uvicorn on 8000 serves the built ``frontend/dist`` alongside the API,
which is why the SPA fallback below exists - the router owns /meetings/... and
/settings, and a hard refresh on one of those must return index.html rather
than a 404.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .config import APP_VERSION, CORS_ORIGINS, FRONTEND_DIST, ensure_dirs
from .db import init_db
from .routers import health, meetings, record, settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mrlistener")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    # Nothing in the pipeline survives a restart; clear any stranded rows so
    # the UI is not left polling a step that will never run.
    pipeline.recover_interrupted()
    yield


app = FastAPI(title="MrListener API", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The audio player needs these to seek across origins in dev.
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.include_router(health.router)
app.include_router(meetings.router)
app.include_router(settings.router)
app.include_router(record.router)


# --------------------------------------------------------------------------
# Production: serve the built frontend from this same process
# --------------------------------------------------------------------------

INDEX_HTML = FRONTEND_DIST / "index.html"
SERVE_UI = INDEX_HTML.is_file()

if SERVE_UI:
    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        # Hashed filenames, so they are safe to cache hard.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    log.info("serving the built frontend from %s", FRONTEND_DIST)


@app.get("/", include_in_schema=False)
def index():
    if SERVE_UI:
        return FileResponse(INDEX_HTML, media_type="text/html")
    return {"name": "MrListener API", "docs": "/docs", "health": "/api/health"}


if SERVE_UI:

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """Any non-API path returns index.html so the SPA router can take over.

        Registered last, so every real route above wins. Real files in dist
        (favicon, manifest, ...) are served from disk; /api and /ws never reach
        here because their routers matched first, but an unknown /api path must
        still 404 as JSON rather than pretending to be a page.
        """
        if full_path.startswith(("api/", "ws/", "docs", "openapi.json", "redoc")):
            raise HTTPException(status_code=404, detail="Not found")

        candidate = (FRONTEND_DIST / full_path).resolve()
        if candidate.is_file() and str(candidate).startswith(str(FRONTEND_DIST.resolve())):
            return FileResponse(candidate)
        return FileResponse(INDEX_HTML, media_type="text/html")
