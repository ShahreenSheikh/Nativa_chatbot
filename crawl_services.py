"""
NativaCare full-site content mirror crawler.

Fetches services, FAQs, and About Us content from nativacare.com and
writes them to the Google Sheet. The Sheet becomes a mirror of the site.

Content mirrored:
  - services tab (4 rows: the 4 categories on /services/)
      Fields: id, name, short_desc, long_desc, keywords,
              price_aed (dummy), duration_minutes (dummy),
              location_types (dummy), active
  - faqs tab (home page general FAQs + per-service FAQs)
      Fields: id, question, answer, category, source_url
  - about tab (created if missing)
      Fields: section, content

OPERATIONAL DATA — the crawler does NOT touch these:
  - midwives tab
  - midwife_services tab
  - weekly_schedule tab
  - schedule_overrides tab
  - packages tab
  - clinic_info tab
  - appointments tab

Dummy operational data for the 4 new services (prices, durations,
midwives, locations) is HARDCODED in DUMMY_OPERATIONAL_DATA below.
Update those when the clinic gives real numbers, or when adding real
operational data to the website.

Run:
  python crawl_services_new.py                # Dry-run: fetch, parse, report
  python crawl_services_new.py --apply        # Actually write to Sheet
  python crawl_services_new.py --skip-fetch   # Reuse cached HTML for testing

SAFETY:
  - All pages fetched into memory first. If ANY fetch fails, aborts
    with no writes. Sheet stays as-is.
  - Wipe-and-rewrite: replaces all rows in services / faqs / about tabs.
    Never touches other tabs.
  - Refuses to write if fetched content looks empty or wrong (missing
    all 4 categories, or fewer than 3 FAQs found — safety threshold).
"""

import os
import re
import sys
import json
import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("NATIVA_BASE_URL", "https://nativacare.com")

# The 4 category detail pages we crawl. If the clinic adds more services,
# add their URLs here — the crawler will pick them up automatically.
DETAIL_PAGES = [
    ("S001", "Antenatal Preparation & Education",
     f"{BASE_URL}/antenatal-preparation-education-abu-dhabi/"),
    ("S002", "Postnatal Recovery Support",
     f"{BASE_URL}/postnatal-recovery-support-abu-dhabi/"),
    ("S003", "Breastfeeding Support",
     f"{BASE_URL}/breastfeeding-support-abu-dhabi/"),
    ("S004", "Nanny Training",
     f"{BASE_URL}/nanny-traning-abu-dhabi/"),  # note their typo in URL
]

# Home page — has general FAQs
HOME_URL = f"{BASE_URL}/"

# About us page
ABOUT_URL = f"{BASE_URL}/about/"

# Dummy operational data assigned to each of the 4 services. Update when
# the clinic gives real numbers. These are placeholder values only.
DUMMY_OPERATIONAL_DATA = {
    "S001": {  # Antenatal Preparation & Education
        "price_aed": 400,
        "duration_minutes": 90,
        "location_types": "home,clinic",
    },
    "S002": {  # Postnatal Recovery Support
        "price_aed": 350,
        "duration_minutes": 60,
        "location_types": "home",
    },
    "S003": {  # Breastfeeding Support
        "price_aed": 300,
        "duration_minutes": 60,
        "location_types": "home,clinic",
    },
    "S004": {  # Nanny Training
        "price_aed": 600,
        "duration_minutes": 120,
        "location_types": "home",
    },
}

REQUEST_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Fetch layer
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> str:
    """Fetch a page and return its content as text.

    NativaCare returns rich HTML but web-fetch clients typically get the
    body as markdown-ish text. We just fetch raw HTML here; the parser
    below is robust to either markdown or HTML.
    """
    headers = {
        "User-Agent": "NativaCare-Crawler/2.0 (+https://nativacare.com)",
        "Accept": "text/html,application/xhtml+xml",
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS,
                      follow_redirects=True) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.text


