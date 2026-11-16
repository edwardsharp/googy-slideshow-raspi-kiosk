import errno
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
import zlib

import slideshow as app

URL = "https://docs.google.com/presentation/d/e/DEMO/pub?start=true&loop=true&delayms=5000"
PAGE = URL.split("/pub")[0] + "/htmlpresent"


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (
        app.PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def markup(revision="1", pages=("first", "second"), timestamp="123"):
    return "".join(
        '<section class="slide-content" style="background-image: url('
        f'{PAGE.rsplit("/", 1)[0]}/viewpage?pageid={page}&amp;rev={revision}'
        f'&amp;hmac=signature&amp;ts={timestamp}&amp;w=960&amp;h=540'
        '&amp;format=png&amp;showText=0);"></section>'
        for page in pages
    )


class ConfigurationTests(unittest.TestCase):
    def test_published_url_and_bom_windows_line_endings(self):
        path = Mock()
        path.read_text.return_value = URL + "\r\n"
        self.assertEqual(app.configuration(path), (PAGE, 5))
        path.read_text.assert_called_once_with(encoding="utf-8-sig")

    def test_rejects_unusable_urls_and_timing(self):
        path = Mock()
        for url in (
            "",
            URL.replace("docs.google.com", "example.com"),
            URL.replace("https:", "http:"),
            URL.replace("/pub?", "/edit?"),
            URL.replace("delayms=5000", ""),
            URL + "&delayms=3000",
            URL.replace("5000", "0"),
            URL.replace("5000", "1500"),
            URL.replace("5000", "86401000"),
        ):
            with self.subTest(url=url):
                path.read_text.return_value = url
                with self.assertRaises(ValueError):
                    app.configuration(path)


class ParsingTests(unittest.TestCase):
    def test_preserves_order_signature_dimensions_and_includes_text(self):
        urls, revisions = app.slide_urls(markup(pages=("second", "first")), PAGE)
        self.assertEqual(revisions, [("second", "1"), ("first", "1")])
        first = parse_qs(urlsplit(urls[0]).query)
        self.assertEqual(
            {key: first[key] for key in ("pageid", "hmac", "w", "h", "showText")},
            {"pageid": ["second"], "hmac": ["signature"],
             "w": ["960"], "h": ["540"], "showText": ["1"]},
        )

    def test_preserves_large_portrait_dimensions_without_an_arbitrary_cap(self):
        html = markup().replace("w=960&amp;h=540", "w=2160&amp;h=3840")
        urls, _ = app.slide_urls(html, PAGE)
        query = parse_qs(urlsplit(urls[0]).query)
        self.assertEqual((query["w"], query["h"]), (["2160"], ["3840"]))

    def test_accepts_more_than_two_hundred_slides(self):
        urls, _ = app.slide_urls(markup(pages=tuple(map(str, range(250)))), PAGE)
        self.assertEqual(len(urls), 250)

    def test_rejects_changed_markup_login_pages_and_unexpected_image_hosts(self):
        for html in (
            "<html>sign in</html>",
            '<section class="slide-content"></section>',
            markup().replace("docs.google.com", "example.com"),
            markup(pages=("same", "same")),
        ):
            with self.subTest(html=html[:100]), self.assertRaises(ValueError):
                app.slide_urls(html, PAGE)

    def test_rejects_truncated_and_corrupt_pngs(self):
        app.check_png(png())
        for data in (b"<html>permission denied</html>", png()[:-5], png()[:-1] + b"x"):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                app.check_png(data)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.config = self.root / "slideshow.txt"
        self.config.write_text("\ufeff" + URL + "\r\n", encoding="utf-8")
        self.html = markup()
        self.download = patch.object(
            app, "fetch", side_effect=lambda url, *_: self.html.encode() if url == PAGE else png(),
        ).start()
        self.addCleanup(patch.stopall)

    def refresh(self, active=None):
        return app.refresh(self.config, self.cache, active)

    def test_complete_deck_survives_reload_and_unchanged_checks_make_no_writes(self):
        deck = self.refresh()
        self.assertEqual(app.cached_deck(self.cache), deck)
        original_files = sorted(self.cache.rglob("*"))
        self.html = markup(timestamp="new-expiring-token")
        self.download.reset_mock()
        self.assertEqual(self.refresh(deck), deck)
        self.assertEqual(sorted(self.cache.rglob("*")), original_files)
        self.download.assert_called_once()

    def test_failed_partial_download_does_not_replace_playable_deck(self):
        deck = self.refresh()
        self.html = markup(revision="2")
        self.download.side_effect = [self.html.encode(), png(), b"broken second slide"]
        with self.assertRaises(ValueError):
            self.refresh(deck)
        self.assertEqual(app.cached_deck(self.cache), deck)
        self.assertFalse(list(self.cache.glob(".incoming-*")))

    def test_network_failure_keeps_persistent_cache(self):
        deck = self.refresh()
        self.download.side_effect = URLError("Wi-Fi down")
        with self.assertRaises(URLError):
            self.refresh(deck)
        self.assertEqual(app.cached_deck(self.cache), deck)

    def test_corrupt_latest_deck_falls_back_to_previous(self):
        previous = self.refresh()
        self.html = markup(revision="2")
        current = self.refresh(previous)
        (current.directory / "000.png").write_bytes(png()[:-5])
        with self.assertLogs("slideshow", level="WARNING"):
            self.assertEqual(app.cached_deck(self.cache), previous)

    def test_previous_and_current_survive_cleanup_but_stale_downloads_do_not(self):
        first = self.refresh()
        self.html = markup(revision="2")
        second = self.refresh(first)
        stale = self.cache / ".incoming-interrupted"
        stale.mkdir()
        (stale / "partial.png").write_bytes(b"partial")
        self.html = markup(revision="3", pages=("only-slide",))
        third = self.refresh(second)
        app.prune(self.cache)
        self.assertFalse(first.directory.exists())
        self.assertFalse(stale.exists())
        self.assertEqual(app.read_deck(self.cache / "previous"), second)
        self.assertEqual(app.cached_deck(self.cache), third)
        self.assertEqual(len(list(third.directory.glob("*.png"))), 1)

    def test_bad_cache_metadata_tries_previous_instead_of_crashing(self):
        deck = self.refresh()
        (deck.directory / "deck.json").write_text(json.dumps([]))
        with self.assertLogs("slideshow", level="WARNING"):
            self.assertIsNone(app.cached_deck(self.cache))

    def test_failed_pointer_switch_preserves_previous_and_next_update_recovers(self):
        first = self.refresh()
        self.html = markup(revision="2")
        original = app.link_deck

        def interrupted(cache, name, directory):
            if name == "current":
                raise OSError("power interrupted activation")
            return original(cache, name, directory)

        with patch.object(app, "link_deck", side_effect=interrupted), self.assertRaises(OSError):
            self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), first)
        second = self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), second)

    def test_full_disk_never_replaces_playable_cache(self):
        first = self.refresh()
        self.html = markup(revision="2")
        with (
            patch.object(app, "save", side_effect=OSError(errno.ENOSPC, "no space left")),
            self.assertRaises(OSError),
        ):
            self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), first)
        self.assertFalse(list(self.cache.glob(".incoming-*")))


