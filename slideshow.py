#!/usr/bin/env python3
"""published Google Slides on a Linux console; no browser or Python packages."""

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import zlib

import slides_pdf
import slides_png

CHECK_SECONDS = 300
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LOG = logging.getLogger("slideshow")


class UpdateError(ValueError):
    """an update failure with a public-safe error code."""

    def __init__(self, code, error):
        super().__init__(str(error))
        self.code = code


def configuration(path):
    """read the published URL and optional original-file URL from the boot partition."""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip()]
    except (OSError, UnicodeError) as error:
        raise UpdateError("E04-F", error) from error
    if not 1 <= len(lines) <= 2:
        raise UpdateError("E04-F", "slideshow.txt needs a published URL and optionally a share URL")
    url = lines[0]
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "docs.google.com"
        or not re.fullmatch(r"/presentation/d/(?:e/)?[\w-]+/(?:pub|embed)", parts.path)
    ):
        raise UpdateError("E04-L", "paste a Google Slides 'publish to the web' URL into slideshow.txt")
    delays = parse_qs(parts.query).get("delayms", [])
    if len(delays) != 1 or not delays[0].isdigit():
        raise UpdateError("E04-T", "the slideshow URL must contain one delayms value")
    try:
        milliseconds = int(delays[0])
    except ValueError as error:
        raise UpdateError("E04-T", error) from error
    if not 1000 <= milliseconds <= 86400000 or milliseconds % 1000:
        raise UpdateError("E04-T", "delayms must be whole seconds, from 1000 to 86400000")
    page = urlunsplit(parts._replace(path=parts.path.rsplit("/", 1)[0] + "/htmlpresent",
                                    query="", fragment=""))
    pdf = None
    if len(lines) == 2:
        share = urlsplit(lines[1])
        match = re.fullmatch(r"/presentation/d/([\w-]+)/(?:edit|view|preview|export/pdf)", share.path)
        if share.scheme != "https" or share.netloc != "docs.google.com" or not match:
            raise UpdateError("E04-L", "the second line must be the original Google Slides share link")
        pdf = f"https://docs.google.com/presentation/d/{match[1]}/export/pdf"
    return page, milliseconds // 1000, pdf


def screen_size():
    """match slides to the framebuffer so fbi shows them without scaling."""
    try:
        width, height = Path("/sys/class/graphics/fb0/virtual_size").read_text().split(",")
        return int(width), int(height)
    except (OSError, ValueError):
        return 1920, 1080


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


def fetch_source(url, deadline, step):
    """label network errors without treating local filesystem errors as network errors."""
    try:
        return fetch(url, deadline)
    except HTTPError as error:
        error.close()
        raise
    except (URLError, OSError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.gaierror):
            code = "E05"
        elif getattr(reason, "errno", None) in (errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH):
            code = "E06"
        else:
            code = "E07"
        raise UpdateError(f"{code}-{step}", error) from error


def source_error(error, content_code, step):
    """label HTTP, storage, and source-specific failures after an update attempt."""
    if isinstance(error, UpdateError):
        return error
    if isinstance(error, HTTPError) or isinstance(error.__cause__, HTTPError):
        code = "E08"
    elif isinstance(error, OSError):
        return UpdateError("E11", error)
    else:
        code = content_code
    return UpdateError(f"{code}-{step}", error)


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


def stage_deck(cache, active, seconds, key, produce):
    """publish a deck only after the chosen source has produced every slide."""
    if active and active.key == key:
        LOG.info("no slideshow changes")
        return active
    prune(cache)
    incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=cache))
    try:
        count = produce(incoming)
        save(incoming / "deck.json", json.dumps({
            "key": key, "seconds": seconds, "count": count,
        }).encode())
        sync_directory(incoming)
        directory = cache / ("deck-" + incoming.name.removeprefix(".incoming-"))
        incoming.rename(directory)
        sync_directory(cache)
        # keep the old files until the viewer has switched, and as the next boot's fallback.
        if active:
            link_deck(cache, "previous", active.directory)
        link_deck(cache, "current", directory)
        LOG.info("cached %s slides, %ss each", count, seconds)
        return Deck(directory, key, seconds)
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)


