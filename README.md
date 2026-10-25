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

With `--checkout`, on stock it opens Chrome and, signed in to your Apple
Account, configures the in-stock phone (preferring Columbia, then Annapolis,
then Bethesda Row, and 256GB over 512GB) with your saved iPhone 17 Pro Max
trade-in and the T-Mobile Equipment Installment Plan, adds it to the bag, picks
in-store pickup at that store, clicks through checkout, and stops on the Review
page. It never places the order.

Steps it can't do itself (signing in, T-Mobile's captcha, anything else that
needs input) send an ntfy alert saying what to do in Chrome; it waits up to 15
minutes for you, then carries on. When it reaches Review it sends a final alert
and leaves the window open for you to place the order.

```sh
pip install playwright        # uses your installed Google Chrome
python3 checkout.py --setup   # one time: sign in to apple.com, then quit Chrome
python3 checkout.py --part MK1F4LL/A   # optional dry run; close the window when done
python3 pickup_watch.py --topic <your-ntfy-topic> --checkout
```

If T-Mobile's number and PIN aren't already filled in for you, the script fills
them from `TMOBILE_NUMBER` and `TMOBILE_PIN`. Set them in the Terminal session
only (zsh), so they aren't saved anywhere:

```sh
read "TMOBILE_NUMBER?T-Mobile number: "; read -s "TMOBILE_PIN?T-Mobile PIN: "; echo
export TMOBILE_NUMBER TMOBILE_PIN
```

Use `--carrier unlocked` to pay in full instead, and `--no-trade-in` to skip the
trade-in. Chrome blocks automation of your everyday profile, so checkout uses
its own profile at `~/.pickup-watch-chrome`.
