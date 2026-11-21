import errno
import hashlib
import io
import itertools
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import zlib

import slideshow as app
import slides_pdf
import slides_png

URL = "https://docs.google.com/presentation/d/e/DEMO/pub?start=true&loop=true&delayms=5000"
PAGE = URL.split("/pub")[0] + "/htmlpresent"
SHARE = "https://docs.google.com/presentation/d/ORIGINAL/edit?usp=sharing"
PDF = "https://docs.google.com/presentation/d/ORIGINAL/export/pdf"


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
        self.assertEqual(app.configuration(path), (PAGE, 5, None))
        path.read_text.assert_called_once_with(encoding="utf-8-sig")

    def test_optional_original_share_link_provides_pdf_fallback(self):
        path = Mock()
        path.read_text.return_value = URL + "\n" + SHARE + "#slide=id.first\n"
        self.assertEqual(app.configuration(path), (PAGE, 5, PDF))
        for invalid in (URL, SHARE.replace("docs.google.com", "example.com"), SHARE + "\nextra"):
            path.read_text.return_value = URL + "\n" + invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                app.configuration(path)

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
    def test_preserves_order_and_signature_requests_screen_size_and_text(self):
        urls, revisions = slides_png.slide_urls(markup(pages=("second", "first")), PAGE, (1920, 1080))
        self.assertEqual([dict(r)["pageid"] for r in revisions], [["second"], ["first"]])
        self.assertNotIn("hmac", dict(revisions[0]))
        sized, fallback = (parse_qs(urlsplit(url).query) for url in urls[0])
        self.assertEqual(
            {key: sized[key] for key in ("pageid", "hmac", "w", "h", "showText")},
            {"pageid": ["second"], "hmac": ["signature"],
             "w": ["1920"], "h": ["1080"], "showText": ["1"]},
        )
        self.assertEqual((fallback["w"], fallback["h"], fallback["showText"]),
                         (["960"], ["540"], ["1"]))

    def test_finds_slide_urls_in_different_tags_and_ignores_other_images(self):
        html = (
            '<div style="background: url(\'viewpage?pageid=first&amp;rev=1\')"></div>'
            '<img src="logo.png">'
            '<img src="viewpage?pageid=second&amp;rev=1"'
            ' data-src="viewpage?pageid=second&amp;rev=1">'
            '<article data-src="viewpage?pageid=third&amp;rev=1"></article>'
        )
        urls, revisions = slides_png.slide_urls(html, PAGE, (1920, 1080))
        self.assertEqual([dict(r)["pageid"][0] for r in revisions], ["first", "second", "third"])
        self.assertTrue(all(url[0].startswith(PAGE.rsplit("/", 1)[0] + "/viewpage?") for url in urls))

    def test_rejects_ambiguous_or_unidentified_slide_urls(self):
        for html in (
            markup(pages=("same", "same")),
            markup().replace("rev=1", "other=1"),
            markup().replace("pageid=first", "pageid=first&amp;pageid=other"),
            markup().replace("pageid=first", "pageid=first&amp;pageid="),
            markup().replace("docs.google.com", "example.com"),
        ):
            with self.subTest(html=html[:120]), self.assertRaises(ValueError):
                slides_png.slide_urls(html, PAGE, (1920, 1080))

    def test_screen_size_reads_framebuffer_or_falls_back(self):
        with patch.object(app.Path, "read_text", return_value="1280,720\n"):
            self.assertEqual(app.screen_size(), (1280, 720))
        with patch.object(app.Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(app.screen_size(), (1920, 1080))

    def test_accepts_more_than_two_hundred_slides(self):
        urls, _ = slides_png.slide_urls(markup(pages=tuple(map(str, range(250)))), PAGE, (1920, 1080))
        self.assertEqual(len(urls), 250)

    def test_rejects_changed_markup_login_pages_and_insecure_image_urls(self):
        for html in (
            "<html>sign in</html>",
            '<section class="slide-content"></section>',
            markup().replace("https://", "http://"),
        ):
            with self.subTest(html=html[:100]), self.assertRaises(ValueError):
                slides_png.slide_urls(html, PAGE, (1920, 1080))

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

    def test_configuration_codes_distinguish_file_link_and_timing(self):
        cases = (
            (b"", "E04-F"),
            ((URL + "\n" + SHARE + "\nextra").encode(), "E04-F"),
            (b"\xff", "E04-F"),
            (b"invalid URL", "E04-L"),
            ((URL + "\ninvalid share link").encode(), "E04-L"),
            (URL.replace("docs.google.com", "[invalid").encode(), "E04-L"),
            (URL.replace("delayms=5000", "").encode(), "E04-T"),
            (URL.replace("delayms=5000", "delayms=1500").encode(), "E04-T"),
        )
        for contents, code in cases:
            self.config.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaises(app.UpdateError) as raised:
                self.refresh()
            self.assertEqual(raised.exception.code, code)
        self.config.unlink()
        with self.assertRaises(app.UpdateError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.code, "E04-F")

    def test_network_and_http_failures_have_distinct_codes(self):
        http_error = HTTPError(PAGE, 403, "denied", None, io.BytesIO())
        self.addCleanup(http_error.close)
        cases = (
            (URLError(socket.gaierror(-2, "name lookup failed")), "E05-P1"),
            (URLError(OSError(errno.ENETUNREACH, "no route")), "E06-P1"),
            (URLError(TimeoutError("connection timed out")), "E07-P1"),
            (http_error, "E08-P1"),
        )
        for error, code in cases:
            self.download.side_effect = error
            with self.subTest(code=code), self.assertRaises(app.UpdateError) as raised:
                self.refresh()
            self.assertEqual(raised.exception.code, code)

    def test_published_content_failure_has_its_own_code(self):
        self.download.side_effect = lambda url, *_: b"bad HTML"
        with self.assertRaises(app.UpdateError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.code, "E09-P1")

    def test_slide_image_failures_identify_the_image_step(self):
        def download(url, *_):
            if url == PAGE:
                return self.html.encode()
            raise URLError(socket.gaierror(-2, "name lookup failed"))

        self.download.side_effect = download
        with self.assertRaises(app.UpdateError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.code, "E05-P2")

        self.download.side_effect = lambda url, *_: self.html.encode() if url == PAGE else b"bad PNG"
        with self.assertRaises(app.UpdateError) as raised:
            self.refresh()
        self.assertEqual(raised.exception.code, "E09-P2")

    def test_pdf_failure_and_denied_export_have_distinct_codes(self):
        self.config.write_text(URL + "\n" + SHARE)
        for response, code in (
            (b"bad PDF", "E10-D1"), (401, "E12"), (403, "E12"), (429, "E08-D1"),
        ):
            def download(url, *_):
                if url != PDF:
                    return b"bad HTML"
                if isinstance(response, int):
                    raise HTTPError(url, response, "denied", None, io.BytesIO())
                return response

            self.download.side_effect = download
            with self.subTest(code=code), self.assertRaises(app.UpdateError) as raised:
                self.refresh()
            self.assertEqual(raised.exception.code, code)

    def test_missing_pdf_renderer_is_not_reported_as_storage_failure(self):
        self.config.write_text(URL + "\n" + SHARE)
        self.download.side_effect = lambda url, *_: b"%PDF-1.4 example" if url == PDF else b"bad HTML"
        with (
            patch.object(slides_pdf.subprocess, "run", side_effect=FileNotFoundError("gs")),
            self.assertRaises(app.UpdateError) as raised,
        ):
            self.refresh()
        self.assertEqual(raised.exception.code, "E10-D2")

    def test_last_fallback_failure_supplies_the_display_code(self):
        self.config.write_text(URL + "\n" + SHARE)

        def download(url, *_):
            if url == PAGE:
                return b"bad HTML"
            raise URLError(socket.gaierror(-2, "name lookup failed"))

        self.download.side_effect = download
        with self.assertLogs("slideshow", level="WARNING") as logs:
            with self.assertRaises(app.UpdateError) as raised:
                self.refresh()
        self.assertIn("E09-P1", logs.output[0])
        self.assertIn("slides_png.py", logs.output[0])
        self.assertIn("Traceback (most recent call last)", logs.output[0])
        self.assertEqual(raised.exception.code, "E05-D1")

    def test_failed_sized_slide_falls_back_to_google_default_size(self):
        def download(url, *_):
            if url == PAGE:
                return self.html.encode()
            if "w=1920" in url:
                raise OSError("HTTP Error 400")
            return png()
        self.download.side_effect = download
        with patch.object(app, "screen_size", return_value=(1920, 1080)):
            deck = self.refresh()
        self.assertEqual(len(list(deck.directory.glob("*.png"))), 2)

    def test_pdf_fallback_then_return_to_png(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE + "\n")

        def download(url, *_):
            if url == PAGE:
                raise OSError("published view unavailable")
            if url == PDF:
                return b"%PDF-1.4 example"
            return png()

        def render(data, size, directory, deadline, check_png, save):
            save(directory / "000.png", png())
            return 1

        self.download.side_effect = download
        with patch.object(slides_pdf, "render", side_effect=render) as renderer:
            fallback = self.refresh(first)
            self.assertEqual(app.cached_deck(self.cache), fallback)
            self.assertEqual(self.refresh(fallback), fallback)
            renderer.assert_called_once()
        self.download.side_effect = lambda url, *_: self.html.encode() if url == PAGE else png()
        recovered = self.refresh(fallback)
        self.assertEqual(app.cached_deck(self.cache), recovered)
        self.assertEqual(recovered.key, first.key)

    def test_bad_pdf_does_not_replace_cached_deck(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE)
        self.download.side_effect = lambda url, *_: b"sign in" if url == PDF else b"bad HTML"
        with self.assertRaisesRegex(ValueError, "other than a PDF.*anyone with the link.*download"):
            self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), first)
        self.assertFalse(list(self.cache.glob(".incoming-*")))

    def test_denied_pdf_explains_permissions_and_keeps_cache(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE)
        for status in (401, 403):
            def download(url, *_):
                if url == PDF:
                    raise HTTPError(url, status, "denied", None, io.BytesIO())
                return b"bad HTML"

            self.download.side_effect = download
            with self.subTest(status=status):
                with self.assertRaisesRegex(
                    ValueError, f"HTTP {status}.*anyone with the link.*viewers can download"
                ):
                    self.refresh(first)
                self.assertEqual(app.cached_deck(self.cache), first)

    def test_other_pdf_http_errors_are_not_mislabeled_as_permission_failures(self):
        error = HTTPError(PDF, 429, "too many requests", None, io.BytesIO())
        self.addCleanup(error.close)
        with self.assertRaises(HTTPError) as raised:
            slides_pdf.fetch_pdf(PDF, 0, Mock(side_effect=error))
        self.assertIs(raised.exception, error)

    def test_partial_png_failure_retries_as_complete_pdf_deck(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE)
        self.html = markup(revision="2")

        def download(url, *_):
            if url == PAGE:
                return self.html.encode()
            if url == PDF:
                return b"%PDF-1.4 example"
            return b"broken" if "pageid=second" in url else png()

        def render(data, size, directory, deadline, check_png, save):
            save(directory / "000.png", png())
            return 1

        self.download.side_effect = download
        with patch.object(slides_pdf, "render", side_effect=render):
            fallback = self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), fallback)
        self.assertEqual(app.read_deck(self.cache / "previous"), first)
        self.assertFalse(list(self.cache.glob(".incoming-*")))
        self.assertEqual(len(list(fallback.directory.glob("*.png"))), 1)

    def test_ambiguous_slide_ids_use_pdf_fallback(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE)
        self.html = markup(pages=("same", "same"))
        self.download.side_effect = lambda url, *_: (
            self.html.encode() if url == PAGE else b"%PDF-1.4 example"
        )

        def render(data, size, directory, deadline, check_png, save):
            save(directory / "000.png", png())
            return 1

        with patch.object(slides_pdf, "render", side_effect=render):
            fallback = self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), fallback)
        self.assertEqual(app.read_deck(self.cache / "previous"), first)

    def test_pdf_renderer_failure_preserves_cache_and_cleans_up(self):
        first = self.refresh()
        self.config.write_text(URL + "\n" + SHARE)
        self.download.side_effect = lambda url, *_: b"%PDF-1.4 bad" if url == PDF else b"bad HTML"
        failure = subprocess.CalledProcessError(1, "gs", output=b"invalid PDF")
        with patch.object(slides_pdf.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "invalid PDF"):
                self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), first)
        self.assertFalse(list(self.cache.glob(".incoming-*")))

    def test_failed_partial_download_does_not_replace_playable_deck(self):
        deck = self.refresh()
        self.html = markup(revision="2")
        broken = b"broken second slide"
        self.download.side_effect = [self.html.encode(), png(), broken, broken]
        with self.assertRaises(ValueError):
            self.refresh(deck)
        self.assertEqual(app.cached_deck(self.cache), deck)
        self.assertFalse(list(self.cache.glob(".incoming-*")))

    def test_network_failure_keeps_persistent_cache(self):
        deck = self.refresh()
        self.download.side_effect = URLError("Wi-Fi down")
        with self.assertRaises(app.UpdateError) as raised:
            self.refresh(deck)
        self.assertEqual(raised.exception.code, "E07-P1")
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

        with (
            patch.object(app, "link_deck", side_effect=interrupted),
            self.assertRaises(app.UpdateError) as raised,
        ):
            self.refresh(first)
        self.assertEqual(raised.exception.code, "E11")
        self.assertEqual(app.cached_deck(self.cache), first)
        second = self.refresh(first)
        self.assertEqual(app.cached_deck(self.cache), second)

    def test_full_disk_never_replaces_playable_cache(self):
        first = self.refresh()
        self.html = markup(revision="2")
        with (
            patch.object(app, "save", side_effect=OSError(errno.ENOSPC, "no space left")),
            self.assertRaises(app.UpdateError) as raised,
        ):
            self.refresh(first)
        self.assertEqual(raised.exception.code, "E11")
        self.assertEqual(app.cached_deck(self.cache), first)
        self.assertFalse(list(self.cache.glob(".incoming-*")))


