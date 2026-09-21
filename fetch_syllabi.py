#!/usr/bin/env python3
"""Download course syllabi from Brightspace (D2L) for a batch of course codes.

Login happens through a real, visible browser window so you can complete your
school's SSO flow (including MFA) by hand. After that, the script tries to
find and download each syllabus automatically via Brightspace's own REST
API (no clicking needed); if that doesn't pan out for some reason, it falls
back to searching the content tree and clicking through the UI itself, and
as a last resort, asks you to click the link so it can capture the result.

Usage:
    python fetch_syllabi.py --courses CSE201,MATH150,ENGL101
    python fetch_syllabi.py --courses-file courses.txt --output-dir ~/Desktop/Syllabi

See README.md for one-time setup (installing Playwright's browser, filling
in config.json).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import (
    BrowserContext,
    Download,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOG = logging.getLogger("fetch_syllabi")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "Syllabi"
SESSION_DIR = PROJECT_DIR / ".auth" / "browser-profile"

LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # generous window to click through SSO/MFA by hand
NAV_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 20_000

SYLLABUS_RE = re.compile(r"syllabus", re.IGNORECASE)

# Extensions Word can open and convert to PDF via docx2pdf. Anything else
# downloaded (a spreadsheet, an image, a zip, ...) is left as-is with a
# warning.
WORD_CONVERTIBLE_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt"}

# Brightspace/D2L instances are re-themed per school, so these selectors are
# best-effort defaults, tried in order. If your MyFire/Brightspace theme uses
# different markup, tweak this list -- everything else in the script is
# selector-agnostic and falls back to asking you to click manually.
COURSE_SELECTOR_BUTTON_CANDIDATES = [
    'button[aria-label^="Select a course"]',  # confirmed via inspecting MyFire
    '[title="Select a course"]',
    'button:has-text("Select a course")',
    'd2l-navigation-main-header >> [title="Course Selector"]',
]
COURSE_SEARCH_INPUT_CANDIDATES = [
    'input[placeholder="Search for a course"]',  # confirmed exact text on MyFire's course selector
    'input[type="search"]',
    'input[placeholder*="ourse" i]',
]
EXPAND_ALL_CANDIDATES = [
    'button:has-text("Expand All")',
    '[title="Expand All"]',
]
DOWNLOAD_BUTTON_CANDIDATES = [
    'button:has-text("Download")',
    'a:has-text("Download")',
    '[title="Download"]',
]
CONTENT_NAV_LINK_CANDIDATES = [
    'a:has-text("Content")',
]


class CourseNotFound(Exception):
    pass


class SyllabusNotFound(Exception):
    pass


# --- Brightspace REST API auto-detection ------------------------------------
#
# Brightspace's own web UI is built by calling its documented REST API
# (https://docs.valence.desire2learn.com/), and that API is reachable with
# the same session cookies the logged-in browser already has. Querying it
# directly for the course's content structure and the syllabus file is far
# more reliable than guessing at rendered HTML/CSS, and needs zero clicking.
# Everything below is best-effort: if anything about it doesn't match this
# Brightspace instance, it returns None/raises nothing, and the caller falls
# back to the click-based flow further down in this file.

def get_api_versions(context: BrowserContext, base_url: str) -> dict[str, str]:
    """Look up the latest supported version of each Brightspace API product
    (e.g. "le", "lp"), needed to build correct API URLs."""
    versions: dict[str, str] = {}
    try:
        resp = context.request.get(f"{base_url.rstrip('/')}/d2l/api/versions/")
        if resp.status != 200:
            LOG.debug("API versions lookup returned HTTP %d: %s", resp.status, resp.text()[:500])
            return versions
        for entry in resp.json():
            code = entry.get("ProductCode")
            latest = entry.get("LatestVersion")
            if code and latest:
                versions[code] = latest
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Couldn't fetch Brightspace API versions.", exc_info=True)
    LOG.debug("Detected API versions: %s", versions)
    return versions


ORG_UNIT_URL_RE = re.compile(r"/d2l/le/content/(\d+)")


def extract_org_unit_id(page: Page) -> int | None:
    """Read the numeric org unit ID straight out of the current page's URL,
    once the course-selector UI has already navigated to the course's
    Content page. Far more reliable than independently re-deriving it by
    matching the course code against a potentially huge enrollment history."""
    match = ORG_UNIT_URL_RE.search(page.url)
    return int(match.group(1)) if match else None


def _filename_from_response(resp, fallback_url: str) -> str:
    content_disposition = resp.headers.get("content-disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
    if match:
        return unquote(match.group(1))
    return Path(fallback_url.split("?")[0]).name or "syllabus.pdf"


def _iter_toc_topics(modules: list[dict]):
    for module in modules or []:
        yield from module.get("Topics", []) or []
        yield from _iter_toc_topics(module.get("Modules", []) or [])


def find_syllabus_via_api(
    context: BrowserContext, base_url: str, le_version: str, org_unit_id: int
) -> tuple[bytes, str] | None:
    try:
        resp = context.request.get(
            f"{base_url.rstrip('/')}/d2l/api/le/{le_version}/{org_unit_id}/content/toc"
        )
        if resp.status != 200:
            LOG.debug("Content TOC lookup returned HTTP %d: %s", resp.status, resp.text()[:500])
            return None
        topics = list(_iter_toc_topics(resp.json().get("Modules", []) or []))
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Content TOC lookup failed.", exc_info=True)
        return None

    LOG.debug(
        "Content TOC has %d topics: %s",
        len(topics),
        [(t.get("Title"), t.get("TypeIdentifier")) for t in topics],
    )

    # Fast path: a topic whose own title says "syllabus" (an uploaded file).
    for topic in topics:
        if SYLLABUS_RE.search(topic.get("Title") or ""):
            LOG.debug("Topic title matches 'syllabus' directly: %r", topic.get("Title"))
            result = _fetch_topic_file(context, base_url, le_version, org_unit_id, topic)
            if result:
                return result
            LOG.debug("Fetching topic file for %r failed.", topic.get("Title"))

    # Slower path: a syllabus link buried inside a content page's body --
    # either an uploaded "File" topic (often an HTML page, like a "Course
    # Resources" checklist) or a "Link"/HTML topic reachable via its own Url.
    for topic in topics:
        type_id = (topic.get("TypeIdentifier") or "").lower()
        html: str | None = None

        if type_id == "file":
            file_result = _fetch_topic_file(context, base_url, le_version, org_unit_id, topic)
            if not file_result:
                LOG.debug("Couldn't fetch File-type topic %r.", topic.get("Title"))
                continue
            content_bytes, _filename = file_result
            try:
                html = content_bytes.decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001 - best-effort
                continue
        elif "html" in type_id or type_id == "link":
            url = topic.get("Url")
            if not url:
                continue
            try:
                full_url = url if url.startswith("http") else f"{base_url.rstrip('/')}{url}"
                page_resp = context.request.get(full_url)
                if page_resp.status != 200:
                    LOG.debug(
                        "Fetching topic page %r (%s) returned HTTP %d.",
                        topic.get("Title"), full_url, page_resp.status,
                    )
                    continue
                html = page_resp.text()
            except Exception:  # noqa: BLE001 - best-effort
                LOG.debug("Fetching topic page %r failed.", topic.get("Title"), exc_info=True)
                continue
        else:
            continue

        if not html:
            continue
        match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>[^<]*syllabus[^<]*</a>', html, re.IGNORECASE)
        if not match:
            LOG.debug(
                "No syllabus link found in topic %r (%d chars of HTML). "
                "Any 'syllabus' mentions: %s. Start of content: %r",
                topic.get("Title"), len(html),
                [html[max(m.start() - 80, 0):m.end() + 80] for m in SYLLABUS_RE.finditer(html)][:5],
                html[:300],
            )
            continue
        href = match.group(1)
        try:
            file_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"
            file_resp = context.request.get(file_url)
            if file_resp.status == 200:
                return file_resp.body(), _filename_from_response(file_resp, file_url)
            LOG.debug("Fetching syllabus file %r returned HTTP %d.", file_url, file_resp.status)
        except Exception:  # noqa: BLE001 - best-effort
            LOG.debug("Fetching syllabus file %r failed.", href, exc_info=True)
            continue

    return None


def _fetch_topic_file(
    context: BrowserContext, base_url: str, le_version: str, org_unit_id: int, topic: dict
) -> tuple[bytes, str] | None:
    # The content TOC endpoint names a topic's numeric ID "TopicId" ("Id" is
    # what the single-topic endpoints use), so accept either.
    topic_id = topic.get("TopicId") or topic.get("Id")
    if topic_id is None:
        LOG.debug(
            "Topic %r has no TopicId/Id, can't fetch its file. Keys present: %s",
            topic.get("Title"), sorted(topic),
        )
        return None
    url = f"{base_url.rstrip('/')}/d2l/api/le/{le_version}/{org_unit_id}/content/topics/{topic_id}/file"
    try:
        resp = context.request.get(url)
        if resp.status != 200:
            LOG.debug(
                "Fetching topic file %r (%s) returned HTTP %d: %s",
                topic.get("Title"), url, resp.status, resp.text()[:300],
            )
            return None
        return resp.body(), _filename_from_response(resp, topic.get("Title") or "syllabus.pdf")
    except Exception:  # noqa: BLE001 - best-effort
        LOG.debug("Fetching topic file %r (%s) raised an exception.", topic.get("Title"), url, exc_info=True)
        return None


def try_api_auto_download(
    context: BrowserContext, base_url: str, api_versions: dict[str, str], org_unit_id: int
) -> tuple[bytes, str] | None:
    """Best-effort content search via Brightspace's REST API for a course
    whose org unit ID is already known (read from the URL after the
    course-selector UI has navigated there). Returns None (never raises) on
    any failure so the caller can fall back to the click-based flow."""
    le_version = api_versions.get("le")
    if not le_version:
        return None
    try:
        return find_syllabus_via_api(context, base_url, le_version, org_unit_id)
    except Exception:  # noqa: BLE001 - this whole path is best-effort
        LOG.debug("API auto-download attempt failed.", exc_info=True)
        return None


def load_config(path: Path) -> dict:
    if not path.exists():
        LOG.error(
            "Config file not found at %s.\n"
            "Copy config.example.json to config.json and fill in base_url first.",
            path,
        )
        sys.exit(1)
    config = json.loads(path.read_text())
    if not config.get("base_url") or config["base_url"].startswith("REPLACE-"):
        LOG.error("config.json's base_url is not set. Edit config.json and try again.")
        sys.exit(1)
    return config


def sanitize_course_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", code.strip())


def read_course_codes(args: argparse.Namespace) -> list[str]:
    codes: list[str] = []
    if args.courses:
        codes.extend(c.strip() for c in args.courses.split(",") if c.strip())
    if args.courses_file:
        text = Path(args.courses_file).expanduser().read_text()
        codes.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not codes:
        LOG.error("No course codes given. Use --courses or --courses-file.")
        sys.exit(1)
    # de-dupe while preserving order
    seen: set[str] = set()
    unique_codes = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)
    return unique_codes


def first_matching_locator(page: Page, selectors: list[str], timeout_ms: int = 3000) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def wait_for_login(page: Page, home_url_fragment: str) -> None:
    LOG.info("=" * 70)
    LOG.info("A browser window has opened.")
    LOG.info("Complete your SSO login (including MFA) in that window now.")
    LOG.info("Waiting up to %d minutes for login to finish...", LOGIN_TIMEOUT_MS // 60000)
    LOG.info("=" * 70)
    try:
        page.wait_for_url(f"**{home_url_fragment}**", timeout=LOGIN_TIMEOUT_MS)
        LOG.info("Login detected, continuing.")
    except PlaywrightTimeoutError:
        input(
            "Didn't detect the post-login page automatically. If you're already "
            "logged in, press Enter here to continue (or Ctrl+C to abort): "
        )


def open_course(page: Page, course_code: str) -> None:
    """Best-effort automated course navigation, with a manual fallback."""
    LOG.info("Looking for course %s...", course_code)
    button = first_matching_locator(page, COURSE_SELECTOR_BUTTON_CANDIDATES)
    if button:
        try:
            button.click()
            search_box = first_matching_locator(page, COURSE_SEARCH_INPUT_CANDIDATES, timeout_ms=3000)
            if search_box:
                search_box.fill(course_code)
                page.wait_for_timeout(800)  # let the results list filter
                result = page.get_by_text(re.compile(re.escape(course_code), re.IGNORECASE)).first
                result.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
                LOG.info("Opened course %s automatically.", course_code)
                return
        except PlaywrightTimeoutError:
            LOG.info("Automated course search didn't pan out for %s.", course_code)

    # Manual fallback
    print()
    print(f">>> Couldn't find '{course_code}' automatically.")
    print(f">>> In the open browser window, navigate to the {course_code} course's Content page.")
    input(">>> Press Enter here once you're there (or Ctrl+C to abort): ")


def ensure_on_content_page(page: Page) -> None:
    if "/d2l/le/content/" in page.url:
        return
    link = first_matching_locator(page, CONTENT_NAV_LINK_CANDIDATES, timeout_ms=3000)
    if link:
        try:
            link.click()
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            pass
    print(">>> Please open the course's Content page in the browser window.")
    input(">>> Press Enter here once you're there (or Ctrl+C to abort): ")


def expand_all_modules(page: Page) -> None:
    expand_button = first_matching_locator(page, EXPAND_ALL_CANDIDATES, timeout_ms=3000)
    if expand_button:
        try:
            expand_button.click()
            page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            pass


def find_syllabus_candidates(context: BrowserContext) -> list[tuple[Locator, Page]]:
    """Find everything matching 'syllabus' across every open tab and iframe.

    Brightspace sometimes renders a clicked-into topic (e.g. a "Course
    Resources" or "Get Started" page) inside an embedded iframe, and
    sometimes opens it in a whole new browser tab instead. Checking only the
    original page/frame would miss a syllabus link living in either place.
    """
    candidates: list[tuple[Locator, Page]] = []
    for page in context.pages:
        for frame in page.frames:
            try:
                matches = frame.get_by_text(SYLLABUS_RE)
                count = matches.count()
            except Exception:  # noqa: BLE001 - a detached/cross-origin frame can throw
                continue
            candidates.extend((matches.nth(i), page) for i in range(count))
    return candidates


def find_syllabus_locator(context: BrowserContext) -> tuple[Locator, Page] | None:
    """Try to auto-detect the syllabus link. Returns None if it can't be found,
    rather than raising -- the caller falls back to a manual-click capture."""
    candidates = find_syllabus_candidates(context)

    if not candidates:
        print()
        print(">>> Nothing named 'syllabus' was found directly in the content list.")
        print(">>> If the syllabus is actually a link inside another page (e.g. a")
        print(">>> 'Course Resources' or 'Get Started' page), click into that page")
        print(">>> now in the browser -- a new tab is fine, the script checks those too.")
        input(">>> Press Enter here once you can see a 'syllabus' link (or Ctrl+C to abort): ")
        candidates = find_syllabus_candidates(context)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    LOG.info("Found %d items matching 'syllabus':", len(candidates))
    for i, (candidate, _page) in enumerate(candidates):
        print(f"  [{i}] {candidate.inner_text().strip()}")
    choice = input("Which one is the syllabus? Enter a number: ").strip()
    try:
        index = int(choice)
        if not (0 <= index < len(candidates)):
            raise ValueError
    except ValueError:
        LOG.error("Invalid selection, defaulting to the first match.")
        index = 0
    return candidates[index]


def _from_download(download: Download) -> tuple[bytes, str]:
    return Path(download.path()).read_bytes(), download.suggested_filename


def download_from_click(page: Page, target: Locator) -> tuple[bytes, str]:
    """Click a content item and capture whatever download it produces.

    Handles three shapes Brightspace commonly uses: a direct download, a
    viewer page/panel with its own Download button, or a new tab/popup.
    """
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            target.click()
        return _from_download(dl_info.value)
    except PlaywrightTimeoutError:
        pass

    # A viewer likely opened instead of downloading directly.
    download_button = first_matching_locator(page, DOWNLOAD_BUTTON_CANDIDATES, timeout_ms=5000)
    if download_button:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            download_button.click()
        return _from_download(dl_info.value)

    raise SyllabusNotFound(
        "Clicked the syllabus item but no download or Download button appeared."
    )


def wait_for_manual_download(context: BrowserContext, timeout_s: int = 180) -> tuple[bytes, str]:
    """Wait for the user to click a download link themselves, and capture the
    result -- whether that's a real browser download, or a file that just
    opens in a new tab (e.g. Chrome's built-in PDF viewer, which never fires
    a download event at all). This sidesteps having to correctly auto-detect
    or auto-click the link.
    """
    found: dict[str, object] = {}

    def remember_download(download: Download) -> None:
        found.setdefault("download", download)

    def remember_page(new_page: Page) -> None:
        new_page.on("download", remember_download)
        found.setdefault("page", new_page)

    for existing_page in context.pages:
        existing_page.on("download", remember_download)
    context.on("page", remember_page)

    print()
    print(">>> Click the syllabus link/download button yourself now in the browser.")
    print(f">>> Waiting up to {timeout_s} seconds for a download or a new tab to open...")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "download" in found:
            return _from_download(found["download"])  # type: ignore[arg-type]
        if "page" in found:
            new_page: Page = found["page"]  # type: ignore[assignment]
            try:
                new_page.wait_for_load_state("load", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(1)  # give a same-page download event a moment to win first
            if "download" in found:
                return _from_download(found["download"])  # type: ignore[arg-type]
            # The file just opened directly in the new tab (e.g. a PDF viewer).
            # Fetch that same URL with the browser's own cookies rather than
            # relying on a download event that will never fire.
            response = context.request.get(new_page.url)
            filename = Path(new_page.url.split("?")[0]).name or "syllabus.pdf"
            return response.body(), filename
        time.sleep(0.5)

    raise SyllabusNotFound("No download or new tab was detected after waiting for a manual click.")


def save_bytes(data: bytes, suggested_filename: str, output_dir: Path, course_code: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(suggested_filename).suffix or ""
    safe_code = sanitize_course_code(course_code)
    target = output_dir / f"{safe_code}{suffix}"
    if target.exists():
        target = output_dir / f"{safe_code}_2{suffix}"
    target.write_bytes(data)
    return target


def ensure_pdf(path: Path) -> Path:
    """Convert a downloaded file to PDF in place, if it isn't one already.

    Drives Microsoft Word itself (via the docx2pdf package) to do the
    conversion, so no extra software beyond Word is required. Windows and
    macOS only, and Word must be installed.
    """
    if path.suffix.lower() == ".pdf":
        return path

    if path.suffix.lower() not in WORD_CONVERTIBLE_EXTENSIONS:
        LOG.warning(
            "%s isn't a Word-openable document; leaving it as-is.",
            path.name,
        )
        return path

    try:
        from docx2pdf import convert
    except ImportError:
        LOG.warning(
            "docx2pdf isn't installed (pip install docx2pdf), so %s couldn't be "
            "converted to PDF.",
            path.name,
        )
        return path

    pdf_path = path.with_suffix(".pdf")
    try:
        convert(str(path), str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - docx2pdf raises platform-specific errors
        LOG.warning(
            "Word couldn't convert %s to PDF (%s). Make sure Microsoft Word is "
            "installed and not blocked by a dialog box. Leaving original file.",
            path.name,
            exc,
        )
        return path

    if not pdf_path.exists():
        LOG.warning("Expected %s after conversion but it wasn't created; leaving original file.", pdf_path.name)
        return path

    path.unlink()
    return pdf_path


def process_course(
    page: Page, course_code: str, output_dir: Path, base_url: str, api_versions: dict[str, str]
) -> Path:
    data: bytes
    filename: str

    open_course(page, course_code)
    ensure_on_content_page(page)

    org_unit_id = extract_org_unit_id(page)
    api_result = (
        try_api_auto_download(page.context, base_url, api_versions, org_unit_id)
        if org_unit_id is not None
        else None
    )
    if api_result is not None:
        LOG.info("Found and downloaded the syllabus via Brightspace's API -- no clicking needed.")
        data, filename = api_result
    else:
        expand_all_modules(page)

        found = find_syllabus_locator(page.context)
        if found is not None:
            syllabus, syllabus_page = found
            try:
                data, filename = download_from_click(syllabus_page, syllabus)
            except SyllabusNotFound:
                data, filename = wait_for_manual_download(page.context)
        else:
            print(">>> Couldn't auto-detect a syllabus download on this page.")
            data, filename = wait_for_manual_download(page.context)

    saved_path = save_bytes(data, filename, output_dir, course_code)
    return ensure_pdf(saved_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--courses", help="Comma-separated course codes, e.g. CSE201,MATH150")
    parser.add_argument("--courses-file", help="Path to a file with one course code per line")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--output-dir", help="Where to save syllabi (default: ~/Desktop/Syllabi)")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible window. Only useful once a session is already saved "
        "in .auth/ -- you can't complete interactive SSO headless.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(Path(args.config).expanduser())
    course_codes = read_course_codes(args)
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else (
        Path(config["output_dir"]).expanduser() if config.get("output_dir") else DEFAULT_OUTPUT_DIR
    )
    home_url_fragment = config.get("home_url_fragment", "/d2l/home")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    results: dict[str, str] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            channel="chrome",
            headless=args.headless,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        page.goto(config["base_url"])
        if home_url_fragment not in page.url:
            wait_for_login(page, home_url_fragment)

        api_versions = get_api_versions(context, config["base_url"])
        if api_versions:
            LOG.info("Brightspace API detected -- will try fully automatic lookup for each course.")
        else:
            LOG.info("Brightspace API not reachable -- falling back to click-based automation.")

        for course_code in course_codes:
            LOG.info("--- %s ---", course_code)
            try:
                saved_path = process_course(page, course_code, output_dir, config["base_url"], api_versions)
                LOG.info("Saved %s -> %s", course_code, saved_path)
                results[course_code] = f"OK: {saved_path}"
            except (CourseNotFound, SyllabusNotFound) as exc:
                LOG.error("%s: %s", course_code, exc)
                results[course_code] = f"FAILED: {exc}"
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - report and move to next course
                LOG.exception("Unexpected error on %s", course_code)
                results[course_code] = f"ERROR: {exc}"

        context.close()

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    ok = 0
    for code, status in results.items():
        print(f"{code}: {status}")
        if status.startswith("OK"):
            ok += 1
    print(f"\n{ok}/{len(results)} syllabi downloaded to {output_dir}")

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