def refresh(config, cache, active):
    """try the published PNG source first, then the optional PDF source."""
    try:
        page, seconds, pdf = configuration(config)
    except UpdateError:
        raise
    except ValueError as error:
        raise UpdateError("E04-L", error) from error
    size = screen_size()
    deadline = time.monotonic() + 180
    step = "P1"
    try:
        urls, revisions = slides_png.slide_urls(
            fetch_source(page, deadline, step).decode("utf-8"), page, size
        )
        key = hashlib.sha256(json.dumps([page, seconds, size, revisions]).encode()).hexdigest()
        step = "P2"
        return stage_deck(
            cache, active, seconds, key,
            lambda directory: slides_png.download(
                urls, directory, deadline,
                lambda url, limit: fetch_source(url, limit, "P2"), check_png, save
            ),
        )
    except (OSError, ValueError) as error:
        failure = source_error(error, "E09", step)
        if not pdf:
            if failure is error:
                raise
            raise failure from error
        LOG.warning("published PNG update failed (%s): %s; trying PDF",
                    failure.code, error, exc_info=True)
    # the PDF gets its own deadline if the PNG source used up its time.
    deadline = time.monotonic() + 180
    step = "D1"
    try:
        data = slides_pdf.fetch_pdf(
            pdf, deadline, lambda url, limit: fetch_source(url, limit, "D1")
        )
        key = hashlib.sha256(json.dumps([
            pdf, seconds, size, hashlib.sha256(data).hexdigest(),
        ]).encode()).hexdigest()

        def render_pdf(directory):
            try:
                return slides_pdf.render(data, size, directory, deadline, check_png, save)
            except TimeoutError as error:
                raise UpdateError("E10-D2", error) from error

        step = "D2"
        return stage_deck(cache, active, seconds, key, render_pdf)
    except slides_pdf.AccessDenied as error:
        raise UpdateError("E12", error) from error
    except (OSError, ValueError) as error:
        failure = source_error(error, "E10", step)
        if failure is error:
            raise
        raise failure from error


def stop_player(player):
    if player is not None and player.poll() is None:
        player.terminate()
        try:
            player.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()


def screen_message(message=""):
    """clear the console to black and show only a short, public-safe message."""
    print(f"\x1b[0m\x1b[37;40m\x1b[2J\x1b[H\x1b[?25l{message}", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", type=Path, default=Path("/boot/firmware/slideshow.txt"))
    parser.add_argument("--cache", type=Path, default=Path("/var/lib/googy"))
    parser.add_argument("--fetch-only", action="store_true", help="update the cache without a display")
    args = parser.parse_args()
    try:
        args.cache = args.cache.resolve()
        args.cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        if not args.fetch_only:
            screen_message("error E03")
        raise
    active = cached_deck(args.cache)
    player, playing, next_check, failures = None, None, 0, 0
    if not active and not args.fetch_only:
        screen_message()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            if active and playing != active:
                # fbi advances slides itself, so synchronous downloads cannot pause playback.
                stop_player(player)
                try:
                    player = subprocess.Popen([
                        "fbi", "-T", "1", "-a", "--noverbose", "--noedit", "--nointeractive",
                        "--blend", "0",
                        "-t", str(active.seconds),
                        *map(str, sorted(active.directory.glob("*.png"), key=lambda path: int(path.stem))),
                    ]) if not args.fetch_only else None
                except OSError:
                    screen_message("error E02")
                    raise
                playing = active
            if player is not None and player.poll() is not None:
                screen_message("error E02")
                raise RuntimeError(f"console viewer exited with status {player.returncode}")
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + CHECK_SECONDS
                try:
                    active = refresh(args.url_file, args.cache, active)
                    failures = 0
                except (OSError, ValueError) as error:
                    failures += 1
                    code = error.code if isinstance(error, UpdateError) else "E01"
                    LOG.error("update failed (%s): %s", code, error, exc_info=True)
                    if args.fetch_only:
                        raise
                    if player is None:
                        # nothing to show yet (e.g. Wi-Fi still connecting); try again soon.
                        next_check = time.monotonic() + 30
                        screen_message(f"error {code} | failures: {failures}")
                if args.fetch_only:
                    return
                if active and playing != active:
                    continue
            time.sleep(1)
    finally:
        stop_player(player)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
