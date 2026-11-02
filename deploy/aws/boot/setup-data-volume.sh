#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# First boot only: find the Georg data volume (the one EBS disk that is not the root
# disk), format it if it is blank, and mount it by label from /etc/fstab.
# A disk that already has a different filesystem is never formatted.
set -euo pipefail
MOUNT=${1:-/srv/georg}
LABEL=georg-data

root_disk=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" | head -n1)

find_disk() {
  lsblk -dno NAME,TYPE | awk '$2 == "disk" { print $1 }' | while read -r disk; do
    [ "$disk" = "$root_disk" ] && continue
    echo "/dev/$disk"
  done | head -n1
}

device=""
for _ in $(seq 120); do
  if blkid -L "$LABEL" >/dev/null 2>&1; then
    device=$(blkid -L "$LABEL")
    break
  fi
  device=$(find_disk)
  [ -n "$device" ] && break
  echo "waiting for the data volume to be attached..."
  sleep 5
done
[ -n "$device" ] || { echo "no data volume found" >&2; exit 1; }

if ! blkid -L "$LABEL" >/dev/null 2>&1; then
  if blkid "$device" >/dev/null 2>&1; then
    echo "$device already has a filesystem without label $LABEL; not formatting it" >&2
    exit 1
  fi
  echo "formatting $device as ext4 ($LABEL)"
  mkfs.ext4 -q -L "$LABEL" "$device"
fi

mkdir -p "$MOUNT"
if ! grep -q "LABEL=$LABEL" /etc/fstab; then
  echo "LABEL=$LABEL $MOUNT ext4 defaults,nofail,x-systemd.device-timeout=5min 0 2" >> /etc/fstab
fi
systemctl daemon-reload
mountpoint -q "$MOUNT" || mount "$MOUNT"

mkdir -p "$MOUNT/data" "$MOUNT/config" "$MOUNT/caddy/data" "$MOUNT/caddy/config"
chown 1000:1000 "$MOUNT/data"
echo "data volume $device mounted at $MOUNT"
