#!/usr/bin/env python3
"""Drive an Apple Store checkout for one iPhone up to the Review page.

Opens a visible Chrome window on a persistent profile, configures the phone on
its buy page (trade-in, T-Mobile installment plan, no AppleCare), adds it to
the bag, chooses in-store pickup at the given store, and clicks through
checkout until the Review page. It never places the order: any button that
looks like "Place Order" is refused. The browser stays open for you to review
and place the order yourself.

Some steps need you: signing in, T-Mobile's account check captcha, and
anything else it can't fill in. At those it calls on_help with what to do,
waits up to 15 minutes for you to do it in the Chrome window, and then carries
on. If T-Mobile's number and PIN fields are empty it fills them from the
TMOBILE_NUMBER and TMOBILE_PIN environment variables, when set.

Chrome won't let automation drive your everyday profile (since Chrome 136), so
this uses a dedicated profile directory. Sign in to apple.com in it once:

    python3 checkout.py --setup

Then try a dry run whenever you like (it stops at Review; quit Chrome with Cmd+Q):

    python3 checkout.py --part MK1F4LL/A

or, with the phone already set up in your bag:

    python3 checkout.py --part MK1F4LL/A --from-bag
    python3 checkout.py --part MK1H4LL/A --store "Bethesda Row" --zip 20814
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

DEFAULT_PROFILE = os.path.expanduser("~/.pickup-watch-chrome")
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
]
DEFAULT_ZIP = "21044"  # Columbia, MD
DEFAULT_STORE = "Columbia"
DEFAULT_STORE_RE = r"\bColumbia\b"

BUY_PAGES = {
    "MK1F4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky",
    "MK1H4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
}
CAPACITY = {"MK1F4LL/A": "256gb", "MK1H4LL/A": "512gb"}
TRADE_IN_DEVICE = re.compile(r"17\s+Pro\s+Max", re.I)
TMOBILE_PLAN = re.compile(r"T-Mobile Equipment Installment Plan", re.I)
BUY_BUTTON = "button#add-to-cart, button[name=add-to-cart], button[name=proceed]"

PLACE_ORDER = re.compile(r"place\s+(your\s+)?order", re.I)
FORBIDDEN = re.compile(r"place\s+(your\s+)?order|buy\s+now|submit\s+order|pay\s+now", re.I)
CONTINUE = re.compile(r"^\s*continue(\s+to\s+[\w\s]+)?\s*$", re.I)
STEP_TIMEOUT = 20_000  # ms
HANDOFF_SECONDS = 15 * 60


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


def settle(page):
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)


def make_ask(page, on_help):
    """Return ask(reason, done): tell the user what to do, then wait until done() is true."""
    def ask(reason, done):
        log(f"waiting for you: {reason}")
        if on_help:
            on_help(reason)
        deadline = time.monotonic() + HANDOFF_SECONDS
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            try:
                if done():
                    log("got it, carrying on")
                    settle(page)
                    return
            except PlaywrightError:
                pass
        raise Stuck(f"timed out waiting for you to {reason[0].lower() + reason[1:]}")
    return ask


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


def signed_out(page):
    """On an apple.com/shop page: is there a Sign In link (only shown when signed out)?"""
    return needs_sign_in(page) or page.locator('a[href*="/shop/signIn"]').count() > 0


def ensure_signed_in(page, ask):
    """Start signed in, so the saved trade-in and T-Mobile details show up."""
    page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded")
    settle(page)
    if signed_out(page):
        ask("Sign in to your Apple Account in Chrome (Sign In on the bag page) so your saved "
            "trade-in and T-Mobile details load",
            lambda: "www.apple.com/shop" in page.url and not signed_out(page))
    log("signed in to Apple")


def fill_tmobile(page):
    """On T-Mobile's account check: fill empty number/PIN from TMOBILE_NUMBER / TMOBILE_PIN.

    Returns True if a captcha is showing (it's then focused for the user to type).
    """
    first_visible(page, page.locator("input[name=wirelessNumber], input[name=accountPin]"), timeout=10_000)
    for name, env in (("wirelessNumber", "TMOBILE_NUMBER"), ("accountPin", "TMOBILE_PIN")):
        value = os.environ.get(env)
        field = page.locator(f"input[name={name}]").first
        if value and field.count() and field.is_visible() and not field.input_value():
            field.fill(value)
            log(f"filled in {name} from {env}")
    captcha = page.locator("input[name=captchaAnswer]").first
    if captcha.count() and captcha.is_visible():
        captcha.focus()
        return True
    return False


def preauth_filled(page):
    fields = page.locator("input[name=wirelessNumber], input[name=accountPin]")
    return fields.count() > 0 and all(fields.nth(i).input_value() for i in range(fields.count()))


def choose_trade_in(page, ask):
    """Add the saved iPhone 17 Pro Max trade-in, or ask the user to."""
    check_radio(page, "tradeupinline", "tradeIn")
    options = page.locator("input[name=purchase_option_group]").first

    def ready():  # payment options unlock once the trade-in section is complete
        return (options.is_enabled()
                and page.get_by_text(TRADE_IN_DEVICE).first.is_visible())

    if ready():
        return
    saved = first_visible(page, page.get_by_role("radio", name=TRADE_IN_DEVICE),
                          page.get_by_role("button", name=TRADE_IN_DEVICE),
                          page.get_by_role("link", name=TRADE_IN_DEVICE), timeout=6000)
    if saved is not None:
        log("selecting the saved iPhone 17 Pro Max trade-in")
        saved.click(force=True)
        page.wait_for_timeout(2000)
        confirm = first_visible(page, page.locator("[role=dialog]").get_by_role(
            "button", name=re.compile(r"^\s*(use|apply|add|continue|done|confirm)\b", re.I)), timeout=3000)
        if confirm is not None:
            safe_click(confirm)
            page.wait_for_timeout(2000)
    if not ready():
        ask("Add your iPhone 17 Pro Max trade-in on the buy page in Chrome (leave the rest to me)", ready)


def configure(page, part, carrier, trade_in, ask):
    log(f"opening buy page for {part}")
    page.goto(BUY_PAGES[part], wait_until="domcontentloaded")
    check_radio(page, "dimensionColor", "nightsky")
    check_radio(page, "dimensionCapacity", CAPACITY[part])
    if trade_in:
        choose_trade_in(page, ask)
    else:
        check_radio(page, "tradeupinline", "noTradeIn")

    if carrier == "tmobile":
        check_radio(page, "purchase_option_group", "finance")
        plan = first_visible(page, page.get_by_label(TMOBILE_PLAN), timeout=8000)
        if plan is None:
            ask("Choose Finance > T-Mobile Equipment Installment Plan on the buy page in Chrome",
                lambda: "t-mobile" in page.url)
        else:
            log("choosing the T-Mobile Equipment Installment Plan")
            plan.check(force=True)
            page.wait_for_timeout(1500)
    else:
        check_radio(page, "purchase_option_group", "fullprice")
        check_radio(page, "carrierModel", "UNLOCKED/US")

    no_applecare = page.get_by_label("No AppleCare coverage")
    no_applecare.wait_for(state="attached", timeout=STEP_TIMEOUT)
    if no_applecare.is_enabled():
        no_applecare.check(force=True)
        page.wait_for_timeout(800)


def get_to_bag(page, part, ask, max_steps=10):
    """From the configured buy page, through any carrier check, until the phone is in the bag."""
    for _ in range(max_steps):
        settle(page)
        url = page.url
        if re.search(r"/shop/bag", url, re.I):
            break
        if needs_sign_in(page):
            ask("Sign in to your Apple Account in Chrome", lambda: not needs_sign_in(page))
            continue
        if "preauth" in url:
            captcha = fill_tmobile(page)
            if not captcha and preauth_filled(page):
                btn = first_visible(page, page.get_by_role("button", name=CONTINUE), timeout=5000)
                if btn is not None:
                    log("T-Mobile details filled in; continuing")
                    safe_click(btn)
                    try:
                        page.wait_for_function("u => location.href !== u", arg=url, timeout=STEP_TIMEOUT)
                        continue
                    except PlaywrightTimeout:
                        pass
            if "preauth" in page.url:
                if captcha and preauth_filled(page):
                    reason = "Type the captcha on T-Mobile's page in Chrome and click Continue (number and PIN are filled in)"
                else:
                    reason = "Finish T-Mobile's account check in Chrome (number, PIN, captcha), then click Continue"
                ask(reason, lambda: "preauth" not in page.url)
            continue

        btn = first_visible(page, page.locator(BUY_BUTTON),
                            page.get_by_role("button", name=re.compile(r"^\s*(add to bag|review bag)\s*$", re.I)),
                            timeout=5000)
        if btn is None or btn.is_disabled():
            ask("Finish the step showing in Chrome",
                lambda: page.url != url or (btn is not None and btn.is_enabled()))
            continue

        adding = (btn.get_attribute("name") == "add-to-cart"
                  or re.search(r"add to bag", btn.inner_text(), re.I) is not None)
        log(f"clicking {btn.inner_text().strip()!r}")
        safe_click(btn)
        try:
            page.wait_for_function("u => location.href !== u", arg=url, timeout=STEP_TIMEOUT)
        except PlaywrightTimeout:
            if not adding:
                ask("Finish the step showing in Chrome", lambda: page.url != url)
                continue
        if adding:
            page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded")
            break
    else:
        raise Stuck("couldn't get the phone into the bag")
    check_bag(page, part, ask)


def check_bag(page, part, ask):
    """On the bag page: make sure the bag holds one iPhone Duo, the size for part."""
    size = CAPACITY[part].upper()
    settle(page)
    if first_visible(page, page.get_by_text(re.compile(rf"iPhone Duo\b.*\b{size}\b", re.I))) is None:
        raise Stuck(f"your bag doesn't have the iPhone Duo {size} Night Sky")
    lines = page.get_by_role("heading", name=re.compile(r"iPhone Duo", re.I))
    if lines.count() > 1:
        ask("Your bag has more than one iPhone Duo. Remove the extra one in Chrome",
            lambda: lines.count() <= 1)


def start_checkout(page):
    log("checking out")
    btn = first_visible(page, page.get_by_role("button", name=re.compile(r"^\s*check\s?out\s*$", re.I)),
                        page.get_by_role("link", name=re.compile(r"^\s*check\s?out\s*$", re.I)))
    if btn is None:
        raise Stuck("couldn't find Check Out in the bag")
    safe_click(btn)
    page.wait_for_url(re.compile(r"/shop/(checkout|sign-?in)|idmsa\.apple\.com", re.I), timeout=STEP_TIMEOUT)


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


def advance_to_review(page, zip_code, store_re, ask, max_steps=12):
    picked = False
    for _ in range(max_steps):
        settle(page)
        if is_review(page):
            return
        if needs_sign_in(page):
            ask("Sign in to your Apple Account in Chrome", lambda: not needs_sign_in(page))
            continue
        before = page.url
        if not picked and re.search(r"fulfillment", before, re.I):
            picked = True
            try:
                choose_pickup(page, zip_code, store_re)
            except Stuck as e:
                ask(f"Choose in-store pickup yourself in Chrome ({e}), then click Continue",
                    lambda: page.url != before)
                continue

        btn = first_visible(page, page.get_by_role("button", name=CONTINUE))
        if btn is None:
            ask(f"Finish {step_name(page)} in Chrome", lambda: page.url != before)
            continue
        log(f"clicking {btn.inner_text().strip()!r}")
        safe_click(btn)
        try:
            page.wait_for_function("u => location.href !== u", arg=before, timeout=STEP_TIMEOUT)
        except PlaywrightTimeout:
            ask(f"Fill in what {step_name(page)} needs in Chrome (e.g. card code), then click Continue",
                lambda: page.url != before)
    raise Stuck("checkout took more steps than expected")


def step_name(page):
    m = re.search(r"_s=([\w-]+)", page.url)
    return f"the {m.group(1)} step" if m else "the current page"


def start_chrome(profile_dir, *args):
    """Start an ordinary Chrome on profile_dir (Apple's sign-in hangs in automation-launched Chrome)."""
    candidates = [os.environ.get("CHECKOUT_CHROME_PATH", ""), *CHROME_PATHS]
    path = next((c for c in candidates if c and os.path.exists(c)), None)
    if path is None:
        raise RuntimeError("couldn't find Google Chrome; set CHECKOUT_CHROME_PATH to it")
    cmd = [path, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
           *os.environ.get("CHECKOUT_CHROME_ARGS", "").split(), *args]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_browser(p, profile_dir):
    """Start Chrome with a local debugging port and attach to it; returns (context, wait_closed)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = start_chrome(profile_dir, f"--remote-debugging-port={port}", "about:blank")
    endpoint = f"http://127.0.0.1:{port}"
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while True:
        try:
            no_proxy.open(f"{endpoint}/json/version", timeout=2).close()
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Chrome didn't start; quit any Chrome window using {profile_dir} "
                                   "(Cmd+Q) and try again")
            time.sleep(0.5)
    browser = p.chromium.connect_over_cdp(endpoint)

    def wait_closed():
        while proc.poll() is None:
            time.sleep(1)

    return browser.contexts[0], wait_closed


def run(part, profile_dir=DEFAULT_PROFILE, zip_code=DEFAULT_ZIP, store_re=DEFAULT_STORE_RE,
        on_result=None, on_help=None, carrier="tmobile", trade_in=True, from_bag=False):
    """Drive checkout for part, call on_result(ok, message, url), then wait for the window to close.

    on_help(reason) is called each time a step needs the user in the Chrome window.
    With from_bag, the phone is already set up in the bag; the buy page is skipped.
    """
    with sync_playwright() as p:
        ctx, wait_closed = open_browser(p, profile_dir)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(STEP_TIMEOUT)
        ask = make_ask(page, on_help)
        try:
            ensure_signed_in(page, ask)
            if from_bag:
                log("checking out the phone already in your bag")
                page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded")
                check_bag(page, part, ask)
            else:
                configure(page, part, carrier, trade_in, ask)
                get_to_bag(page, part, ask)
            start_checkout(page)
            advance_to_review(page, zip_code, store_re, ask)
            ok, message = True, "Checkout is on the Review page. Check it over and place the order."
        except Stuck as e:
            ok, message = False, f"Auto-checkout stopped: {e}. Finish in the open Chrome window."
        except Exception as e:  # anything unexpected: still alert and leave the window open
            ok, message = False, f"Auto-checkout hit an error ({e}). Finish in the open Chrome window."
        log(message)
        if on_result:
            on_result(ok, message, page.url)
        log("leaving Chrome open; quit it (Cmd+Q) when you're done")
        wait_closed()
        return ok


def setup(profile_dir):
    log("sign in to your Apple Account in the Chrome window (Sign In on the bag page; tick "
        "'Keep me signed in' if offered), then quit Chrome with Cmd+Q")
    start_chrome(profile_dir, "https://www.apple.com/shop/bag").wait()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--setup", action="store_true", help="open the profile to sign in to apple.com")
    p.add_argument("--part", choices=list(BUY_PAGES), default="MK1F4LL/A")
    p.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    p.add_argument("--store", default=DEFAULT_STORE, help="store name, e.g. Annapolis or \"Bethesda Row\"")
    p.add_argument("--zip", default=DEFAULT_ZIP, help="ZIP to search pickup stores near")
    p.add_argument("--carrier", choices=["tmobile", "unlocked"], default="tmobile",
                   help="tmobile: T-Mobile Equipment Installment Plan; unlocked: pay in full")
    p.add_argument("--no-trade-in", action="store_true", help="skip the iPhone 17 Pro Max trade-in")
    p.add_argument("--from-bag", action="store_true",
                   help="check out the phone already set up in your bag instead of configuring one")
    args = p.parse_args()
    if args.setup:
        setup(args.profile_dir)
    else:
        store_re = rf"\b{re.escape(args.store)}\b"
        sys.exit(0 if run(args.part, args.profile_dir, args.zip, store_re,
                          on_help=lambda reason: log(f"NEEDS YOU: {reason}"),
                          carrier=args.carrier, trade_in=not args.no_trade_in,
                          from_bag=args.from_bag) else 1)


if __name__ == "__main__":
    main()
