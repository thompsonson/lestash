"""CLI entry point for the LeStash API server."""

import os
from pathlib import Path

import uvicorn


def _env_or(key: str, default: str) -> str:
    return os.environ.get(key, default)


def main() -> None:
    """Start the LeStash API server."""
    import argparse

    home = Path.home()
    default_cert = home / ".config/tailscale-certs/pop-mini.monkey-ladon.ts.net.crt"
    default_key = home / ".config/tailscale-certs/pop-mini.monkey-ladon.ts.net.key"

    parser = argparse.ArgumentParser(description="LeStash API Server")
    parser.add_argument("--host", default=_env_or("LESTASH_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(_env_or("LESTASH_PORT", "8444")))
    parser.add_argument("--cert", default=_env_or("LESTASH_TLS_CERT", str(default_cert)))
    parser.add_argument("--key", default=_env_or("LESTASH_TLS_KEY", str(default_key)))
    parser.add_argument(
        "--static-dir",
        default=_env_or("LESTASH_STATIC_DIR", ""),
        help="Directory to serve static files from",
    )
    args = parser.parse_args()

    # Auto-detect frontend directory if not specified
    static_dir = args.static_dir
    if not static_dir:
        # Look for app/src/ relative to working directory
        candidate = Path.cwd() / "app" / "src"
        if candidate.is_dir() and (candidate / "index.html").exists():
            static_dir = str(candidate)

    if static_dir:
        os.environ["LESTASH_STATIC_DIR"] = static_dir

    cert_path = Path(args.cert)
    key_path = Path(args.key)
    has_tls = cert_path.is_file() and key_path.is_file() and cert_path.stat().st_size > 0

    if has_tls:
        print(f"Starting LeStash API on https://{args.host}:{args.port}")
        uvicorn.run(
            "lestash_server.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            ssl_certfile=args.cert,
            ssl_keyfile=args.key,
            log_level="info",
        )
    else:
        print(f"No TLS certs found, starting on http://{args.host}:{args.port}")
        uvicorn.run(
            "lestash_server.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level="info",
        )


if __name__ == "__main__":
    main()