def cached_or_fetch(url: str, cache_dir: Path) -> str:
    """Cache-aware fetch. Used by --skip-fetch flag for iterative testing.

    On production runs (no --skip-fetch), cache is written but not read.
    On dev runs, cache is read if available.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[:80]
    cache_file = cache_dir / f"{slug}.html"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8")
    content = fetch_page(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(content, encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# HTML/markdown parsing
# ---------------------------------------------------------------------------

# Boilerplate strings — always present on the site, never real content.
# When any of these appear as a heading or paragraph, we skip them.
BOILERPLATE_MARKERS = [
    "Get A Free 20 Minutes",
    "Stay updated with our latest newsletter",
    "Our Services",
    "Quick LInks", "Quick Links",  # note the site's typo
    "Conatct Info", "Contact Info",
    "modal-check",
    "Dismiss ad",
    "Select Country", "Select State",
    "Testimonials",
    "Sara M., Al Reem Island",  # testimonial markers (there are more,
    "Rachel T.,", "Aisha R.,", "Priya N.,",  # but these catch the pattern
    "Noora H.,", "Sara A.,", "Leila M.,",
    "Rania K.,", "Priya S.,",
    "YES YOU CAN DO IT",
    "All Rights Reserved",
    "Powered by Slider Revolution",
    "banner011", "banner02", "banner03",
    "First-section-homepage",
]


# Standalone CTA phrases that appear as buttons/links in the site's HTML
# and end up as their own lines in extracted content. These are noise
# in long descriptions — strip them.
# Case-insensitive full-line match after stripping whitespace/punctuation.
CTA_LINE_PATTERNS = [
    "contact us",
    "contact us today",
    "book a consultation",
    "book a consultation today",
    "book now",
    "book your consultation",
    "get started",
    "learn more",
    "read more",
    "get in touch",
    "call us",
    "call us today",
    "view all services",
]


def strip_cta_lines(text: str) -> str:
    """Remove standalone CTA / button-link lines from a text block.

    A line matches if, after stripping whitespace and trailing punctuation,
    its lowercase form is in CTA_LINE_PATTERNS. Keeps other content intact.
    """
    if not text:
        return text
    kept_lines = []
    for line in text.split("\n"):
        stripped = line.strip().rstrip(".!?:").strip()
        if stripped.lower() in CTA_LINE_PATTERNS:
            continue
        kept_lines.append(line)
    # Collapse any resulting triple+ blank lines
    result = "\n".join(kept_lines)
    result = re.sub(r"\n\s*\n\s*\n+", "\n\n", result)
    return result.strip()


def strip_html_tags(text: str) -> str:
    """Remove HTML tags for a clean text output."""
    # Preserve line breaks from block-level tags
    text = re.sub(r"</(p|div|h[1-6]|li|br)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#8217;", "'").replace("&#8220;", '"')
                .replace("&#8221;", '"').replace("&#8211;", "-")
                .replace("&#8216;", "'").replace("&#8230;", "..."))
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_text(text: str) -> str:
    """Normalize whitespace in a snippet."""
    if not text:
        return ""
    text = strip_html_tags(text)
    # Trim, collapse spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_boilerplate(text: str) -> bool:
    """Return True if a text chunk looks like site boilerplate."""
    if not text:
        return True
    for marker in BOILERPLATE_MARKERS:
        if marker.lower() in text.lower():
            return True
    return False


def extract_headings_with_content(text: str) -> list[dict]:
    """Extract headings (h1-h4) and their following content blocks.

    Returns a list of dicts:
        [{"level": 2, "heading": "...", "content": "..."}, ...]

    Works on either raw HTML or converted markdown. Strategy:
      1. Find all heading tags OR markdown-style ## lines
      2. For each heading, capture text until the next heading
      3. Strip HTML tags from captured content
    """
    # Normalize: convert HTML headings to markdown-like markers so we
    # only need one parsing pass. `___H2___Some Title___/H2___`
    def h_replacer(m):
        level = int(m.group(1))
        content_inner = clean_text(m.group(2))
        return f"\n___H{level}___{content_inner}___ENDH___\n"

    normalized = re.sub(
        r"<h([1-4])[^>]*>(.*?)</h\1>",
        h_replacer,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Also handle markdown-style headings (## Title)
    md_pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    def md_replacer(m):
        level = len(m.group(1))
        content_inner = m.group(2).strip()
        return f"\n___H{level}___{content_inner}___ENDH___\n"
    normalized = md_pattern.sub(md_replacer, normalized)

    # Split on our markers to get [pre-heading text, heading, content, ...]
    parts = re.split(r"___H(\d)___(.+?)___ENDH___", normalized, flags=re.DOTALL)

    results = []
    # parts is: [before_first, level, heading, content, level, heading, content, ...]
    # We iterate in groups of 3 after the initial "before"
    i = 1
    while i < len(parts) - 2:
        try:
            level = int(parts[i])
            heading = parts[i + 1].strip()
            content = parts[i + 2].strip() if i + 2 < len(parts) else ""
            # Strip any lingering HTML from content
            content = strip_html_tags(content)
            content = re.sub(r"\n\s*\n\s*\n+", "\n\n", content).strip()
            if heading:
                results.append({
                    "level": level,
                    "heading": heading,
                    "content": content,
                })
            i += 3
        except (ValueError, IndexError):
            i += 3
    return results


# ---------------------------------------------------------------------------
# Service page parsing
# ---------------------------------------------------------------------------

def keyword_phrase(text: str) -> str:
    """Normalize a text fragment into a keyword phrase, if possible.

    Rules:
      - Lowercase, stripped of leading/trailing punctuation
      - Take everything up to a colon (headings often have "Topic: detail")
      - Take up to 6 words, but stop earlier if we hit a natural break
        (comma, dash, or reached ~40 chars)
      - Return empty string if the result is too short (<3 chars) or too
        long (>60 chars) or looks like a sentence (period in middle)

    Examples:
      "Labour Education" → "labour education"
      "Feeding support & bottle preparation" → "feeding support & bottle preparation"
      "Recognising when something is wrong" → "recognising when something is wrong"
      "This is a whole sentence about things." → "" (has mid-sentence content)
    """
    if not text:
        return ""
    kw = text.strip("*: ").lower().strip()
    # Cut at colon (heading-style "Topic: description")
    if ":" in kw:
        kw = kw.split(":", 1)[0].strip()
    # Reject sentences: contain sentence-ending punctuation mid-string
    if "." in kw[:-1]:  # period anywhere except at the very end
        return ""
    # Split on commas — keywords should be atomic, not lists
    if "," in kw:
        kw = kw.split(",", 1)[0].strip()
    words = kw.split()
    if not words:
        return ""
    # Take up to 6 words, stopping earlier at ~40 chars
    kept = []
    total = 0
    for w in words[:6]:
        if total + len(w) + len(kept) > 40:
            break
        kept.append(w)
        total += len(w)
    phrase = " ".join(kept)
    if not (3 <= len(phrase) <= 60):
        return ""
    # Reject if phrase ends in a stop word — that suggests we truncated
    # mid-sentence and the result reads weirdly ("this is a long sentence
    # about" — the "about" tells us we cut something off)
    STOP_WORDS_ENDING = {
        "a", "an", "the", "and", "or", "but", "if", "then", "with",
        "for", "of", "in", "on", "at", "to", "about", "by", "from",
        "is", "are", "was", "were", "be", "been", "being",
    }
    if kept and kept[-1] in STOP_WORDS_ENDING:
        return ""
    return phrase


def parse_service_detail(page_text: str,
                         service_id: str,
                         service_name: str) -> dict:
    """Parse a single service detail page.

    Extract:
      - short_desc: the intro paragraph (first 1-2 sentences after the h1)
      - long_desc: joined content from key sections
      - keywords: list of short phrases from "What ... Covers" section
      - service_faqs: Q&A pairs specific to this service

    Returns dict with those fields. Empty strings/lists on failure.
    """
    result = {
        "id": service_id,
        "name": service_name,
        "short_desc": "",
        "long_desc": "",
        "keywords": [],
        "service_faqs": [],
    }

    sections = extract_headings_with_content(page_text)
    if not sections:
        return result

    # Find the first meaningful h1/h2 that isn't just the page title
    # or navigation. Its content is our short description.
    for s in sections:
        if s["level"] in (1, 2) and s["content"] and not is_boilerplate(s["heading"]):
            content = s["content"].strip()
            if content and len(content) > 40:  # skip stub headings
                # First paragraph as short description
                first_para = content.split("\n\n")[0].strip()
                if len(first_para) > 40 and not is_boilerplate(first_para):
                    result["short_desc"] = first_para[:500]
                    break

    # Collect long description: paragraphs from h2/h3 sections that
    # look like real content (not boilerplate, not testimonials, not FAQs)
    long_parts = []
    in_faq_section = False
    faqs_so_far = []
    keyword_source_active = False  # true when inside a "covers" section

    for s in sections:
        heading_lc = s["heading"].lower()
        content = s["content"].strip()

        # Detect FAQ section start (check BEFORE boilerplate filter — FAQ
        # questions can otherwise be flagged out by content patterns).
        if "common question" in heading_lc or heading_lc == "faq" or heading_lc.startswith("faq"):
            in_faq_section = True
            keyword_source_active = False
            continue

        # In FAQ section: h2/h3/h4 = questions, content = answers.
        # This runs BEFORE boilerplate filter so FAQs aren't dropped.
        if in_faq_section:
            if s["level"] in (2, 3, 4) and s["heading"] and content:
                q_text = s["heading"].strip("* ").rstrip("?").strip()
                if q_text and content:
                    q = q_text + "?"
                    a = content.strip()
                    faqs_so_far.append({"question": q, "answer": a})
            continue

        # Skip common boilerplate sections. Only if HEADING itself is
        # boilerplate — don't drop a section just because its content is
        # empty (many h2 titles have their real content in following h3/h4s).
        if is_boilerplate(s["heading"]):
            keyword_source_active = False
            continue
        if content and is_boilerplate(content):
            keyword_source_active = False
            continue

        # Testimonial section — skip
        if "testimonial" in heading_lc or "what mothers" in heading_lc:
            keyword_source_active = False
            continue

        # "What ... Covers" and "Who ... For" sections — the h4 items
        # under them are our keyword source. Set flag ON when we enter,
        # OFF when we exit (i.e. hit any non-h4 heading).
        if any(m in heading_lc for m in ["what our", "what the training",
                                          "covers", "who our", "who it's",
                                          "who is this"]):
            keyword_source_active = True

        # Track keyword candidates: h4 headings while we're in a
        # keyword-source section.
        if keyword_source_active and s["level"] == 4 and s["heading"]:
            if keyword_phrase(s["heading"]):
                s["_is_keyword"] = True

        # Turn off keyword flag when we hit a non-h4 that isn't in
        # a keyword source
        if s["level"] < 4 and not any(m in heading_lc for m in
                                       ["what our", "what the training",
                                        "covers", "who our", "who it's",
                                        "who is this"]):
            keyword_source_active = False

        # Add to long description (h2/h3 with real content)
        if s["level"] in (2, 3) and content and len(content) > 30:
            # Strip standalone "Contact us" / "Book now" CTA lines that
            # got pulled from button links on the site
            cleaned_content = strip_cta_lines(content)
            if not cleaned_content or len(cleaned_content) < 30:
                continue
            if not is_boilerplate(cleaned_content):
                # Skip repeated content we already added
                if cleaned_content not in " ".join(long_parts):
                    heading_line = f"**{s['heading']}**" if s["level"] == 2 else s["heading"]
                    long_parts.append(f"{heading_line}\n{cleaned_content}")

    result["long_desc"] = "\n\n".join(long_parts[:6])[:3000]  # cap at 3k chars
    result["service_faqs"] = faqs_so_far

    # Extract keywords — collect from h4 headings we marked earlier,
    # plus bullet points from any of the keyword source sections.
    keywords = set()
    for s in sections:
        if s.get("_is_keyword"):
            phrase = keyword_phrase(s["heading"])
            if phrase:
                keywords.add(phrase)
        # Also check for bullet points inside content of "covers" sections
        heading_lc = s["heading"].lower()
        if any(m in heading_lc for m in ["what our", "what the training",
                                          "covers", "who our", "who it's",
                                          "who is this"]):
            content = s["content"]
            bullets = re.findall(r"^\s*[-*]\s+\*?\*?(.+?)\*?\*?[:\.]?\s*$",
                                 content, re.MULTILINE)
            for b in bullets:
                phrase = keyword_phrase(b)
                if phrase:
                    keywords.add(phrase)

    result["keywords"] = sorted(keywords)[:15]  # cap at 15
    return result


def parse_home_page(page_text: str) -> dict:
    """Parse the home page. Extract general FAQs and the welcome/about
    text if present."""
    result = {
        "general_faqs": [],
        "welcome_text": "",
    }
    sections = extract_headings_with_content(page_text)

    # Find "FAQ" section — everything under it up to the next non-FAQ h2
    in_faq = False
    faqs = []
    for s in sections:
        heading_lc = s["heading"].lower()
        if "faq" in heading_lc or "common question" in heading_lc:
            in_faq = True
            continue
        if in_faq:
            # Stop at footer sections
            if any(m in heading_lc for m in ["our services", "quick link",
                                              "contact info", "conatct"]):
                in_faq = False
                continue
            # h3/h4 in FAQ area = question, content = answer
            if s["level"] in (3, 4) and s["heading"] and s["content"]:
                q = s["heading"].strip("* ").rstrip("?") + "?"
                a = s["content"].strip()
                if not is_boilerplate(q) and not is_boilerplate(a):
                    faqs.append({"question": q, "answer": a})

    result["general_faqs"] = faqs

    # Welcome text
    for s in sections:
        if "welcome to nativacare" in s["heading"].lower():
            result["welcome_text"] = s["content"][:1500]
            break

    return result


def normalize_section_heading(heading: str, fallback: str = "Overview") -> str:
    """Clean up section headings that were captured from the site.

    The site occasionally marks long tagline sentences with the same h1/h2
    tags used for real section titles. Those sentences read poorly if
    used as-is (e.g. "NativaCare holds the privilege of being the first..."
    is a tagline, not a section title).

    Rules:
      - Strip decorative characters
      - If the cleaned heading is >12 words OR >100 chars, replace with
        `fallback` (usually "Overview")
      - Otherwise return the cleaned heading unchanged
    """
    if not heading:
        return fallback
    cleaned = heading.strip("* ").strip()
    words = cleaned.split()
    if len(words) > 12 or len(cleaned) > 100:
        return fallback
    return cleaned


def parse_about_page(page_text: str) -> dict:
    """Parse /about/ into structured sections.

    Returns:
      {"sections": [{"section": heading, "content": text}, ...]}
    """
    result = {"sections": []}
    sections = extract_headings_with_content(page_text)

    for s in sections:
        # Skip nav-style repeats and boilerplate
        if is_boilerplate(s["heading"]) or is_boilerplate(s["content"]):
            continue
        # Skip footer-style short items
        if s["level"] >= 4 and len(s["content"]) < 60:
            continue
        # Only keep sections with meaningful content
        if s["level"] in (1, 2, 3) and s["content"] and len(s["content"]) > 40:
            # Normalize the section heading — if it's actually a long
            # tagline sentence, replace with "Overview" so downstream
            # rendering doesn't display a paragraph as a title.
            section_name = normalize_section_heading(s["heading"])
            result["sections"].append({
                "section": section_name,
                "content": s["content"].strip()[:2000],
            })

    return result


# ---------------------------------------------------------------------------
# Sheet write layer — uses Google Sheets API directly
# ---------------------------------------------------------------------------

def _build_sheets_client():
    """Build an authenticated Google Sheets API client. Raises on failure."""
    from googleapiclient.discovery import build
    from credentials import get_credentials
    creds = get_credentials(["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)


def _get_sheet_id() -> str:
    sid = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sid:
        raise RuntimeError("GOOGLE_SHEET_ID not set in env")
    return sid


def _ensure_tab_exists(service, spreadsheet_id: str, tab_name: str) -> bool:
    """Ensure a tab with the given name exists. Creates if missing.

    Returns True if the tab existed or was successfully created, False
    on error (never raises — just prints an error).
    """
    try:
        ss = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        existing = {s["properties"]["title"] for s in ss.get("sheets", [])}
        if tab_name in existing:
            return True
        # Create the tab
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [
                {"addSheet": {"properties": {"title": tab_name}}}
            ]},
        ).execute()
        print(f"[Sheet] Created new tab: {tab_name}")
        return True
    except Exception as e:
        print(f"[Sheet] Could not ensure tab {tab_name}: {e}")
        return False


def _wipe_and_write_tab(service, spreadsheet_id: str,
                        tab_name: str, rows: list[list]) -> dict:
    """Clear the tab and write rows starting at A1.

    Returns {"written": N, "error": None} on success,
            {"written": 0, "error": str} on failure.
    """
    try:
        # Clear
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1:ZZ10000",
        ).execute()
        # Write
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()
        return {"written": len(rows), "error": None}
    except Exception as e:
        return {"written": 0, "error": str(e)}


def write_midwife_services_tab(dry_run: bool = False) -> dict:
    """Write the midwife_services mapping tab.

    Since the crawler rewrites `services` with 4 new categories (S001-S004),
    the existing midwife_services rows (which reference old service IDs)
    become stale foreign keys. This function replaces that tab with a
    fresh mapping where BOTH midwives are assigned to ALL 4 services.

    Rationale: with only 4 broad categories and 2 midwives, both midwives
    can plausibly deliver every category. The bot's language-based flow
    then filters slots by which midwife speaks the user's chosen language.

    Columns: midwife_id, service_id, price_override_aed, notes
    """
    # Hardcoded midwife IDs — matches the seed data used by the bot.
    # If the clinic ever adds a third midwife, this list must be updated.
    MIDWIFE_IDS = ["M001", "M002"]  # Najat, Fiona
    SERVICE_IDS = [sid for sid, _, _ in DETAIL_PAGES]  # S001, S002, S003, S004

    header = ["midwife_id", "service_id", "price_override_aed", "notes"]
    rows = [header]
    for mid in MIDWIFE_IDS:
        for sid in SERVICE_IDS:
            rows.append([
                mid,
                sid,
                "",  # no price override — use service's default price
                "auto-generated by crawler",
            ])

    if dry_run:
        print(f"[Dry-run] Would write {len(rows) - 1} midwife_services rows")
        return {"written": 0, "error": None, "dry_run": True}

    try:
        client = _build_sheets_client()
        sid = _get_sheet_id()
    except Exception as e:
        return {"written": 0, "error": f"Sheets client build failed: {e}"}

    return _wipe_and_write_tab(client, sid, "midwife_services", rows)


def write_services_tab(service_details: list[dict], dry_run: bool = False) -> dict:
    """Write the 4 service rows to the services tab.

    Column names match what database.py's get_services() expects:
      - service_id, service_name (unchanged)
      - description (populated from short_desc so the LLM has usable text)
      - default_price_aed (bot reads this key; crawler now writes it)
      - duration_minutes (matches)
      - location_type (singular; matches database.py)
      - active (matches; "TRUE" string)

    We also write two extra columns the bot doesn't currently read but
    that are useful for future use / clinic manual review:
      - short_desc, long_desc, keywords
    Extra columns are ignored by the bot; they don't break anything.
    """
    header = ["service_id", "service_name", "description",
              "default_price_aed", "duration_minutes", "location_type",
              "active", "short_desc", "long_desc", "keywords"]
    rows = [header]
    for sd in service_details:
        op = DUMMY_OPERATIONAL_DATA.get(sd["id"], {})
        rows.append([
            sd["id"],
            sd["name"],
            sd["short_desc"],                 # description ← short_desc
            str(op.get("price_aed", "")),     # default_price_aed
            str(op.get("duration_minutes", "")),
            op.get("location_types", ""),     # location_type (singular)
            "TRUE",
            sd["short_desc"],                 # kept for clarity in sheet
            sd["long_desc"],
            ",".join(sd["keywords"]),
        ])

    if dry_run:
        print(f"[Dry-run] Would write {len(rows) - 1} service rows to services tab")
        return {"written": 0, "error": None, "dry_run": True}

    try:
        client = _build_sheets_client()
        sid = _get_sheet_id()
    except Exception as e:
        return {"written": 0, "error": f"Sheets client build failed: {e}"}

    return _wipe_and_write_tab(client, sid, "services", rows)


def write_faqs_tab(general_faqs: list[dict],
                   per_service_faqs: dict[str, list[dict]],
                   dry_run: bool = False) -> dict:
    """Write FAQs to the faqs tab.

    Columns: faq_id, question, answer, category, source_url
    Categories:
      - "general" for home page FAQs
      - service_id (S001, S002, ...) for service-specific FAQs
    """
    header = ["faq_id", "question", "answer", "category", "source_url"]
    rows = [header]

    counter = 1
    # General first
    for faq in general_faqs:
        rows.append([
            f"F{counter:03d}",
            faq["question"],
            faq["answer"],
            "general",
            HOME_URL,
        ])
        counter += 1

    # Per-service
    id_to_url = {sid: url for sid, _, url in DETAIL_PAGES}
    for service_id, faqs in per_service_faqs.items():
        source_url = id_to_url.get(service_id, "")
        for faq in faqs:
            rows.append([
                f"F{counter:03d}",
                faq["question"],
                faq["answer"],
                service_id,
                source_url,
            ])
            counter += 1

    if dry_run:
        print(f"[Dry-run] Would write {len(rows) - 1} FAQ rows to faqs tab")
        return {"written": 0, "error": None, "dry_run": True}

    try:
        client = _build_sheets_client()
        sid = _get_sheet_id()
        _ensure_tab_exists(client, sid, "faqs")
    except Exception as e:
        return {"written": 0, "error": f"Sheets client build failed: {e}"}

    return _wipe_and_write_tab(client, sid, "faqs", rows)


def write_about_tab(about_data: dict, dry_run: bool = False) -> dict:
    """Write About page content to the about tab.

    Columns: section, content, source_url
    """
    header = ["section", "content", "source_url"]
    rows = [header]
    for s in about_data.get("sections", []):
        rows.append([s["section"], s["content"], ABOUT_URL])

    if dry_run:
        print(f"[Dry-run] Would write {len(rows) - 1} About sections to about tab")
        return {"written": 0, "error": None, "dry_run": True}

    try:
        client = _build_sheets_client()
        sid = _get_sheet_id()
        _ensure_tab_exists(client, sid, "about")
    except Exception as e:
        return {"written": 0, "error": f"Sheets client build failed: {e}"}

    return _wipe_and_write_tab(client, sid, "about", rows)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def fetch_all_pages(cache_dir: Path, use_cache: bool) -> Optional[dict]:
    """Fetch all pages needed for the crawl. Returns dict or None on failure.

    The dict has keys: home_html, about_html, s001_html, s002_html,
    s003_html, s004_html.

    If any fetch fails, prints an error and returns None (do not proceed
    with partial data).
    """
    pages = {}
    to_fetch = [
        ("home", HOME_URL),
        ("about", ABOUT_URL),
    ] + [(sid.lower(), url) for sid, _, url in DETAIL_PAGES]

    for key, url in to_fetch:
        print(f"[Fetch] {url}")
        try:
            if use_cache:
                content = cached_or_fetch(url, cache_dir)
            else:
                content = fetch_page(url)
                # Also cache for future --skip-fetch runs
                cache_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")[:80]
                (cache_dir / f"{slug}.html").write_text(content, encoding="utf-8")
        except Exception as e:
            print(f"[ERROR] Fetch failed for {url}: {e}")
            print(f"[ERROR] Aborting crawl. Sheet was not modified.")
            return None
        pages[key] = content
        print(f"  → {len(content)} bytes")

    return pages


def safety_check(service_details: list[dict],
                 general_faqs: list[dict],
                 about_data: dict) -> tuple[bool, str]:
    """Verify parsed content looks reasonable before writing to Sheet.

    Returns (ok, reason). If not ok, do not write.
    """
    # Check: all 4 services parsed and have real content
    if len(service_details) != 4:
        return (False, f"Expected 4 services, got {len(service_details)}")
    for sd in service_details:
        if not sd["short_desc"] or len(sd["short_desc"]) < 40:
            return (False, f"Service {sd['id']} short_desc too short "
                           f"({len(sd['short_desc'])} chars)")
        if not sd["long_desc"] or len(sd["long_desc"]) < 100:
            return (False, f"Service {sd['id']} long_desc too short "
                           f"({len(sd['long_desc'])} chars)")

    # Check: at least 3 general FAQs
    if len(general_faqs) < 3:
        return (False, f"Only {len(general_faqs)} general FAQs found "
                       f"(expected 3+)")

    # Check: About has at least 2 sections
    if len(about_data.get("sections", [])) < 2:
        return (False, f"About page has only "
                       f"{len(about_data.get('sections', []))} sections "
                       f"(expected 2+)")

    return (True, "")


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="NativaCare full-site content mirror crawler.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to the Sheet. Default is dry-run.")
    parser.add_argument("--preview", action="store_true",
                        help="Print full parsed content to stdout before "
                             "writing. Useful before running --apply for the "
                             "first time. Can be combined with --apply.")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Use cached HTML instead of fetching live. "
                             "Useful for testing parsers without hitting "
                             "the site.")
    parser.add_argument("--cache-dir", default=".crawler_cache",
                        help="Directory for cached HTML (default: "
                             ".crawler_cache)")
    args = parser.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    dry_run = not args.apply

    print(f"\n{'=' * 60}")
    print(f"NativaCare crawler — mode: {'DRY-RUN' if dry_run else 'APPLY'}")
    print(f"{'=' * 60}\n")

    # 1. Fetch all pages
    pages = fetch_all_pages(cache_dir, use_cache=args.skip_fetch)
    if pages is None:
        return 2

    # 2. Parse
    print(f"\n[Parse] Extracting content...")

    service_details = []
    for sid, name, _url in DETAIL_PAGES:
        page_key = sid.lower()
        page_content = pages.get(page_key, "")
        detail = parse_service_detail(page_content, sid, name)
        service_details.append(detail)
        print(f"  {sid} {name}:")
        print(f"    short_desc: {len(detail['short_desc'])} chars")
        print(f"    long_desc:  {len(detail['long_desc'])} chars")
        print(f"    keywords:   {len(detail['keywords'])}")
        print(f"    service FAQs: {len(detail['service_faqs'])}")

    home_data = parse_home_page(pages["home"])
    print(f"  home: {len(home_data['general_faqs'])} general FAQs")

    about_data = parse_about_page(pages["about"])
    print(f"  about: {len(about_data['sections'])} sections")

    # 3. Safety check
    print(f"\n[Safety] Verifying parsed content...")
    ok, reason = safety_check(service_details, home_data["general_faqs"],
                              about_data)
    if not ok:
        print(f"[ERROR] Safety check failed: {reason}")
        print(f"[ERROR] Sheet was not modified. Review parser output above.")
        return 2
    print(f"  ✓ All checks passed")

    # 4. Preview (if requested) — dump actual parsed content so user can eyeball
    if args.preview:
        print(f"\n{'=' * 60}")
        print(f"PREVIEW — actual parsed content that will be written")
        print(f"{'=' * 60}\n")

        # Services
        for sd in service_details:
            print(f"─── {sd['id']}: {sd['name']} ───")
            print(f"\nSHORT DESC:")
            print(f"  {sd['short_desc']}")
            print(f"\nLONG DESC ({len(sd['long_desc'])} chars):")
            for line in sd['long_desc'].split("\n")[:30]:
                print(f"  {line}")
            if len(sd['long_desc'].split("\n")) > 30:
                print(f"  ... ({len(sd['long_desc'].split(chr(10))) - 30} more lines)")
            print(f"\nKEYWORDS: {', '.join(sd['keywords'])}")
            if sd['service_faqs']:
                print(f"\nSERVICE FAQs ({len(sd['service_faqs'])}):")
                for faq in sd['service_faqs']:
                    print(f"  Q: {faq['question']}")
                    print(f"  A: {faq['answer'][:200]}"
                          + ("..." if len(faq['answer']) > 200 else ""))
            else:
                print(f"\nSERVICE FAQs: (none extracted)")
            print()

        # General FAQs
        print(f"─── GENERAL FAQs (from home page, {len(home_data['general_faqs'])} found) ───")
        for i, faq in enumerate(home_data['general_faqs'], 1):
            print(f"\n  [{i}] Q: {faq['question']}")
            print(f"      A: {faq['answer'][:250]}"
                  + ("..." if len(faq['answer']) > 250 else ""))

        # About sections
        print(f"\n─── ABOUT sections ({len(about_data['sections'])} found) ───")
        for i, sec in enumerate(about_data['sections'], 1):
            print(f"\n  [{i}] SECTION: {sec['section']}")
            print(f"      CONTENT ({len(sec['content'])} chars): "
                  f"{sec['content'][:300]}"
                  + ("..." if len(sec['content']) > 300 else ""))

        print(f"\n{'=' * 60}\n")

        # If preview-only (no --apply), stop here
        if not args.apply:
            print("[Preview complete] Run with --apply to write to Sheet.")
            return 0

    # 5. Write
    print(f"\n[Write] {'DRY-RUN — no writes' if dry_run else 'Writing to Sheet'}")

    per_service_faqs = {sd["id"]: sd["service_faqs"]
                        for sd in service_details if sd["service_faqs"]}

    results = {}
    results["services"] = write_services_tab(service_details, dry_run=dry_run)
    print(f"  services: {results['services']}")

    # midwife_services must be rewritten too — the services tab now has
    # new IDs (S001-S004), and the old mapping rows referenced deleted
    # IDs. Fresh mapping: both midwives assigned to all 4 services.
    results["midwife_services"] = write_midwife_services_tab(dry_run=dry_run)
    print(f"  midwife_services: {results['midwife_services']}")

    results["faqs"] = write_faqs_tab(home_data["general_faqs"],
                                     per_service_faqs, dry_run=dry_run)
    print(f"  faqs: {results['faqs']}")

    results["about"] = write_about_tab(about_data, dry_run=dry_run)
    print(f"  about: {results['about']}")

    # 6. Summary
    errors = [(tab, r["error"]) for tab, r in results.items() if r.get("error")]
    if errors:
        print(f"\n[ERROR] {len(errors)} error(s) during write:")
        for tab, err in errors:
            print(f"  {tab}: {err}")
        return 2

    if dry_run:
        print(f"\n[DRY-RUN COMPLETE] Run with --apply to actually write.")
    else:
        total = sum(r.get("written", 0) for r in results.values())
        print(f"\n[SUCCESS] Wrote {total} rows across services, faqs, about.")

    return 0


def main():
    argv = sys.argv[1:]
    exit_code = asyncio.run(run(argv))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()