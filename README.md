# googy-slideshow-raspi-kiosk

googy slides on a Raspberry Pi's HDMI console.

no browser, desktop, keyboard, or SSH required.

## SD card setup

1. download & run [**Raspberry Pi Imager 2.x**](https://www.raspberrypi.com/software/), open **App Options -> Content Repository ->
   Edit -> Use custom URL**, paste this catalogue URL, and apply:

   ```text
   https://github.com/edwardsharp/googy-slideshow-raspi-kiosk/releases/latest/download/os.json
   ```

2. choose the googy image. use the Pi Imager app interface to complete setup and write card: Wi-Fi settings
   **including country**, optionally add ssh key or enter a username/password, etc.
3. unplug + reinsert the card on your computer; on its visible boot partition, open
   `slideshow.txt` in a plain-text editor and paste your Google Slides
   **File -> Share -> Publish to the web** link and save the file. _note:_ keep the full URL:
   `delayms=5000` means five seconds per slide. verify it opens without signing in.
4. safely eject sd card and insert into raspberry pi, connect HDMI, and power on. allow several minutes,
   first boot saves the settings and reboots once. **don't interrupt initial setup!**

checks for changes every five minutes and plays cached slides offline.
static slides only; whole-second timing; always loops. the OS is read-only,
with a separate writable cache. this reduces, but cannot eliminate,
power-cut corruption. Google's image interface is undocumented and may change.

[development, image builds, and alternative setup](devel.md)

---

made with 💖 in NYC
