import pytest

pytest.importorskip("playwright")

from playwright.sync_api import expect


def test_login_page_loads_clean(page):
    page.goto("/app")
    expect(page.locator("#auth-title")).to_have_text("Sign in")
    assert page.js_errors == []


def test_bad_login_shows_error(page):
    page.goto("/app")
    page.fill("#auth-email", "nobody@example.com")
    page.fill("#auth-password", "wrongpass")
    page.click("#auth-submit")
    expect(page.locator("#auth-error")).to_be_visible()
    expect(page.locator("#app-view")).to_be_hidden()


def test_signup_lands_on_devices_tab_with_no_cameras(signed_in):
    p = signed_in
    expect(p.locator("#tab-devices")).to_have_class("panel active")
    expect(p.locator("#devices-list")).to_contain_text("No cameras yet")
    expect(p.locator("#device-pick")).to_be_hidden()
    assert p.js_errors == []


def test_add_camera_shows_token_and_picker(signed_in):
    p = signed_in
    p.fill("#new-device-name", "Living room")
    p.click("#btn-add-device")
    expect(p.locator("#token-box")).to_be_visible()
    expect(p.locator("#token-cmd")).to_contain_text("--token")
    expect(p.locator("#device-pick option")).to_have_text(["Living room (offline)"])
    assert p.js_errors == []


@pytest.mark.parametrize("tab", ["live", "events", "sound", "settings", "devices"])
def test_every_tab_opens_without_errors(signed_in, tab):
    p = signed_in
    p.fill("#new-device-name", "Cam")
    p.click("#btn-add-device")
    p.click(f".tab[data-tab={tab}]")
    expect(p.locator(f"#tab-{tab}")).to_have_class("panel active")
    p.wait_for_timeout(300)
    assert p.js_errors == []


def test_settings_slider_updates_label_and_saves(signed_in):
    p = signed_in
    p.fill("#new-device-name", "Cam")
    p.click("#btn-add-device")
    p.click(".tab[data-tab=settings]")
    p.locator("#al-repeat").fill("9")
    expect(p.locator("#al-repeat-val")).to_have_text("9")
    with p.expect_response(lambda r: "/settings" in r.url and r.request.method != "GET"):
        p.click("#btn-save-alert")
    p.reload()
    p.click(".tab[data-tab=settings]")
    expect(p.locator("#al-repeat")).to_have_value("9")


def test_session_survives_reload_and_logout_returns_to_login(signed_in):
    p = signed_in
    p.reload()
    expect(p.locator("#app-view")).to_be_visible()
    p.click("#btn-logout")
    expect(p.locator("#auth-view")).to_be_visible()
    p.reload()
    expect(p.locator("#auth-view")).to_be_visible()


def test_signup_and_login_toggle(page):
    page.goto("/app")
    page.click("#auth-switch a")
    expect(page.locator("#auth-title")).to_have_text("Create an account")
    expect(page.locator("#auth-submit")).to_have_text("Create account")
    page.click("#auth-switch a")
    expect(page.locator("#auth-title")).to_have_text("Sign in")


def test_short_password_is_rejected_on_signup(page):
    page.goto("/app")
    page.click("#auth-switch a")
    page.fill("#auth-email", "short@example.com")
    page.fill("#auth-password", "abc")
    page.click("#auth-submit")
    expect(page.locator("#app-view")).to_be_hidden()


def test_login_with_existing_account(page, signed_in):
    email = signed_in.inner_text("#who")
    signed_in.click("#btn-logout")
    signed_in.fill("#auth-email", email)
    signed_in.fill("#auth-password", "password123")
    signed_in.click("#auth-submit")
    expect(signed_in.locator("#app-view")).to_be_visible()
    expect(signed_in.locator("#who")).to_have_text(email)


def test_events_tab_empty_state(camera):
    p = camera.page
    p.click(".tab[data-tab=events]")
    expect(p.locator("#events-list")).to_contain_text("No alerts yet")


def test_events_tab_lists_event_and_delete_removes_it(camera):
    import time
    camera.event("evt_000000001", time.time() - 60)
    p = camera.page
    p.click(".tab[data-tab=events]")
    row = p.locator("#events-list .event")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Stayed")
    expect(row.locator(".pill.video")).to_be_visible()
    expect(row.locator("img.thumb")).to_be_visible()
    p.once("dialog", lambda d: d.accept())
    row.locator(".btn-del").click()
    expect(p.locator("#events-list .event")).to_have_count(0)
    p.reload()
    p.click(".tab[data-tab=events]")
    expect(p.locator("#events-list")).to_contain_text("No alerts yet")
    assert p.js_errors == []


