from __future__ import annotations

import json
import random
import re
import signal
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import store
from .store import log

TIKTOK = "https://www.tiktok.com"

MESSAGE_BUTTONS = ['[data-e2e="message-button"]', 'button:has-text("Message")', 'a:has-text("Message")']
MESSAGE_INPUTS = [
    '[data-e2e="message-input-area"] [contenteditable="true"]',
    '[data-e2e="dm-new-input-editor"][contenteditable="true"]',
    '[data-e2e="dm-new-input-editor"] [contenteditable="true"]',
    'div[contenteditable="true"][aria-label*="message" i]',
    'div[contenteditable="true"]',
]
SEND_BUTTONS = ['[data-e2e="dm-new-send-btn"]', '[data-e2e="message-send"]']
CHAT_HEADER = '[data-e2e="chat-uniqueid"]'
MESSAGE_LIST = '[data-e2e="dm-new-message-list"]'
CONVERSATION = '[data-e2e="dm-new-conversation-item"]'
CONVERSATION_NAME = '[data-e2e="dm-new-conversation-nickname"]'
# EU cookie banner: it covers the message box and makes the click time out. Decline first, accept as a fallback.
COOKIE_BUTTONS = [
    re.compile(r"^\s*(decline optional cookies|decline all|reject all|rifiuta (i cookie )?(facoltativi|tutto))\s*$", re.I),
    re.compile(r"^\s*(allow all|accept all|consenti tutto|accetta tutto)\s*$", re.I),
]


def browser_ready() -> bool:
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def install_browser() -> int:
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli = compute_driver_executable()
    log("Downloading Chromium for Detok (first run only, takes a few minutes)…")
    result = subprocess.run([node, cli, "install", "chromium"], env=get_driver_env())
    log("Chromium ready." if result.returncode == 0 else "Chromium download failed.")
    return result.returncode


def ensure_browser() -> None:
    if not browser_ready() and install_browser() != 0:
        raise RuntimeError("Chromium is not installed: try 'detok setup'")


def open_profile(p, headless: bool = True):
    store.ensure_dirs()
    return p.chromium.launch_persistent_context(
        str(store.PROFILE), headless=headless, locale="en-US", viewport={"width": 1280, "height": 900},
    )


def logged_in(context) -> bool:
    return any(c["name"] in store.SESSION_COOKIES for c in context.cookies(TIKTOK))


def first_visible(page, selectors: list[str], timeout_ms: int):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible():
                    return locator
            except Exception:
                pass
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(300)


