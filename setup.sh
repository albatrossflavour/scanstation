#!/bin/bash
# Install or update the scan station on Raspberry Pi OS (Debian 13). Run from the repo checkout:
#
#   sudo ./setup.sh                    asks for each setting, showing the current value as the default
#   sudo ./setup.sh --non-interactive  takes settings from the environment and the existing config
#
# Settings live in /etc/scanstation/scanstation.env (root only, mode 600). Re-running is safe.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"

CONFIG=${CONFIG:-/etc/scanstation/scanstation.env}
INTERACTIVE=1
[[ ${1:-} == --non-interactive ]] && INTERACTIVE=0
KEYS=(PAPERLESS_URL PAPERLESS_PUBLIC_URL PAPERLESS_TOKEN GPT_TAG PUSHOVER_TOKEN PUSHOVER_USER
      FAILSAFE_NFS FAILSAFE_DIR FAILSAFE_NAME ARCHIVE)
declare -A CFG
for k in "${KEYS[@]}"; do CFG[$k]=""; done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- read what's already there ----------------------------------------------------------------

# Older installs kept the settings in secrets.env and the archive switch in the unit file.
OLD=/etc/scanstation/secrets.env
MIGRATED=0
if [[ ! -e $CONFIG && -s $OLD && $CONFIG == /etc/scanstation/scanstation.env ]]; then
  MIGRATED=1
  mv "$OLD" "$CONFIG"
  echo "moved $OLD to $CONFIG"
  grep -q '^Environment=ARCHIVE_DIR=' /etc/systemd/system/scanstation.service 2>/dev/null && echo "ARCHIVE=yes" >> "$CONFIG"
fi