def test_event_play_button_toggles_video_row(camera):
    import time
    camera.event("evt_000000002", time.time() - 60)
    p = camera.page
    p.click(".tab[data-tab=events]")
    p.locator(".btn-play").click()
    expect(p.locator(".event-video-row video")).to_have_count(1)
    p.locator(".btn-play").click()
    expect(p.locator(".event-video-row")).to_have_count(0)


def test_delete_camera_returns_to_empty_state(camera):
    p = camera.page
    p.once("dialog", lambda d: d.accept())
    p.locator(".btn-deldev").click()
    expect(p.locator("#devices-list")).to_contain_text("No cameras yet")
    expect(p.locator("#device-pick")).to_be_hidden()


def test_camera_picker_switches_between_cameras(camera):
    p = camera.page
    p.fill("#new-device-name", "Kitchen")
    p.click("#btn-add-device")
    expect(p.locator("#device-pick option")).to_have_count(2)
    p.select_option("#device-pick", label="Living room (offline)")
    expect(p.locator("#device-pick option:checked")).to_have_text("Living room (offline)")
    p.reload()
    assert p.evaluate("localStorage.getItem('dfc.device')") is not None
    assert p.js_errors == []


def test_live_tab_shows_offline_state_for_camera_without_agent(camera):
    p = camera.page
    p.click(".tab[data-tab=live]")
    expect(p.locator("#stream-empty")).to_be_visible()
    expect(p.locator("#s-state")).not_to_have_text("")


def test_live_tab_without_cameras_prompts_to_add_one(signed_in):
    signed_in.click(".tab[data-tab=live]")
    expect(signed_in.locator("#stream-empty")).to_contain_text("Add a camera")


def test_sound_tab_lists_builtin_sounds(camera):
    p = camera.page
    p.click(".tab[data-tab=sound]")
    expect(p.locator("#sounds-list")).to_be_visible()
    expect(p.locator("#btn-rec")).to_be_visible()
    expect(p.locator("#rec-preview")).to_be_hidden()


def test_video_settings_roundtrip(camera):
    p = camera.page
    p.click(".tab[data-tab=settings]")
    p.locator("#v-pre").fill("7")
    expect(p.locator("#v-pre-val")).to_have_text("7")
    with p.expect_response(lambda r: "/settings" in r.url and r.request.method != "GET"):
        p.click("#btn-save-video")
    p.reload()
    p.click(".tab[data-tab=settings]")
    expect(p.locator("#v-pre")).to_have_value("7")


def _mock_telegram(page, **tg):
    import json
    body = {"shared_bot": True, "connected": False, **tg}
    page.route("**/api/me/telegram", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(body)))


def test_telegram_banner_shows_for_shared_bot_and_can_be_dismissed(page):
    _mock_telegram(page)
    page.goto("/app")
    page.click("#auth-switch a")
    page.fill("#auth-email", "tgbanner@example.com")
    page.fill("#auth-password", "password123")
    page.click("#auth-submit")
    expect(page.locator("#tg-banner")).to_be_visible()
    page.click("#btn-banner-dismiss")
    expect(page.locator("#tg-banner")).to_be_hidden()
    page.reload()
    expect(page.locator("#tg-banner")).to_be_hidden()


def test_telegram_banner_hidden_when_already_connected(page):
    _mock_telegram(page, connected=True)
    page.goto("/app")
    page.click("#auth-switch a")
    page.fill("#auth-email", "tgconn@example.com")
    page.fill("#auth-password", "password123")
    page.click("#auth-submit")
    page.wait_for_selector("#app-view:not(.hidden)")
    expect(page.locator("#tg-banner")).to_be_hidden()


def test_telegram_banner_hidden_without_shared_bot(signed_in):
    expect(signed_in.locator("#tg-banner")).to_be_hidden()


def test_landing_page_links_into_the_app(page):
    page.goto("/")
    page.click("a[href='/app#signup']:visible >> nth=0")
    expect(page.locator("#auth-title")).to_have_text("Create an account")
    assert page.js_errors == []
