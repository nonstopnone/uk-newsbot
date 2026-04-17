#!/usr/bin/env python3
"""
UK News Article Relevance Scorer
─────────────────────────────────
Strips everything except the scoring pipeline from the newsbot.

Usage (CLI):
  python score_article.py --url https://www.bbc.co.uk/news/...
  python score_article.py --title "PM visits Scotland" --summary "..." --body "..."
  python score_article.py --paste   # reads full text from stdin

Environment variables (only needed for AI confirmation step):
  GEMINI_API_KEY   – if omitted the AI step is skipped gracefully

Exit codes: 0 = relevant, 1 = rejected / low-score
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ── optional Gemini ────────────────────────────────────────────────────────────
try:
    from google import genai as _genai
    _GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    _gemini_client = _genai.Client(api_key=_GEMINI_KEY) if _GEMINI_KEY else None
except ImportError:
    _gemini_client = None

GEMINI_MODEL = "gemini-2.5-flash"

# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD TABLES  (identical weights to the bot)
# ══════════════════════════════════════════════════════════════════════════════
UK_KEYWORDS = {
    "uk": 6, "united kingdom": 6, "britain": 6, "great britain": 6,
    "england": 5, "scotland": 5, "wales": 5, "northern ireland": 5,
    "london": 5, "westminster": 5, "parliament": 5, "downing street": 5,
    "house of commons": 5, "house of lords": 5,
    "prime minister": 5, "home office": 4, "foreign office": 4,
    "treasury": 4, "bank of england": 4, "chancellor": 4,
    "nhs": 6, "national health service": 6,
    "met police": 4, "metropolitan police": 4, "scotland yard": 4,
    "bbc": 4, "itv": 4, "sky news": 4, "guardian": 4, "telegraph": 4,
    "labour": 4, "labour party": 4, "conservative": 4, "tory": 4,
    "lib dem": 4, "liberal democrat": 4, "snp": 4,
    "reform uk": 4, "green party": 3, "king charles": 5, "royal": 4,
}

NEGATIVE_KEYWORDS = {
    "clinton": -15, "biden": -12, "trump": -12, "harris": -10,
    "white house": -8, "congress": -8, "senate": -8, "washington": -6,
    "fbi": -6, "cia": -6, "pentagon": -6, "wall street": -6,
    "nfl": -6, "nba": -6, "mlb": -6, "super bowl": -6,
    "beijing": -6, "china": -6, "moscow": -6, "russia": -6, "putin": -8,
}

BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone", "where to watch",
    "fantasy football", "fpl", "opinion:", "comment:", "letters:", "wordle", "crossword",
]

FLUFF_PATTERNS = [
    re.compile(r"^Why\s",    re.I),
    re.compile(r"^How\s",    re.I),
    re.compile(r"^Here'?s\s", re.I),
    re.compile(r"^\d+\s(ways|things|reasons)", re.I),
]

CATEGORY_KEYS = {
    "Politics":      ["parliament", "government", "minister", "mp", "election", "brexit", "labour", "tory"],
    "Economy":       ["economy", "inflation", "budget", "tax", "bank"],
    "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "prison"],
    "Sport":         ["football", "cricket", "match", "cup", "trophy"],
    "Royals":        ["royal", "king", "queen", "palace"],
    "Environment":   ["storm", "weather", "flood", "climate", "met office"],
}

# ══════════════════════════════════════════════════════════════════════════════
# COMPILED PATTERNS
# ══════════════════════════════════════════════════════════════════════════════
def _compile(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

UK_PATTERNS  = _compile(UK_KEYWORDS)
NEG_PATTERNS = _compile(NEGATIVE_KEYWORDS)

# ══════════════════════════════════════════════════════════════════════════════
# FETCH
# ══════════════════════════════════════════════════════════════════════════════
def fetch_article_text(url: str) -> list[str]:
    """Download URL and return non-trivial <p> texts."""
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.content, "html.parser")
        return [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text()) > 40]
    except Exception as exc:
        print(f"[WARN] fetch failed: {exc}", file=sys.stderr)
        return []

# ══════════════════════════════════════════════════════════════════════════════
# SCORING
# ══════════════════════════════════════════════════════════════════════════════
def calculate_score(text: str) -> tuple[int, int, int, dict]:
    """
    Returns (total_score, positive_sum, negative_sum, matched_keywords).
    Negative weights in the table are negative; neg_sum is the *absolute* total.
    """
    tl = text.lower()
    score = pos = neg = 0
    matched: dict[str, int] = {}

    for k, w, pat in UK_PATTERNS:
        count = len(pat.findall(tl))
        if count:
            score += w * count
            pos   += w * count
            matched[k] = matched.get(k, 0) + count

    for k, w, pat in NEG_PATTERNS:
        count = len(pat.findall(tl))
        if count:
            score += w * count          # w is already negative
            neg   += abs(w) * count
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + count

    return score, pos, neg, matched


def is_hard_reject(text: str, pos: int, neg: int) -> tuple[bool, str]:
    tl = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in tl:
            return True, f"banned phrase: '{phrase}'"
    for pat in FLUFF_PATTERNS:
        if pat.search(text):
            return True, "fluff/opinion headline pattern"
    if neg > max(10, 2.0 * pos):
        return True, f"negative dominance (neg={neg} pos={pos})"
    return False, ""


def detect_category(text: str) -> str:
    tl = text.lower()
    scores = {c: sum(1 for k in keys if k in tl) for c, keys in CATEGORY_KEYS.items()}
    if all(v == 0 for v in scores.values()):
        return "Notable International"
    return max(scores, key=scores.get)


# ══════════════════════════════════════════════════════════════════════════════
# AI CONFIRMATION  (exactly what the bot sends)
# ══════════════════════════════════════════════════════════════════════════════
def build_ai_prompt(title: str, summary: str, full_text: str) -> str:
    """Returns the exact prompt the bot sends to Gemini."""
    excerpt = " ".join(full_text.split()[:200])
    return (
        "Strict UK news filter. Respond YES or NO. "
        "Is this hard news relevant to the UK? (No fluff/sports previews/lifestyle).\n"
        f"Title: {title}\n"
        f"Summary: {summary}\n"
        f"Excerpt: {excerpt}"
    )


def check_ai_relevance(title: str, summary: str, full_text: str) -> tuple[bool | None, str]:
    """
    Calls Gemini with the exact prompt the bot uses.
    Returns (result_bool, prompt_sent).  result_bool is None if AI unavailable.
    """
    prompt = build_ai_prompt(title, summary, full_text)
    if _gemini_client is None:
        return None, prompt
    try:
        resp = _gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        ).text.strip().lower()
        return "yes" in resp, prompt
    except Exception as exc:
        print(f"[WARN] Gemini error: {exc}", file=sys.stderr)
        return None, prompt


# ══════════════════════════════════════════════════════════════════════════════
# VERDICT
# ══════════════════════════════════════════════════════════════════════════════
def verdict(score: int, pos: int, full_text: str) -> str:
    """Replicates the bot's target-assignment logic."""
    has_uk_anchor = any(g in full_text.lower() for g in ["uk", "britain", "london", "england"])
    if score >= 15 and has_uk_anchor:
        return "UK"
    if 4 <= score < 15:
        return "AMBIGUOUS"   # AI step needed
    if score >= 2:
        return "INTL"
    return "REJECT"


