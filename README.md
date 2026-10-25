# K

## Apple pickup watcher

`pickup_watch.py` polls Apple's in-store pickup availability for the iPhone Duo
Night Sky 256GB (`MK1F4LL/A`) and 512GB (`MK1H4LL/A`) at all 12 Apple Stores in
the DC/Maryland/Virginia area (see `STORES` in `pickup_watch.py`) every ~2
minutes with jitter, and sends one ntfy.sh push when either model is available
at any of them. Use `--store` (repeatable) to watch a different set.

Check the push arrives, then watch (alerts only):

```sh
python3 pickup_watch.py --topic <your-ntfy-topic> --test-notify
python3 pickup_watch.py --topic <your-ntfy-topic>
```

To also get each alert as an iMessage (sent from this Mac's Messages app to
your phone number or Apple ID email), add `--imessage <phone-or-email>`. The
first time, macOS asks to let Terminal control Messages; allow it.

Add `--status` to post every check result (or failed check) to the ntfy topic
`<your-ntfy-topic>-status` without a push notification. Subscribe to that topic
in the ntfy app and open it (pull down to refresh) for a live feed of what the
watcher sees; only real stock alerts, on your main topic, notify your phone.

### Auto-checkout to the Review page

With `--checkout`, on stock it opens Chrome and, signed in to your Apple
Account, configures the in-stock phone (preferring stores in the order of
`STORES`, Columbia first, and 256GB over 512GB) with your saved iPhone 17 Pro Max
trade-in and the T-Mobile Equipment Installment Plan, adds it to the bag, picks
in-store pickup at that store, clicks through checkout, and stops on the Review
page. It never places the order.

Steps it can't do itself (signing in, T-Mobile's captcha, anything else that
needs input) send an ntfy alert saying what to do in Chrome; it waits up to 15
minutes for you, then carries on. When it reaches Review it sends a final alert
and leaves the window open for you to place the order.

1. Install Playwright (it drives your installed Google Chrome): `pip install playwright`
2. One time, sign in to apple.com in the checkout profile, then quit Chrome (Cmd+Q):
   `python3 checkout.py --setup`
3. Optional dry run; quit Chrome when it stops: `python3 checkout.py --part MK1F4LL/A`
4. Watch: `python3 pickup_watch.py --topic <your-ntfy-topic> --checkout`

If T-Mobile's number and PIN aren't already filled in for you, the script fills
them from `TMOBILE_NUMBER` and `TMOBILE_PIN`. Set them in the Terminal session
only (zsh), so they aren't saved anywhere:

```sh
read "TMOBILE_NUMBER?T-Mobile number: "; read -s "TMOBILE_PIN?T-Mobile PIN: "; echo
export TMOBILE_NUMBER TMOBILE_PIN
```

If the phone is already set up in your bag (model, trade-in, T-Mobile plan),
add `--from-bag` and watch just that model with `--part`. On stock it skips the
buy page and goes straight from your bag through checkout to Review:

```sh
python3 pickup_watch.py --topic <your-ntfy-topic> --checkout --from-bag --part MK1F4LL/A
```

Use `--carrier unlocked` to pay in full instead, and `--no-trade-in` to skip the
trade-in. Chrome blocks automation of your everyday profile, so checkout uses
its own profile at `~/.pickup-watch-chrome`, started as a normal Chrome window
(Apple's sign-in hangs in a browser launched for automation).
