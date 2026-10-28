"""Use an ordinary anonymous browser session for public publisher RSS."""
import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET

from monitor import SourceError, clean


def read_feed(url, diagnostics):
    if not url.startswith('https://www.nature.com/'):
        raise SourceError('Browser feed retrieval is restricted to the configured publisher')
    try:
        from playwright.sync_api import sync_playwright, Error as BrowserError
    except ImportError:
        raise SourceError('Browser runtime unavailable; install requirements and Chromium before retrying') from None

    def inspect(response):
        data = response.body()
        headers = response.headers
        final = urllib.parse.urlsplit(response.url)
        diagnostics.update(http_status=response.status, content_type=headers.get('content-type', ''),
                           final_url=urllib.parse.urlunsplit((final.scheme, final.netloc, final.path, '', '')),
                           bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        try:
            root = ET.fromstring(data)
            if response.status == 200 and root.tag.rsplit('}', 1)[-1] in ('RDF', 'rss', 'feed'):
                return data
        except ET.ParseError:
            pass
        title = re.search(br'<title[^>]*>(.*?)</title>', data, re.I | re.S)
        diagnostics['page_title'] = clean(title.group(1).decode('utf-8', 'replace'))[:120] if title else ''
        lower = data.lower()
        if b'captcha' in lower:
            raise SourceError('Publisher requires CAPTCHA; browser retrieval stopped for manual attention')
        return None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                # Ordinary page loading only: no stealth patches, cookie injection,
                # CAPTCHA solving, or changes to the publisher's scripts.
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    if response:
                        data = inspect(response)
                        if data:
                            return data
                except BrowserError:
                    # XML navigation can start a download or be superseded by JS.
                    pass
                for pause in (3000, 6000, 10000):
                    page.wait_for_timeout(pause)
                    response = context.request.get(url, timeout=15000)
                    data = inspect(response)
                    if data:
                        return data
                raise SourceError('Normal browser session did not receive valid RSS; publisher response: ' + diagnostics.get('page_title', 'unknown'))
            finally:
                browser.close()
    except BrowserError:
        raise SourceError('Browser navigation or runtime failed; no feed accepted') from None
