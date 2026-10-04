"""Development server: `python -m server [--host 0.0.0.0] [--port 8000]`.

For production use gunicorn (see the Dockerfile) -- Flask's built-in
server is single-machine dev tooling, not something to expose online.

Running this way is what "a dev run" means, so DEV_MODE defaults on here
(and only here): the dashboard then offers "Share logs" on a camera
station. Setting DEV_MODE in the environment wins either way, so
`DEV_MODE=0 python -m server` is a dev server without it.
"""
import argparse
import os

from .app import create_app
from .config import Config


def main():
    ap = argparse.ArgumentParser(description="Dog Free Couch server (development)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    cfg = Config()
    if os.environ.get("DEV_MODE") is None:
        cfg.DEV_MODE = True
    create_app(cfg).run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
