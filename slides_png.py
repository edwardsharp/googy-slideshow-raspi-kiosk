"""download slides from Google's published HTML view.

the published URL becomes an /htmlpresent page. slide image URLs point to
/viewpage and contain a page ID and revision. preserve their expiring
parameters, include text, and request screen-sized images.
"""

from html.parser import HTMLParser
import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

LOG = logging.getLogger("slideshow")
CSS_URL = re.compile(r"""url\s*\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)


class Slides(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        # read inline CSS and image attributes in document order, regardless of tag or class.
        urls = [match.group(2).strip() for match in CSS_URL.finditer(attrs.get("style") or "")]
        if tag in ("img", "source") and attrs.get("src"):
            urls.append(attrs["src"].strip())
        if attrs.get("data-src"):
            urls.append(attrs["data-src"].strip())
        self.urls.extend(dict.fromkeys(urls))


def slide_urls(html, page, size):
    """find known slide image URLs and stable revision information in the published page."""
    parser = Slides()
    parser.feed(html)
    images, revisions, page_ids = [], [], set()
    expected_path = urlsplit(page).path.rsplit("/", 1)[0] + "/viewpage"
    for url in parser.urls:
        parts = urlsplit(urljoin(page, url))
        if not parts.path.endswith("/viewpage"):
            continue
        if parts.scheme != "https" or parts.netloc != "docs.google.com" or parts.path != expected_path:
            raise ValueError("unexpected slide image URL")
        query = parse_qs(parts.query, keep_blank_values=True)
        if any(len(query.get(key, [])) != 1 or not query[key][0].strip()
               for key in ("pageid", "rev")):
            raise ValueError("slide image URL is missing a unique page ID or revision")
        page_id = query["pageid"][0]
        if page_id in page_ids:
            raise ValueError("duplicate slide ID in Google's response")
        page_ids.add(page_id)
        # hmac and ts expire; the other parameters identify the slide and revision.
        revisions.append(sorted((key, value) for key, value in query.items()
                                if key not in ("hmac", "ts")))
        # the HTML page draws text separately, so include it in each downloaded PNG.
        query["showText"] = ["1"]
        sized = dict(query, w=[str(size[0])], h=[str(size[1])])
        images.append(tuple(urlunsplit(parts._replace(query=urlencode(q, doseq=True)))
                            for q in (sized, query)))
    if not images:
        raise ValueError("no slides found in Google's response")
    return images, revisions


def download(urls, directory, deadline, fetch, check_png, save):
    """download every slide, retrying at Google's default size when necessary."""
    for index, (url, fallback) in enumerate(urls):
        try:
            data = fetch(url, deadline)
            check_png(data)
        except (OSError, ValueError) as error:
            LOG.warning("slide %s: %s; retrying at Google's default size", index + 1, error)
            data = fetch(fallback, deadline)
            check_png(data)
        save(directory / f"{index:03}.png", data)
    return len(urls)
