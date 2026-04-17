# =========================
# Section: Imports and Configuration
# =========================
import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import urllib.parse
import re
import hashlib
import html
import json
import difflib
from dateutil import parser as dateparser
from collections import Counter
from google import genai
import random
import string

# =========================
# Section: Console Colors & Logging
# =========================
class Col:
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'
    MAGENTA = '\033[95m'
    WHITE   = '\033[97m'
    DIM     = '\033[2m'
    RESET   = '\033[0m'

def log(tag, msg, color=Col.RESET):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{color}[{ts}] [{tag}] {msg}{Col.RESET}", flush=True)

def log_score_detail(entry_title, score, pos, neg, matched, target, reason):
    """Prints a colour-coded scoring breakdown for a single article."""
    pos_hits = {k: v for k, v in matched.items() if not k.startswith("NEG:")}
    neg_hits = {k[4:]: v for k, v in matched.items() if k.startswith("NEG:")}

    log("SCORE", f"{'─'*60}", Col.DIM)
    log("SCORE", f"{Col.WHITE}{entry_title[:70]}{Col.RESET}", Col.DIM)
    log("SCORE", f"Total={Col.YELLOW}{score:+d}{Col.RESET}  "
                 f"UK={Col.GREEN}+{pos}{Col.RESET}  "
                 f"Non-UK={Col.RED}-{neg}{Col.RESET}  "
                 f"→ Target={Col.CYAN}{target}{Col.RESET}", Col.DIM)

    if pos_hits:
        kw_str = ", ".join(
            f"{Col.GREEN}{k}{Col.RESET}(×{v})" for k, v in
            sorted(pos_hits.items(), key=lambda x: -x[1])[:6]
        )
        log("SCORE", f"UK hits: {kw_str}", Col.DIM)

    if neg_hits:
        kw_str = ", ".join(
            f"{Col.RED}{k}{Col.RESET}(×{v})" for k, v in
            sorted(neg_hits.items(), key=lambda x: -x[1])[:4]
        )
        log("SCORE", f"Non-UK:  {kw_str}", Col.DIM)

    log("SCORE", f"Reason: {Col.MAGENTA}{reason}{Col.RESET}", Col.DIM)
    log("SCORE", f"{'─'*60}", Col.DIM)

# =========================
# Section: Reddit & Gemini Setup
# =========================
REQUIRED_ENV = [
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDITPASSWORD",
    "GEMINI_API_KEY"
]

for v in REQUIRED_ENV:
    if v not in os.environ:
        sys.exit(f"Missing env var: {v}")

try:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    log("ERROR", f"Failed to init Gemini: {e}", Col.RED)
    sys.exit(1)

reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDITPASSWORD"],
    user_agent="BreakingUKNewsBot/6.4"
)

try:
    log("SYSTEM", f"Logged in as: {reddit.user.me()}", Col.GREEN)
except Exception as e:
    log("CRITICAL", f"Login failed: {e}", Col.RED)
    sys.exit(1)

model_name = 'gemini-2.5-flash'
subreddit_uk   = reddit.subreddit("BreakingUKNews")
subreddit_intl = reddit.subreddit("InternationalBulletin")

# =========================
# Section: Files and Constants
# =========================
DEDUP_FILE    = "posted_urls.txt"
AI_CACHE_FILE = "ai_cache.json"
METRICS_FILE  = "metrics.json"

IN_RUN_FUZZY_THRESHOLD = 0.55
TARGET_POSTS           = 8
MAX_PER_SOURCE         = 3
INITIAL_ARTICLES       = 80
TIME_WINDOW_HOURS      = 12

# =========================
# Section: Keyword Definitions
# =========================
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
    "reform uk": 4, "green party": 3, "king charles": 5, "royal": 4
}

NEGATIVE_KEYWORDS = {
    "clinton": -15, "biden": -12, "trump": -12, "harris": -10,
    "white house": -8, "congress": -8, "senate": -8, "washington": -6,
    "fbi": -6, "cia": -6, "pentagon": -6, "wall street": -6,
    "nfl": -6, "nba": -6, "mlb": -6, "super bowl": -6,
    "beijing": -6, "china": -6, "moscow": -6, "russia": -6, "putin": -8
}

BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone", "where to watch",
    "fantasy football", "fpl", "opinion:", "comment:", "letters:", "wordle", "crossword"
]

FLUFF_PATTERNS = [
    re.compile(r"^Why\s", re.I),
    re.compile(r"^How\s", re.I),
    re.compile(r"^Here'?s\s", re.I),
    re.compile(r"^\d+\s(ways|things|reasons)", re.I)
]

MAJOR_EVENT_KEYWORDS = ["final", "champion", "trophy", "gold", "won", "wins", "victory", "defeat", "dead", "died"]

