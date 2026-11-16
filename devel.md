# development

```sh
python3 -m unittest -v
bash -n build-image.sh firstboot.sh
shellcheck build-image.sh firstboot.sh
python3 slideshow.py --url-file /path/to/slideshow.txt --cache /path/to/cache --fetch-only
```

fetch-only works without a Pi. playback uses tty1; tty2 remains available.
optional SSH logs: `journalctl -u slideshow -u googy-firstboot -b`.
the service runs as root for console access, with filesystem writes restricted
to the cache.

## github image builds

pushes changing the player, service, image recipe, or workflow on the default
branch automatically build and publish an image. for a manual build, open
**Actions -> build SD card image -> Run workflow** and select the default branch.
on a fork, enable Actions first; no additional secrets are needed.
repository/organization policy must allow the workflow's `contents: write` permission.

each successful run uploads an image and `os.json` to a new release, then marks
it latest. the catalogue points to that release's image, so an ongoing download
can't accidentally switch to a newer build. failed builds leave the latest
published release unchanged.

the main README's URL becomes available after the first successful publication.
for a fork, substitute your owner/repository:

```text
https://github.com/OWNER/REPO/releases/latest/download/os.json
```

the workflow also saves `googy-sd-card` as an Actions artifact.

## local image build

on Linux with Docker, Git, and Python 3, run `bash build-image.sh`.
this uses pinned pi-gen to build a 32-bit Raspberry Pi OS Lite image.
the builder lives in `.build/`; the image and Imager catalogue appear in `deploy/`.
local catalogues use absolute `file:///...` image URLs; update `url` if moved.

## use custom file

instead of the main README's **Use custom URL**, select **Use custom file**
and open `os.json` from a local build, release, or Actions artifact.
release/Actions catalogues still download their image from GitHub.
for offline flashing, also download the `.img.xz` and change the catalogue's
`url` to its absolute `file:///...` URL.

don't select the raw image with **Use custom**: the catalogue supplies
`cloudinit-rpi` metadata so Imager offers Wi-Fi/user customization.

## hardware checks

test initial setup, HDMI, peak RAM during updates, offline reboot, read-only
mounts, and power-cut recovery on the actual Pi before deployment.
fetch tests don't verify the image or hardware.
first boot saves cloud-init's status report to `googy-setup.txt` on the card's
boot partition; read it on any computer if setup looks wrong.

the display driver chooses the HDMI mode from the connected monitor's EDID.
`fbi` uses its default cache and preloading behavior; measure peak RAM before
tuning them for a particular Pi. there are no Python slide-count or deck-size caps; the
512 MiB cache partition holds the current/previous decks and staged downloads.

---

made with 💖 in NYC