class RuntimeTests(unittest.TestCase):
    def test_error_screen_is_black_and_contains_only_the_generic_message(self):
        output = io.StringIO()
        with patch.object(sys, "stdout", output):
            app.screen_message("error E01 | failures: 2")
        self.assertEqual(
            output.getvalue(),
            "\x1b[0m\x1b[37;40m\x1b[2J\x1b[H\x1b[?25lerror E01 | failures: 2",
        )

    def test_no_cache_shows_consecutive_failures_then_starts_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = app.Deck(Path(directory), "new", 5)
            player = Mock()
            player.poll.return_value = None
            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=None),
                patch.object(app, "screen_message") as screen,
                patch.object(app.subprocess, "Popen", return_value=player),
                patch.object(app, "refresh", side_effect=[
                    OSError("private network details"), ValueError("private URL"), deck,
                ]),
                patch.object(app.signal, "signal"),
                patch.object(app.time, "monotonic", side_effect=itertools.count(0, 31)),
                patch.object(app.time, "sleep", side_effect=[None, None, KeyboardInterrupt]),
                self.assertLogs("slideshow", level="ERROR"),
                self.assertRaises(KeyboardInterrupt),
            ):
                app.main()
            self.assertEqual(
                [call.args for call in screen.call_args_list],
                [(), ("error E01 | failures: 1",), ("error E01 | failures: 2",)],
            )
            player.terminate.assert_called_once()

    def test_update_error_code_is_displayed_without_diagnostic_details(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=None),
                patch.object(app, "screen_message") as screen,
                patch.object(app, "refresh", side_effect=app.UpdateError("E05-P1", "private details")),
                patch.object(app.signal, "signal"),
                patch.object(app.time, "sleep", side_effect=KeyboardInterrupt),
                self.assertLogs("slideshow", level="ERROR"),
                self.assertRaises(KeyboardInterrupt),
            ):
                app.main()
            self.assertEqual([call.args for call in screen.call_args_list],
                             [(), ("error E05-P1 | failures: 1",)])

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
                patch.object(app, "screen_message") as screen,
                patch.object(app.subprocess, "Popen", side_effect=start),
                patch.object(app, "refresh", side_effect=offline),
                patch.object(app.signal, "signal"),
                patch.object(app.time, "sleep", side_effect=KeyboardInterrupt),
                self.assertLogs("slideshow", level="ERROR"),
                self.assertRaises(KeyboardInterrupt),
            ):
                app.main()
            self.assertEqual(calls, ["play", "fetch"])
            screen.assert_not_called()
            player.terminate.assert_called_once()
            player.wait.assert_called_once_with(timeout=5)

    def test_recovery_starts_slides_and_later_update_failures_stay_off_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = app.Deck(Path(directory), "new", 5)
            player = Mock()
            player.poll.return_value = None
            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=None),
                patch.object(app, "screen_message") as screen,
                patch.object(app.subprocess, "Popen", return_value=player),
                patch.object(app, "refresh", side_effect=[
                    OSError("offline"), deck, OSError("offline again"),
                ]),
                patch.object(app.signal, "signal"),
                patch.object(app.time, "monotonic", side_effect=itertools.count(0, 301)),
                patch.object(app.time, "sleep", side_effect=[None, KeyboardInterrupt]),
                self.assertLogs("slideshow", level="ERROR"),
                self.assertRaises(KeyboardInterrupt),
            ):
                app.main()
            self.assertEqual(
                [call.args for call in screen.call_args_list],
                [(), ("error E01 | failures: 1",)],
            )
            player.terminate.assert_called_once()

    def test_viewer_failure_shows_only_its_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = app.Deck(Path(directory), "cached", 5)
            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=deck),
                patch.object(app, "screen_message") as screen,
                patch.object(app.subprocess, "Popen", side_effect=OSError("private details")),
                patch.object(app.signal, "signal"),
                self.assertRaises(OSError),
            ):
                app.main()
            screen.assert_called_once_with("error E02")

    def test_viewer_exit_shows_only_its_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = app.Deck(Path(directory), "cached", 5)
            player = Mock()
            player.poll.return_value = 1
            with (
                patch("sys.argv", ["slideshow.py", "--cache", directory]),
                patch.object(app, "cached_deck", return_value=deck),
                patch.object(app, "screen_message") as screen,
                patch.object(app.subprocess, "Popen", return_value=player),
                patch.object(app.signal, "signal"),
                self.assertRaises(RuntimeError),
            ):
                app.main()
            screen.assert_called_once_with("error E02")

    def test_cache_initialization_failure_shows_only_its_error_code(self):
        with (
            patch("sys.argv", ["slideshow.py", "--cache", "/unavailable"]),
            patch.object(app.Path, "mkdir", side_effect=OSError("private details")),
            patch.object(app, "screen_message") as screen,
            self.assertRaises(OSError),
        ):
            app.main()
        screen.assert_called_once_with("error E03")

    def test_fetch_checks_deadline_even_while_data_keeps_arriving(self):
        with (
            patch.object(app, "urlopen", return_value=io.BytesIO(b"partial image")),
            patch.object(app.time, "monotonic", side_effect=[0, 11]),
            self.assertRaises(TimeoutError),
        ):
            app.fetch("https://example.com", 10)


