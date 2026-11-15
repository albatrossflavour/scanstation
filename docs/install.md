# Installing

## The Pi

Start from a fresh Raspberry Pi OS (Debian 13) install with network access and SSH. The desktop isn't needed. The Pi needs to reach your Paperless server, and the scanner plugs into one of its USB ports.

Setup installs everything else from Debian's own packages: `sane-utils`, `python3-numpy`, `python3-pil`, `tesseract-ocr`, `tesseract-ocr-osd`, `nfs-common`, and `rsync`. There is no pip and no virtualenv.

## Running setup

```bash
git clone https://github.com/albatrossflavour/scanstation.git
cd scanstation
sudo ./setup.sh
```

Setup asks for each setting in turn. Where you already have a value it shows it in brackets (secrets show only their last four characters), and Enter keeps it. Type `-` to clear an optional one.

### Paperless

1. The Paperless URL, as the Pi reaches it. For example `http://paperless.lan:8000`.
2. A different URL for links in messages, if your phone reaches Paperless by another name. Leave it blank otherwise.
3. The paperless-gpt tag. The default is `paperless-gpt-auto`. If that tag doesn't exist in Paperless the station doesn't wait for it, so leave the default if you don't run paperless-gpt.
4. Whether to create the station's Paperless user now. See below.
5. The station's API token. If setup just created the user, the token is already filled in.

Setup then calls the Paperless API with the token. If that fails it says why and offers to let you enter the details again.

### The Paperless side

The station uploads as its own Paperless user, `scanstation`, which can add documents and see only what it needs to follow them through. A workflow hands every document it uploads to the person who should own it.

The easy way is to let setup do it. It asks for an admin's API token (used for that run and not stored) and the username that should own the scans, then runs `tools/paperless-setup.py`, which creates:

- the `scanstation` user, with the permissions `add_document`, `view_document`, `view_tag`, and `view_paperlesstask`
- a workflow called "Scan station owner", triggered on Document Added for filenames matching `scanstation-*`, that sets the owner and lets `scanstation` view the document
- view rights for `scanstation` on the paperless-gpt tag, if it exists

You can also run the tool yourself, from any machine with Python. It's a dry run unless you pass `--apply`:

```bash
PAPERLESS_ADMIN_TOKEN=... tools/paperless-setup.py --url http://paperless.lan:8000 --owner alice
PAPERLESS_ADMIN_TOKEN=... tools/paperless-setup.py --url http://paperless.lan:8000 --owner alice --apply
```

To do it by hand in the Paperless UI instead, create the user and workflow above, then create an API token for the user from its profile.

### Pushover

The Pushover application token and your user (or group) key. Setup checks them with Pushover's validate call. Leave both blank and messages only go to the journal, which works but loses most of the point.

### Failsafe share

When Paperless hasn't taken a scan within five minutes, the station copies the PDF to the failsafe share and tells you it's safe to shred. When Paperless comes back the scan goes in and the copy is removed.

1. An NFS export to mount, as `host:/path`. Setup test-mounts it. Leave it blank if you don't have one, or if the folder you want is already mounted.
2. The folder to copy into. With NFS this is the mount point, `/mnt/scanstation-failsafe` by default. Without NFS it can be any path, but a folder on the Pi's own SD card doesn't protect you from much. Setup creates the folder if it doesn't exist and leaves an existing one alone, warning if the `scanstation` user can't write to it.
3. What messages call the share, for example "the NAS".
4. Whether to also keep a copy of every scan in an `archive` folder on the share, filed by year. Useful while you're getting confidence in the station.

The NFS share is mounted on first use with a ten second timeout, so a share that doesn't answer can't hang a scan. The `scanstation` user needs to be able to write to it.

## After setup

Setup installs the code to `/opt/scanstation`, writes your settings to `/etc/scanstation/scanstation.env`, installs and starts the `scanstation` systemd service, and turns off `scanbd` if it's installed, because the station talks to the scanner itself.

Open the scanner's chute. Within a couple of seconds the service logs "scanner ready" and sends a quiet "Scanner ready" message. Load a document and press the button.

Watch it work with:

```bash
journalctl -u scanstation -f
```

## Updating

```bash
cd scanstation
git pull
sudo ./setup.sh
```

Press Enter through the questions to keep your settings.

## Scripted installs

`sudo ./setup.sh --non-interactive` asks nothing. It takes each setting from an environment variable of the same name if one is set, otherwise from the existing config, and stops if the Paperless URL or token is missing. Checks that fail are reported but don't stop it.

sudo clears the environment, so put the settings after `sudo`:

```bash
sudo PAPERLESS_URL=http://paperless.lan:8000 PAPERLESS_TOKEN=... ./setup.sh --non-interactive
```
