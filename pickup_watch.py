#!/usr/bin/env python3
"""Watch Apple in-store pickup availability for one or more parts at one store.

Polls Apple's pickup-message endpoint every ~2 minutes (with jitter) and sends
a single ntfy.sh push notification with the reservation link(s) the first time
any watched part shows as available, then exits.

Usage:
    python3 pickup_watch.py --topic my-secret-topic-name
    python3 pickup_watch.py --topic my-secret-topic-name --part MK1F4LL/A
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Parts watched by default, with the buy page each alert links to.
LINKS = {
    "MK1F4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky",
    "MK1H4LL/A": "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-512gb-night-sky",
}
DEFAULT_STORE = "R168"  # Apple Columbia (Columbia Mall, Columbia, MD)

PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def link_for(part):
    return LINKS.get(part, f"https://www.apple.com/shop/product/{part}")


def check(parts, store):
    """Return (store_name, [(part, available, title, quote), ...]) for the store."""
    params = {"pl": "true", "store": store}
    params.update({f"parts.{i}": part for i, part in enumerate(parts)})
    req = urllib.request.Request(
        f"{PICKUP_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    stores = data["body"]["stores"]
    match = next((s for s in stores if s.get("storeNumber") == store), None)
    if match is None:
        raise RuntimeError(f"store {store} not in response")

    results = []
    for part in parts:
        info = match["partsAvailability"][part]
        regular = info.get("messageTypes", {}).get("regular", {})
        title = regular.get("storePickupProductTitle", part)
        quote = regular.get("storePickupQuote", "")
        results.append((part, info.get("pickupDisplay") == "available", title, quote))
    return match.get("storeName", store), results


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


def run_checkout(args, store_name, result):
    """Take one in-stock part to the Review page in Chrome, then send a single alert."""
    part, _, title, _ = result
    link = link_for(part)

    def on_result(ok, message, _url):
        heading = (f"{title} is ready to order at Apple {store_name}" if ok
                   else f"{title} in stock at Apple {store_name}; checkout needs you")
        notify(args.topic, heading, message, [("Open buy page", link)])
        log("alert sent")

    log(f"{title} in stock; starting auto-checkout")
    try:
        import checkout  # needs playwright; only loaded when --checkout is used
        checkout.run(part, args.profile_dir, args.zip, on_result=on_result)
    except Exception as e:  # checkout never got far enough to report; still alert
        log(f"auto-checkout failed to start: {e}")
        notify(args.topic, f"{title} in stock at Apple {store_name}!",
               f"Auto-checkout couldn't start ({e}). Reserve now: {link}", [("Reserve", link)])
        log("alert sent")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", required=True, help="ntfy.sh topic (pick something hard to guess)")
    p.add_argument("--part", action="append", help="part number to watch (repeatable; default: %s)"
                   % ", ".join(LINKS))
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--interval", type=float, default=120, help="base seconds between checks")
    p.add_argument("--jitter", type=float, default=30, help="max +/- seconds of random jitter")
    p.add_argument("--test-notify", action="store_true", help="send a test push and exit")
    p.add_argument("--checkout", action="store_true",
                   help="on stock, drive Chrome to the checkout Review page (see checkout.py)")
    p.add_argument("--profile-dir", default=os.path.expanduser("~/.pickup-watch-chrome"),
                   help="Chrome profile used by --checkout")
    p.add_argument("--zip", default="21044", help="ZIP used to find the store at checkout")
    args = p.parse_args()
    parts = args.part or list(LINKS)

    if args.test_notify:
        notify(args.topic, "Test: pickup watcher", "Notifications are working.",
               [("Open", link_for(parts[0]))])
        log("test notification sent")
        return

    log(f"watching {', '.join(parts)} at store {args.store}; checking every "
        f"{args.interval:.0f}s +/- {args.jitter:.0f}s")
    failures = 0
    while True:
        try:
            store_name, results = check(parts, args.store)
            failures = 0
            log(" | ".join(f"{title}: {'AVAILABLE' if ok else 'unavailable'}"
                           for _, ok, title, _ in results))
            in_stock = [r for r in results if r[1]]
            if in_stock and args.checkout:
                run_checkout(args, store_name, in_stock[0])
                return
            if in_stock:
                notify(
                    args.topic,
                    f"In stock for pickup at Apple {store_name}!",
                    "\n".join(f"{title} ({part}): {quote}\n{link_for(part)}"
                              for part, _, title, quote in in_stock),
                    [(f"Reserve {title}", link_for(part)) for part, _, title, _ in in_stock],
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