@unittest.skipUnless(os.environ.get("GOOGY_LIVE_TEST") == "1", "set GOOGY_LIVE_TEST=1")
class LiveSourceTests(unittest.TestCase):
    def setUp(self):
        if "GOOGY_TEST_URL_FILE" in os.environ:
            self.config = Path(os.environ["GOOGY_TEST_URL_FILE"])
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.config = Path(temporary.name) / "slideshow.txt"
            self.config.write_text(
                "https://docs.google.com/presentation/d/e/"
                "2PACX-1vT3yjbwXlbP3pDD-LUF0625sQs5p1Y_0Z7BAeIoWq6N83eerVWHnCVRhth-XHNxosYqCoLkevTtAR4r"
                "/pub?start=true&loop=true&delayms=5000\n"
                "https://docs.google.com/presentation/d/"
                "1bDKppy7gcps0-Av4PABypJ6HO6SV8WmlNr5PNZz9Kqs/edit\n"
            )

    def pdf_url(self):
        pdf = app.configuration(self.config)[2]
        if not pdf:
            self.skipTest("add the original share link to test the PDF fallback")
        return pdf

    def test_ghostscript_rejects_bad_pdf(self):
        self.pdf_url()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "PDF rendering failed"):
                slides_pdf.render(b"%PDF-1.4 invalid", (640, 360), Path(temporary),
                                  time.monotonic() + 30, app.check_png, app.save)

    def test_png_source(self):
        page, _, _ = app.configuration(self.config)
        deadline = time.monotonic() + 180
        urls, _ = slides_png.slide_urls(app.fetch(page, deadline).decode(), page, (640, 360))
        self.assertTrue(urls)
        with tempfile.TemporaryDirectory() as temporary:
            count = slides_png.download(urls, Path(temporary), deadline,
                                        app.fetch, app.check_png, app.save)
            self.assertEqual(count, len(urls))

    def test_pdf_source(self):
        pdf = self.pdf_url()
        deadline = time.monotonic() + 180
        data = slides_pdf.fetch_pdf(pdf, deadline, app.fetch)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            count = slides_pdf.render(data, (640, 360), directory, deadline,
                                      app.check_png, app.save)
            self.assertGreater(count, 0)
            first = (directory / "000.png").read_bytes()
            self.assertEqual(struct.unpack_from(">II", first, 16), (640, 360))

    def test_second_published_deck(self):
        if "GOOGY_TEST_URL_FILE" in os.environ:
            self.skipTest("testing the supplied slideshow instead")
        page = (
            "https://docs.google.com/presentation/d/e/"
            "2PACX-1vSDlWCEsOh9Qw4byhLu1B_YRqLSS4suH3EkfTE972GtkwjQKOwSE3r-2tl1LBHTA9Q_pUYxARRKd9JS"
            "/htmlpresent"
        )
        deadline = time.monotonic() + 180
        urls, _ = slides_png.slide_urls(app.fetch(page, deadline).decode(), page, (640, 360))
        self.assertTrue(urls)
        app.check_png(app.fetch(urls[0][0], deadline))

    def test_falls_back_to_pdf_and_recovers_png(self):
        self.pdf_url()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache = root / "cache"
            cache.mkdir()
            with patch.object(app, "screen_size", return_value=(640, 360)):
                with patch.object(slides_png, "slide_urls", side_effect=ValueError("test failure")):
                    fallback = app.refresh(self.config, cache, None)
                    self.assertEqual(app.cached_deck(cache), fallback)
                    self.assertEqual(app.refresh(self.config, cache, fallback), fallback)
                recovered = app.refresh(self.config, cache, fallback)
            self.assertNotEqual(recovered.key, fallback.key)
            self.assertEqual(app.cached_deck(cache), recovered)
            self.assertEqual(app.read_deck(cache / "previous"), fallback)


