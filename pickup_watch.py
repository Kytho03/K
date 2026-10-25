#!/usr/bin/env python3
"""Watch Apple in-store pickup availability for one part at one store.

Polls Apple's pickup-message endpoint every ~2 minutes (with jitter) and sends
a single ntfy.sh push notification with the reservation link the first time
the part shows as available, then exits.

Usage:
    python3 pickup_watch.py --topic my-secret-topic-name
"""

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PART = "MK1F4LL/A"  # iPhone Duo 256GB Night Sky
DEFAULT_STORE = "R168"  # Apple Columbia (Columbia Mall, Columbia, MD)
DEFAULT_LINK = "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky"

PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def check(part, store):
    """Return (available, store_name, quote) for the part at the store."""
    query = urllib.parse.urlencode({"pl": "true", "parts.0": part, "store": store})
    req = urllib.request.Request(
        f"{PICKUP_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    stores = data["body"]["stores"]
    match = next((s for s in stores if s.get("storeNumber") == store), None)
    if match is None:
        raise RuntimeError(f"store {store} not in response")

    info = match["partsAvailability"][part]
    quote = info.get("messageTypes", {}).get("regular", {}).get("storePickupQuote", "")
    available = info.get("pickupDisplay") == "available"
    return available, match.get("storeName", store), quote


def notify(topic, title, message, link):
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=message.encode(),
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "iphone,rotating_light",
            "Click": link,
            "Actions": f"view, Reserve now, {link}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", required=True, help="ntfy.sh topic (pick something hard to guess)")
    p.add_argument("--part", default=DEFAULT_PART)
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--link", default=DEFAULT_LINK, help="reservation link sent in the alert")
    p.add_argument("--interval", type=float, default=120, help="base seconds between checks")
    p.add_argument("--jitter", type=float, default=30, help="max +/- seconds of random jitter")
    p.add_argument("--test-notify", action="store_true", help="send a test push and exit")
    args = p.parse_args()

    if args.test_notify:
        notify(args.topic, "Test: pickup watcher", "Notifications are working.", args.link)
        log("test notification sent")
        return

    log(f"watching {args.part} at store {args.store}; checking every "
        f"{args.interval:.0f}s +/- {args.jitter:.0f}s")
    failures = 0
    while True:
        try:
            available, store_name, quote = check(args.part, args.store)
            failures = 0
            log(quote or ("available" if available else "unavailable"))
            if available:
                notify(
                    args.topic,
                    f"In stock for pickup at Apple {store_name}!",
                    f"{quote}\n{args.part} - tap to reserve: {args.link}",
                    args.link,
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
