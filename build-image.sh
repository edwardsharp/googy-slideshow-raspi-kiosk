#!/bin/bash
set -euo pipefail

# builds deploy/googy-*.img.xz and deploy/os.json on Linux: start from the official
# Raspberry Pi OS Lite (32-bit) image, add the player, then append a cache partition.
# firstboot.sh is the only part that runs on the Pi.

# to update the OS, copy a newer image URL and its .sha256 from downloads.raspberrypi.com.
BASE_URL=https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2026-10-06/2026-10-06-raspios-trixie-armhf-lite.img.xz
BASE_SHA256=2c302771f34c67a0b7ae7aa2e0dcdd3eac660504be59193c011ba79d412e1632

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" != 0 ]; then
    echo "run as root on Linux (sudo bash build-image.sh), or use the GitHub Actions image workflow." >&2
    exit 1
fi
repo="$(cd "$(dirname "$0")" && pwd)"
work="$repo/.build"
base="$work/$(basename "$BASE_URL")"
image="$work/googy.img"
root="$work/root"
mkdir -p "$work" "$root"
rm -rf "$repo/deploy" && mkdir "$repo/deploy"

# download once; the checksum guards both the pin and interrupted downloads.
if ! echo "$BASE_SHA256  $base" | sha256sum --check --status; then
    curl -fL --retry 3 -o "$base" "$BASE_URL"
    echo "$BASE_SHA256  $base" | sha256sum --check
fi
xz -dc "$base" > "$image"

# mount the image's boot and root partitions; the trap unmounts even if a step fails.
loop="$(losetup --partscan --show --find "$image")"
trap 'umount -R "$root" 2>/dev/null || true; losetup -d "$loop"' EXIT
mount "${loop}p2" "$root"
mount "${loop}p1" "$root/boot/firmware"
for fs in proc sys dev dev/pts; do mount --bind "/$fs" "$root/$fs"; done
# apt inside the image needs this computer's DNS; the original is restored afterward.
mv -T "$root/etc/resolv.conf" "$root/etc/resolv.conf.googy"
cp /etc/resolv.conf "$root/etc/resolv.conf"

install -d "$root/usr/local/lib/googy" "$root/var/lib/googy" \
    "$root/etc/systemd/journald.conf.d" "$root/etc/cloud/cloud.cfg.d" "$root/etc/rpi/swap.conf.d"
install -m 644 "$repo"/{slideshow.py,slides_png.py,slides_pdf.py,firstboot.sh} "$root/usr/local/lib/googy/"
install -m 644 "$repo/slideshow.service" "$root/etc/systemd/system/"
# cloud-final runs after multi-user.target; don't make that target wait for our player.
cat > "$root/etc/systemd/system/googy.target" <<'EOF'
[Unit]
Description=console slideshow
Requires=multi-user.target
After=multi-user.target
Wants=slideshow.service
EOF
# leave the first boot writable until Imager's cloud-init configuration has finished.
# subsequent boots skip this unit when the overlay argument is on the kernel command line.
cat > "$root/etc/systemd/system/googy-firstboot.service" <<'EOF'
[Unit]
Description=finish provisioning before enabling the read-only kiosk
Wants=cloud-final.service
After=cloud-final.service
Before=slideshow.service
ConditionKernelCommandLine=!overlayroot=tmpfs:recurse=0
RequiresMountsFor=/boot/firmware

[Service]
Type=oneshot
ExecStart=/bin/bash /usr/local/lib/googy/firstboot.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=10min
RemainAfterExit=yes
EOF
# logs stay in bounded RAM storage instead of writing to the SD card or filling the overlay.
cat > "$root/etc/systemd/journald.conf.d/googy.conf" <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=8M
EOF
# root must not grow into the third partition reserved for persistent slideshow images.
cat > "$root/etc/cloud/cloud.cfg.d/99-googy.cfg" <<'EOF'
growpart:
  mode: "off"
