#!/bin/bash
set -euo pipefail

# runs once on the Pi (googy-firstboot.service) after Imager's cloud-init setup,
# then switches the OS to a read-only root and reboots into the slideshow.

# a previous attempt may have made boot read-only before power was interrupted.
mount -o remount,rw /boot/firmware
# wait for Imager's settings, but don't stop on its exit code: harmless warnings
# (exit 2, "degraded") fail it too. save the report where any computer can read it.
cloud-init status --wait --long > /boot/firmware/googy-setup.txt 2>&1 || true
# reserve tty1 for playback and restore our boot target. this waits until now because
# Imager's user setup re-enables the tty1 login and resets the default target.
systemctl mask getty@tty1.service
systemctl set-default googy.target
# the overlay root can't be remounted; skip that unit once read-only boots begin.
systemctl mask systemd-remount-fs.service
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
