# Configuration

Everything site-specific lives in `/etc/scanstation/scanstation.env`, owned by root with mode 600, because it holds tokens. `setup.sh` writes it and the systemd unit reads it. You can edit it by hand and run `sudo systemctl restart scanstation`, but re-running setup checks the values for you. Setup keeps any line it doesn't ask about, so a setting you add by hand survives a re-run.

Values are written single-quoted, so `$` and `\` in a token are kept as they are. A value can't contain a single quote.

| Setting | Required | Default | What it does |
|---|---|---|---|
| `PAPERLESS_URL` | yes | | Paperless base URL as the Pi reaches it, without `/api` |
| `PAPERLESS_TOKEN` | yes | | API token for the station's Paperless user |
| `PAPERLESS_PUBLIC_URL` | no | `PAPERLESS_URL` | Base for the document links in messages, if your phone uses a different name |
| `GPT_TAG` | no | `paperless-gpt-auto` | The tag paperless-gpt removes once it has titled a document. The station waits up to three minutes for it to go. Set it blank to never wait. If Paperless has no tag by this name, it doesn't wait either |
| `PUSHOVER_TOKEN` | no | | Pushover application token |
| `PUSHOVER_USER` | no | | Pushover user or group key. With either key blank, messages only go to the journal |
| `FAILSAFE_NFS` | no | | NFS export, `host:/path`, that setup adds to `/etc/fstab` as an automount at `FAILSAFE_DIR` |
| `FAILSAFE_DIR` | no | `/mnt/scanstation-failsafe` with NFS | Folder for failsafe copies. Blank means no failsafe |
| `FAILSAFE_NAME` | no | `the failsafe share` | What messages call the share |
| `ARCHIVE` | no | `no` | `yes` keeps a copy of every scan in `FAILSAFE_DIR/archive/<year>/` |

## Fixed settings

These are set in the systemd unit, and you shouldn't need to change them:

| Setting | Value | What it does |
|---|---|---|
| `STATE_DIR` | `/var/lib/scanstation` | The spool, work area and status file |
| `SANE_CONFIG_DIR` | `/etc/scanstation/sane.d` | A SANE config that loads only the `fujitsu` backend. The network backends search the LAN on every call, which made each button poll take 9 seconds instead of 25 ms |

## The status file

`/var/lib/scanstation/status.json` holds the last message sent: when, what kind, whether it was safe, the text, and the spool id. It's there for a future screen or LED to read.

## Timings

These are constants in the code, not settings:

| What | Value | Where |
|---|---|---|
| Failsafe copy if Paperless hasn't taken the scan | 5 minutes | `FAILSAFE_AFTER` in `worker.py` |
| Wait for paperless-gpt | 3 minutes | `GPT_WAIT` in `worker.py` |
| Give up on a consume that never finishes | 1 hour | `GIVE_UP_AFTER` in `worker.py` |
| Card retry window after a jam | 2 minutes | `CARD_RETRY` in `service.py` |
| Keep finished scans on the Pi | 30 days | `KEEP_DONE` in `spool.py` |
