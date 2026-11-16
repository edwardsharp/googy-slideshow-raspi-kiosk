#!/bin/bash
set -euo pipefail

# build runs on the Linux host; install/partition are pi-gen hooks; firstboot runs on the Pi.
# keeping them here lets the image carry its own setup code without extra helper scripts.
build() {
    if [ "$(uname -s)" != Linux ]; then
        echo "build on Linux, or use the GitHub Actions image workflow; flash the result on any computer." >&2
        exit 1
    fi
    local repo builder revision
    repo="$(cd "$(dirname "$0")" && pwd)"
    builder="$repo/.build/pi-gen"
    # pin the builder because our stage and export hooks depend on its directory layout.
    revision=6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78
    case "$repo" in *" "*) echo "pi-gen requires a path without spaces." >&2; exit 1 ;; esac
    if [ ! -d "$builder" ]; then
        mkdir -p "$builder"
        git -C "$builder" init
        git -C "$builder" remote add origin https://github.com/RPi-Distro/pi-gen.git
        git -C "$builder" fetch --depth 1 origin "$revision"
        git -C "$builder" checkout --detach FETCH_HEAD
    fi
    if [ "$(git -C "$builder" rev-parse HEAD)" != "$revision" ]; then
        echo "unexpected pi-gen revision in $builder; use a fresh checkout of this project." >&2
        exit 1
    fi
    # stage2 is the Lite OS. our extra stage installs the player before image export.
    mkdir -p "$builder/stage-googy/00-googy/files" "$builder/export-image/04-slideshow-cache"
    cp "$repo"/{slideshow.py,slideshow.service,image.sh} "$builder/stage-googy/00-googy/files/"
    cp "$builder/stage2/prerun.sh" "$builder/stage-googy/prerun.sh"
    printf 'IMG_SUFFIX=""\n' > "$builder/stage-googy/EXPORT_IMAGE"
    printf 'fbi overlayroot\n' > "$builder/stage-googy/00-googy/00-packages"
    printf '#!/bin/bash -e\nbash files/image.sh install\n' > "$builder/stage-googy/00-googy/00-run.sh"
    # the export hook adds the cache after boot/root exist, before final compression/hashing.
    cat > "$builder/export-image/04-slideshow-cache/00-run.sh" <<'EOF'
#!/bin/bash -e
bash "${BASE_DIR}/stage-googy/00-googy/files/image.sh" partition
EOF
    chmod +x "$builder/stage-googy/00-googy/00-run.sh" \
        "$builder/export-image/04-slideshow-cache/00-run.sh"
    # export only the customized image, not a second, unmodified Lite image.
    touch "$builder/stage2/SKIP_IMAGES"
    cat > "$builder/config" <<'EOF'
IMG_NAME=googy
PI_GEN_RELEASE="googy slideshow"
RELEASE=trixie
STAGE_LIST="stage0 stage1 stage2 stage-googy"
DEPLOY_COMPRESSION=xz
ENABLE_CLOUD_INIT=1
TARGET_HOSTNAME=googy
LOCALE_DEFAULT=en_US.UTF-8
EOF
    (cd "$builder" && ./build-docker.sh)
    # reuse pi-gen's sizes/checksums. Imager needs init_format to offer Wi-Fi/user setup.
    # CI supplies an immutable release URL; local builds use this computer's file:// path.
    python3 - "$repo" "$builder/deploy" <<'PY'
import json
import os
from pathlib import Path
import shutil
import sys

repo, source = map(Path, sys.argv[1:])
metadata = max(source.glob("*.os.json"), key=lambda path: path.stat().st_mtime)
entry = json.loads(metadata.read_text())
image = source / entry.pop("image_filename")
destination = repo / "deploy"
destination.mkdir(exist_ok=True)
shutil.copy2(image, destination / image.name)
base_url = os.environ.get("GOOGY_IMAGE_BASE_URL")
entry.update(
    name="googy slideshow - Raspberry Pi OS Lite (32-bit)",
    description="console slideshow; configure Wi-Fi here, then edit slideshow.txt on the card.",
    url=f"{base_url.rstrip('/')}/{image.name}" if base_url else (destination / image.name).as_uri(),
    init_format="cloudinit-rpi",
    capabilities=["user_required"],
    devices=["pi1-32bit", "pi2-32bit", "pi3-32bit", "pi4-32bit", "pi5-32bit"],
)
(destination / "os.json").write_text(json.dumps({"os_list": [entry]}, indent=2) + "\n")
print(f"image and Imager catalogue: {destination}")
PY
}

