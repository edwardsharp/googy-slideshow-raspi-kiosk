"""render slides from an anonymously shared Google Slides PDF export.

the original share URL provides the file ID for /export/pdf. Ghostscript
converts every page to a screen-sized PNG for the existing player and cache.
"""

import subprocess
import time
from urllib.error import HTTPError


class AccessDenied(ValueError):
    """Google denied access to the original presentation's PDF export."""


def fetch_pdf(url, deadline, fetch):
    """reject sign-in pages and other responses that are not PDF files."""
    permissions = ("check that the original file is shared with anyone with the link "
                   "and viewers can download")
    try:
        data = fetch(url, deadline)
    except HTTPError as error:
        if error.code not in (401, 403):
            raise
        error.close()
        raise AccessDenied(f"PDF export denied (HTTP {error.code}); {permissions}") from error
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"Google returned something other than a PDF; {permissions}")
    return data


def render(data, size, directory, deadline, check_png, save):
    """render all PDF pages with Ghostscript, preserving their aspect ratio."""
    pdf = directory / "slides.pdf"
    pdf.write_bytes(data)
    # fixed media fits each page inside the screen; the remaining area is white.
    command = [
        "gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dPDFSTOPONERROR",
        "-sDEVICE=png16m",
        "-r72", f"-g{size[0]}x{size[1]}", "-dFIXEDMEDIA", "-dPDFFitPage",
        f"-sOutputFile={directory}/page-%05d.png", str(pdf),
    ]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("slideshow update exceeded three minutes")
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=remaining)
    except subprocess.CalledProcessError as error:
        raise ValueError(f"PDF rendering failed: {error.output.decode(errors='replace')}") from error
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("PDF rendering exceeded the update deadline") from error
    except OSError as error:
        raise ValueError(f"cannot run PDF renderer: {error}") from error
    images = sorted(directory.glob("page-*.png"))
    if not images:
        raise ValueError("PDF contains no rendered pages")
    for index, image in enumerate(images):
        data = image.read_bytes()
        check_png(data)
        save(directory / f"{index:03}.png", data)
        image.unlink()
    pdf.unlink()
    return len(images)
