#!/usr/bin/env python3
"""published Google Slides on a Linux console; no browser or Python packages."""

import argparse
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen
import zlib

CHECK_SECONDS = 300
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LOG = logging.getLogger("slideshow")


def configuration(path):
    """read the one-line config and derive Google's static-view URL and interval."""
    url = path.read_text(encoding="utf-8-sig").strip()
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "docs.google.com"
        or not re.fullmatch(r"/presentation/d/(?:e/)?[\w-]+/(?:pub|embed)", parts.path)
    ):
        raise ValueError("paste a Google Slides 'publish to the web' URL into slideshow.txt")
    delays = parse_qs(parts.query).get("delayms", [])
    if len(delays) != 1 or not delays[0].isdigit():
        raise ValueError("the slideshow URL must contain one delayms value")
    milliseconds = int(delays[0])
    if not 1000 <= milliseconds <= 86400000 or milliseconds % 1000:
        raise ValueError("delayms must be whole seconds, from 1000 to 86400000")
    page = urlunsplit(parts._replace(path=parts.path.rsplit("/", 1)[0] + "/htmlpresent",
                                    query="", fragment=""))
    return page, milliseconds // 1000


class Slides(HTMLParser):
    """collect slide backgrounds in presentation order, without executing JavaScript."""

    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "slide-content" in attrs.get("class", "").split():
            match = re.search(r"background-image:\s*url\(([^)]+)\)", attrs.get("style", ""))
            if not match:
                raise ValueError("Google's slide markup changed: missing image URL")
            self.urls.append(match[1])


def slide_urls(html, page):
    """turn the static HTML view's backgrounds into complete slide images."""
    parser = Slides()
    parser.feed(html)
    if not parser.urls:
        raise ValueError("no slides found in Google's response")
    images, revisions = [], []
    for url in parser.urls:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "docs.google.com" or (
            parts.path != urlsplit(page).path.rsplit("/", 1)[0] + "/viewpage"
        ):
            raise ValueError("unexpected Google slide image URL")
        query = parse_qs(parts.query)
        try:
            page_id, revision = query["pageid"][0], query["rev"][0]
        except (KeyError, IndexError) as error:
            raise ValueError("Google's slide image parameters changed") from error
        if not page_id or not revision:
            raise ValueError("invalid slide ID or revision")
        # the HTML view draws text over its backgrounds; ask Google to include it in the PNG.
        query["showText"] = ["1"]
        images.append(urlunsplit(parts._replace(query=urlencode(query, doseq=True))))
        revisions.append((page_id, revision))
    if len(set(revisions)) != len(revisions):
        raise ValueError("duplicate slides in Google's response")
    return images, revisions


