"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from lestash_server import __version__
from lestash_server.deps import get_db
from lestash_server.models import HealthResponse
from lestash_server.routes import (
    audible_auth,
    collections,
    embeddings,
    google_auth,
    imports,
    items,
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

    # CORS for Tauri app and browser access
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "tauri://localhost",
            "https://tauri.localhost",
            "http://tauri.localhost",  # Tauri Android WebView
            "http://localhost:1420",  # Tauri dev
            "http://localhost:5173",  # Vite dev
        ],
        allow_origin_regex=r"https?://.*\.ts\.net(:\d+)?",  # Any Tailscale domain
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
    app.include_router(audible_auth.router)
    app.include_router(google_auth.router)

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

    # Serve static frontend files with SPA fallback
    if static_dir:
        from pathlib import Path

        from fastapi.responses import FileResponse, HTMLResponse

        index_path = Path(static_dir) / "index.html"

        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str):
            """Serve static files or index.html for SPA routing."""
            file_path = Path(static_dir) / path
            if file_path.is_file():
                return FileResponse(file_path)
            return HTMLResponse(index_path.read_text())

    return app
