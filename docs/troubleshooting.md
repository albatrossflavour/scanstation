# Troubleshooting

Start with the journal. Almost everything the station does is logged there:

```bash
journalctl -u scanstation -f
journalctl -u scanstation --since today
```

## Pressing the button does nothing

Check the journal for "scanner ready". If the last line about the scanner is "scanner gone (chute closed?)", the station can't see it.

- Is the chute open? The S1500 is off USB while it's closed.
- `lsusb` should list `04c5:11a2 Fujitsu, Ltd ScanSnap S1500`. If it doesn't, check the cable and power.
- If it's on USB but the station still can't see it, run the lookup the way the service does:

  ```bash
  sudo systemctl stop scanstation
  sudo -u scanstation env SANE_CONFIG_DIR=/etc/scanstation/sane.d scanimage -L
  sudo systemctl start scanstation
  ```

  The service has to be stopped first, because it holds the scanner every half second and you'll get "Resource busy" otherwise. Add `SANE_DEBUG_FUJITSU=10` to see what the driver is doing.

## The scanner goes to sleep

About 15 minutes after it last woke, the S1500 goes into power save. It stays on USB but stops answering commands, and SANE shows `write failed: Operation timed out` in debug output. The station handles this by sending the scanner a USB reset, logged as "scanner not answering, sent it a USB reset". You'll see that line about every 15 minutes while the scanner sits idle with the chute open, and that's normal.

If the reset doesn't bring it back, closing and opening the chute does the same thing by hand. If you have to do that often, please open an issue with the journal output.

## Jams and double feeds

"HOLD this stack: jam or double feed" means nothing from that press was kept. Clear the paper and feed the whole document again.

Cards, thick envelopes, and paper with something stuck to it trip the ultrasonic double-feed sensor. Press the button again within two minutes and that scan runs with the thickness check off.

## Paperless problems

- "Safe to shred: Paperless is unreachable, so ... is on the failsafe share" means Paperless didn't take the scan within five minutes. It goes in automatically when Paperless is back.
- "HOLD this stack: Paperless and the failsafe share are both unreachable" means the scan is only on the Pi, under `/var/lib/scanstation/spool`. It's still retried, but keep the paper.
- "HOLD this stack: Paperless already has it" is a real duplicate: Paperless had that exact file from somewhere else.
- "HOLD this stack: upload refused: HTTP 403" or similar means the station's token or permissions are wrong. Re-run `sudo ./setup.sh` and let it check the token.

To see where every scan is up to:

```bash
sudo sh -c 'for f in /var/lib/scanstation/spool/*/state.json; do python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[\"id\"], d[\"state\"], d.get(\"last_error\") or \"\")" "$f"; done'
```

## A scan was processed badly

If processing crashes, the raw pages are kept under `/var/lib/scanstation/work/failed/<time>/` and you get a hold message naming the folder. Turn on `ARCHIVE=yes` to keep a copy of every finished PDF on the share while you build confidence in the output.

Issues with an example scan attached are the most useful kind, but check it's something you're happy to share first.

## No messages arrive

Re-run `sudo ./setup.sh`, which checks the Pushover keys with Pushover's validate call. With no keys set, messages are only written to the journal, and the last one is always in `/var/lib/scanstation/status.json`.
