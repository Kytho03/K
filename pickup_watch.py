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
import csv
import json
import os
import random
import re
import shutil
import subprocess
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
# Stores watched by default (every DC/Maryland/Virginia store), in the order auto-checkout
# prefers them.
STORES = {
    "R168": "Columbia",  # Columbia Mall, Columbia, MD
    "R142": "Annapolis",  # Annapolis Mall, Annapolis, MD
    "R112": "Bethesda Row",  # Bethesda, MD
    "R084": "Montgomery Mall",  # Westfield Montgomery, Bethesda, MD
    "R516": "Carnegie Library",  # Washington, DC
    "R287": "Georgetown",  # Washington, DC
    "R036": "Clarendon",  # Arlington, VA
    "R129": "Pentagon City",  # Arlington, VA
    "R010": "Tysons Corner",  # McLean, VA
    "R271": "Reston",  # Reston, VA
    "R221": "Fairfax Corner",  # Fairfax, VA
    "R598": "Stonebridge Potomac Town Center",  # Woodbridge, VA
}
DEFAULT_ZIP = "21044"  # searching near Columbia returns all of these stores
BAG_URL = "https://www.apple.com/shop/bag"

PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def link_for(part):
    return LINKS.get(part, f"https://www.apple.com/shop/product/{part}")


Hit = namedtuple("Hit", "store store_name zip part available title quote place", defaults=("",))


def fetch(parts, **where):
    params = {"pl": "true", **where}
    params.update({f"parts.{i}": part for i, part in enumerate(parts)})
    req = urllib.request.Request(
        f"{PICKUP_URL}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.load(resp).get("body") or {}
    if "stores" not in body:
        raise RuntimeError("Apple's response had no stores (check the part numbers)")
    return body["stores"]


def check(parts, stores, zip_code):
    """Return a Hit for every (store, part), in the order of stores then parts."""
    found = {s.get("storeNumber"): s for s in fetch(parts, location=zip_code)}
    for store in stores:
        if store not in found:  # too far from zip_code to be listed; ask for it directly
            found.update({s.get("storeNumber"): s for s in fetch(parts, store=store)})
        if store not in found:
            raise RuntimeError(f"store {store} not in response")

    return [h for store in stores for h in store_hits(found[store], parts, zip_code)]


def check_near(parts, zips, skip):
    """Return a Hit for every (store, part) Apple lists near the ZIPs, except stores in skip."""
    found = {}
    for zip_code in zips:
        for info in fetch(parts, location=zip_code):
            if info.get("storeNumber") not in skip:
                found.setdefault(info.get("storeNumber"), info)
    return [h for info in found.values() for h in store_hits(info, parts, "")]


def store_hits(info, parts, zip_code):
    store = info.get("storeNumber")
    name = info.get("storeName", store)
    zip_ = (info.get("address") or {}).get("postalCode") or zip_code
    hits = []
    for part in parts:
        avail = info["partsAvailability"][part]
        regular = avail.get("messageTypes", {}).get("regular", {})
        hits.append(Hit(store, name, zip_, part, avail.get("pickupDisplay") == "available",
                        regular.get("storePickupProductTitle", part),
                        regular.get("storePickupQuote", ""),
                        ", ".join(x for x in (info.get("city"), info.get("state")) if x)))
    return hits


def size(hit):
    found = re.search(r"\d+\s*[GT]B", hit.title)
    return found.group(0) if found else hit.part


def summary(hits):
    """One log line: 'Annapolis: 512GB AVAILABLE | ...', or that no watched store has stock."""
    found = [f"{h.store_name}: {size(h)} AVAILABLE" for h in hits if h.available]
    if found:
        return " | ".join(found)
    sizes = ", ".join(dict.fromkeys(size(h) for h in hits))
    return f"no {sizes} stock at any of {len({h.store for h in hits})} stores"


IMESSAGE_TO = None  # phone number or Apple ID email, set by --imessage

SEND_IMESSAGE = """
on run argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send (item 1 of argv) to participant (item 2 of argv) of svc
    end tell
end run
"""


def send_ntfy(topic, title, message, links=(), priority="urgent", tags="iphone,rotating_light", push=True):
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if not push:  # keep it on the topic for the app to fetch, but don't push it to the phone
        headers["Firebase"] = "no"
    if links:
        headers["Click"] = links[0][1]
        headers["Actions"] = "; ".join(f"view, {label}, {url}" for label, url in links[:3])
    req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                                 data=message.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20):
        pass


def post_status(topic, text):
    """Post a check result to <topic>-status for the ntfy app's feed, without a push notification."""
    try:
        send_ntfy(f"{topic}-status", "Pickup watcher", text, priority="min", tags="eyes", push=False)
    except (urllib.error.URLError, ValueError) as e:
        log(f"status update failed: {e}")


def send_imessage(to, text):
    """Send an iMessage through the Messages app (macOS only)."""
    subprocess.run(["osascript", "-e", SEND_IMESSAGE, text, to],
                   check=True, capture_output=True, timeout=30)


def notify(topic, title, message, links):
    """Alert by ntfy, plus iMessage if --imessage was given; links is a list of (label, url),
    the first is the tap target. Returns True if any channel got through."""
    sent = False
    body = message if links[0][1] in message else f"{message}\n{links[0][1]}"
    try:
        send_ntfy(topic, title, message, links)
        sent = True
    except (urllib.error.URLError, ValueError) as e:
        log(f"ntfy alert failed: {e}")
    if IMESSAGE_TO:
        try:
            send_imessage(IMESSAGE_TO, f"{title}\n{body}")
            sent = True
        except (OSError, subprocess.SubprocessError) as e:
            detail = getattr(e, "stderr", b"") or b""
            log(f"iMessage alert failed: {e} {detail.decode(errors='replace').strip()}")
    return sent


