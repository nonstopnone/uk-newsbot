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

# ── scoring thresholds (must match newsbot.py) ─────────────────────────────────
SCORE_DIRECT_POST  = 18   # score >= this + UK anchor → post without AI
SCORE_AI_THRESHOLD =  7   # score >= this (but < DIRECT) → send to AI
NEG_DOM_MULTIPLIER = 1.5  # neg > max(10, multiplier × pos) → hard reject

# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD TABLES  (must stay in sync with newsbot.py)
# ══════════════════════════════════════════════════════════════════════════════

# ── Tier 1 (6 pts): Core UK identifiers ──────────────────────────────────────
UK_KEYWORDS = {
    "uk": 6, "united kingdom": 6, "britain": 6, "great britain": 6,
    "nhs": 6, "national health service": 6,

    # ── Tier 2 (5 pts): Nations, capital, parliament, head of state ───────────
    "england": 5, "scotland": 5, "wales": 5, "northern ireland": 5,
    "london": 5, "westminster": 5, "parliament": 5, "downing street": 5,
    "house of commons": 5, "house of lords": 5, "prime minister": 5,
    "holyrood": 5, "stormont": 5, "senedd": 5, "devolution": 5,
    "king charles": 5, "prince william": 5, "princess of wales": 5,
    "buckingham palace": 5, "windsor castle": 5,

    # ── Tier 3 (4 pts): Major institutions, departments, regulators ───────────
    "home office": 4, "foreign office": 4, "foreign commonwealth": 4,
    "treasury": 4, "bank of england": 4, "chancellor": 4,
    "met police": 4, "metropolitan police": 4, "scotland yard": 4,
    "hmrc": 4, "companies house": 4, "ofcom": 4, "ofsted": 4,
    "environment agency": 4, "electoral commission": 4,
    "financial conduct authority": 4, "fca": 4,
    "uk health security agency": 4, "ukhsa": 4, "mhra": 4,
    "office for national statistics": 4, "ons": 4,
    "office for budget responsibility": 4, "obr": 4,
    "care quality commission": 4, "cqc": 4,
    "ministry of defence": 4, "mod": 4,
    "gchq": 4, "mi5": 4, "mi6": 4, "secret intelligence service": 4,
    "british army": 4, "royal navy": 4, "royal air force": 4, "raf": 4,
    "dvla": 4, "dvsa": 4,
    "network rail": 4, "hs2": 4, "transport for london": 4, "tfl": 4,
    "met office": 4, "national grid": 4,
    "bbc": 4, "itv": 4, "sky news": 4, "channel 4": 4, "channel 5": 4,
    "guardian": 4, "telegraph": 4,
    "ftse": 4, "ftse 100": 4, "ftse 250": 4,
    "royal": 4,

    # ── Tier 3 (4 pts): Political parties ─────────────────────────────────────
    "labour": 4, "labour party": 4, "conservative": 4, "tory": 4,
    "tories": 4, "lib dem": 4, "liberal democrat": 4, "liberal democrats": 4,
    "snp": 4, "scottish national party": 4, "reform uk": 4,
    "plaid cymru": 4, "dup": 4, "sinn fein": 4, "alliance party": 4,
    "green party": 3, "alba party": 3,

    # ── Tier 3 (4 pts): Named politicians ─────────────────────────────────────
    "keir starmer": 4, "rishi sunak": 4, "boris johnson": 4,
    "theresa may": 4, "gordon brown": 4, "tony blair": 4,
    "jeremy hunt": 4, "rachel reeves": 4, "yvette cooper": 4,
    "angela rayner": 4, "david lammy": 4, "wes streeting": 4,
    "pat mcfadden": 4, "bridget phillipson": 4, "ed miliband": 4,
    "nigel farage": 4, "kemi badenoch": 4,

    # ── Tier 3 (4 pts): Major UK cities ───────────────────────────────────────
    "manchester": 4, "birmingham": 4, "leeds": 4, "glasgow": 4,
    "edinburgh": 4, "cardiff": 4, "belfast": 4, "liverpool": 4,
    "sheffield": 4, "bristol": 4, "newcastle": 4, "nottingham": 4,
    "leicester": 4, "southampton": 4, "portsmouth": 4,

    # ── Tier 3 (4 pts): Legal & judicial ──────────────────────────────────────
    "old bailey": 4, "crown court": 4, "supreme court": 4,
    "court of appeal": 4, "high court": 4, "magistrates court": 4,
    "judicial review": 4, "coroner": 4, "inquest": 4,

    # ── Tier 3 (4 pts): Key UK economic terms ─────────────────────────────────
    "gilt": 4, "gilts": 4, "sterling": 4, "pound sterling": 4,
    "base rate": 4, "monetary policy committee": 4, "mpc": 4,
    "autumn statement": 4, "spring statement": 4, "spending review": 4,
    "universal credit": 4, "personal independence payment": 4,
    "council tax": 4, "stamp duty": 4, "national insurance": 4,

    # ── Tier 3 (4 pts): Health-specific UK terms ──────────────────────────────
    "nhs england": 4, "nhs scotland": 4, "nhs wales": 4,
    "nice": 4, "accident and emergency": 4,
    "ambulance trust": 4, "integrated care": 4, "icb": 4,

    # ── Tier 3 (4 pts): Major UK transport hubs ───────────────────────────────
    "heathrow": 4, "gatwick": 4, "stansted": 4, "luton airport": 4,
    "manchester airport": 4, "national rail": 4, "eurostar": 4,

    # ── Tier 3 (4 pts): Major UK companies & brands ───────────────────────────
    "rolls-royce": 4, "bae systems": 4, "bp": 4, "shell uk": 4,
    "barclays": 4, "lloyds": 4, "natwest": 4, "hsbc uk": 4,
    "tesco": 4, "sainsbury": 4, "asda": 4, "marks and spencer": 4,
    "john lewis": 4, "bt group": 4, "vodafone uk": 4, "astrazeneca": 4,
    "glaxosmithkline": 4, "gsk": 4, "unilever": 4,

    # ── Tier 3 (4 pts): UK sport institutions ─────────────────────────────────
    "premier league": 4, "fa cup": 4,
    "the ashes": 4, "six nations": 4, "british lions": 4,
    "wimbledon": 4, "british grand prix": 4,

    # ── Tier 2 (3 pts): Secondary cities, regions, cultural terms ─────────────
    "brighton": 3, "oxford": 3, "cambridge": 3, "york": 3,
    "aberdeen": 3, "dundee": 3, "inverness": 3, "swansea": 3,
    "newport": 3, "derby": 3, "coventry": 3, "hull": 3,
    "middlesbrough": 3, "sunderland": 3, "stoke": 3, "exeter": 3,
    "english channel": 3, "north sea": 3, "irish sea": 3,
    "the midlands": 3, "east anglia": 3,
    "cornwall": 3, "devon": 3, "kent": 3, "surrey": 3,
    "yorkshire": 3, "lancashire": 3, "cumbria": 3,
    "radio 4": 3, "radio 1": 3, "bbc one": 3, "bbc two": 3,
    "daily mail": 3, "the sun": 3, "daily mirror": 3,
    "the independent": 3, "evening standard": 3,
    "russell group": 3, "ucl": 3, "imperial college": 3, "lse": 3,
    "oxford university": 3, "cambridge university": 3,
    "nhs trust": 3, "mental health trust": 3,
    "british": 3,

    # ── Tier 1 (2 pts): Supporting / weaker signals ───────────────────────────
    "english": 2, "scottish": 2, "welsh": 2,
    "ulster": 2, "whitehall": 2, "cabinet": 2, "backbench": 2,
    "mp": 2, "msp": 2, "assembly member": 2,
    "home secretary": 2, "foreign secretary": 2, "health secretary": 2,
    "education secretary": 2, "defence secretary": 2,
    "shadow chancellor": 2, "shadow home secretary": 2,
    "welsh government": 2, "scottish government": 2,
    "northern ireland executive": 2,
    "george osborne": 2, "alastair campbell": 2,
    "ukip": 2, "george galloway": 2,
    "armed forces": 2, "british forces": 2,
    "special air service": 2, "sas": 2, "parachute regiment": 2,
    "old trafford": 2, "wembley": 2, "twickenham": 2,
    "lord's cricket": 2, "ryder cup": 2, "british open": 2,
    "bedroom tax": 2, "furlough": 2, "help to buy": 2,
    "british passport": 2, "right to remain": 2,
    "a&e": 2, "gp surgery": 2,
    "pip": 2,
}

