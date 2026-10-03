"""
Camera agent: runs next to the camera and reports to the server.

    python agent.py --server https://couch.example.com --token dfc_...

Get the token by adding a device in the dashboard (Devices tab). It is saved
to instance/device_token the first time, so later runs only need --server
(or set DFC_SERVER / DFC_TOKEN in the environment instead).

Detection, the alarm sound and clip recording all happen here, so the alarm
still goes off if the internet is down; events and clips are queued and
uploaded once the server is reachable again.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

import yaml

from cloud_client import (AgentLink, CloudClient, RemoteSettings, RemoteStore,
                          Uploader)
from service import MonitorService

TOKEN_FILE = Path("instance/device_token")


def resolve_token(arg: str | None) -> str:
    if arg:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(arg.strip())
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except Exception:
            pass
        return arg.strip()
    if os.environ.get("DFC_TOKEN"):
        return os.environ["DFC_TOKEN"].strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    sys.exit("No device token. Add a device in the dashboard, then run:\n"
             "  python agent.py --server <url> --token <token>")


def main():
    ap = argparse.ArgumentParser(description="Dog Free Couch camera agent")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--server", help="Server URL, e.g. https://couch.example.com")
    ap.add_argument("--token", help="Device token from the dashboard (saved for next time)")
    ap.add_argument("--keep-local", action="store_true",
                    help="Keep snapshots/clips on this machine after uploading them")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    server = (args.server or os.environ.get("DFC_SERVER")
              or config.get("cloud", {}).get("server"))
    if not server:
        sys.exit("No server URL. Pass --server, set DFC_SERVER, or add cloud.server to config.yaml.")
    if server.startswith("http://") and not any(h in server for h in ("localhost", "127.0.0.1")):
        print("WARNING: plain http to a remote server sends your device token and "
              "clips unencrypted. Use https.", file=sys.stderr)
    if not config.get("zone", {}).get("points"):
        print("Note: no couch zone in config.yaml. Draw one on the live view in "
              "the dashboard (or run `python calibrate.py` here); until then "
              "nothing counts as being on the couch.", file=sys.stderr)

    client = CloudClient(server, resolve_token(args.token))
    uploader = Uploader(client, delete_after_upload=not args.keep_local)
    settings = RemoteSettings()
    monitor = MonitorService(config, RemoteStore(uploader), settings)
    link = AgentLink(client, monitor, settings, uploader)

    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())

    link.start()
    monitor.start()
    print(f"Agent running, reporting to {server}. Ctrl+C to stop.")
    try:
        while not done.wait(1.0):
            pass
    finally:
        link.stop()
        monitor.stop()
        if uploader.pending():
            print(f"[cloud] {uploader.pending()} upload(s) still queued; they will be "
                  "lost on exit (the files stay on disk).", file=sys.stderr)


if __name__ == "__main__":
    main()