FLAIR_CACHE = {}

# =========================
# Section: Compilation & Utilities
# =========================
def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

UK_PATTERNS  = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)

class NewsEntry:
    def __init__(self, source, title, link, summary, published, entry_obj=None):
        self.source    = source
        self.title     = title
        self.link      = link
        self.summary   = summary
        self.published = published
        self.entry_obj = entry_obj

def normalize_url(u):
    if not u: return ""
    p = urllib.parse.urlparse(u)
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))

def normalize_title(t):
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r"[^\w\s£$€]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()

def content_hash(text_blob):
    return hashlib.md5(text_blob.encode('utf-8')).hexdigest()

def generate_ref():
    """Generate a short 7-character reference: 3 letters + 4 digits, e.g. XKP-4821."""
    letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    digits  = ''.join(random.choices(string.digits, k=4))
    return f"{letters}-{digits}"

def load_json_data(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: pass
    return default_val

def save_json_data(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except: pass

def update_metrics(source, category):
    data = load_json_data(METRICS_FILE, {"sources": {}, "categories": {}})
    data["sources"][source]     = data["sources"].get(source, 0) + 1
    data["categories"][category] = data["categories"].get(category, 0) + 1
    save_json_data(METRICS_FILE, data)

# =========================
# Section: Deduplication
# =========================
def load_dedup():
    urls, titles, hashes = set(), set(), set()
    cleaned_lines = []
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    if os.path.exists(DEDUP_FILE):
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4:
                    try:
                        ts = dateparser.parse(parts[0])
                        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
                        if ts > seven_days_ago:
                            urls.add(parts[1])
                            titles.add(parts[2])
                            hashes.add(parts[-1])
                            cleaned_lines.append(line)
                    except: continue
    with open(DEDUP_FILE, 'w', encoding='utf-8') as f:
        f.writelines(cleaned_lines)
    return urls, titles, hashes

POSTED_URLS, POSTED_TITLES, POSTED_HASHES = load_dedup()

def add_to_dedup(entry_obj, title_override=None, url_override=None):
    ts = datetime.now(timezone.utc).isoformat()
    if hasattr(entry_obj, 'link'):
        link, title, summary = entry_obj.link, entry_obj.title, getattr(entry_obj, 'summary', '')
    else:
        link, title, summary = url_override, title_override, ""
    norm_link  = normalize_url(link)
    norm_title = normalize_title(title)
    h = content_hash(title + summary)
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    except: pass
    POSTED_URLS.add(norm_link)
    POSTED_TITLES.add(norm_title)
    POSTED_HASHES.add(h)

# =========================
# Section: Fetching
# =========================
def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=12, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.content, 'html.parser')
        return [p.get_text(" ", strip=True) for p in soup.find_all('p') if len(p.get_text()) > 40]
    except: return []

# =========================
# Section: Analysis
# =========================
def calculate_score(text):
    text_l = text.lower()
    score, pos, neg, matched = 0, 0, 0, {}
    for k, w, pat in UK_PATTERNS:
        count = len(pat.findall(text_l))
        if count:
            score += w * count
            pos   += w * count
            matched[k] = matched.get(k, 0) + count
    for k, w, pat in NEG_PATTERNS:
        count = len(pat.findall(text_l))
        if count:
            score += w * count
            neg   += abs(w) * count
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + count
    return score, pos, neg, matched

def is_hard_reject(text, pos, neg):
    t_l = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in t_l: return True, f"banned: {phrase}"
    for pat in FLUFF_PATTERNS:
        if pat.search(text): return True, "fluff/opinion"
    if neg > max(10, 2.0 * pos): return True, "negative dominance"
    return False, ""

def detect_category(text):
    t_l = text.lower()
    cats = {
        "Politics":      ["parliament", "government", "minister", "mp", "election", "brexit", "labour", "tory"],
        "Economy":       ["economy", "inflation", "budget", "tax", "bank"],
        "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "prison"],
        "Sport":         ["football", "cricket", "match", "cup", "trophy"],
        "Royals":        ["royal", "king", "queen", "palace"],
        "Environment":   ["storm", "weather", "flood", "climate", "met office"],
    }
    scores = {c: sum(1 for k in v if k in t_l) for c, v in cats.items()}
    if all(v == 0 for v in scores.values()): return "Notable International", 0.0
    best = max(scores, key=scores.get)
    return best, 1.0

def get_flair_id(sub, text):
    key = f"{sub.display_name}:{text}"
    if key in FLAIR_CACHE: return FLAIR_CACHE[key]
    try:
        for t in sub.flair.link_templates:
            if t['text'] == text:
                FLAIR_CACHE[key] = t['id']
                return t['id']
    except: pass
    return None

def check_ai_relevance(title, summary, excerpt, entry_hash):
    cache = load_json_data(AI_CACHE_FILE, {})
    if entry_hash in cache and cache[entry_hash].get('timestamp', 0) > (datetime.now(timezone.utc) - timedelta(days=7)).timestamp():
        log("AI", f"Cache hit for hash {entry_hash[:8]}…", Col.DIM)
        return cache[entry_hash]['is_relevant']
    prompt = (
        "Strict UK news filter. Respond YES or NO. "
        "Is this hard news relevant to the UK? (No fluff/sports previews/lifestyle).\n"
        f"Title: {title}\nSummary: {summary}\nExcerpt: {excerpt}"
    )
    try:
        res = client.models.generate_content(model=model_name, contents=prompt).text.strip().lower()
        is_rel = "yes" in res
        log("AI", f"Response: {'YES' if is_rel else 'NO'} — {title[:50]}", Col.MAGENTA)
        cache[entry_hash] = {"is_relevant": is_rel, "timestamp": datetime.now(timezone.utc).timestamp()}
        save_json_data(AI_CACHE_FILE, cache)
        return is_rel
    except:
        return False

def post_article(target_sub, entry, category, score, pos, neg, matched, ai, paras, is_intl=False, post_reason=""):
    flair_label = "Notable International News🌍" if is_intl else category
    flair_id    = get_flair_id(target_sub, flair_label)
    ref         = generate_ref()
    posted_at   = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    try:
        sub = target_sub.submit(title=entry.title, url=entry.link, flair_id=flair_id)

        pos_hits = {k: v for k, v in matched.items() if not k.startswith("NEG:")}
        neg_hits = {k[4:]: v for k, v in matched.items() if k.startswith("NEG:")}

        lines = [
            f"**Source:** {entry.source}  |  **Ref:** `{ref}`  |  *{posted_at}*",
            ""
        ]

        if paras:
            lines.extend([f"> {p}" for p in paras[:3]] + [""])

        if not is_intl:
            k_list = ", ".join(
                f"`{k}`" for k in sorted(pos_hits, key=pos_hits.get, reverse=True)[:5]
            )
            lines += [
                f"**UK Relevance Score:** {score:+d}  "
                f"(UK signal: +{pos} / Non-UK signal: -{neg})",
                f"**Matched keywords:** {k_list}",
                ""
            ]

            if neg_hits:
                neg_list = ", ".join(
                    f"`{k}`" for k in sorted(neg_hits, key=neg_hits.get, reverse=True)[:3]
                )
                lines += [f"**Non-UK signals detected:** {neg_list}", ""]

        lines.append(f"**Why posted:** {post_reason}")
        lines.append("")
        lines.append(
            "This was posted automatically"
            + (" and validated by AI." if ai else ".")
        )

        sub.reply('\n'.join(lines))
        add_to_dedup(entry)
        update_metrics(entry.source, category)

        log("POSTED", f"[{ref}] [{entry.source}] {entry.title[:55]}…", Col.GREEN)
        log("POSTED", f"  Score={score:+d}  Reason: {post_reason}", Col.GREEN)
        return True

    except Exception as e:
        log("ERROR", f"Post failed: {e}", Col.RED)
        return False

# =========================
# Section: Main
# =========================
def main():
    log("START", "=" * 60, Col.CYAN)
    log("START", "  BreakingUKNewsBot v6.4 — Run starting", Col.CYAN)
    log("START", "=" * 60, Col.CYAN)

    feeds = [
        ("BBC",       "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("Sky",       "https://feeds.skynews.com/feeds/rss/home.xml"),
        ("Telegraph", "https://www.telegraph.co.uk/rss.xml")
    ]
    cutoff     = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    raw_entries = []

    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            count = 0
            for e in feed.entries:
                dt = None
                for k in ['published', 'updated']:
                    if hasattr(e, k):
                        try: dt = dateparser.parse(getattr(e, k)); break
                        except: pass
                if dt and (not dt.tzinfo or dt.replace(tzinfo=timezone.utc) > cutoff):
                    raw_entries.append(NewsEntry(source, e.title, e.link, getattr(e, 'summary', ''), dt, e))
                    count += 1
            log("FEED", f"{source}: {count} articles within window", Col.BLUE)
        except Exception as ex:
            log("FEED", f"{source}: fetch failed — {ex}", Col.RED)
            continue

    raw_entries.sort(
        key=lambda x: x.published if x.published else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )
    log("INFO", f"Total articles to evaluate: {len(raw_entries)}", Col.WHITE)

    candidates, posted_titles_this_run = [], set()
    stats = {"duplicate": 0, "in_run_dup": 0, "rejected": 0, "uk": 0, "intl": 0, "ai_checked": 0}

    for entry in raw_entries:
        if len(candidates) >= INITIAL_ARTICLES: break

        norm_link  = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        h          = content_hash(entry.title + entry.summary)

        if norm_link in POSTED_URLS or h in POSTED_HASHES:
            log("SKIP", f"[DUP]      {entry.title[:55]}…", Col.DIM)
            stats["duplicate"] += 1
            continue

        if any(difflib.SequenceMatcher(None, norm_title, t).ratio() > IN_RUN_FUZZY_THRESHOLD
               for t in posted_titles_this_run):
            log("SKIP", f"[IN-RUN-DUP] {entry.title[:55]}…", Col.DIM)
            stats["in_run_dup"] += 1
            continue

        paras     = fetch_article_text(entry.link)
        full_text = entry.title + " " + entry.summary + " " + " ".join(paras)
        score, pos, neg, matched = calculate_score(full_text)

        cat, _        = detect_category(full_text)
        reject, reason = is_hard_reject(full_text, pos, neg)

        target      = "NONE"
        ai_confirmed = False
        post_reason  = ""

        if not reject:
            has_uk_anchor = any(g in full_text.lower() for g in ['uk', 'britain', 'london', 'england'])

            if score >= 15 and has_uk_anchor:
                target      = "UK"
                post_reason = f"High UK score ({score:+d}) with UK anchor term present"

            elif score >= 4:
                stats["ai_checked"] += 1
                if check_ai_relevance(entry.title, entry.summary, " ".join(full_text.split()[:200]), h):
                    target       = "UK"
                    ai_confirmed = True
                    post_reason  = f"Ambiguous score ({score:+d}), confirmed relevant by AI"
                else:
                    target      = "INTL"
                    post_reason = f"Ambiguous score ({score:+d}), AI deemed not UK-specific"

        if target == "NONE":
            if (reject and "negative dominance" in reason) or (score >= 2 or "NEG:" in str(matched)):
                target      = "INTL"
                post_reason = (
                    f"Negative dominance (neg={neg} vs pos={pos}), routed to International"
                    if reject else
                    f"Low UK score ({score:+d}), routed to International"
                )

        log_score_detail(entry.title, score, pos, neg, matched, target, post_reason or (reason if reject else "Low score / no path"))

        if target != "NONE":
            candidates.append({
                "entry": entry, "score": score, "pos": pos, "neg": neg,
                "cat": cat, "matched": matched, "ai": ai_confirmed,
                "target": target, "paras": paras, "post_reason": post_reason
            })
            posted_titles_this_run.add(norm_title)
            if target == "UK":   stats["uk"]   += 1
            if target == "INTL": stats["intl"] += 1
        else:
            log("REJECTED", f"{reason}: {entry.title[:55]}…", Col.RED)
            stats["rejected"] += 1

    # ── Candidate summary ────────────────────────────────────────────────────
    log("INFO", "=" * 60, Col.CYAN)
    log("INFO", f"Candidates found:  UK={stats['uk']}  INTL={stats['intl']}  "
                f"Rejected={stats['rejected']}  Dupes={stats['duplicate']}  "
                f"AI calls={stats['ai_checked']}", Col.CYAN)
    log("INFO", "=" * 60, Col.CYAN)

    # ── Round-robin by source ────────────────────────────────────────────────
    final_list = []
    source_map = {
        s: [c for c in candidates if c['entry'].source == s]
        for s in set(c['entry'].source for c in candidates)
    }
    while len(final_list) < TARGET_POSTS:
        added = False
        for s in list(source_map.keys()):
            if source_map[s]:
                final_list.append(source_map[s].pop(0))
                added = True
                if len(final_list) >= TARGET_POSTS: break
        if not added: break

    log("INFO", f"Posting {len(final_list)} articles (target={TARGET_POSTS})", Col.CYAN)

    posted_count = 0
    for item in final_list:
        sub = subreddit_uk if item['target'] == "UK" else subreddit_intl
        log("POSTING", f"[{item['target']}] [{item['entry'].source}] {item['entry'].title[:50]}…", Col.CYAN)
        log("POSTING", f"  Score={item['score']:+d}  Category={item['cat']}  AI={item['ai']}", Col.CYAN)
        log("POSTING", f"  Reason: {item['post_reason']}", Col.CYAN)

        ok = post_article(
            sub, item['entry'], item['cat'],
            item['score'], item['pos'], item['neg'],
            item['matched'], item['ai'], item['paras'],
            is_intl=(item['target'] == "INTL"),
            post_reason=item['post_reason']
        )
        if ok: posted_count += 1
        time.sleep(5)

    log("DONE", "=" * 60, Col.GREEN)
    log("DONE", f"Run complete. Posted {posted_count}/{len(final_list)} articles.", Col.GREEN)
    log("DONE", "=" * 60, Col.GREEN)

if __name__ == "__main__":
    main()