# ══════════════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════════════
HR = "─" * 64

def print_report(
    *,
    title: str,
    summary: str,
    full_text: str,
    url: str = "",
    run_ai: bool = True,
) -> int:
    """Pretty-prints the full scoring report. Returns 0 if relevant, 1 if not."""

    score, pos, neg, matched = calculate_score(full_text)
    rejected, reject_reason = is_hard_reject(full_text, pos, neg)
    category = detect_category(full_text)
    verd = verdict(score, pos, full_text)

    # ── positive / negative keyword tables ──────────────────────────────────
    pos_hits = {k: v for k, v in matched.items() if not k.startswith("NEG:")}
    neg_hits = {k[4:]: v for k, v in matched.items() if k.startswith("NEG:")}

    print(f"\n{HR}")
    print("  UK NEWS RELEVANCE SCORER")
    print(HR)
    if url:
        print(f"  URL     : {url}")
    if title:
        print(f"  Title   : {title}")
    print(HR)

    print(f"\n  SCORE BREAKDOWN")
    print(f"    Total score    : {score:+d}")
    print(f"    Positive total : {pos:+d}")
    print(f"    Negative total : -{neg}")

    if pos_hits:
        print(f"\n  ✅ POSITIVE KEYWORD HITS")
        for k, cnt in sorted(pos_hits.items(), key=lambda x: -x[1]):
            weight = UK_KEYWORDS[k]
            print(f"    {k:<25}  ×{cnt}  ({weight * cnt:+d})")

    if neg_hits:
        print(f"\n  ❌ NEGATIVE KEYWORD HITS")
        for k, cnt in sorted(neg_hits.items(), key=lambda x: -x[1]):
            weight = NEGATIVE_KEYWORDS[k]
            print(f"    {k:<25}  ×{cnt}  ({weight * cnt:+d})")

    print(f"\n  CATEGORY     : {category}")
    print(f"  HARD REJECT  : {'YES — ' + reject_reason if rejected else 'No'}")

    # ── AI step ─────────────────────────────────────────────────────────────
    ai_result = None
    if not rejected and verd == "AMBIGUOUS" and run_ai:
        print(f"\n  SCORE IS AMBIGUOUS (4–14) — running AI confirmation …")
        ai_result, prompt_sent = check_ai_relevance(title, summary, full_text)
        print(f"\n  ── EXACT PROMPT SENT TO GEMINI ──────────────────────────")
        for line in prompt_sent.splitlines():
            print(f"  {line}")
        print(f"  ─────────────────────────────────────────────────────────")
        if ai_result is None:
            print("  AI RESULT    : SKIPPED (no GEMINI_API_KEY)")
        else:
            print(f"  AI RESULT    : {'YES – relevant' if ai_result else 'NO – not relevant'}")

    # ── final verdict ────────────────────────────────────────────────────────
    print(f"\n{HR}")
    final: str
    exit_code: int

    if rejected:
        final = f"REJECTED  ({reject_reason})"
        exit_code = 1
    elif verd == "UK":
        final = "POSTED → r/BreakingUKNews"
        exit_code = 0
    elif verd == "AMBIGUOUS":
        if ai_result is True:
            final = "AI CONFIRMED → r/BreakingUKNews"
            exit_code = 0
        elif ai_result is False:
            final = "AI REJECTED → r/InternationalBulletin"
            exit_code = 1
        else:
            final = "AMBIGUOUS (no AI available) → would try AI in bot"
            exit_code = 0
    elif verd == "INTL":
        final = "LOW UK SCORE → r/InternationalBulletin"
        exit_code = 1
    else:
        final = "REJECTED  (score too low)"
        exit_code = 1

    print(f"  VERDICT  : {final}")
    print(HR + "\n")

    # ── GitHub Actions output ────────────────────────────────────────────────
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"score={score}\n")
            f.write(f"category={category}\n")
            f.write(f"verdict={final}\n")
            f.write(f"exit_code={exit_code}\n")

    return exit_code


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score a news article for UK relevance using the newsbot pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python score_article.py --url https://www.bbc.co.uk/news/uk-12345678
              python score_article.py --title "NHS waiting lists" --summary "..." --body "..."
              echo "full article text here" | python score_article.py --paste
              python score_article.py --url https://... --no-ai
        """),
    )
    ap.add_argument("--url",     help="Article URL to fetch and score")
    ap.add_argument("--title",   default="", help="Article title (manual mode)")
    ap.add_argument("--summary", default="", help="Article summary (manual mode)")
    ap.add_argument("--body",    default="", help="Article body text (manual mode)")
    ap.add_argument("--paste",   action="store_true", help="Read full text from stdin")
    ap.add_argument("--no-ai",   action="store_true", help="Skip the Gemini AI step")
    args = ap.parse_args()

    title = summary = body = url_str = ""

    if args.url:
        url_str = args.url
        print(f"Fetching: {url_str} …", file=sys.stderr)
        paras = fetch_article_text(url_str)
        body = " ".join(paras)
        # Use URL path as title placeholder if none scraped
        title = args.title or url_str.split("/")[-1].replace("-", " ")
        summary = args.summary

    elif args.paste:
        body = sys.stdin.read()
        title   = args.title
        summary = args.summary

    elif args.body or args.title:
        title   = args.title
        summary = args.summary
        body    = args.body

    else:
        ap.print_help()
        return 2

    full_text = f"{title} {summary} {body}".strip()
    if not full_text:
        print("No content to score.", file=sys.stderr)
        return 2

    return print_report(
        title=title,
        summary=summary,
        full_text=full_text,
        url=url_str,
        run_ai=not args.no_ai,
    )


if __name__ == "__main__":
    sys.exit(main())
