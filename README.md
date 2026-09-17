# syllabus-fetcher

Downloads course syllabi from MyFire/Brightspace (D2L) for a batch of course
codes, always as PDF. Login goes through a real, visible browser window so
you do your school's SSO (and any MFA) by hand; the script automates the
repetitive part: searching each course's content tree for something named
"syllabus" and saving it into one flat folder, named by course code.

**Run this on your own machine, not in a headless/remote environment** — the
login step needs a real window you can click through.

## Setup (one time)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.json config.json
```

The script drives your actual installed Google Chrome (via Playwright's
`channel="chrome"`), not a separate downloaded browser, so make sure Chrome
is installed and there's nothing else to fetch for it.

PDF conversion drives Microsoft Word itself (via the `docx2pdf` package
already in requirements.txt), so no extra software to install — just make
sure Word is installed and not blocked by a dialog box when the script runs.
Windows and macOS only. If Word isn't available, the script still downloads
the file but leaves it in its original format and warns you.

Edit `config.json`:

- `base_url` — the URL that starts your MyFire/Brightspace SSO login (the
  page you'd normally open in a browser to log in).
- `home_url_fragment` — a substring of the URL Brightspace lands you on once
  login succeeds (default `/d2l/home`). Used to detect that login finished.
- `output_dir` — optional; defaults to `~/Desktop/Syllabi` if left `null`.

`config.json` is gitignored, since it may reflect your personal login URL.

## Usage

```bash
python fetch_syllabi.py --courses CSE201,MATH150,ENGL101
# or
python fetch_syllabi.py --courses-file courses.txt
```

A Chrome window opens. Log in via SSO (the script waits up to 5 minutes).
After that, for each course code the script tries to:

1. Find and open the course automatically via Brightspace's course
   selector/search.
2. Open its Content page and expand all modules.
3. Find an item with "syllabus" in the name and download it.

Every downloaded file is saved flat into the output folder, named by course
code and converted to PDF if it wasn't one already (e.g. `Syllabi/CSE201.pdf`,
`Syllabi/MATH150.pdf`) — see the Word/docx2pdf note above.

### Manual fallback

Brightspace themes vary by school, so if a step can't be automated (course
not found, no Content link, several items match "syllabus"), the script
pauses and asks you to do that one step by hand in the open browser window,
then press Enter in the terminal to let it continue. It still saves you from
re-doing the repetitive per-course search-and-download by hand.

### Session reuse

Your logged-in session is kept in `.auth/` (gitignored) so you don't have to
SSO in again on every run, as long as the session hasn't expired.

## Tuning selectors

If automated course search or "Expand All" never succeeds for your school's
theme, open `fetch_syllabi.py` and adjust the `*_CANDIDATES` selector lists
near the top of the file — the rest of the script doesn't need to change.

## Claude Code permissions

To scope Claude Code's write access to this project directory and the
default `~/Desktop/Syllabi` output folder (so it doesn't prompt for approval
on every action here), add a `.claude/settings.json` with:

```json
{
  "permissions": {
    "allow": [
      "Write(**)",
      "Edit(**)",
      "Write(~/Desktop/Syllabi/**)",
      "Edit(~/Desktop/Syllabi/**)",
      "Bash(mkdir -p ~/Desktop/Syllabi*)"
    ]
  }
}
```
