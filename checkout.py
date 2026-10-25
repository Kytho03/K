#!/usr/bin/env python3
"""Drive an Apple Store checkout for one iPhone up to the Review page.

Opens a visible Chrome window on a persistent profile, configures the phone on
its buy page, adds it to the bag, chooses in-store pickup at the given store,
and clicks through checkout until the Review page. It never places the order:
any button that looks like "Place Order" is refused. The browser stays open for
you to review and place the order yourself.

Chrome won't let automation drive your everyday profile (since Chrome 136), so
this uses a dedicated profile directory. Sign in to apple.com in it once:

    python3 checkout.py --setup

Then try a dry run whenever you like (it stops at Review; just close the window):

    python3 checkout.py --part MK1F4LL/A
    python3 checkout.py --part MK1H4LL/A --store "Bethesda Row" --zip 20814
"""

import argparse
import os
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEFAULT_PROFILE = os.path.expanduser("~/.pickup-watch-chrome")
DEFAULT_ZIP = "21044"  # Columbia, MD
DEFAULT_STORE = "Columbia"
DEFAULT_STORE_RE = r"\bColumbia\b"

BUY_PAGES = {
    "MK1F4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky",
    "MK1H4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
}
CAPACITY = {"MK1F4LL/A": "256gb", "MK1H4LL/A": "512gb"}

PLACE_ORDER = re.compile(r"place\s+(your\s+)?order", re.I)
FORBIDDEN = re.compile(r"place\s+(your\s+)?order|buy\s+now|submit\s+order|pay\s+now", re.I)
CONTINUE = re.compile(r"^\s*continue(\s+to\s+[\w\s]+)?\s*$", re.I)
STEP_TIMEOUT = 20_000  # ms


class Stuck(Exception):
    """Checkout can't go further on its own; the reason is shown to the user."""


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] checkout: {msg}", flush=True)


def safe_click(locator):
    text = (locator.inner_text(timeout=2000) or "") + " " + (locator.get_attribute("aria-label") or "")
    if FORBIDDEN.search(text):
        raise RuntimeError(f"refusing to click {text.strip()!r}")
    locator.click()