resize_rootfs: false
EOF
# compressed swap in RAM only; a swap file would write to the SD card (or fill the overlay).
cat > "$root/etc/rpi/swap.conf.d/googy.conf" <<'EOF'
[Main]
Mechanism=zram
EOF
# only the cache persists during playback; temporary files use separate, bounded RAM mounts.
# nofail keeps a missing/broken cache from sending the whole OS into emergency mode.
cat >> "$root/etc/fstab" <<'EOF'
LABEL=googy-cache /var/lib/googy ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
tmpfs /tmp tmpfs nosuid,nodev,size=32M 0 0
tmpfs /var/tmp tmpfs nosuid,nodev,size=16M 0 0
EOF
# this remains editable on another computer's FAT-mounted boot partition.
install -m 644 /dev/null "$root/boot/firmware/slideshow.txt"
sed -i -E 's/(^| )resize( |$)/ /g' "$root/boot/firmware/cmdline.txt"
# keep the console unblanked; leave output selection and display modes to the driver.
sed -i 's/$/ consoleblank=0 vt.global_cursor_default=0 quiet loglevel=3/' "$root/boot/firmware/cmdline.txt"

# commands inside the image run through qemu-user-static on non-ARM computers.
# provisioning comes from Imager rather than a local wizard; firstboot.sh frees tty1 later,
# because Imager's user setup fails if the tty1 login is already masked.
# disable background package updates and check for systemd ordering cycles.
chroot "$root" /bin/bash -euo pipefail <<'EOF'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends fbi ghostscript overlayroot
apt-get clean
systemctl enable slideshow.service
systemctl set-default googy.target
systemctl mask userconfig.service rpi-resize.service apt-daily.timer apt-daily-upgrade.timer
systemd-analyze verify /etc/systemd/system/googy.target
EOF
mv -T "$root/etc/resolv.conf.googy" "$root/etc/resolv.conf"
# release freed blocks so they compress to nothing.
fstrim "$root"
umount -R "$root"
losetup -d "$loop"
trap - EXIT

# the image ends after root. append a third partition without moving existing data.
start="$(stat -c %s "$image")"
size=$((512 * 1024 * 1024))
truncate -s "$((start + size))" "$image"
parted --script "$image" unit B mkpart primary ext4 "$start" "$((start + size - 1))"
# offset is bytes, while the final size argument counts 4096-byte filesystem blocks.
# finish metadata initialization now instead of leaving background work for the Pi.
mke2fs -q -t ext4 -b 4096 -L googy-cache \
    -E "offset=$start,lazy_itable_init=0,lazy_journal_init=0" "$image" "$((size / 4096))"

# xz level 1 compresses several times faster than the default level 6, for a slightly larger file.
xz -T0 -1 -c "$image" > "$repo/deploy/googy-$(date -u +%F).img.xz"
# Imager needs init_format to offer Wi-Fi/user setup.
# CI supplies an immutable release URL; local builds use this computer's file:// path.
python3 - "$image" "$repo"/deploy/googy-*.img.xz <<'PY'
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys

image, download = map(Path, sys.argv[1:])

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()

base_url = os.environ.get("GOOGY_IMAGE_BASE_URL")
entry = {
    "name": "googy slideshow - Raspberry Pi OS Lite (32-bit)",
    "description": "console slideshow; configure Wi-Fi here, then edit slideshow.txt on the card.",
    "url": f"{base_url.rstrip('/')}/{download.name}" if base_url else download.resolve().as_uri(),
    "extract_size": image.stat().st_size,
    "extract_sha256": sha256(image),
    "image_download_size": download.stat().st_size,
    "image_download_sha256": sha256(download),
    "release_date": date.today().isoformat(),
    "init_format": "cloudinit-rpi",
    "capabilities": ["user_required"],
    "devices": ["pi1-32bit", "pi2-32bit", "pi3-32bit", "pi4-32bit", "pi5-32bit"],
}
(download.parent / "os.json").write_text(json.dumps({"os_list": [entry]}, indent=2) + "\n")
print(f"image and Imager catalogue: {download.parent}")
PY