def dismiss_cookie_banner(page, timeout_ms: int = 3000) -> bool:
    """Close TikTok's cookie banner if it shows up (it lives in a shadow DOM, which Playwright pierces)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for name in COOKIE_BUTTONS:
            button = page.get_by_role("button", name=name).first
            try:
                if button.is_visible():
                    button.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                pass
        if time.monotonic() > deadline:
            return False
        page.wait_for_timeout(300)


def login(timeout_s: int = 900) -> int:
    if not store.has_display():
        log("Login: there is no screen here (a server?). Log in on a PC, run 'detok session export' "
            "and import the session here.")
        return 1
    ensure_browser()
    with sync_playwright() as p:
        chromium = p.chromium.executable_path

    with store.Lock():
        store.ensure_dirs()
        browser = subprocess.Popen(
            [chromium, f"--user-data-dir={store.PROFILE}", "--password-store=basic", "--no-sandbox",
             "--no-first-run", "--no-default-browser-check", "--lang=en-US", f"{TIKTOK}/login"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("Login: sign in to TikTok in the window that just opened. Once you see your feed, CLOSE the window.")
        try:
            browser.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            browser.send_signal(signal.SIGTERM)
            browser.wait(timeout=30)

    expiry = store.session_expiry()
    if expiry:
        log(f"Login successful: session saved (valid until {expiry:%Y-%m-%d}).")
        return 0
    log("Login not completed: no TikTok session found in the profile.")
    return 1


def _embedded_user(page) -> dict:
    raw = page.evaluate("() => document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent")
    if not raw:
        return {}
    detail = json.loads(raw).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
    return detail.get("userInfo", {}).get("user", {})


def _save_avatar(page, username: str, url: str) -> bool:
    try:
        response = page.request.get(url, timeout=15_000)
        if response.ok and response.headers.get("content-type", "").startswith("image/"):
            (store.AVATARS / f"{username}.img").write_bytes(response.body())
            return True
    except Exception:
        pass
    return False


def read_profile(page, username: str) -> dict:
    profile = {"checked": f"{store.now():%Y-%m-%d %H:%M}"}
    user = _embedded_user(page)
    if not user.get("uniqueId"):
        profile["found"] = False
        return profile

    profile.update(
        found=True,
        id=str(user.get("id") or ""),
        nickname=user.get("nickname") or username,
        private=bool(user.get("privateAccount")),
        can_message=first_visible(page, MESSAGE_BUTTONS, 6_000) is not None,
    )
    avatar_url = user.get("avatarMedium") or user.get("avatarThumb")
    if avatar_url and _save_avatar(page, username, avatar_url):
        profile["avatar"] = True
    return profile


def visit_profile(page, username: str, state: dict) -> dict:
    page.goto(f"{TIKTOK}/@{username}?lang=en", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    dismiss_cookie_banner(page)
    profile = read_profile(page, username)
    state.setdefault("profiles", {})[username] = profile
    return profile


def check(usernames: list[str] | None = None) -> int:
    cfg = store.load_config()
    usernames = [u for u in usernames or [f["username"] for f in cfg["friends"]] if store.USERNAME_RE.fullmatch(u)]
    if not usernames:
        return 0

    ensure_browser()
    with store.Lock(), sync_playwright() as p:
        context = open_profile(p)
        try:
            if not logged_in(context):
                log("Check: you are not logged in to TikTok.")
                return 2
            page = context.pages[0] if context.pages else context.new_page()
            state = store.load_state()
            for username in usernames:
                try:
                    profile = visit_profile(page, username, state)
                    store.save_state(state)
                except Exception as e:
                    log(f"Check @{username}: error ({_short(e, 150)})")
                    continue
                if not profile["found"]:
                    log(f"Check @{username}: account NOT found, double-check the username.")
                elif not profile["can_message"]:
                    log(f"Check @{username}: {profile['nickname']} (warning: you can't message them, "
                        "do you follow each other?)")
                else:
                    log(f"Check @{username}: {profile['nickname']}")
        finally:
            context.close()
    return 0


def chat_username(page) -> str:
    try:
        return page.locator(CHAT_HEADER).first.inner_text(timeout=1000).strip().lstrip("@").lower()
    except Exception:
        return ""


def wait_for_chat(page, username: str, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if chat_username(page) == username:
            return True
        page.wait_for_timeout(300)
    return False


def open_chat(page, username: str, profile: dict) -> None:
    user_id = profile.get("id")
    page.goto(f"{TIKTOK}/messages?lang=en" + (f"&u={user_id}" if user_id else ""), wait_until="domcontentloaded")
    if wait_for_chat(page, username, 15_000):
        return

    nickname = profile.get("nickname")
    if nickname:
        exact_name = re.compile(rf"^\s*{re.escape(nickname)}\s*$")
        rows = page.locator(CONVERSATION).filter(has=page.locator(CONVERSATION_NAME, has_text=exact_name))
        for i in range(min(rows.count(), 5)):
            rows.nth(i).click()
            if wait_for_chat(page, username, 4_000):
                return
    raise RuntimeError(f"chat with @{username} not found (do you follow each other?)")


def send_message(page, username: str, message: str, state: dict) -> bool:
    try:
        profile = visit_profile(page, username, state)
    except Exception:
        profile = state.get("profiles", {}).get(username, {})
    if profile.get("found") is False:
        raise RuntimeError("account does not exist: check the username")

    open_chat(page, username, profile)
    box = first_visible(page, MESSAGE_INPUTS, 20_000)
    if box is None:
        raise RuntimeError("message box not found")

    page.wait_for_timeout(2500)
    history = page.locator(MESSAGE_LIST)
    history = history.first if history.count() else page
    copies_before = history.get_by_text(message, exact=True).count()

    if chat_username(page) != username:
        raise RuntimeError("the open chat is not the right one: send cancelled")
    dismiss_cookie_banner(page, 1000)
    box.click()
    box.fill(message)
    page.wait_for_timeout(600)
    button = first_visible(page, SEND_BUTTONS, 2_000)
    if button is not None:
        button.click()
    else:
        page.keyboard.press("Enter")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _box_empty(box) and history.get_by_text(message, exact=True).count() > copies_before:
            return True
    if _box_empty(box):
        return False
    raise RuntimeError("the message is still in the box: sending failed")


def _box_empty(box) -> bool:
    try:
        return not box.inner_text().strip()
    except Exception:
        return True


def _short(error: Exception, limit: int = 200) -> str:
    return str(error).splitlines()[0][:limit]


def send(force: bool = False) -> int:
    cfg = store.load_config()
    if not force and store.now().strftime("%H:%M") < cfg["send_after"]:
        return 0

    with store.Lock():
        state = store.load_state()
        pending = [f for f in cfg["friends"] if f["enabled"] and not store.sent_today(state, f["username"])]
        if not pending:
            if force:
                log("Nothing to send: every active friend already got today's message.")
            return 0

        names = ", ".join(f["username"] for f in pending)
        log(f"Sending to {len(pending)} friend{'' if len(pending) == 1 else 's'}: {names}")
        ensure_browser()
        with sync_playwright() as p:
            context = open_profile(p, cfg["headless"])
            try:
                if not logged_in(context):
                    log("ERROR: you are not logged in to TikTok. Use 'Log in to TikTok' (or import a session) in the UI.")
                    notify_once(state, "Detok: login needed", "Your TikTok session expired: open the UI and log in again.")
                    return 2
                page = context.pages[0] if context.pages else context.new_page()
                failures = _send_all(page, pending, cfg, state)
            finally:
                context.close()

        if failures:
            notify_once(state, "Detok: sending failed",
                        f"Couldn't message: {', '.join(failures)}. Retrying in an hour.")
            return 1
    log("Done: all of today's streaks are safe.")
    return 0


def _send_all(page, friends: list[dict], cfg: dict, state: dict) -> list[str]:
    failures = []
    for i, friend in enumerate(friends):
        username = friend["username"]
        message = store.message_for(cfg, friend, state)
        record = state["users"].setdefault(username, {})
        record["last_attempt"] = f"{store.now():%Y-%m-%d %H:%M}"
        try:
            confirmed = send_message(page, username, message, state)
        except Exception as e:
            record["last_error"] = _short(e)
            failures.append(username)
            log(f"✗ {username}: {record['last_error']}")
            try:
                page.screenshot(path=str(store.SHOTS / f"{username}.png"))
            except Exception:
                pass
        else:
            days = store.streak(record) + (0 if store.sent_today(state, username) else 1)
            record.update(last_sent=record["last_attempt"], verified=confirmed, last_error=None,
                          message=message, streak=days)
            log(f"✓ {username}: sent “{message}”{'' if confirmed else ' (not confirmed)'}")
        store.save_state(state)
        if i < len(friends) - 1:
            page.wait_for_timeout(random.randint(2000, 5000))
    return failures


def notify_once(state: dict, title: str, body: str) -> None:
    if state.get("notified") == store.today():
        return
    store.notify(title, body)
    state["notified"] = store.today()
    store.save_state(state)


def session_export(path: str) -> int:
    ensure_browser()
    with store.Lock(), sync_playwright() as p:
        context = open_profile(p)
        try:
            if not logged_in(context):
                log("Export: you are not logged in to TikTok, nothing to export.")
                return 2
            cookies = [c for c in context.cookies() if "tiktok" in c["domain"]]
        finally:
            context.close()

    target = Path(path)
    target.write_text(json.dumps({"detok_session": 1, "cookies": cookies}, indent=1), encoding="utf-8")
    if not store.WINDOWS:
        target.chmod(0o600)
    log(f"Session exported to {target} ({len(cookies)} cookies). Keep it secret and delete it after importing.")
    return 0


def session_import(path: str, delete: bool = False) -> int:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        cookies = [c for c in data.get("cookies", []) if "tiktok" in str(c.get("domain", ""))]
    except (OSError, ValueError, AttributeError) as e:
        log(f"Import: invalid file ({e}).")
        return 1
    finally:
        if delete:
            source.unlink(missing_ok=True)

    if not cookies:
        log("Import: the file contains no TikTok cookies.")
        return 1

    ensure_browser()
    with store.Lock(), sync_playwright() as p:
        context = open_profile(p)
        try:
            context.add_cookies(cookies)
            ok = logged_in(context)
        finally:
            context.close()
    log("Session imported: you are connected to TikTok." if ok else "Import done, but the session does not look valid.")
    return 0 if ok else 1