class RuntimeTests(unittest.TestCase):
    def test_cached_playback_starts_before_offline_fetch_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = app.Deck(Path(directory), "cached", 5)
            calls = []
            player = Mock()
            player.poll.return_value = None

            def start(*args):
                calls.append("play")
                return player

            def offline(*args):
                calls.append("fetch")
                raise URLError("offline")

            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=deck),
                patch.object(app.subprocess, "Popen", side_effect=start),
                patch.object(app, "refresh", side_effect=offline),
                patch.object(app.signal, "signal"),
                patch.object(app.time, "sleep", side_effect=KeyboardInterrupt),
                self.assertLogs("slideshow", level="ERROR"),
                self.assertRaises(KeyboardInterrupt),
            ):
                app.main()
            self.assertEqual(calls, ["play", "fetch"])
            player.terminate.assert_called_once()
            player.wait.assert_called_once_with(timeout=5)

    def test_fetch_checks_deadline_even_while_data_keeps_arriving(self):
        with (
            patch.object(app, "urlopen", return_value=io.BytesIO(b"partial image")),
            patch.object(app.time, "monotonic", side_effect=[0, 11]),
            self.assertRaises(TimeoutError),
        ):
            app.fetch("https://example.com", 10)


class CatalogueTests(unittest.TestCase):
    def test_local_and_fork_release_catalogues_keep_image_metadata(self):
        # exercise the recipe's Python packaging step without building an entire Linux image.
        recipe = Path(__file__).with_name("build-image.sh").read_text()
        packaging = recipe.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.mkdir()
            (source / "googy.img.xz").write_bytes(b"image fixture")
            metadata = {
                "image_filename": "googy.img.xz",
                "extract_size": 4096,
                "extract_sha256": "a" * 64,
                "image_download_sha256": "b" * 64,
            }
            (source / "googy.os.json").write_text(json.dumps(metadata))
            base_url = "https://github.com/example/fork/releases/download/image-42/"
            for base in (None, base_url):
                with self.subTest(base=base):
                    environment = os.environ.copy()
                    environment.pop("GOOGY_IMAGE_BASE_URL", None)
                    if base:
                        environment["GOOGY_IMAGE_BASE_URL"] = base
                    subprocess.run(
                        [sys.executable, "-c", packaging, str(root), str(source)],
                        env=environment, check=True, capture_output=True, text=True,
                    )
                    catalogue = json.loads((root / "deploy/os.json").read_text())
                    entry, = catalogue["os_list"]
                    expected_url = (
                        base + "googy.img.xz" if base
                        else (root / "deploy/googy.img.xz").as_uri()
                    )
                    self.assertEqual(entry["url"], expected_url)
                    self.assertEqual(entry["init_format"], "cloudinit-rpi")
                    for field in ("extract_size", "extract_sha256", "image_download_sha256"):
                        self.assertEqual(entry[field], metadata[field])
                    self.assertEqual((root / "deploy/googy.img.xz").read_bytes(), b"image fixture")


if __name__ == "__main__":
    unittest.main()
