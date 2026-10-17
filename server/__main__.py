"""Development server: `python -m server [--host 0.0.0.0] [--port 8000]`.

For production use gunicorn (see the Dockerfile) -- Flask's built-in
server is single-machine dev tooling, not something to expose online.
"""
import argparse
import os
from pathlib import Path

from .app import create_app


def load_dotenv(path: Path) -> None:
    """Reads KEY=value lines from a .env file into the environment. Dev
    convenience only; a variable already set in the environment wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        if value:
            os.environ.setdefault(key.strip(), value)


def main():
    ap = argparse.ArgumentParser(description="Dog Free Couch server (development)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    create_app().run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