class CatalogueTests(unittest.TestCase):
    def test_local_and_fork_release_catalogues_keep_image_metadata(self):
        # exercise the recipe's Python packaging step without building an entire Linux image.
        recipe = Path(__file__).with_name("build-image.sh").read_text()
        packaging = recipe.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            image = root / "googy.img"
            image.write_bytes(b"raw image")
            download = root / "googy.img.xz"
            download.write_bytes(b"compressed")
            base_url = "https://github.com/example/fork/releases/download/image-42/"
            for base in (None, base_url):
                with self.subTest(base=base):
                    environment = os.environ.copy()
                    environment.pop("GOOGY_IMAGE_BASE_URL", None)
                    if base:
                        environment["GOOGY_IMAGE_BASE_URL"] = base
                    subprocess.run(
                        [sys.executable, "-c", packaging, str(image), str(download)],
                        env=environment, check=True, capture_output=True, text=True,
                    )
                    entry, = json.loads((root / "os.json").read_text())["os_list"]
                    self.assertEqual(
                        entry["url"], base + "googy.img.xz" if base else download.as_uri()
                    )
                    self.assertEqual(entry["init_format"], "cloudinit-rpi")
                    self.assertEqual(entry["extract_size"], len(b"raw image"))
                    self.assertEqual(entry["extract_sha256"], hashlib.sha256(b"raw image").hexdigest())
                    self.assertEqual(entry["image_download_size"], len(b"compressed"))
                    self.assertEqual(
                        entry["image_download_sha256"], hashlib.sha256(b"compressed").hexdigest()
                    )


if __name__ == "__main__":
    unittest.main()
