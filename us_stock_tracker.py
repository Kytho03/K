#!/usr/bin/env python3
"""Track iPhone Duo pickup stock at every US Apple Store, like a "recent changes" list.

Apple's pickup data only lists the 12 stores nearest a location, so this walks a fixed set of
ZIP codes chosen so every US store is among the 12 nearest to one of them, one request every
--pace seconds. Each time any size or colour comes into stock or sells out anywhere, it's logged
to the ntfy topic <topic>-us (no push; open the topic in the ntfy app) and to us_stock_changes.csv.
It never sends alerts; pickup_watch.py does that for the stores you'd pick up from.

    python3 us_stock_tracker.py --topic my-secret-topic-name
"""

import argparse
import random
import sys
import time
import urllib.error

import pickup_watch as w
from pickup_watch import log

PARTS = w.ALL_MODELS

# Every US Apple Store (269 in Oct 2026) is among the 12 stores Apple lists nearest to one of
# these; picked from a survey of ~160 ZIPs across the country.
ZIPS = [
    "04101", "06103", "11201", "11743", "12207", "13202", "14604", "19103", "22030", "23510",
    "29401", "33401", "34236", "37919", "39201", "47715", "48226", "50309", "60601", "62701",
    "74103", "78701", "82001", "83702", "84101", "85373", "87102", "91361", "92101", "92614",
    "93401", "95113", "95814", "96001", "96813", "98402", "99501",
]


def in_stock_now(hits):
    """'Brea Mall (Brea, CA): 1TB Star White, 512GB Night Sky' lines for what's in stock."""
    by_store = {}
    for h in hits:
        if h.available:
            by_store.setdefault(f"{h.store_name} ({h.place})", []).append(PARTS.get(h.part, h.part))
    return [f"{store}: {', '.join(models)}" for store, models in sorted(by_store.items())]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--topic", required=True, help="ntfy.sh topic; changes go to <topic>-us")
    p.add_argument("--pace", type=float, default=8, help="seconds between requests to Apple (plus jitter)")
    p.add_argument("--changes-file", default="us_stock_changes.csv", help="CSV log of changes")
    p.add_argument("--allow-sleep", action="store_true", help="don't keep the Mac awake while tracking")
    p.add_argument("--skip-store", action="append", default=[], metavar="STORE",
                   help="leave out a store number another watcher already covers (repeatable)")
    p.add_argument("--feed-topic", metavar="TOPIC", help="ntfy topic for changes (default <topic>-us)")
    args = p.parse_args()
    if not ZIPS:
        p.error("no ZIPs configured yet; fill in ZIPS")
    feed = args.feed_topic or f"{args.topic}-us"
    skip = set(args.skip_store)
    parts = list(PARTS)

    log(f"tracking {len(parts)} iPhone Duo models at US Apple Stores near {len(ZIPS)} ZIPs; "
        f"one request every ~{args.pace:.0f}s; changes go to ntfy topic {feed}")
    if not args.allow_sleep:
        w.keep_awake()
    prev = set()  # (store, part)s in stock as last seen
    baseline = True  # the first sweep records what's in stock without posting it as changes
    failures = 0
    while True:
        started, seen, all_hits = time.monotonic(), set(), []
        for zip_code in ZIPS:
            try:
                infos = w.fetch(parts, location=zip_code)
                hits = [h for info in infos if info.get("storeNumber") not in seen | skip
                        for h in w.store_hits(info, parts, "")]
                failures = 0
            except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as e:
                failures += 1
                log(f"{zip_code}: check failed ({failures} in a row): {e}")
                time.sleep(min(60 * failures, 600))  # Apple is pushing back; give it room
                continue
            seen.update(info.get("storeNumber") for info in infos)
            all_hits += hits
            batch = {h.store for h in hits}
            if baseline:
                prev |= {(h.store, h.part) for h in hits if h.available}
            else:
                now = w.record_changes(feed, hits, {k for k in prev if k[0] in batch}, args.changes_file)
                prev = {k for k in prev if k[0] not in batch} | now
            time.sleep(args.pace + random.uniform(0, args.pace / 2))

        lines = in_stock_now(all_hits)
        if baseline:
            baseline = False
            text = "\n".join(lines) or "Nothing in stock anywhere right now."
            try:
                w.send_ntfy(feed, f"Tracking {len(seen)} US stores: {len(lines)} with stock now",
                            text[:3900], priority="min", tags="eyes", push=False)
            except (urllib.error.URLError, ValueError) as e:
                log(f"changes feed update failed: {e}")
        log(f"sweep done: {len(seen)} stores in {time.monotonic() - started:.0f}s; "
            f"{len(lines)} with stock")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