# ── Negative keywords ──────────────────────────────────────────────────────────
NEGATIVE_KEYWORDS = {
    # US politics
    "clinton": -15, "biden": -12, "trump": -12, "obama": -12, "harris": -10,
    "maga": -10, "republican party": -8, "democratic party": -8,
    "white house": -8, "oval office": -8, "air force one": -6,
    "congress": -8, "senate": -8, "house of representatives": -8,
    "capitol hill": -8, "desantis": -8, "pelosi": -6, "aoc": -6, "mcconnell": -6,
    # US institutions & finance
    "fbi": -6, "cia": -6, "pentagon": -6, "federal reserve": -6,
    "wall street": -6, "nasdaq": -5, "dow jones": -5, "sec": -5,
    "fda": -6, "cdc": -6, "nasa": -4, "silicon valley": -6,
    # US geography
    "washington": -6, "new york city": -5, "los angeles": -5,
    "california": -5, "texas": -5, "florida": -5, "chicago": -4, "hollywood": -4,
    # Russia / former Soviet
    "putin": -8, "kremlin": -8, "moscow": -6, "russia": -6,
    "lukashenko": -6, "belarus": -4,
    # China
    "beijing": -6, "xi jinping": -8, "chinese communist party": -6, "politburo": -6,
    # Middle East
    "netanyahu": -5, "tel aviv": -4, "ayatollah": -6, "hezbollah": -5,
    # Other non-UK governments
    "narendra modi": -6, "new delhi": -4, "scott morrison": -6,
    "anthony albanese": -6, "justin trudeau": -6,
    "ottawa": -4, "canberra": -4, "macron": -3,
    # US sports
    "nfl": -6, "nba": -6, "mlb": -6, "nhl": -5, "super bowl": -8,
    "world series": -6, "stanley cup": -5, "march madness": -5,
    "ncaa": -5, "mls": -4,
    # US media
    "fox news": -6, "cnn": -4, "msnbc": -5,
    "new york times": -4, "washington post": -4, "wall street journal": -5,
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
            score += w * count
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
    if neg > max(10, NEG_DOM_MULTIPLIER * pos):
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
        "Is this a current or developing UK news story of public interest or significance "
        "(politics, emergencies, legal developments, culture, sports, or actions involving "
        "public figures such as royals or MPs)? Exclude fluff, previews, or lifestyle content..\n"
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
# VERDICT  — mirrors newsbot.py's target-assignment logic exactly
# ══════════════════════════════════════════════════════════════════════════════
def verdict(score: int, pos: int, full_text: str) -> str:
    """
    DIRECT  — score >= 18 AND a UK anchor word present  → post without AI
    AMBIGUOUS — score >= 7 (but below direct threshold) → AI check needed
    INTL    — score >= 2 (but below AI threshold)       → r/InternationalBulletin
    REJECT  — score < 2                                 → drop entirely
    """
    has_uk_anchor = any(
        g in full_text.lower() for g in ["uk", "britain", "london", "england"]
    )
    if score >= SCORE_DIRECT_POST and has_uk_anchor:
        return "DIRECT"
    if score >= SCORE_AI_THRESHOLD:
        return "AMBIGUOUS"
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
        print(f"  URL      : {url}")
    if title:
        print(f"  Title    : {title}")
    print(f"  Thresholds: direct≥{SCORE_DIRECT_POST}, AI≥{SCORE_AI_THRESHOLD}, "
          f"neg-dom>{NEG_DOM_MULTIPLIER}×pos")
    print(HR)

    print(f"\n  SCORE BREAKDOWN")
    print(f"    Total score    : {score:+d}")
    print(f"    Positive total : {pos:+d}")
    print(f"    Negative total : -{neg}")

    if pos_hits:
        print(f"\n  ✅ POSITIVE KEYWORD HITS")
        for k, cnt in sorted(pos_hits.items(), key=lambda x: -UK_KEYWORDS.get(x[0], 0) * x[1]):
            weight = UK_KEYWORDS[k]
            print(f"    {k:<35}  ×{cnt}  ({weight * cnt:+d})")

    if neg_hits:
        print(f"\n  ❌ NEGATIVE KEYWORD HITS")
        for k, cnt in sorted(neg_hits.items(), key=lambda x: NEGATIVE_KEYWORDS.get(x[0], 0) * x[1]):
            weight = NEGATIVE_KEYWORDS[k]
            print(f"    {k:<35}  ×{cnt}  ({weight * cnt:+d})")

    print(f"\n  CATEGORY     : {category}")
    print(f"  HARD REJECT  : {'YES — ' + reject_reason if rejected else 'No'}")

    # ── show exactly why the score falls where it does ──────────────────────
    if not rejected:
        has_anchor = any(g in full_text.lower() for g in ["uk", "britain", "london", "england"])
        print(f"\n  ROUTING ANALYSIS")
        print(f"    UK anchor present : {'Yes' if has_anchor else 'No'}")
        if score >= SCORE_DIRECT_POST and has_anchor:
            print(f"    Score {score:+d} ≥ {SCORE_DIRECT_POST} + anchor  →  direct post path")
        elif score >= SCORE_AI_THRESHOLD:
            gap = SCORE_DIRECT_POST - score
            print(f"    Score {score:+d} ≥ {SCORE_AI_THRESHOLD}  →  AI check path"
                  + (f"  (needs +{gap} more for direct post)" if has_anchor else "  (no UK anchor)"))
        elif score >= 2:
            gap = SCORE_AI_THRESHOLD - score
            print(f"    Score {score:+d} < {SCORE_AI_THRESHOLD}  →  International path"
                  f"  (needs +{gap} more to reach AI check)")
        else:
            print(f"    Score {score:+d} < 2  →  rejected entirely")

    # ── AI step ─────────────────────────────────────────────────────────────
    ai_result = None
    if not rejected and verd == "AMBIGUOUS" and run_ai:
        print(f"\n  SCORE IS AMBIGUOUS ({SCORE_AI_THRESHOLD}–{SCORE_DIRECT_POST - 1})"
              f" — running AI confirmation …")
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
    elif verd == "DIRECT":
        final = "POSTED → r/BreakingUKNews  (direct, no AI needed)"
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
