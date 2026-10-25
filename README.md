# K

## Apple pickup watcher

`pickup_watch.py` polls Apple's in-store pickup availability for the iPhone Duo
Night Sky 256GB (`MK1F4LL/A`) and 512GB (`MK1H4LL/A`) at Apple Columbia
(`R168`), Annapolis (`R142`) and Bethesda Row (`R112`) every ~2 minutes with
jitter, and sends one ntfy.sh push when either model is available at any of
them. Use `--store` (repeatable) to watch a different set.

```sh
python3 pickup_watch.py --topic <your-ntfy-topic> --test-notify   # check the push arrives
python3 pickup_watch.py --topic <your-ntfy-topic>                 # alert only
```

### Auto-checkout to the Review page

With `--checkout`, on stock it opens Chrome, adds the in-stock phone to the bag
(preferring Columbia, then Annapolis, then Bethesda Row, and 256GB over 512GB),
picks in-store pickup at that store, clicks through checkout,
and stops on the Review page. It never places the order. Then it sends one
alert saying it's ready (or where it got stuck) and leaves the window open.

```sh
pip install playwright        # uses your installed Google Chrome
python3 checkout.py --setup   # one time: sign in to apple.com, then close Chrome
python3 checkout.py --part MK1F4LL/A   # optional dry run; close the window at Review
python3 pickup_watch.py --topic <your-ntfy-topic> --checkout
```

Chrome blocks automation of your everyday profile, so checkout uses its own
profile at `~/.pickup-watch-chrome`. Sign in there once and save a card on
your Apple Account so checkout can get through payment.