def first_visible(page, *locators, timeout=STEP_TIMEOUT):
    """Return the first of the locators to become visible, or None."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for loc in locators:
            try:
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except PlaywrightError:
                pass
        page.wait_for_timeout(300)
    return None


def check_radio(page, name, value):
    radio = page.locator(f'input[type=radio][name="{name}"][value="{value}"]')
    radio.wait_for(state="attached", timeout=STEP_TIMEOUT)
    radio.scroll_into_view_if_needed()
    if not radio.is_checked():
        radio.check(force=True)
    page.wait_for_timeout(800)


def is_review(page):
    if re.search(r"_s=Review", page.url, re.I):
        return True
    btn = page.get_by_role("button", name=PLACE_ORDER)
    try:
        return btn.count() > 0 and btn.first.is_visible()
    except PlaywrightError:
        return False


def needs_sign_in(page):
    if re.search(r"/sign-?in|idmsa\.apple\.com", page.url, re.I):
        return True
    return page.locator('iframe[src*="idmsa.apple.com"]').count() > 0


def add_to_bag(page, part):
    log(f"opening buy page for {part}")
    page.goto(BUY_PAGES[part], wait_until="domcontentloaded")
    check_radio(page, "dimensionColor", "nightsky")
    check_radio(page, "dimensionCapacity", CAPACITY[part])
    check_radio(page, "tradeupinline", "noTradeIn")
    check_radio(page, "purchase_option_group", "fullprice")
    check_radio(page, "carrierModel", "UNLOCKED/US")
    page.get_by_label("No AppleCare coverage").check(force=True)
    page.wait_for_timeout(800)

    add = page.locator("button#add-to-cart, button[name=add-to-cart]").first
    add.wait_for(state="visible", timeout=STEP_TIMEOUT)
    if add.is_disabled():
        raise Stuck("Add to Bag is disabled on the buy page")
    log("adding to bag")
    with page.expect_navigation(wait_until="domcontentloaded", timeout=STEP_TIMEOUT):
        safe_click(add)

    page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded")
    if first_visible(page, page.get_by_text(re.compile(r"iPhone Duo", re.I))) is None:
        raise Stuck("the phone didn't show up in the bag")


def start_checkout(page):
    log("checking out")
    btn = first_visible(page, page.get_by_role("button", name=re.compile(r"^\s*check\s?out\s*$", re.I)),
                        page.get_by_role("link", name=re.compile(r"^\s*check\s?out\s*$", re.I)))
    if btn is None:
        raise Stuck("couldn't find Check Out in the bag")
    safe_click(btn)
    page.wait_for_url(re.compile(r"/shop/(checkout|sign-?in)", re.I), timeout=STEP_TIMEOUT)


def choose_pickup(page, zip_code, store_re):
    """On the fulfillment step: pick up, at the store matching store_re."""
    pickup = first_visible(page, page.get_by_role("radio", name=re.compile(r"pick\s+it\s+up", re.I)),
                           page.get_by_role("button", name=re.compile(r"pick\s+it\s+up", re.I)),
                           page.get_by_text(re.compile(r"pick\s+it\s+up", re.I)))
    if pickup is None:
        raise Stuck("no in-store pickup option at checkout")
    log("choosing in-store pickup")
    pickup.click()
    page.wait_for_timeout(1500)

    store = re.compile(store_re, re.I)
    option = first_visible(page, page.get_by_role("radio", name=store), timeout=4000)
    if option is None:
        search = first_visible(page, page.get_by_role("textbox", name=re.compile(r"zip|postal|city|location", re.I)),
                               page.locator('input[placeholder*="ZIP" i], input[name*="zip" i], input[name*="location" i]'))
        if search is None:
            raise Stuck("couldn't find the store search box")
        log(f"searching stores near {zip_code}")
        search.fill(zip_code)
        search.press("Enter")
        option = first_visible(page, page.get_by_role("radio", name=store),
                               page.locator("label").filter(has_text=store))
    if option is None:
        raise Stuck(f"no store matching {store_re!r} is offered for pickup near {zip_code}")
    log(f"selecting the store matching {store_re!r}")
    option.click(force=True)
    page.wait_for_timeout(1500)

    # Pick the earliest pickup window if Apple asks for one.
    window = first_visible(page, page.locator("select").filter(
        has=page.locator("option", has_text=re.compile(r"\d"))), timeout=3000)
    if window is not None:
        values = window.locator("option").evaluate_all("os => os.map(o => o.value).filter(v => v)")
        if values:
            window.select_option(values[0])
            page.wait_for_timeout(800)


def advance_to_review(page, zip_code, store_re, max_steps=8):
    picked = False
    for _ in range(max_steps):
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)
        if is_review(page):
            return
        if needs_sign_in(page):
            raise Stuck("Apple wants you to sign in (run checkout.py --setup to stay signed in)")
        if not picked and re.search(r"fulfillment", page.url, re.I):
            choose_pickup(page, zip_code, store_re)
            picked = True

        before = page.url
        btn = first_visible(page, page.get_by_role("button", name=CONTINUE))
        if btn is None:
            raise Stuck(f"no Continue button on {step_name(page)}")
        log(f"clicking {btn.inner_text().strip()!r}")
        safe_click(btn)
        try:
            page.wait_for_function("u => location.href !== u", arg=before, timeout=STEP_TIMEOUT)
        except PlaywrightTimeout:
            raise Stuck(f"{step_name(page)} needs your input (e.g. a missing field or card code)")
    raise Stuck("checkout took more steps than expected")


def step_name(page):
    m = re.search(r"_s=([\w-]+)", page.url)
    return f"the {m.group(1)} step" if m else "the current page"


def open_browser(p, profile_dir):
    return p.chromium.launch_persistent_context(
        profile_dir, channel=os.environ.get("CHECKOUT_CHROME_CHANNEL", "chrome"),
        headless=False, viewport=None, args=["--start-maximized"])


def run(part, profile_dir=DEFAULT_PROFILE, zip_code=DEFAULT_ZIP, store_re=DEFAULT_STORE_RE,
        on_result=None):
    """Drive checkout for part, call on_result(ok, message, url), then wait for the window to close."""
    with sync_playwright() as p:
        ctx = open_browser(p, profile_dir)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(STEP_TIMEOUT)
        try:
            add_to_bag(page, part)
            start_checkout(page)
            advance_to_review(page, zip_code, store_re)
            ok, message = True, "Checkout is on the Review page. Check it over and place the order."
        except Stuck as e:
            ok, message = False, f"Auto-checkout stopped: {e}. Finish in the open Chrome window."
        except Exception as e:  # anything unexpected: still alert and leave the window open
            ok, message = False, f"Auto-checkout hit an error ({e}). Finish in the open Chrome window."
        log(message)
        if on_result:
            on_result(ok, message, page.url)
        log("leaving the browser open; close it when you're done")
        try:
            ctx.wait_for_event("close", timeout=0)
        except PlaywrightError:
            pass
        return ok


def setup(profile_dir):
    with sync_playwright() as p:
        ctx = open_browser(p, profile_dir)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.apple.com/shop/bag")
        log("sign in to your Apple Account in this window (Sign In in the bag menu), then close it")
        ctx.wait_for_event("close", timeout=0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--setup", action="store_true", help="open the profile to sign in to apple.com")
    p.add_argument("--part", choices=list(BUY_PAGES), default="MK1F4LL/A")
    p.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    p.add_argument("--store", default=DEFAULT_STORE, help="store name, e.g. Annapolis or \"Bethesda Row\"")
    p.add_argument("--zip", default=DEFAULT_ZIP, help="ZIP to search pickup stores near")
    args = p.parse_args()
    if args.setup:
        setup(args.profile_dir)
    else:
        store_re = rf"\b{re.escape(args.store)}\b"
        sys.exit(0 if run(args.part, args.profile_dir, args.zip, store_re) else 1)


if __name__ == "__main__":
    main()
