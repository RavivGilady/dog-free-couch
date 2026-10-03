"""Development server: `python -m server [--host 0.0.0.0] [--port 8000]`.

For production use gunicorn (see the Dockerfile) -- Flask's built-in
server is single-machine dev tooling, not something to expose online.
"""
import argparse

from .app import create_app


def main():
    ap = argparse.ArgumentParser(description="Dog Free Couch server (development)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    create_app().run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
