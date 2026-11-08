"""FastAPI application factory."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from lestash_server import __version__
from lestash_server.deps import get_db
from lestash_server.models import HealthResponse
from lestash_server.routes import (
    audible_auth,
    collections,
    embeddings,
    enrich,
    google_auth,
    imports,
    items,
    media,
    profiles,
    sources,
    stats,
    voice,
)


def create_app(static_dir: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        static_dir: Directory to serve frontend files from. Falls back to
            LESTASH_STATIC_DIR env var if not provided.
    """
    import os

    if static_dir is None:
        static_dir = os.environ.get("LESTASH_STATIC_DIR") or None

    app = FastAPI(
        title="LeStash API",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # CORS for Tauri app, browser access, and Chrome extension
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "tauri://localhost",
            "https://tauri.localhost",
            "http://tauri.localhost",  # Tauri Android WebView
            "http://localhost:1420",  # Tauri dev
            "http://localhost:5173",  # Vite dev
        ],
        allow_origin_regex=r"(https?://.*\.ts\.net(:\d+)?|chrome-extension://.+)",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register route modules
    app.include_router(items.router)
    app.include_router(sources.router)
    app.include_router(profiles.router)
    app.include_router(stats.router)
    app.include_router(imports.router)
    app.include_router(voice.router)
    app.include_router(collections.router)
    app.include_router(embeddings.router)
    app.include_router(media.router)
    app.include_router(enrich.router)
    app.include_router(audible_auth.router)
    app.include_router(google_auth.router)

    # Optional LinkedIn routes (only if lestash-linkedin is installed)
    try:
        from lestash_server.routes import linkedin

        app.include_router(linkedin.router)
    except ImportError:
        pass

    # Optional micro.blog routes (only if lestash-microblog is installed)
    try:
        from lestash_server.routes import microblog

        app.include_router(microblog.router)
    except ImportError:
        pass

    # Optional YouTube routes (only if lestash-youtube is installed)
    try:
        from lestash_server.routes import youtube

        app.include_router(youtube.router)
    except ImportError:
        pass

    @app.get("/api/health", response_model=HealthResponse)
    def health():
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return HealthResponse(version=__version__, items=count)

    # Serve the frontend: static files, and index.html for client-side routes. This runs
    # as a 404 handler rather than a catch-all route, so routing is untouched: a known API
    # path called with the wrong method stays a 405, and an unknown /api path stays a JSON
    # 404 for every method, never HTTP 200 with the app page.
    if static_dir:
        from pathlib import Path

        from fastapi.exception_handlers import http_exception_handler
        from fastapi.responses import FileResponse
        from starlette.exceptions import HTTPException as StarletteHTTPException

        static_root = Path(static_dir).resolve()
        index_path = static_root / "index.html"

        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.exception_handler(StarletteHTTPException)
        async def spa_fallback(request: Request, exc: StarletteHTTPException):
            path = request.url.path.lstrip("/")  # "//api/x" is still an API path
            is_api = path == "api" or path.startswith("api/")
            if exc.status_code != 404 or is_api or request.method not in ("GET", "HEAD"):
                return await http_exception_handler(request, exc)
            # Resolve first: a decoded "../" must not reach outside the static folder
            file_path = (static_root / path).resolve()
            if file_path.is_relative_to(static_root) and file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(index_path, media_type="text/html")

    return app
