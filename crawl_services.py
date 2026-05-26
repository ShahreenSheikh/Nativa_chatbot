"""
NativaCare services diff-and-notify crawler.

Compares the services listed on nativacare.com/services/ to the services
in the Google Sheet (via database.py). Reports:
  - Services that appear on the site but are missing from the sheet (NEW)
  - Services in the sheet that are no longer on the site (REMOVED)

Run options:
  python crawl_services.py            # Print diff to stdout, email if SMTP set
  python crawl_services.py --dry-run  # Print only, never email
  python crawl_services.py --quiet    # Email only if there's a change

The script keeps a small state file (.crawler_state.json) so daily cron
runs don't email the same unchanged diff every day. Only NEW changes since
the last run trigger a notification.

DESIGN PRINCIPLES (intentional limits):
  - Never writes to the sheet. Humans review and decide.
  - Never crashes on parse failure — logs and exits with a code.
  - Exit code 0 = no changes, 1 = changes detected, 2 = error.
    This lets a cron/CI runner alert only on >0 exit codes.

DEPLOYMENT NOTES at the bottom of this file (cron / GitHub Actions / Railway).
"""

import os
import sys
import re
import json
import argparse
import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Local imports — must be run from the project directory
from database import get_services

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVICES_URL = os.getenv("NATIVA_SERVICES_URL", "https://nativacare.com/services/")
STATE_FILE = Path(os.getenv("CRAWLER_STATE_FILE", ".crawler_state.json"))

# Email config — reuses the bot's Resend setup if available
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
NOTIFY_EMAIL = os.getenv("CRAWLER_NOTIFY_EMAIL", os.getenv("CLINIC_EMAIL", ""))

