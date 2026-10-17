# Getting ready for production

Meeting: Chair, UX Engineer, Legal Person, Logs & improvement specialist, User advocate. Four rounds. Date: 2026-10-17.
(Legal notes are a get-live checklist, not legal advice; a real lawyer should skim the final texts, ~1-2h.)

## Decision

**Go live as a free, invite-only private beta (10-20 users, hard cap 50), not a public launch.** Public signup opens only when the exit criteria below are met. Two gates:

- **Entry gate** (before the first invite): the "Before first invite" list below.
- **Exit gate** (before opening public signup): the "During beta" list plus the success criteria.

Why a beta and not a launch: the product puts a camera (and optionally a microphone) in people's homes; the repo had no Privacy Policy, Terms, consent, account deletion, password reset, rate limiting or feedback loop; and Windows/macOS users have no installer. A small invited group lets us fix these with a support channel we can answer by hand, and learn whether alerts are actually right.

## Facts verified in the repo (they drove the scope)

- No privacy/terms/consent anywhere in `frontend/` or `server/`. No account deletion/export; only per-device/event/sound delete. Retention sweeper exists (`RETENTION_DAYS`, 30d).
- No forgot-password; no email sender at all. Auth: email + password (min 8), no rate limiting found.
- **No Windows/macOS installer.** `frontend/install-agent.sh` dies unless Linux; `help.html` says "There is no installer yet" (Python + git + venv, terminal must stay open, no auto-start, no self-update).
- `config.yaml` has `audio.enabled: true` (mic muxed into clips) by default.
- `server.log` is committed to the repo.
- Observability code exists (Sentry, JSON logs, `/readyz`, Prometheus rules) but is not wired to anything watching it on Render + Neon + R2; `BackupStale` would fire forever.
- `ALLOW_SIGNUP` toggle exists; `SUPPORT_EMAIL` defaults to empty (risk of raw `{{SUPPORT_EMAIL}}` on pages).
- Detector is MobileNet-SSD via OpenCV: **no AGPL/Ultralytics exposure** (Legal's R1 flag was withdrawn; remaining task is a 30-min licence note).
- "Sound" is a siren/custom clip played on the user's own camera machine; **no two-way live audio** (Legal corrected R1). Whether the live view is relayed through our server is still to confirm (see Open).

## What we will do

### Before first invite (entry gate, ~8-10 dev days + lawyer skim; items run in parallel)

**Founder decisions (day 0, blockers for the text):** contracting entity name + address (or support-email-only, with the EU/IL caveat), home market for the soft geo-limit, `SUPPORT_EMAIL` set in prod, invite list.

**Legal & trust**
1. Privacy Policy + Terms, plain-English summary on top. Cover: data collected (email, hash, snapshots, clips, optional audio, Telegram chat id, uploaded sounds, feedback labels, product events), processors (Render, Neon, R2/Cloudflare, Telegram, Sentry, log vendor, later email provider), retention 30d, backups <=35d, rights + how to exercise, 18+, beta / no SLA / no warranty / **not a security, safety, child- or elder-monitoring system**, acceptable use (film only places/people you may), "streamed, not stored" for live view if true. No secondary use of user frames (training, sharing) without separate opt-in.
2. Signup consent checkbox; store `terms_version` + `accepted_at`. Collapse consent UI to three touchpoints only: signup checkbox, inline "cameras may see other people" note on Add camera, inline acknowledgement when enabling the mic.
3. **Mic off by default**, per-camera opt-in with acknowledgement (flip the hosted-agent default; `config.yaml` standalone default is not the concern but the installer must write off).
4. **Telegram text-only option** (no snapshots) as one Settings switch; rating buttons and test alert still work.
5. Landing/help copy sweep: "best-effort alerts when your dog is on the couch"; no 24/7 protection claims; free beta; stated supported OS; real screenshots only, no fake testimonials; support promise "reply within 2 business days".
6. `THIRD_PARTY.md` (model weights, OpenCV, sounddevice, ffmpeg: don't ship a GPL ffmpeg build); self-host fonts / no tracker embeds; one-page breach plan (who decides, 72h); delete or minimise the invite list after beta.

**Account lifecycle**
7. **Delete account + export** (JSON of metadata, clips optional). Cascade: user, cameras, devices/tokens, events, R2 clips + snapshots, sounds, Telegram link, product_events, feedback rows. One test proves R2 objects are gone. Fallback if time-boxed out: manual via support email, only from the registered address, 30-day SLA, only for the first <=20 users.
8. **Password reset is manual in the beta**: operator CLI prints a one-time link, sent only to the registered address, never on a Telegram claim; stated on the help page. No email provider is needed to launch. No email verification until public signup.

**Works and safe**
9. Rate limiting / lockout on login, signup, token endpoints; log + count failed logins and 429s. Per-user upload/storage caps (R2 cost). Revocable device tokens.
10. Remove `server.log` from git (check history for PII), gitignore it. Extend redaction to emails, Telegram chat ids, signed clip/snapshot URLs, IPs, with a unit test; Sentry `send_default_pii=False` + `before_send` scrub; agent Sentry build must not capture frames or camera names/paths.
11. **Platform scope:** see "Supported platforms" below.
12. **Guided first-run panel**: "Step 1 of 4", OS tabs, install command pre-filled with the user's token, live status (agent -> camera -> zone -> Telegram) with fix-hints for the four real failure states (agent stopped reporting, camera unavailable, Telegram unlinked/blocked, zone not set), **Send test alert** button. Loading/empty/error audit limited to those four states. Mobile-width check of dashboard + landing.
13. **Per-user "agent stopped reporting" Telegram notice (server-side)**: no heartbeat >10 min; ONE offline message, ONE recovery message, re-arm only after recovery, max one per device per 6h, suppressed if paused/uninstalled; `notified_offline_at` on device; reuse the retention sweeper thread; test with a fake clock. Wording says "stopped reporting" (a sleeping PC looks identical to a crash) and includes "keep this computer on and awake". Counters for Telegram send ok/fail.
14. **Alert cooldown** between "dog on couch" pings is sane and visible (a napping dog must not send 40 messages); 3 sensitivity presets (fewer alerts / balanced / catch everything) backed by existing config values. Log preset/cooldown changes.

**Feedback & measurement (pre-invite because the exit criteria are unmeasurable without them)**
15. **Telegram inline 👍/👎 "Was this a dog?"** on each alert (+ dashboard); store `label`, detector version and confidence on the event. Wire in-app "Send feedback / Report a problem" (no `{{placeholder}}` leaks).
16. Minimal first-party `product_events` table: signup, agent_registered, camera_added, first_alert, feedback_given, preset_changed. No cookies, no third-party analytics (so no cookie banner). Deleted with the account, included in export.

**Watching it**
17. Sentry DSN (server + agent, release = git SHA) -> founder; external uptime on `/readyz` -> Telegram; log drain with <=30d retention (verify the vendor's actual retention); Neon PITR confirmed (<=7d window) + one restore drill; neutralise `BackupStale`.
18. One private founder Telegram chat receives: Sentry/uptime alerts, delivery-failure-rate alert (>10%), and a **daily 09:00 digest** (agents online, events, alerts sent/failed, 👍/👎, signups). The five numbers owned by the founder: uptime, 5xx, offline cameras, delivery failures, false-alarm rate. Support mailbox read daily, reply <=2 business days.

### Supported platforms (the decision most likely to bite)
- Verified supported at launch: **Linux/Raspberry Pi** (existing `install-agent.sh`) and **Windows** only after a clean-machine run by an outsider succeeds. macOS: **unsupported**, said so on landing.
- Wave 1 invites go only to technical people (founder's circle) who can do the Windows manual setup; landing says "Windows: manual setup" until the installer exists.
- **`install-agent.ps1`** (checks Python, venv, pip, model download, optional Scheduled Task auto-start, documented uninstall, HTTPS-only, versioned, Sentry DSN baked in, rotating local log + `--diag` bundle) is built in the first two weeks of the beta; wave 2 (non-technical invitees) waits for it. Auto-start requires an explicit "start automatically? y/n" in the installer, not silent persistence.

### During beta (before opening public signup, ~6-8 dev days)
- Transactional email provider (SPF/DKIM on a real domain) + self-serve password reset; add provider to the subprocessor list. Automated deletion/export if item 7 shipped as manual.
- Windows installer (above); agent version in heartbeat + min-version gate.
- Precision per camera/user weekly (>=50 labels before the number is trusted); rule: >25% 👎 on a camera -> in-app and Telegram nudge to the **user** ("lots of false alarms on <camera>: redraw zone / choose 'fewer alerts'").
- "Send diagnostics" (user-initiated, says what is attached), "Reference: <id>" on error toasts.
- Help troubleshooting (camera not found, Telegram not linking, false alarms); owner-posted banner/Telegram note for known incidents (no status page).
- Weekly 15-min metrics + feedback review; >=5 user interviews (verbal OK to record; no named quotes without written permission).
- Beta terms told to testers: free, no SLA, data may be wiped.

## Success criteria
- **Entry (to invite anyone):** an outside tester on a clean supported machine reaches a first Telegram alert unaided in <15 min (3 testers, one Windows, one Pi/Linux; time recorded by hand); deletion verified (R2 objects gone); Privacy/Terms live with entity named; Sentry/uptime alerts reach the founder's phone; reset-by-operator rehearsed.
- **Exit (to open public signup):** >=70% of invitees get a first alert within a day; >=50% active at day 14; 👎 <20% of rated alerts; zero silent-offline complaints; >=5 interviews; automated reset + deletion + email live; Windows installer shipped; a measured false-alarm rate on >=50 labels.

## Options that lost, and why
- **Public launch now / ship fast and fix later:** rejected; sensitive home video with no policy, no deletion, no reset.
- **Email provider + password reset + verification as an entry blocker (Chair R1, Advocate R2):** lost to a manual reset for <=20 hand-invited users; email is the largest single new dependency and nothing else at launch needs it (offline notices go via Telegram). Reset moves into the beta; verification waits for public signup because invite-only gives equivalent assurance.
- **Self-hosted Prometheus/Alertmanager/Grafana:** overkill for a 20-user beta on Render; replaced by Sentry + uptime ping + daily digest. Delete or neutralise the unused alert rules.
- **Third-party analytics/NPS tools and a cookie banner:** first-party `product_events` only; avoids a banner and IP leakage.
- **Mic on by default / "default on is OK" (Legal R1):** lost; Legal, UX, Advocate all converged on opt-in. Cheaper than defending an eavesdropping claim; dog detection does not need sound.
- **Dashboard-only 👍/👎:** lost; Telegram inline buttons or response rate ~0.
- **Feedback buttons "within 2 weeks" (Logs R1):** lost; labels and funnel events pre-invite, otherwise the exit criteria are fiction.
- **Offline notice by email or "camera offline" wording:** lost; Telegram, "stopped reporting".
- **Sensitivity slider:** lost to 3 presets + test alert (a slider without guidance causes misconfiguration).
- **Geo-blocking EU:** lost; soft limit via invite list and ToS wording; the policy is GDPR-shaped anyway.
- **Treating AGPL/YOLO as the top legal risk:** withdrawn after checking `detector.py` and requirements.
- **Claiming Mac/Pi/Windows "work" without testing:** lost; state supported OS honestly.
- **Billing, native apps, multi-language, SOC2, DPA, trademark filing, status page, a11y pass, pricing page:** deferred (not needed for a free beta).

## Cut order if time is short
Email verification -> funnel extras beyond the six events -> min-version gate -> error-toast reference -> mobile polish. **Never cut:** Privacy/Terms/consent, mic-off default, deletion path, offline notice, label store, Sentry/uptime, rate limiting.

## Still open
1. **Founder:** contracting entity/name/address, home market, who answers support, whether macOS is ever promised. Blocks legal text and the first invite.
2. **Is the live view relayed through our server, and is it stored?** (Legal asked; policy wording depends on it.) Also confirm the siren/custom-sound path only plays on the user's own device and what token-theft could trigger (rate-limit, revocation).
3. **Is Windows install effort ~2 days (UX estimate) realistic**, or does wave 2 slip? Decides how long the beta stays technical-only.
4. **Current alert cooldown default** and whether `service.py` auto-start can be reused by the installer (unchecked).
5. Caffemodel licence text (30 min) and Neon free-tier PITR window vs. the stated backup window; log vendor and Sentry retention vs. the stated 30d.
6. Whether manual deletion will actually be needed (depends on dev capacity); the 30-day SLA and identity-check procedure must be written either way.
7. When billing arrives: Paddle (merchant of record) vs Stripe, refund policy, VAT; deferred to the exit decision.
8. Real false-alarm rate (cat, pillow, person on couch) is unknown until the dogfood week with labels; if bad, the beta message and presets change and the exit criteria may need revising.