if [[ -s $CONFIG ]]; then
  while IFS= read -r line; do
    [[ $line =~ ^([A-Z_]+)=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]} val=${BASH_REMATCH[2]}
    [[ $val =~ ^\'(.*)\'$ || $val =~ ^\"(.*)\"$ ]] && val=${BASH_REMATCH[1]}
    CFG[$key]=$val
  done < "$CONFIG"
fi
# The environment wins, so a script can fill or change any setting.
for k in "${KEYS[@]}"; do [[ -n ${!k:-} ]] && CFG[$k]=${!k}; done

# An NFS failsafe from an older install is in fstab but not in the config.
if [[ $MIGRATED -eq 1 && -z ${CFG[FAILSAFE_NFS]:-} && -n ${CFG[FAILSAFE_DIR]:-} ]]; then
  CFG[FAILSAFE_NFS]=$(awk -v d="${CFG[FAILSAFE_DIR]}" '$2 == d && $3 ~ /^nfs/ {print $1}' /etc/fstab)
fi

PREV_DIR=${CFG[FAILSAFE_DIR]}  # so a changed or cleared share can be taken out of fstab

# --- prompts ----------------------------------------------------------------------------------

shown() {  # how a current value is displayed: secrets are masked
  local v=${CFG[$1]:-}
  if [[ -z $v ]]; then echo "none"
  elif [[ $2 == secret ]]; then echo "****${v: -4}"
  else echo "$v"; fi
}

ask() {  # ask KEY "question" [secret]. Enter keeps the current value, "-" clears it.
  local key=$1 question=$2 kind=${3:-plain} answer
  [[ $INTERACTIVE -eq 1 ]] || return 0
  if [[ $kind == secret ]]; then
    read -r -s -p "$question [$(shown "$key" secret)]: " answer; echo
  else
    read -r -p "$question [$(shown "$key" plain)]: " answer
  fi
  if [[ $answer == - ]]; then CFG[$key]=""
  elif [[ -n $answer ]]; then CFG[$key]=$answer; fi
}

yes_no() {  # yes_no "question" default(y|n)
  local answer
  [[ $INTERACTIVE -eq 1 ]] || { [[ $2 == y ]]; return; }
  read -r -p "$1 [$([[ $2 == y ]] && echo Y/n || echo y/N)]: " answer
  answer=${answer:-$2}
  [[ ${answer,,} == y* ]]
}

check() {  # check "what" command... : run a check, offer to re-enter or keep on failure. 0 = keep going
  local what=$1; shift
  if "$@"; then echo "  $what: ok"; return 0; fi
  echo "  $what: failed"
  [[ $INTERACTIVE -eq 1 ]] || return 0
  yes_no "  Enter it again?" y && return 1
  return 0
}

http_ok() {  # http_ok METHOD URL [header] [form-data]: true if the call answers 2xx
  python3 - "$@" <<'PY'
import sys, urllib.request
method, url = sys.argv[1], sys.argv[2]
header = sys.argv[3] if len(sys.argv) > 3 else ""
data = sys.argv[4].encode() if len(sys.argv) > 4 else None
req = urllib.request.Request(url, data=data, method=method)
if header:
    k, v = header.split(": ", 1)
    req.add_header(k, v)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception as e:
    print(f"  {e}", file=sys.stderr)
    sys.exit(1)
PY
}

paperless_ok() {
  http_ok GET "${CFG[PAPERLESS_URL]%/}/api/tags/?page_size=1" "Authorization: Token ${CFG[PAPERLESS_TOKEN]}"
}

pushover_ok() {
  http_ok POST https://api.pushover.net/1/users/validate.json "" \
    "token=${CFG[PUSHOVER_TOKEN]}&user=${CFG[PUSHOVER_USER]}"
}

nfs_ok() {
  local t
  command -v mount.nfs >/dev/null || apt-get install -y -qq nfs-common >/dev/null
  t=$(mktemp -d)
  if timeout 20 mount -t nfs -o soft,timeo=50,retrans=2 "${CFG[FAILSAFE_NFS]}" "$t"; then
    umount "$t"; rmdir "$t"; return 0
  fi
  rmdir "$t"; return 1
}

paperless_setup() {  # create the station's Paperless user and workflow, and take its token
  local admin owner tokfile
  read -r -s -p "  An admin's Paperless API token (used now, not stored): " admin; echo
  owner=""
  while [[ -z $owner ]]; do read -r -p "  Paperless username that should own scanned documents: " owner; done
  tokfile=$(mktemp)
  if PAPERLESS_ADMIN_TOKEN=$admin python3 tools/paperless-setup.py --url "${CFG[PAPERLESS_URL]}" \
       --owner "$owner" --tag "${CFG[GPT_TAG]}" --token-file "$tokfile" --apply; then
    CFG[PAPERLESS_TOKEN]=$(cat "$tokfile")
  fi
  rm -f "$tokfile"
}

: "${CFG[GPT_TAG]:=paperless-gpt-auto}"
: "${CFG[ARCHIVE]:=no}"

say "Paperless"
while true; do
  ask PAPERLESS_URL "Paperless URL the Pi can reach (e.g. http://paperless.lan:8000)"
  ask PAPERLESS_PUBLIC_URL "URL for links in messages, if different (- for none)"
  ask GPT_TAG "paperless-gpt's processing tag; the station waits for it to go (ignored if it doesn't exist)"
  if [[ $INTERACTIVE -eq 1 ]] && yes_no "Create or update the station's Paperless user now? Needs an admin token" \
       "$([[ -z ${CFG[PAPERLESS_TOKEN]:-} ]] && echo y || echo n)"; then
    paperless_setup
  fi
  ask PAPERLESS_TOKEN "The station user's API token" secret
  [[ -n ${CFG[PAPERLESS_URL]:-} && -n ${CFG[PAPERLESS_TOKEN]:-} ]] || {
    echo "a Paperless URL and token are required" >&2
    [[ $INTERACTIVE -eq 1 ]] && continue || exit 1
  }
  check "Paperless" paperless_ok && break
done

say "Pushover (leave both blank to only log messages)"
while true; do
  ask PUSHOVER_TOKEN "Pushover application token" secret
  ask PUSHOVER_USER "Pushover user or group key" secret
  [[ -z ${CFG[PUSHOVER_TOKEN]:-} || -z ${CFG[PUSHOVER_USER]:-} ]] && { echo "  no messages will be sent"; break; }
  check "Pushover" pushover_ok && break
done

say "Failsafe share (where scans wait when Paperless is down; blank for none)"
while true; do
  ask FAILSAFE_NFS "NFS export to mount, host:/path (- for none)"
  if [[ -n ${CFG[FAILSAFE_NFS]:-} ]]; then
    : "${CFG[FAILSAFE_DIR]:=/mnt/scanstation-failsafe}"
    check "NFS export" nfs_ok || continue
  fi
  break
done
ask FAILSAFE_DIR "Folder for failsafe copies (the NFS mount point, or a local path; - for none)"
if [[ -n ${CFG[FAILSAFE_DIR]:-} ]]; then
  ask FAILSAFE_NAME "What messages call it, for example: the NAS"
  ask ARCHIVE "Also keep a copy of every scan in an archive folder there? (yes/no)"
fi
[[ ${CFG[ARCHIVE],,} == y* ]] && CFG[ARCHIVE]=yes || CFG[ARCHIVE]=no
if [[ -n ${CFG[FAILSAFE_NFS]:-} && ${CFG[FAILSAFE_DIR]:-} =~ [[:space:]] ]]; then
  echo "the NFS mount point can't contain spaces" >&2; exit 1
fi

# --- write the config -------------------------------------------------------------------------

for k in "${KEYS[@]}"; do
  [[ ${CFG[$k]:-} == *"'"* ]] && { echo "$k can't contain a single quote" >&2; exit 1; }
done
install -d -m 750 "$(dirname "$CONFIG")"
tmp=$(mktemp "$(dirname "$CONFIG")/.scanstation.XXXXXX")
for k in "${KEYS[@]}"; do
  [[ -n ${CFG[$k]:-} ]] && printf "%s='%s'\n" "$k" "${CFG[$k]}"
done > "$tmp"
chmod 600 "$tmp"; chown root:root "$tmp"
mv "$tmp" "$CONFIG"
echo "settings written to $CONFIG"
[[ ${SETUP_CONFIG_ONLY:-} == 1 ]] && exit 0

# --- install ----------------------------------------------------------------------------------

say "Installing"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  sane-utils python3-numpy python3-pil tesseract-ocr tesseract-ocr-osd nfs-common rsync >/dev/null

id scanstation >/dev/null 2>&1 || useradd --system --home /var/lib/scanstation --shell /usr/sbin/nologin scanstation
usermod -aG scanner,lp,plugdev scanstation

install -d -m 755 /opt/scanstation
rsync -a --delete --exclude __pycache__ ./scanstation /opt/scanstation/
# SANE's default config loads every backend, and the network ones (net, airscan, escl, hplip) search
# the LAN on every call: 9 s per lookup or button poll instead of 25 ms. The service loads fujitsu only.
install -d -m 755 /etc/scanstation/sane.d
echo fujitsu > /etc/scanstation/sane.d/dll.conf
cp /etc/sane.d/fujitsu.conf /etc/scanstation/sane.d/fujitsu.conf
chmod 755 /etc/scanstation
install -m 644 systemd/scanstation.service /etc/systemd/system/scanstation.service

# scanbd wants to own the scanner; the station does that itself.
if systemctl list-unit-files scanbd.service >/dev/null 2>&1; then
  systemctl disable --now scanbd >/dev/null 2>&1 || true
fi

drop_automount() {  # remove the station's NFS automount line for a mount point, if there is one
  local d=$1 tmp
  [[ -n $d ]] && awk -v d="$d" '$2 == d && $3 ~ /^nfs/ && $4 ~ /x-systemd.automount/ {f=1} END {exit !f}' /etc/fstab || return 0
  [[ -e /etc/fstab.scanstation-backup ]] || cp -p /etc/fstab /etc/fstab.scanstation-backup
  tmp=$(mktemp /etc/.fstab.XXXXXX); cp -p /etc/fstab "$tmp"
  awk -v d="$d" '!($2 == d && $3 ~ /^nfs/ && $4 ~ /x-systemd.automount/)' /etc/fstab > "$tmp"
  mv "$tmp" /etc/fstab
  systemctl stop "$(systemd-escape --path --suffix=automount "$d")" 2>/dev/null || true
}

dir=${CFG[FAILSAFE_DIR]:-}
[[ $PREV_DIR != "$dir" ]] && drop_automount "$PREV_DIR"
if [[ -n ${CFG[FAILSAFE_NFS]:-} && -n $dir ]]; then
  # Mounted on first use with a short timeout, so a dead NAS can't hang a scan.
  mountpoint -q "$dir" || install -d -m 755 "$dir"
  line="${CFG[FAILSAFE_NFS]} $dir nfs noauto,x-systemd.automount,x-systemd.mount-timeout=10s,x-systemd.idle-timeout=10min,soft,timeo=50,retrans=2,_netdev 0 0"
  if ! grep -qxF "$line" /etc/fstab; then
    drop_automount "$dir"
    echo "$line" >> /etc/fstab
  fi
  systemctl daemon-reload
  systemctl restart "$(systemd-escape --path --suffix=automount "$dir")" || true
elif [[ -n $dir ]]; then
  drop_automount "$dir"
  systemctl daemon-reload
  # An existing folder is left as it is: it may be someone's own, or a mount the station can't chown.
  [[ -d $dir ]] || install -d -m 750 -o scanstation -g scanstation "$dir"
  sudo -u scanstation test -w "$dir" || echo "warning: the scanstation user can't write to $dir"
fi
if [[ -n $dir && ${CFG[ARCHIVE]} == yes ]]; then
  sudo -u scanstation timeout 15 mkdir -p "$dir/archive" || echo "warning: couldn't create $dir/archive (share down?)"
fi

systemctl daemon-reload
systemctl enable scanstation >/dev/null
systemctl restart scanstation
echo "scanstation: $(systemctl is-active scanstation). Follow it with: journalctl -u scanstation -f"
