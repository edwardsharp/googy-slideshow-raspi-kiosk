# devel business

```sh
python3 -m unittest -v
bash -n build-image.sh firstboot.sh
shellcheck build-image.sh firstboot.sh
python3 slideshow.py --url-file /path/to/slideshow.txt --cache /path/to/cache --fetch-only
```

fetch-only works without a Pi. playback uses tty1; tty2 remains available for
other stuff. optional SSH logs: `journalctl -u slideshow -u googy-firstboot -b`.
the service runs as root for console access, with filesystem writes restricted
to the cache.

## slideshow sources

here's an example slideshow: https://docs.google.com/presentation/d/e/2PACX-1vT3yjbwXlbP3pDD-LUF0625sQs5p1Y_0Z7BAeIoWq6N83eerVWHnCVRhth-XHNxosYqCoLkevTtAR4r/pub?start=true&loop=true&delayms=5000

`slides_png.py` finds the published page's `/viewpage` image URLs by their slide
IDs and revisions, regardless of the surrounding tag or class. it adds text and
requests the screen size. `slides_pdf.py` downloads a PDF from the optional
original share link and uses Ghostscript (`gs`) to render its pages. both feed
the same cache and player in `slideshow.py`: a complete, validated deck replaces
the old one only after all pages are ready. PNG is tried first on every update;
if it fails, PDF is tried when a share link is configured.
the PDF is an export of the original file, so it may differ from the published
view if publishing settings or changes aren't in sync.
the image parser cannot detect a slide missing entirely from Google's HTML.

to test your own deck, put its published URL and optional original share URL on
separate lines in `slideshow.txt` (see the README for PDF permissions), then run:

```sh
GOOGY_LIVE_TEST=1 GOOGY_TEST_URL_FILE=/path/to/slideshow.txt python3 -m unittest -v test_slideshow.LiveSourceTests
```

this needs internet and Ghostscript for PDF tests. without a share URL, PDF tests
are skipped. omit `GOOGY_TEST_URL_FILE` to test the example decks instead.
ordinary `python3 -m unittest -v` tests run without internet or Ghostscript.

## github image builds

pushes changing the player, service, image recipe, or workflow on the default
branch automatically build and publish an image. for a manual build, open
**actions -> build SD card image -> Run workflow** and select the default
branch. on a fork, enable actions first; repository/organization policy must
allow the workflow's `contents: write` permission.

each successful run uploads an image and `os.json` to a new release, then marks
it latest.

for a fork, substitute your owner/repository:

```text
https://github.com/OWNER/REPO/releases/latest/download/os.json
```

the github actions workflow saves `googy-sd-card` as an actions artifact.

## local image build

on kinux with `qemu-user-static`, `parted`, and python 3, run
`sudo bash build-image.sh`. this downloads the pinned official 32-bit Raspberry
Pi OS Lite image, expands its root partition, adds the slideshow player, and
appends the cache partition.
downloads and work files live in `.build/`; the image and Imager catalogue
appear in `deploy/`. local catalogues use absolute `file:///...` image URLs;
update `url` if moved.

## use custom file

instead of the main README's **Use custom URL**, select **Use custom file** and
open `os.json` from a local build, release, or actions artifact. release/actions
catalogues still download their image from gihub. for offline flashing, also
download the `.img.xz` and change the catalogue's `url` to its absolute
`file:///...` URL.

don't select the raw image with **Use custom**: the catalogue supplies
`cloudinit-rpi` metadata so imager offers wi-fi/user/etc. customization.

## hardware checks

first boot saves cloud-init's status report to `googy-setup.txt` on the card's
boot partition; read it on any computer if setup looks wrong.

the display driver chooses the HDMI mode from the connected monitor's EDID.
`fbi` uses its default cache and image preloading behavior. there are no python
slide-count or deck-size caps; the 512 MiB disk cache partition holds the
current/previous decks and staged downloads.

---

made with 💖 in NYC