install_image() {
    # pi-gen supplies ROOTFS_DIR and on_chroot: file edits target the image, not the host.
    install -d "$ROOTFS_DIR/usr/local/lib/googy" "$ROOTFS_DIR/var/lib/googy" \
        "$ROOTFS_DIR/etc/systemd/journald.conf.d" "$ROOTFS_DIR/etc/cloud/cloud.cfg.d"
    install -m 644 files/{slideshow.py,image.sh} "$ROOTFS_DIR/usr/local/lib/googy/"
    install -m 644 files/slideshow.service "$ROOTFS_DIR/etc/systemd/system/"
    # cloud-final runs after multi-user.target; don't make that target wait for our player.
    cat > "$ROOTFS_DIR/etc/systemd/system/googy.target" <<'EOF'
[Unit]
Description=console slideshow
Requires=multi-user.target
After=multi-user.target
Wants=slideshow.service
EOF
    # leave the first boot writable until Imager's cloud-init configuration has finished.
    # subsequent boots skip this unit when the overlay argument is on the kernel command line.
    cat > "$ROOTFS_DIR/etc/systemd/system/googy-firstboot.service" <<'EOF'
[Unit]
Description=finish provisioning before enabling the read-only kiosk
Wants=cloud-final.service
After=cloud-final.service
Before=slideshow.service
ConditionKernelCommandLine=!overlayroot=tmpfs:recurse=0
RequiresMountsFor=/boot/firmware

[Service]
Type=oneshot
ExecStart=/bin/bash /usr/local/lib/googy/image.sh firstboot
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=10min
RemainAfterExit=yes
EOF
    # logs stay in bounded RAM storage instead of writing to the SD card or filling the overlay.
    cat > "$ROOTFS_DIR/etc/systemd/journald.conf.d/googy.conf" <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=8M
EOF
    # root must not grow into the third partition reserved for persistent slideshow images.
    cat > "$ROOTFS_DIR/etc/cloud/cloud.cfg.d/99-googy.cfg" <<'EOF'
growpart:
  mode: "off"
resize_rootfs: false
EOF
    # only the cache persists during playback; temporary files use separate, bounded RAM mounts.
    # nofail keeps a missing/broken cache from sending the whole OS into emergency mode.
    cat >> "$ROOTFS_DIR/etc/fstab" <<'EOF'
LABEL=googy-cache /var/lib/googy ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2
tmpfs /tmp tmpfs nosuid,nodev,size=32M 0 0
tmpfs /var/tmp tmpfs nosuid,nodev,size=16M 0 0
EOF
    # this remains editable on another computer's FAT-mounted boot partition.
    install -m 644 /dev/null "$ROOTFS_DIR/boot/firmware/slideshow.txt"
    sed -i -E 's/(^| )resize( |$)/ /g' "$ROOTFS_DIR/boot/firmware/cmdline.txt"
    # keep the console unblanked; leave output selection and display modes to the driver.
    sed -i 's/$/ consoleblank=0 vt.global_cursor_default=0 quiet loglevel=3/' \
        "$ROOTFS_DIR/boot/firmware/cmdline.txt"
    # reserve tty1 for playback; provisioning comes from Imager rather than a local wizard.
    # disable disk swap/background package updates and check for systemd ordering cycles.
    on_chroot <<'EOF'
systemctl enable slideshow.service
systemctl set-default googy.target
systemctl mask getty@tty1.service userconfig.service rpi-resize.service \
    rpi-swap.service dphys-swapfile.service apt-daily.timer apt-daily-upgrade.timer
systemd-analyze verify /etc/systemd/system/googy.target
EOF
}

partition_cache() {
    # pi-gen's image ends after root. append a third partition without moving existing data.
    local image start size
    image="${STAGE_WORK_DIR}/${IMG_FILENAME}${IMG_SUFFIX}.img"
    start="$(stat -c %s "$image")"
    size=$((512 * 1024 * 1024))
    truncate -s "$((start + size))" "$image"
    parted --script "$image" unit B mkpart primary ext4 "$start" "$((start + size - 1))"
    # format only the appended partition; the earlier partitions are mounted by pi-gen.
    # offset is bytes, while the final size argument counts 4096-byte filesystem blocks.
    # finish metadata initialization now instead of leaving background work for the Pi.
    mke2fs -q -t ext4 -b 4096 -L googy-cache \
        -E "offset=$start,lazy_itable_init=0,lazy_journal_init=0" "$image" "$((size / 4096))"
}

firstboot() {
    # don't freeze a partly configured user/network setup, or require Google to be reachable.
    cloud-init status --wait
    # a previous attempt may have made boot read-only before power was interrupted.
    mount -o remount,rw /boot/firmware
    # save provisioning results once; rerunning cloud-init in the RAM overlay would lose writes.
    touch /etc/cloud/cloud-init.disabled
    sed -i '\|[[:space:]]/boot/firmware[[:space:]]| { /defaults,ro/! s/defaults/defaults,ro/; }' /etc/fstab
    # recurse=0 leaves the separate cache writable, rather than overlaying it in RAM.
    if ! grep -q 'overlayroot=tmpfs:recurse=0' /boot/firmware/cmdline.txt; then
        sed -i 's/^/overlayroot=tmpfs:recurse=0 /' /boot/firmware/cmdline.txt
    fi
    # these settings take effect next boot; flush them before requesting that reboot.
    sync
    echo "setup complete. rebooting into the read-only slideshow."
    systemctl --no-block reboot
}

case "${1:-build}" in
    build) build ;;
    install) install_image ;;
    partition) partition_cache ;;
    firstboot) firstboot ;;
    *) echo "usage: bash image.sh [build]" >&2; exit 1 ;;
esac