def record_changes(feed_topic, hits, prev, path):
    """Log stock that came in or sold out since the last check, to the console, to the ntfy
    feed_topic (no push) and to the CSV at path. Returns the (store, part)s now in stock."""
    now = {(h.store, h.part) for h in hits if h.available}
    for h in hits:
        key = (h.store, h.part)
        if key in now and key not in prev:
            event, tag = "IN STOCK", "green_circle"
        elif key in prev and key not in now:
            event, tag = "SOLD OUT", "red_circle"
        else:
            continue
        where = f"Apple {h.store_name}" + (f" ({h.place or h.zip})" if h.place or h.zip else "")
        log(f"{event}: {h.title} at {where}")
        try:
            send_ntfy(feed_topic, event, f"{h.title} at {where}", priority="min", tags=tag, push=False)
        except (urllib.error.URLError, ValueError) as e:
            log(f"changes feed update failed: {e}")
        try:
            new_file = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                out = csv.writer(f)
                if new_file:
                    out.writerow(["time", "event", "store", "store_name", "place", "zip", "part", "model"])
                out.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), event, h.store, h.store_name, h.place,
                              h.zip, h.part, h.title])
        except OSError as e:
            log(f"couldn't write {path}: {e}")
    return now


def keep_awake():
    """On a Mac, stop idle and system sleep for as long as this process runs."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])
        log("keeping this Mac awake while watching (normal sleep resumes when it stops)")


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
        notify(args.topic, f"{hit.title} at {where}: checkout needs you", reason,
               [("Open buy page", link)])

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
                   help="a phone is already set up in your bag: alerts open the bag (plus a Reserve "
                        "button per model), and --checkout checks out the bag")
    p.add_argument("--imessage", metavar="PHONE_OR_EMAIL",
                   help="also send each alert as an iMessage from this Mac's Messages app")
    p.add_argument("--allow-sleep", action="store_true",
                   help="don't keep the Mac awake while watching")
    p.add_argument("--status", action="store_true",
                   help="post every check result to the ntfy topic <topic>-status, without push "
                        "notifications (open the topic in the app to see them)")
    p.add_argument("--changes", action="store_true",
                   help="keep watching after alerts, alert again whenever stock newly appears, and log "
                        "every in-stock/sold-out change to <topic>-changes (no push) and --changes-file")
    p.add_argument("--changes-file", default="stock_changes.csv", help="CSV log for --changes")
    p.add_argument("--near", action="append", default=[], metavar="ZIP",
                   help="with --changes, also track every store Apple lists near this ZIP (repeatable; "
                        "changes feed only, no alerts; one extra request per ZIP per check)")
    args = p.parse_args()
    global IMESSAGE_TO
    IMESSAGE_TO = args.imessage
    parts = args.part or list(LINKS)
    stores = args.store or list(STORES)
    unknown = [part for part in parts if part not in LINKS]
    if unknown:
        p.error(f"unknown part {', '.join(unknown)}; use one of {', '.join(LINKS)}")

    if args.test_notify:
        ok = notify(args.topic, "Test: pickup watcher", "Notifications are working.",
                    [("Open", link_for(parts[0]))])
        log("test alert sent" if ok else "test alert failed on every channel")
        return

    log(f"watching {', '.join(parts)} at stores {', '.join(stores)}; checking every "
        f"{args.interval:.0f}s +/- {args.jitter:.0f}s")
    if args.near and not args.changes:
        p.error("--near only feeds the changes log; add --changes")
    if not args.allow_sleep:
        keep_awake()
    failures = 0
    prev = set()  # (store, part)s in stock at the last check, for --changes
    while True:
        try:
            hits = check(parts, stores, args.zip)
            extra = check_near(parts, args.near, set(stores)) if args.near else []
            failures = 0
            log(summary(hits))
            if args.status:
                post_status(args.topic, summary(hits))
            in_stock = [h for h in hits if h.available]
            if args.changes:  # alert only on stock that wasn't there last check
                in_stock = [h for h in in_stock if (h.store, h.part) not in prev]
                prev = record_changes(f"{args.topic}-changes", hits + extra, prev, args.changes_file)
            if in_stock and args.checkout:
                run_checkout(args, in_stock[0])
                if not args.changes:
                    return
            elif in_stock:
                links = {}
                for h in in_stock:
                    links.setdefault(h.part, (f"Reserve {h.title}", link_for(h.part)))
                where = ", ".join(dict.fromkeys(f"Apple {h.store_name}" for h in in_stock))
                actions = list(links.values())
                if args.from_bag:  # the phone is set up in the bag: tapping the alert opens it
                    actions.insert(0, ("Open bag", BAG_URL))
                alerted = notify(
                    args.topic,
                    f"In stock for pickup at {where}!",
                    "\n".join(f"{h.title} at Apple {h.store_name}: {h.quote}" for h in in_stock)
                    + "\n" + "\n".join(url for _, url in links.values()),
                    actions,
                )
                if alerted and not args.changes:
                    log("alert sent; exiting")
                    return
                if alerted:
                    log("alert sent; still watching")
                else:
                    log("alert failed; trying again next check")
                    prev -= {(h.store, h.part) for h in in_stock}
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
            failures += 1
            log(f"check failed ({failures} in a row): {e}")
            if args.status:
                post_status(args.topic, f"check failed ({failures} in a row): {e}")

        # Back off on repeated errors (up to 10 minutes) so an Apple block can clear.
        base = min(args.interval * 2 ** (failures - 1), 600) if failures else args.interval
        time.sleep(max(10, base + random.uniform(-args.jitter, args.jitter)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
