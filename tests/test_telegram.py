"""The shared Telegram bot: linking a chat to an account, the webhook's
secret check, and sending through the server's token. No network."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_server import Agent, Browser, make_app

from server import telegram, telegram_bot
from server.models import User

TOKEN = "123456:shared-bot-token"


def _app(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "send_message",
                        lambda token, chat, text, button=None:
                        sent.append((token, str(chat), text, button)) or True)
    monkeypatch.setattr(telegram, "bot_username", lambda token: "dfc_bot")
    app, _ = make_app(TELEGRAM_BOT_TOKEN=TOKEN)
    return app, sent


def _start(app, chat_id, text):
    telegram_bot.handle_update(app, {"update_id": 1, "message": {
        "chat": {"id": chat_id}, "text": text}})


def _code(url):
    return url.split("?start=")[1]


def test_link_flow_connects_chat_to_account(monkeypatch):
    app, sent = _app(monkeypatch)
    b = Browser(app, "a@example.com")
    assert b.req("get", "/api/me/telegram").get_json()["shared_bot"] is True

    url = b.req("post", "/api/me/telegram/link").get_json()["url"]
    assert url.startswith("https://t.me/dfc_bot?start=")

    _start(app, 555, f"/start {_code(url)}")
    tg = b.req("get", "/api/me/telegram").get_json()
    assert tg["connected"] and tg["chat_id"] == "555" and tg["enabled"]
    assert "a@example.com" in sent[-1][2]
    assert sent[-1][3] is None  # no PUBLIC_URL, so no link back
    # The code is single use.
    _start(app, 777, f"/start {_code(url)}")
    assert "expired" in sent[-1][2]


def test_expired_or_unknown_code_is_rejected(monkeypatch):
    app, sent = _app(monkeypatch)
    b = Browser(app, "a@example.com")
    code = _code(b.req("post", "/api/me/telegram/link").get_json()["url"])
    with app.app_context():
        s = app.extensions["dfc"]["db"]
        s.query(User).one().tg_link_expires = time.time() - 1
        s.commit()
    _start(app, 1, f"/start {code}")
    _start(app, 1, "/start nonsense")
    assert all("expired" in m[2] for m in sent)
    assert not b.req("get", "/api/me/telegram").get_json()["connected"]


def test_chat_belongs_to_one_account(monkeypatch):
    app, _ = _app(monkeypatch)
    a, b = Browser(app, "a@example.com"), Browser(app, "b@example.com")
    for who in (a, b):
        _start(app, 42, f"/start {_code(who.req('post', '/api/me/telegram/link').get_json()['url'])}")
    assert not a.req("get", "/api/me/telegram").get_json()["connected"]
    assert b.req("get", "/api/me/telegram").get_json()["connected"]


def test_stop_and_disconnect(monkeypatch):
    app, _ = _app(monkeypatch)
    b = Browser(app, "a@example.com")
    _start(app, 9, f"/start {_code(b.req('post', '/api/me/telegram/link').get_json()['url'])}")
    _start(app, 9, "/stop")
    assert not b.req("get", "/api/me/telegram").get_json()["connected"]

    _start(app, 9, f"/start {_code(b.req('post', '/api/me/telegram/link').get_json()['url'])}")
    assert b.req("post", "/api/me/telegram/disconnect").get_json()["connected"] is False


def test_webhook_requires_the_secret(monkeypatch):
    app, sent = _app(monkeypatch)
    b = Browser(app, "a@example.com")
    code = _code(b.req("post", "/api/me/telegram/link").get_json()["url"])
    update = {"update_id": 1, "message": {"chat": {"id": 5}, "text": f"/start {code}"}}
    c = app.test_client()
    assert c.post("/api/telegram/webhook", json=update).status_code == 403
    bad = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    assert c.post("/api/telegram/webhook", json=update, headers=bad).status_code == 403
    assert not b.req("get", "/api/me/telegram").get_json()["connected"]

    good = {"X-Telegram-Bot-Api-Secret-Token": app.config["DFC"].TELEGRAM_WEBHOOK_SECRET}
    assert c.post("/api/telegram/webhook", json=update, headers=good).status_code == 200
    assert b.req("get", "/api/me/telegram").get_json()["connected"]


def test_no_shared_bot_means_no_link():
    app, _ = make_app()
    b = Browser(app, "a@example.com")
    assert b.req("get", "/api/me/telegram").get_json()["shared_bot"] is False
    assert b.req("post", "/api/me/telegram/link").status_code == 404


def test_events_are_sent_through_the_shared_token(monkeypatch):
    app, _ = _app(monkeypatch)
    calls = []
    monkeypatch.setattr(telegram, "send_photo",
                        lambda token, chat, caption, jpeg: calls.append((token, chat)) or True)
    b = Browser(app, "a@example.com")
    _, token = b.add_device()
    _start(app, 77, f"/start {_code(b.req('post', '/api/me/telegram/link').get_json()['url'])}")

    Agent(app, token).full_event("evt_000000001")
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls == [(TOKEN, "77")]


def test_confirmation_links_back_to_the_app_when_public(monkeypatch):
    app, sent = _app(monkeypatch)
    app.config["DFC"].PUBLIC_URL = "https://dfc.example.com"
    b = Browser(app, "a@example.com")
    _start(app, 5, f"/start {_code(b.req('post', '/api/me/telegram/link').get_json()['url'])}")
    assert sent[-1][3] == ("Back to Dog Free Couch", "https://dfc.example.com")


def test_polling_leaves_an_existing_webhook_alone(monkeypatch):
    app, _ = _app(monkeypatch)
    deleted = []
    monkeypatch.setattr(telegram, "webhook_url", lambda token: "https://prod.example.com/api/telegram/webhook")
    monkeypatch.setattr(telegram, "delete_webhook", lambda token: deleted.append(token))
    monkeypatch.setattr(telegram, "get_updates", lambda *a, **k: (_ for _ in ()).throw(AssertionError("polled")))
    telegram_bot._poll(app)  # returns instead of looping
    assert deleted == []
