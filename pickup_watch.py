#!/usr/bin/env python3
"""Watch Apple in-store pickup availability for one or more parts at several stores.

Polls Apple's pickup-message endpoint every ~2 minutes (with jitter) and sends
a single ntfy.sh push notification with the reservation link(s) the first time
any watched part shows as available at any watched store, then exits.

Usage:
    python3 pickup_watch.py --topic my-secret-topic-name
    python3 pickup_watch.py --topic my-secret-topic-name --part MK1F4LL/A --store R168
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

# Parts watched by default, with the buy page each alert links to.
LINKS = {
    "MK1F4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky",
    "MK1H4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
}
# Stores watched by default, in the order auto-checkout prefers them.
STORES = {
    "R168": "Columbia",  # Columbia Mall, Columbia, MD
    "R142": "Annapolis",  # Annapolis Mall, Annapolis, MD
    "R112": "Bethesda Row",  # Bethesda, MD
}
DEFAULT_ZIP = "21044"  # searching near Columbia returns all three stores

PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def link_for(part):
    return LINKS.get(part, f"https://www.apple.com/shop/product/{part}")


Hit = namedtuple("Hit", "store store_name zip part available title quote")


def fetch(parts, **where):
    params = {"pl": "true", **where}
    params.update({f"parts.{i}": part for i, part in enumerate(parts)})
    req = urllib.request.Request(
        f"{PICKUP_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["body"]["stores"]


def check(parts, stores, zip_code):
    """Return a Hit for every (store, part), in the order of stores then parts."""
    found = {s.get("storeNumber"): s for s in fetch(parts, location=zip_code)}
    for store in stores:
        if store not in found:  # too far from zip_code to be listed; ask for it directly
            found.update({s.get("storeNumber"): s for s in fetch(parts, store=store)})
        if store not in found:
            raise RuntimeError(f"store {store} not in response")

    hits = []
    for store in stores:
        info = found[store]
        name = info.get("storeName", store)
        zip_ = (info.get("address") or {}).get("postalCode") or zip_code
        for part in parts:
            avail = info["partsAvailability"][part]
            regular = avail.get("messageTypes", {}).get("regular", {})
            hits.append(Hit(store, name, zip_, part, avail.get("pickupDisplay") == "available",
                            regular.get("storePickupProductTitle", part),
                            regular.get("storePickupQuote", "")))
    return hits


def summary(hits):
    """One log line: 'Columbia: 256GB no, 512GB no | Annapolis: ...'."""
    by_store = {}
    for h in hits:
        size = re.search(r"\d+\s*[GT]B", h.title)
        by_store.setdefault(h.store_name, []).append(
            f"{size.group(0) if size else h.part} {'AVAILABLE' if h.available else 'no'}")
    return " | ".join(f"{name}: {', '.join(v)}" for name, v in by_store.items())


def notify(topic, title, message, links):
    """Send one push; links is a list of (label, url), the first is the tap target."""
    actions = "; ".join(f"view, {label}, {url}" for label, url in links[:3])
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=message.encode(),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "iphone,rotating_light",
            "Click": links[0][1],
            "Actions": actions,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20):
        pass


def run_checkout(args, hit):
    """Take one in-stock part to the Review page in Chrome, then send a single alert."""
    link = link_for(hit.part)
    where = f"Apple {hit.store_name}"

    def on_result(ok, message, _url):
        heading = (f"{hit.title} is ready to order at {where}" if ok
                   else f"{hit.title} in stock at {where}; checkout needs you")
        notify(args.topic, heading, message, [("Open buy page", link)])
        log("alert sent")

    def on_help(reason):
        try:
            notify(args.topic, f"{hit.title} at {where}: checkout needs you", reason,
                   [("Open buy page", link)])
        except (urllib.error.URLError, ValueError) as e:
            log(f"couldn't send alert: {e}")

    log(f"{hit.title} in stock at {where}; starting auto-checkout")
    try:
        import checkout  # needs playwright; only loaded when --checkout is used
        checkout.run(hit.part, args.profile_dir, hit.zip, rf"\b{re.escape(hit.store_name)}\b",
                     on_result=on_result, on_help=on_help,
                     carrier=args.carrier, trade_in=not args.no_trade_in, from_bag=args.from_bag)
    except Exception as e:  # checkout never got far enough to report; still alert
        log(f"auto-checkout failed to start: {e}")
        notify(args.topic, f"{hit.title} in stock at {where}!",
               f"Auto-checkout couldn't start ({e}). Reserve now: {link}", [("Reserve", link)])
        log("alert sent")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", required=True, help="ntfy.sh topic (pick something hard to guess)")
    p.add_argument("--part", action="append", help="part number to watch (repeatable; default: %s)"
                   % ", ".join(LINKS))
    p.add_argument("--store", action="append", help="store number to watch (repeatable; default: %s)"
                   % ", ".join(f"{k} {v}" for k, v in STORES.items()))
    p.add_argument("--interval", type=float, default=120, help="base seconds between checks")
    p.add_argument("--jitter", type=float, default=30, help="max +/- seconds of random jitter")
    p.add_argument("--test-notify", action="store_true", help="send a test push and exit")
    p.add_argument("--checkout", action="store_true",
                   help="on stock, drive Chrome to the checkout Review page (see checkout.py)")
    p.add_argument("--profile-dir", default=os.path.expanduser("~/.pickup-watch-chrome"),
                   help="Chrome profile used by --checkout")
    p.add_argument("--zip", default=DEFAULT_ZIP, help="ZIP to search stores near")
    p.add_argument("--carrier", choices=["tmobile", "unlocked"], default="tmobile",
                   help="checkout plan: T-Mobile Equipment Installment Plan, or pay in full unlocked")
    p.add_argument("--no-trade-in", action="store_true", help="checkout without the iPhone 17 Pro Max trade-in")
    p.add_argument("--from-bag", action="store_true",
                   help="checkout the phone already set up in your bag (watch just its --part)")
    args = p.parse_args()
    parts = args.part or list(LINKS)
    stores = args.store or list(STORES)

    if args.test_notify:
        notify(args.topic, "Test: pickup watcher", "Notifications are working.",
               [("Open", link_for(parts[0]))])
        log("test notification sent")
        return

    log(f"watching {', '.join(parts)} at stores {', '.join(stores)}; checking every "
        f"{args.interval:.0f}s +/- {args.jitter:.0f}s")
    failures = 0
    while True:
        try:
            hits = check(parts, stores, args.zip)
            failures = 0
            log(summary(hits))
            in_stock = [h for h in hits if h.available]
            if in_stock and args.checkout:
                run_checkout(args, in_stock[0])
                return
            if in_stock:
                links = {}
                for h in in_stock:
                    links.setdefault(h.part, (f"Reserve {h.title}", link_for(h.part)))
                where = ", ".join(dict.fromkeys(f"Apple {h.store_name}" for h in in_stock))
                notify(
                    args.topic,
                    f"In stock for pickup at {where}!",
                    "\n".join(f"{h.title} at Apple {h.store_name}: {h.quote}" for h in in_stock)
                    + "\n" + "\n".join(url for _, url in links.values()),
                    list(links.values()),
                )
                log("alert sent; exiting")
                return
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
            failures += 1
            log(f"check failed ({failures} in a row): {e}")

        # Back off on repeated errors so we don't hammer Apple if they block us.
        base = args.interval * min(2 ** max(failures - 1, 0), 8) if failures else args.interval
        time.sleep(max(30, base + random.uniform(-args.jitter, args.jitter)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