def fetch(url, deadline):
    """use network timeouts and a shared deadline so an update cannot hang indefinitely."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("slideshow update exceeded three minutes")
    with urlopen(url, timeout=min(20, remaining)) as response:
        chunks = []
        while chunk := response.read(64 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError("slideshow update exceeded three minutes")
            chunks.append(chunk)
        return b"".join(chunks)


def check_png(data):
    """reject truncated downloads and bad chunk checksums before changing the cache."""
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Google returned something other than a PNG")
    offset, image_data = 8, False
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 8 + length
        if end + 4 > len(data):
            break
        # each PNG chunk is: length, type, payload, checksum of type + payload.
        if zlib.crc32(data[offset + 4:end]) != struct.unpack_from(">I", data, end)[0]:
            raise ValueError("corrupt PNG checksum")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError("missing PNG header")
        image_data |= kind == b"IDAT"
        if kind == b"IEND" and length == 0 and end + 4 == len(data) and image_data:
            return
        offset = end + 4
    raise ValueError("incomplete PNG")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path, data):
    # flush Python's buffer, then the filesystem's buffer, before publishing a new deck.
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class Deck:
    directory: Path
    key: str
    seconds: int


def read_deck(path):
    """check a complete cached generation before handing its files to the viewer."""
    directory = path.resolve(strict=True)
    if directory.parent != path.parent.resolve() or not directory.name.startswith("deck-"):
        raise ValueError("invalid cache directory")
    metadata = json.loads((directory / "deck.json").read_text())
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("key"), str)
        or type(metadata.get("seconds")) is not int
        or not 1 <= metadata["seconds"] <= 86400
        or type(metadata.get("count")) is not int
        or metadata["count"] < 1
    ):
        raise ValueError("invalid slideshow cache metadata")
    for index in range(metadata["count"]):
        image = directory / f"{index:03}.png"
        check_png(image.read_bytes())
    return Deck(directory, metadata["key"], metadata["seconds"])


def cached_deck(cache):
    # the previous generation is retained in case the latest one is damaged by a power cut.
    for name in ("current", "previous"):
        try:
            return read_deck(cache / name)
        except (OSError, ValueError) as error:
            LOG.warning("cannot use %s cache: %s", name, error)
    return None


def link_deck(cache, name, directory):
    # rename a relative symlink on the same filesystem so readers see one complete deck.
    temporary = cache / ".next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(directory.name)
    temporary.replace(cache / name)
    sync_directory(cache)


def prune(cache):
    keep = {(cache / name).resolve() for name in ("current", "previous")}
    for directory in cache.iterdir():
        if (
            directory.is_dir() and not directory.is_symlink() and directory not in keep
            and directory.name.startswith(("deck-", ".incoming-"))
        ):
            shutil.rmtree(directory)


def refresh(config, cache, active):
    """stage a changed deck, then activate it without touching the playing generation."""
    page, seconds = configuration(config)
    deadline = time.monotonic() + 180
    urls, revisions = slide_urls(fetch(page, deadline).decode("utf-8"), page)
    # signed image URLs expire/change even when the slides do not; compare stable revisions.
    key = hashlib.sha256(json.dumps([page, seconds, revisions]).encode()).hexdigest()
    if active and active.key == key:
        LOG.info("no slideshow changes")
        return active
    prune(cache)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=cache))
    try:
        for index, url in enumerate(urls):
            data = fetch(url, deadline)
            check_png(data)
            save(incoming / f"{index:03}.png", data)
        save(incoming / "deck.json", json.dumps({
            "key": key, "seconds": seconds, "count": len(urls),
        }).encode())
        sync_directory(incoming)
        directory = cache / ("deck-" + incoming.name.removeprefix(".incoming-"))
        incoming.rename(directory)
        sync_directory(cache)
        # keep the old files until the viewer has switched, and as the next boot's fallback.
        if active:
            link_deck(cache, "previous", active.directory)
        link_deck(cache, "current", directory)
        LOG.info("cached %s slides, %ss each", len(urls), seconds)
        return Deck(directory, key, seconds)
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)


def stop_player(player):
    if player is not None and player.poll() is None:
        player.terminate()
        try:
            player.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", type=Path, default=Path("/boot/firmware/slideshow.txt"))
    parser.add_argument("--cache", type=Path, default=Path("/var/lib/googy"))
    parser.add_argument("--fetch-only", action="store_true", help="update the cache without a display")
    args = parser.parse_args()
    args.cache = args.cache.resolve()
    args.cache.mkdir(parents=True, exist_ok=True)
    active = cached_deck(args.cache)
    player, playing, next_check = None, None, 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            if active and playing != active:
                # fbi advances slides itself, so synchronous downloads cannot pause playback.
                stop_player(player)
                player = subprocess.Popen([
                    "fbi", "-T", "1", "-a", "--noverbose", "--noedit", "--nointeractive",
                    "--blend", "0",
                    "-t", str(active.seconds), *map(str, sorted(active.directory.glob("*.png"))),
                ]) if not args.fetch_only else None
                playing = active
            if player is not None and player.poll() is not None:
                raise RuntimeError(f"console viewer exited with status {player.returncode}")
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + CHECK_SECONDS
                try:
                    active = refresh(args.url_file, args.cache, active)
                except (OSError, ValueError) as error:
                    LOG.error("update failed; keeping cached slides: %s", error)
                    if args.fetch_only:
                        raise
                    if player is None:
                        print(f"waiting for slideshow. {error}\nretrying in five minutes.", flush=True)
                if args.fetch_only:
                    return
            time.sleep(1)
    finally:
        stop_player(player)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
