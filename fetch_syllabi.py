#!/usr/bin/env python3
"""Download course syllabi from Brightspace (D2L) for a batch of course codes.

Login happens through a real, visible browser window so you can complete your
school's SSO flow (including MFA) by hand; this script only automates the
repetitive part -- finding "syllabus" in each course's content tree and
saving it.

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
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import (
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
PDF_CONVERT_TIMEOUT_S = 60

SYLLABUS_RE = re.compile(r"syllabus", re.IGNORECASE)

# Extensions LibreOffice can convert to PDF. Anything else downloaded (an
# image, a zip, ...) is left as-is with a warning, since it isn't really a
# "document" to convert.
CONVERTIBLE_EXTENSIONS = {".doc", ".docx", ".rtf", ".odt", ".txt", ".ppt", ".pptx", ".xls", ".xlsx"}

# Brightspace/D2L instances are re-themed per school, so these selectors are
# best-effort defaults, tried in order. If your MyFire/Brightspace theme uses
# different markup, tweak this list -- everything else in the script is
# selector-agnostic and falls back to asking you to click manually.
COURSE_SELECTOR_BUTTON_CANDIDATES = [
    '[title="Select a course"]',
    'button:has-text("Select a course")',
    'd2l-navigation-main-header >> [title="Course Selector"]',
]
COURSE_SEARCH_INPUT_CANDIDATES = [
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


def find_syllabus_locator(page: Page) -> Locator:
    matches = page.get_by_text(SYLLABUS_RE)
    count = matches.count()
    if count == 0:
        raise SyllabusNotFound("No content item with 'syllabus' in its name was found.")
    if count == 1:
        return matches.first

    LOG.info("Found %d items matching 'syllabus':", count)
    options = []
    for i in range(count):
        text = matches.nth(i).inner_text().strip()
        options.append(text)
        print(f"  [{i}] {text}")
    choice = input("Which one is the syllabus? Enter a number: ").strip()
    try:
        index = int(choice)
        if not (0 <= index < count):
            raise ValueError
    except ValueError:
        LOG.error("Invalid selection, defaulting to the first match.")
        index = 0
    return matches.nth(index)


def download_from_click(page: Page, target: Locator) -> Download:
    """Click a content item and capture whatever download it produces.

    Handles three shapes Brightspace commonly uses: a direct download, a
    viewer page/panel with its own Download button, or a new tab/popup.
    """
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            target.click()
        return dl_info.value
    except PlaywrightTimeoutError:
        pass

    # A viewer likely opened instead of downloading directly.
    download_button = first_matching_locator(page, DOWNLOAD_BUTTON_CANDIDATES, timeout_ms=5000)
    if download_button:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            download_button.click()
        return dl_info.value

    raise SyllabusNotFound(
        "Clicked the syllabus item but no download or Download button appeared. "
        "You may need to download it manually this time."
    )


def save_download(download: Download, output_dir: Path, course_code: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    suggested = download.suggested_filename
    suffix = Path(suggested).suffix or ""
    safe_code = sanitize_course_code(course_code)
    target = output_dir / f"{safe_code}{suffix}"
    if target.exists():
        target = output_dir / f"{safe_code}_2{suffix}"
    download.save_as(target)
    return target


def ensure_pdf(path: Path) -> Path:
    """Convert a downloaded file to PDF in place, if it isn't one already.

    Uses LibreOffice's headless converter (the `soffice` binary), since it's
    free, cross-platform, and doesn't require MS Office to be installed.
    """
    if path.suffix.lower() == ".pdf":
        return path

    if path.suffix.lower() not in CONVERTIBLE_EXTENSIONS:
        LOG.warning(
            "%s isn't a document LibreOffice can convert to PDF; leaving it as-is.",
            path.name,
        )
        return path

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        LOG.warning(
            "LibreOffice ('soffice') isn't installed, so %s couldn't be converted "
            "to PDF. Install LibreOffice and re-run, or convert it by hand.",
            path.name,
        )
        return path

    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(path.parent), str(path)],
            check=True,
            capture_output=True,
            timeout=PDF_CONVERT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOG.warning("PDF conversion failed for %s: %s. Leaving original file.", path.name, exc)
        return path

    pdf_path = path.with_suffix(".pdf")
    if not pdf_path.exists():
        LOG.warning("Expected %s after conversion but it wasn't created; leaving original file.", pdf_path.name)
        return path

    path.unlink()
    return pdf_path


def process_course(page: Page, course_code: str, output_dir: Path) -> Path:
    open_course(page, course_code)
    ensure_on_content_page(page)
    expand_all_modules(page)
    syllabus = find_syllabus_locator(page)
    download = download_from_click(page, syllabus)
    saved_path = save_download(download, output_dir, course_code)
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
            headless=args.headless,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        page.goto(config["base_url"])
        if home_url_fragment not in page.url:
            wait_for_login(page, home_url_fragment)

        for course_code in course_codes:
            LOG.info("--- %s ---", course_code)
            try:
                saved_path = process_course(page, course_code, output_dir)
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
