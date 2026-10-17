"""Browser tests: the real dashboard in headless Chromium against a real
server on a throwaway SQLite db. Skipped when Playwright isn't installed
(pip install pytest-playwright && playwright install chromium)."""
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from werkzeug.serving import make_server

from server.app import create_app
from server.config import Config


@pytest.fixture(scope="session")
def base_url():
    tmp = Path(tempfile.mkdtemp())
    cfg = Config(DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp / 'b.db'}",
                 MEDIA_DIR=tmp / "media", SECRET_KEY="test", STORAGE="local",
                 RETENTION_DAYS=30, ALLOW_SIGNUP=True)
    srv = make_server("127.0.0.1", 0, create_app(cfg, start_background=False), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def page(base_url, browser):
    """A fresh browser context per test, which also collects JS errors."""
    ctx = browser.new_context(base_url=base_url)
    p = ctx.new_page()
    p.js_errors = []
    p.on("pageerror", lambda e: p.js_errors.append(str(e)))
    yield p
    ctx.close()


@pytest.fixture
def signed_in(page):
    """A newly registered user, landed on the dashboard."""
    import uuid
    page.goto("/app")
    page.click("#auth-switch a")
    page.fill("#auth-email", f"u{uuid.uuid4().hex[:8]}@example.com")
    page.fill("#auth-password", "password123")
    page.click("#auth-submit")
    page.wait_for_selector("#app-view:not(.hidden)")
    return page


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000


@pytest.fixture
def camera(signed_in, base_url):
    """Signs in, adds a camera through the UI, and returns an object that
    can post events the way the camera agent would."""
    import re
    import requests

    p = signed_in
    p.fill("#new-device-name", "Living room")
    p.click("#btn-add-device")
    p.wait_for_selector("#token-box:not(.hidden)")
    token = re.search(r"--token (\S+)", p.inner_text("#token-cmd")).group(1)

    class Cam:
        page = p

        def event(self, client_id, start):
            h = {"Authorization": f"Bearer {token}"}
            api = base_url + "/api/agent"
            requests.post(f"{api}/events", headers=h, json={
                "client_id": client_id, "start_ts": start, "confidence": 0.9}).raise_for_status()
            requests.post(f"{api}/events/{client_id}/snapshot", headers=h,
                          files={"file": ("s.jpg", JPEG)}).raise_for_status()
            requests.post(f"{api}/events/{client_id}/video", headers=h,
                          files={"file": ("c.mp4", MP4)}).raise_for_status()
            requests.post(f"{api}/events/{client_id}/end", headers=h,
                          json={"end_ts": start + 12, "duration": 12}).raise_for_status()

    return Cam()