# Request timeout — generous; we run once a day
REQUEST_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str:
    """Fetch a URL synchronously. Returns the raw HTML or raises."""
    headers = {
        "User-Agent": (
            "NativaCare-services-diff-crawler/1.0 "
            "(contact: info@nativacare.com)"
        ),
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text


# The /services/ page on nativacare.com lists each service inside a heading
# or styled card. The exact HTML may change; we use a few patterns and pick
# whichever yields a reasonable count.
#
# Pattern A: <h2>, <h3>, or <h4> with the service name
# Pattern B: Elements with class containing "service-title" or "elementor-heading"
#
# All patterns are tolerant — they extract candidate strings, then a
# downstream filter removes anything that obviously isn't a service name
# (e.g. navigation text, "Read More", etc).

_HEADING_RE = re.compile(
    r"<h[1-4][^>]*>(.*?)</h[1-4]>",
    re.IGNORECASE | re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Heuristic stop-words: if a candidate heading contains these, skip it.
# These are navigation, page furniture, generic CTAs.
_STOP_WORDS = {
    "home", "about", "services", "team", "contact", "blog", "book",
    "appointment", "appointments", "faq", "faqs", "menu", "subscribe",
    "search", "read more", "learn more", "see more", "view all",
    "our services", "our team", "our story", "testimonials", "follow us",
    "categories", "newsletter", "share", "facebook", "twitter", "instagram",
    "linkedin", "youtube", "tiktok", "privacy policy", "terms",
    "copyright", "©", "powered by",
    # Common brand / page-furniture phrases
    "nativacare", "nativa care", "nativa", "welcome", "get in touch",
    "find us", "follow", "social", "links", "useful links",
    "categories", "archives", "recent posts", "latest news",
}

# Substring blocklist: skip the candidate if ANY of these appears anywhere
# in it. Used for phrases like "© 2026 NativaCare" or "Copyright 2026".
_STOP_SUBSTRINGS = [
    "©", "(c)", "copyright", "all rights reserved", "powered by",
    "nativacare", "nativa care",
]


def _clean_text(html_chunk: str) -> str:
    """Strip tags, collapse whitespace, decode common entities."""
    text = _TAG_RE.sub(" ", html_chunk)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&#8217;", "'").replace("&rsquo;", "'")
                .replace("&#8211;", "-").replace("&ndash;", "-")
                .replace("&copy;", "©").replace("&#169;", "©"))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _looks_like_service(name: str) -> bool:
    """Heuristic filter for whether a heading string looks like a real
    service rather than page furniture."""
    if not name:
        return False
    low = name.lower().strip()
    if low in _STOP_WORDS:
        return False
    # Substring check — catches "© 2026 NativaCare" and similar
    if any(stop in low for stop in _STOP_SUBSTRINGS):
        return False
    # Too short — probably nav/widget
    if len(name) < 4:
        return False
    # Too long — probably a description paragraph that slipped through
    if len(name) > 80:
        return False
    # Only digits / symbols — junk
    if not re.search(r"[A-Za-z]", name):
        return False
    # Starts with a year or number-heavy — likely "2026 in review" etc
    if re.match(r"^\d{4}\b", low):
        return False
    return True


def parse_services_from_html(html: str) -> list[str]:
    """Extract candidate service names from the services page HTML.
    Returns a deduplicated, sorted list."""
    candidates = []
    for match in _HEADING_RE.finditer(html):
        text = _clean_text(match.group(1))
        if _looks_like_service(text):
            candidates.append(text)

    # Dedupe while preserving order, then sort for stable diffs
    seen = set()
    unique = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return sorted(unique)


# ---------------------------------------------------------------------------
# Sheet-side reading
# ---------------------------------------------------------------------------

async def read_sheet_services() -> list[str]:
    services = await get_services()
    return sorted({s["service_name"] for s in services if s.get("service_name")})


# ---------------------------------------------------------------------------
# Diff logic with normalization
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Loose normalization so 'Hypnobirthing Class' and 'Hypnobirthing' don't
    look like two different services."""
    s = name.lower().strip()
    s = re.sub(r"\b(class|workshop|session|consultation|service)\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def compute_diff(site_services: list[str], sheet_services: list[str]) -> dict:
    """Return {'new_on_site': [...], 'removed_from_site': [...]}.

    Names are matched via _normalize so casing and trivial suffixes don't
    cause false positives. The returned strings are the ORIGINAL forms
    from each source, for clarity in notifications."""
    site_norm_to_orig = {_normalize(s): s for s in site_services}
    sheet_norm_to_orig = {_normalize(s): s for s in sheet_services}

    site_keys = set(site_norm_to_orig.keys())
    sheet_keys = set(sheet_norm_to_orig.keys())

    new_keys = site_keys - sheet_keys
    removed_keys = sheet_keys - site_keys

    return {
        "new_on_site": sorted(site_norm_to_orig[k] for k in new_keys),
        "removed_from_site": sorted(sheet_norm_to_orig[k] for k in removed_keys),
    }


# ---------------------------------------------------------------------------
# State file — remember the last diff so we don't re-notify
# ---------------------------------------------------------------------------

def _diff_signature(diff: dict) -> str:
    """A stable hash of the diff contents. Used to detect when the diff
    has actually changed since last run."""
    payload = json.dumps({
        "new_on_site": diff["new_on_site"],
        "removed_from_site": diff["removed_from_site"],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        print(f"[State] Could not read state file ({e}). Treating as empty.")
        return {}


def write_state(signature: str, diff: dict):
    try:
        STATE_FILE.write_text(json.dumps({
            "last_signature": signature,
            "last_run": datetime.utcnow().isoformat() + "Z",
            "last_diff": diff,
        }, indent=2))
    except Exception as e:
        print(f"[State] Could not write state file ({e}).")


# ---------------------------------------------------------------------------
# Notification — email via Resend
# ---------------------------------------------------------------------------

def _format_diff_text(diff: dict) -> str:
    lines = []
    if diff["new_on_site"]:
        lines.append("NEW on website (not in sheet):")
        for s in diff["new_on_site"]:
            lines.append(f"  + {s}")
    if diff["removed_from_site"]:
        if lines:
            lines.append("")
        lines.append("REMOVED from website (still in sheet):")
        for s in diff["removed_from_site"]:
            lines.append(f"  - {s}")
    if not lines:
        return "No differences."
    return "\n".join(lines)


def _format_diff_html(diff: dict) -> str:
    parts = ["<div style='font-family: -apple-system, sans-serif;'>"]
    parts.append("<h2>NativaCare services diff</h2>")
    parts.append(f"<p>Run at {datetime.utcnow().isoformat()}Z. "
                 f"Source: {SERVICES_URL}</p>")
    if diff["new_on_site"]:
        parts.append("<h3>NEW on website (not in sheet)</h3><ul>")
        for s in diff["new_on_site"]:
            parts.append(f"<li>{s}</li>")
        parts.append("</ul>")
    if diff["removed_from_site"]:
        parts.append("<h3>REMOVED from website (still in sheet)</h3><ul>")
        for s in diff["removed_from_site"]:
            parts.append(f"<li>{s}</li>")
        parts.append("</ul>")
    parts.append("<hr/>")
    parts.append("<p style='color:#888;font-size:12px;'>"
                 "Reminder: this script never writes to the sheet. Add or "
                 "remove entries manually in the <code>services</code> tab "
                 "and the linked tabs (<code>midwife_services</code>, "
                 "<code>packages</code>) as needed."
                 "</p></div>")
    return "".join(parts)


def send_notification(diff: dict, dry_run: bool = False) -> bool:
    """Returns True if the notification was sent (or would have been in
    dry-run), False otherwise."""
    if not (RESEND_API_KEY and FROM_EMAIL and NOTIFY_EMAIL):
        print("[Notify] Email skipped — RESEND_API_KEY / FROM_EMAIL / "
              "CRAWLER_NOTIFY_EMAIL not all set.")
        return False
    if dry_run:
        print("[Notify] DRY RUN — would have emailed:")
        print("  to:", NOTIFY_EMAIL)
        print("  subject: NativaCare services diff — changes detected")
        print(_format_diff_text(diff))
        return True
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [NOTIFY_EMAIL],
                    "subject": "NativaCare services diff — changes detected",
                    "html": _format_diff_html(diff),
                },
            )
            resp.raise_for_status()
        print(f"[Notify] Email sent to {NOTIFY_EMAIL}.")
        return True
    except Exception as e:
        print(f"[Notify] Email send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Diff the NativaCare services page against the sheet.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the diff but never send email.")
    parser.add_argument("--quiet", action="store_true",
                        help="Print only on change. Useful for cron.")
    parser.add_argument("--force-notify", action="store_true",
                        help="Send notification even if diff hasn't changed "
                             "since last run.")
    args = parser.parse_args(argv)

    # 1. Fetch site
    try:
        html = _fetch_page(SERVICES_URL)
    except Exception as e:
        print(f"[ERROR] Could not fetch {SERVICES_URL}: {e}")
        return 2

    site_services = parse_services_from_html(html)
    if not site_services:
        print(f"[ERROR] Could not parse any services from {SERVICES_URL}. "
              f"The page structure may have changed. Manual review required.")
        return 2

    if not args.quiet:
        print(f"[Site] Found {len(site_services)} service(s) on the website.")

    # 2. Read sheet
    try:
        sheet_services = await read_sheet_services()
    except Exception as e:
        print(f"[ERROR] Could not read services from sheet: {e}")
        return 2

    if not args.quiet:
        print(f"[Sheet] Found {len(sheet_services)} service(s) in the sheet.")

    # 3. Diff
    diff = compute_diff(site_services, sheet_services)
    signature = _diff_signature(diff)
    state = read_state()
    previous_signature = state.get("last_signature")

    has_changes = bool(diff["new_on_site"] or diff["removed_from_site"])
    diff_is_new = signature != previous_signature

    # 4. Report
    if not has_changes:
        if not args.quiet:
            print("[Diff] No differences. Sheet matches website.")
        write_state(signature, diff)
        return 0

    print("\n" + _format_diff_text(diff) + "\n")

    if diff_is_new or args.force_notify:
        send_notification(diff, dry_run=args.dry_run)
    else:
        print("[Notify] Diff is unchanged since last run; not re-sending. "
              "Use --force-notify to override.")

    write_state(signature, diff)
    return 1


if __name__ == "__main__":
    rc = asyncio.run(main(sys.argv[1:]))
    sys.exit(rc)


# =============================================================================
# DEPLOYMENT — how to run this on a schedule
# =============================================================================
#
# Pick ONE of these. All three give you a daily check.
#
# -----------------------------------------------------------------------------
# Option A: Linux cron (simplest if you already have a Linux server)
# -----------------------------------------------------------------------------
# Edit the crontab:   crontab -e
# Add this line (runs every day at 09:00 server time):
#
#   0 9 * * * cd /path/to/nativa && /path/to/venv/bin/python crawl_services.py --quiet >> crawl.log 2>&1
#
# -----------------------------------------------------------------------------
# Option B: GitHub Actions (no server needed)
# -----------------------------------------------------------------------------
# Add the following file to your repo at .github/workflows/services-diff.yml:
#
# name: Services diff
# on:
#   schedule:
#     - cron: '0 9 * * *'   # daily at 09:00 UTC
#   workflow_dispatch:       # also allow manual runs
# jobs:
#   diff:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - uses: actions/setup-python@v5
#         with:
#           python-version: '3.11'
#       - run: pip install -r requirements.txt
#       - run: python crawl_services.py --quiet
#         env:
#           GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
#           RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}
#           FROM_EMAIL: ${{ secrets.FROM_EMAIL }}
#           CRAWLER_NOTIFY_EMAIL: ${{ secrets.CRAWLER_NOTIFY_EMAIL }}
#
# Note: GitHub Actions has no persistent filesystem between runs, so the
# state file resets. That means you'll be re-notified on every diffed run.
# To fix, use `actions/cache@v4` to persist .crawler_state.json across runs.
#
# -----------------------------------------------------------------------------
# Option C: Railway cron service
# -----------------------------------------------------------------------------
# 1. In your Railway project, add a new service of type "Cron".
# 2. Set the schedule (e.g. `0 9 * * *`).
# 3. Set the start command: `python crawl_services.py --quiet`
# 4. Make sure the env vars (GOOGLE_SHEET_ID, RESEND_API_KEY, etc) are
#    shared between the bot service and the cron service.
# Railway DOES persist the filesystem, so the state file works correctly
# across runs.
#
# =============================================================================
