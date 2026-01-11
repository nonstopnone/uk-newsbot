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
# =========================
# Section: Global Regex Compilations (Performance)
# =========================
SPORTS_PREVIEW_REGEX = re.compile(r"\b(?:preview|odds|prediction|fight night|upcoming)\b", re.IGNORECASE)
# =========================
# Section: Reddit Setup
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
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDITPASSWORD"],
    user_agent="BreakingUKNewsBot/2.3"
)
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel('gemini-1.5-flash')
subreddit = reddit.subreddit("BreakingUKNews")
# =========================
# Section: Files and Constants
# =========================
DEDUP_FILE = "posted_urls.txt"
RUN_LOG_FILE = "run_log.txt"
DAILY_PREFIX = "posted_urls_"
FUZZY_DUP_THRESHOLD = 0.40
TARGET_POSTS = 7
INITIAL_ARTICLES = 30
# =========================
# Section: UK Keyword Definitions (Full)
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
    "daily mail": 3, "financial times": 4, "independent": 3,
    "labour": 4, "labour party": 4, "conservative": 4, "tory": 4,
    "lib dem": 4, "liberal democrat": 4, "snp": 4,
    "manchester": 4, "birmingham": 4, "leeds": 4, "liverpool": 4,
    "sheffield": 4, "nottingham": 4, "bristol": 4,
    "glasgow": 4, "edinburgh": 4, "dundee": 4, "aberdeen": 4,
    "cardiff": 4, "newport": 4, "swansea": 4,
    "belfast": 4, "derry": 4, "lisburn": 4,
    "brexit": 5, "article 50": 5,
    "ofsted": 3, "dvla": 3, "hmrc": 4, "dwp": 3,
    "heathrow": 4, "gatwick": 4, "stansted": 4, "luton": 4,
    "channel tunnel": 4, "north sea": 4,
    "oxford": 3, "cambridge": 3, "imperial college": 4,
    "university of oxford": 4, "university of cambridge": 4,
    "royal": 4, "monarchy": 4,
    "king charles": 4, "queen camilla": 3,
    "prince william": 4, "princess kate": 4,
    "wimbledon": 4, "premier league": 4,
    "fa cup": 4, "six nations": 4,
    "glastonbury": 4, "edinburgh festival": 4,
    "ukraine uk support": 3, "uk aid": 3,
    "high court": 4, "supreme court uk": 4,
    "local council": 3, "borough council": 3,
    "general election": 5, "by-election": 4,
    "nhs trust": 4, "national health service england": 4,
    "british museum": 3, "tate": 3, "tate modern": 3,
    "british army": 3, "ministry of defence": 4, "moj": 3,
    "hm treasury": 4, "hmrc": 4, "council tax": 3,
    "a-levels": 3, "gcse": 3, "university tuition": 2,
    "level crossing": 2, "network rail": 3, "national rail": 3,
    "tube": 3, "london underground": 3, "heathrow airport": 3,
    "gatwick airport": 3, "nhs england": 4
}
# =========================
# Section: Negative / Foreign-Dominant Keywords (Full)
# =========================
NEGATIVE_KEYWORDS = {
    "clinton": -15, "bill clinton": -15, "hillary clinton": -15,
    "biden": -12, "joe biden": -12,
    "trump": -12, "donald trump": -12,
    "kamala harris": -10,
    "white house": -8, "congress": -8, "senate": -8,
    "washington": -6, "washington dc": -6,
    "california": -6, "texas": -6, "new york": -6,
    "fbi": -6, "cia": -6, "pentagon": -6,
    "supreme court us": -8, "wall street": -6,
    "cnn": -5, "fox news": -5,
    "nfl": -6, "nba": -6, "mlb": -6,
    "eu commission": -4, "european commission": -4,
    "brussels": -4, "germany": -4, "france": -4,
    "beijing": -6, "china": -6, "xi jinping": -8,
    "moscow": -6, "russia": -6, "putin": -8,
    "justin trudeau": -4, "ottawa": -4, "canberra": -4
}
# =========================
# Section: Flair Mapping
# =========================
FLAIR_TEXTS = {
    "Breaking News": "Breaking News",
    "Culture": "Culture",
    "Sport": "Sport",
    "Crime & Legal": "Crime & Legal",
    "Royals": "Royals",
    "Immigration": "Immigration",
    "Politics": "Politics",
    "Economy": "Economy",
    "Notable International": "Notable International News🌍",
    "Trade and Diplomacy": "Trade and Diplomacy"
}
FLAIR_CACHE = {}
# =========================
# Section: Compile Keyword Patterns
# =========================
def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]
UK_PATTERNS = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)
PROMO_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in [
    "deal","discount","voucher","offer","buy","sale","promo","competition","giveaway"]]
OPINION_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in [
    "opinion","comment","editorial","analysis","column","viewpoint","perspective"]]
# =========================
# Section: Utilities
# =========================
def normalize_url(u):
    if not u:
        return ""
    p = urllib.parse.urlparse(u)
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))
def normalize_title(t):
    if not t:
        return ""
    t = html.unescape(t)
    t = re.sub(r"[^\w\s£$€]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()
def content_hash(entry):
    blob = (getattr(entry, 'title', '') + " " + getattr(entry, 'summary', ''))[:700]
    return hashlib.md5(blob.encode('utf-8')).hexdigest()
# =========================
# Section: Deduplication and Daily Tracking
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
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts > seven_days_ago:
                            urls.add(parts[1])
                            titles.add(parts[2])
                            hashes.add(parts[-1])
                            cleaned_lines.append(line)
                    except Exception:
                        continue
    with open(DEDUP_FILE, 'w', encoding='utf-8') as f:
        f.writelines(cleaned_lines)
    return urls, titles, hashes
POSTED_URLS, POSTED_TITLES, POSTED_HASHES = load_dedup()
def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    norm_link = normalize_url(getattr(entry, 'link', ''))
    norm_title = normalize_title(getattr(entry, 'title', ''))
    h = content_hash(entry)
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    except Exception:
        pass
    POSTED_URLS.add(norm_link)
    POSTED_TITLES.add(norm_title)
    POSTED_HASHES.add(h)
# =========================
# Section: Fetching Article Text
# =========================
def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        paras = []
        for p in soup.find_all('p'):
            text = p.get_text(" ", strip=True)
            if len(text) > 40:
                paras.append(text)
        return paras
    except Exception:
        return []
# =========================
# Section: Scoring and Decision Logic
# =========================
def calculate_uk_relevance_score(text):
    text_l = text.lower()
    score = 0
    positive_total = 0
    negative_total = 0
    matched = {}
    for k, w, pat in UK_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c
            positive_total += w * c
            matched[k] = matched.get(k, 0) + c
    for k, w, pat in NEG_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c
            negative_total += abs(w) * c
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + c
    postcodes = re.findall(r"\b([a-z]{1,2}\d{1,2}[a-z]?\s*\d[a-z]{2})\b", text_l)
    if postcodes:
        score += 3 * len(postcodes)
        positive_total += 3 * len(postcodes)
        matched["UK_POSTCODE"] = matched.get("UK_POSTCODE", 0) + len(postcodes)
    return score, positive_total, negative_total, matched
def is_hard_negative_rejection(text, positive_total, negative_total, matched):
    if negative_total > max(6, 1.5 * positive_total):
        return True, "negative_dominance"
    for banned in ["clinton", "bill clinton", "hillary clinton", "biden", "trump"]:
        if re.search(r"\b" + re.escape(banned) + r"\b", text.lower()):
            has_strong_uk = any(term in text.lower() for term in ["uk", "united kingdom", "britain", "london", "parliament", "nhs"])
            if not has_strong_uk:
                return True, f"banned_name:{banned}"
    return False, ""
def compute_confidence(positive_total, negative_total, category_strength=1.0, hybrid=False):
    pos = max(0.0, float(positive_total))
    neg = float(negative_total)
    denom = pos + neg + 1.0
    base = (pos / denom)
    conf = int(30 + base * 68 * category_strength)
    if hybrid:
        conf = max(20, int(conf * 0.7))
    conf = max(10, min(99, conf))
    return conf
# =========================
# Section: Content Heuristics
# =========================
def contains_promotional(text):
    t = text.lower()
    return any(p.search(t) for p in PROMO_PATTERNS)
def contains_opinion(text):
    t = text.lower()
    return any(p.search(t) for p in OPINION_PATTERNS)
def is_sports_preview(text):
    if not text:
        return False
    t = text.lower()
    has_preview_word = SPORTS_PREVIEW_REGEX.search(t) is not None
    has_result_word = any(w in t for w in ["won", "wins", "beat", "defeated", "victory"])
    return has_preview_word and not has_result_word
# =========================
# Section: Categorisation
# =========================
CATEGORY_KEYWORDS = {
    "Politics": ["parliament", "government", "minister", "mp", "prime minister", "election", "brexit"],
    "Economy": ["economy", "chancellor", "bank of england", "inflation", "budget", "sterling"],
    "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "charged"],
    "Sport": ["football", "cricket", "tennis", "match", "premier league", "wimbledon"],
    "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess"],
    "Culture": ["culture", "art", "music", "film", "festival"],
    "Immigration": ["immigration", "asylum", "refugee", "border", "home office"],
    "Trade and Diplomacy": ["trade", "diplomacy", "ambassador", "summit", "treaty"]
}
def detect_category(full_text):
    txt = full_text.lower()
    scores = {}
    keyword_contribs = {}
    for cat, keys in CATEGORY_KEYWORDS.items():
        contrib = {}
        s = 0
        for k in keys:
            c = len(re.findall(r"\b" + re.escape(k) + r"\b", txt))
            if c > 0:
                contrib[k] = c
                s += c
        if s > 0:
            scores[cat] = s
            keyword_contribs[cat] = contrib
    if not scores:
        return "Notable International", 0.0, "no dominant category keywords"
    chosen = max(scores, key=scores.get)
    strength = float(scores[chosen]) / sum(scores.values())
    contrib = keyword_contribs.get(chosen, {})
    if contrib:
        top_k = max(contrib, key=contrib.get)
        top_count = contrib[top_k]
        top_trigger = f"{top_k} ({top_count} {'time' if top_count == 1 else 'times'})"
    else:
        top_trigger = "general category signals"
    return chosen, strength, top_trigger
# =========================
# Section: Flair ID Retrieval and Caching
# =========================
def get_flair_id(flair_text):
    if flair_text in FLAIR_CACHE:
        return FLAIR_CACHE[flair_text]
    try:
        templates = list(subreddit.flair.link_templates)
        for t in templates:
            if t.get('text') == flair_text:
                FLAIR_CACHE[flair_text] = t.get('id')
                return t.get('id')
    except Exception:
        pass
    FLAIR_CACHE[flair_text] = None
    return None
# =========================
# Section: Posting, Replying and Logging
# =========================
def write_run_log(data):
    try:
        with open(RUN_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass
def write_daily_post(data):
    try:
        name = DAILY_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".txt"
        with open(name, 'a', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass
def post_with_flair_and_reply(source, entry, published_dt, score, positive_total, negative_total, matched, category, category_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed=False):
    flair_text = FLAIR_TEXTS.get(category, FLAIR_TEXTS.get('Notable International'))
    flair_id = get_flair_id(flair_text)
    confidence = compute_confidence(positive_total, negative_total, category_strength, hybrid_flag)
    try:
        submission = subreddit.submit(title=getattr(entry, 'title', ''), url=getattr(entry, 'link', '')) if not flair_id else subreddit.submit(title=getattr(entry, 'title', ''), url=getattr(entry, 'link', ''), flair_id=flair_id)
    except Exception as e:
        write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "post_failed", "source": source, "title": getattr(entry, 'title', ''), "url": normalize_url(getattr(entry, 'link', '')), "error": str(e)})
        return False
    lines = []
    if full_paras:
        for para in full_paras[:3]:
            lines.append('> ' + para)
            lines.append('')
    lines.append(f"[Read more]({getattr(entry, 'link', '')})")
    lines.append("")
    if hybrid_flag:
        lines.append("UK–International (hybrid): this article has a meaningful UK connection but is primarily international in focus.")
        lines.append("")
    positive_matches = {k: v for k, v in matched.items() if not str(k).startswith('NEG:')}
    top_positive = sorted(positive_matches.items(), key=lambda x: x[1], reverse=True)[:3]
    if top_positive:
        phrases = []
        for k, v in top_positive:
            times = "time" if v == 1 else "times"
            phrases.append(f"{k} ({v} {times})")
        if len(phrases) == 1:
            keyword_list = phrases[0]
        elif len(phrases) == 2:
            keyword_list = f"{phrases[0]} and {phrases[1]}"
        else:
            keyword_list = f"{', '.join(phrases[:-1])} and {phrases[-1]}"
    else:
        keyword_list = "relevant UK-related terms"
    lines.append("**UK Relevance**")
    lines.append(f"This article was posted because the system detected key UK-related terms such as {keyword_list}, which indicate that it fits the {flair_text} category and is likely of interest to a UK audience.")
    lines.append("")
    lines.append(f"Based on this assessment, the system automatically assigned the {flair_text} flair with {confidence}% confidence.")
    lines.append(f"Triggered by {top_trigger}")
    lines.append("")
    lines.append("This was posted automatically. (For more information about how this works, please see the subreddit wiki)")
    if ai_confirmed:
        lines.append("Relevance confirmed by AI")
    reply_text = '\n'.join(lines)
    try:
        submission.reply(reply_text)
    except Exception:
        pass
    add_to_dedup(entry)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "posted",
        "source": source,
        "title": getattr(entry, 'title', ''),
        "url": normalize_url(getattr(entry, 'link', '')),
        "published": published_dt.isoformat() if published_dt else None,
        "score": score,
        "positive_total": positive_total,
        "negative_total": negative_total,
        "matched": matched,
        "category": category,
        "category_strength": category_strength,
        "hybrid": hybrid_flag,
        "confidence": confidence,
        "flair_text": flair_text,
        "ai_confirmed": ai_confirmed
    }
    write_run_log(record)
    write_daily_post(record)
    return True
# =========================
# Section: Gemini UK Relevance Check
# =========================
def is_uk_relevant_gemini(title, summary, full_paras):
    excerpt = ' '.join(full_paras[:2])[:800]
    prompt = f"""You are a strict UK-news relevance classifier.
Decide whether this article is meaningfully relevant to the United Kingdom.
MEANINGFULLY RELEVANT means:

* The UK is the primary focus, OR

* UK people, institutions, locations, laws, elections, courts, or policies are directly involved, OR

* The story has clear consequences for the UK (political, legal, economic, security, or societal).

NOT RELEVANT means:

* The story is mainly about another country

* The UK is mentioned only in passing, comparison, or quotation

* No direct UK impact or involvement

Output rules:

* Respond with exactly ONE word: Yes or No

* No explanations

* No punctuation

Article content:
Title: {title}
Summary: {summary}
Excerpt: {excerpt}
"""
    try:
        response = model.generate_content(prompt)
        decision = response.text.strip().lower()
        return decision.startswith('yes')
    except Exception as e:
        write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "gemini_error", "error": str(e)})
        return False
# =========================
# Section: Main Orchestration
# =========================
def get_entry_published_datetime(entry):
    for field in ['published', 'updated', 'created', 'date']:
        if hasattr(entry, field):
            try:
                dt = dateparser.parse(getattr(entry, field))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None
def is_duplicate(entry):
    norm_link = normalize_url(getattr(entry, 'link', ''))
    norm_title = normalize_title(getattr(entry, 'title', ''))
    if not norm_link:
        return True, 'missing_url'
    if norm_link in POSTED_URLS:
        return True, 'duplicate_url'
    for pt in POSTED_TITLES:
        if difflib.SequenceMatcher(None, pt, norm_title).ratio() > FUZZY_DUP_THRESHOLD:
            return True, 'duplicate_title'
    if content_hash(entry) in POSTED_HASHES:
        return True, 'duplicate_hash'
    return False, ''
def main():
    feeds = {
        "BBC": "https://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml"
    }
    now = datetime.now(timezone.utc)
    six_hours_ago = now - timedelta(hours=6)
    entries = []
    for name, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published_dt = get_entry_published_datetime(entry)
                if published_dt and six_hours_ago <= published_dt <= now + timedelta(minutes=5):
                    entries.append((name, entry, published_dt))
        except Exception:
            continue
    entries.sort(key=lambda x: x[2], reverse=True)
    candidates = []
    category_counts = Counter()
    for name, entry, published_dt in entries:
        if len(candidates) >= INITIAL_ARTICLES:
            break
        dup, reason = is_duplicate(entry)
        title = getattr(entry, 'title', '')
        summary = getattr(entry, 'summary', '')
        if dup:
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "url": normalize_url(getattr(entry, 'link', '')), "reason": reason})
            continue
        preview_text = title + ' ' + summary
        preview_lower = preview_text.lower()
        if contains_promotional(preview_lower):
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "reason": "promotional"})
            continue
        if contains_opinion(preview_lower):
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "reason": "opinion"})
            continue
        full_paras = fetch_article_text(getattr(entry, 'link', ''))
        if not full_paras:
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "url": normalize_url(getattr(entry, 'link', '')), "reason": "fetch_failed"})
            continue
        article_text = ' '.join(full_paras)
        combined = title + ' ' + summary + ' ' + article_text
        if is_sports_preview(combined):
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "reason": "sports_preview"})
            continue
        score, pos_total, neg_total, matched = calculate_uk_relevance_score(combined)
        hard_reject, hr_reason = is_hard_negative_rejection(combined, pos_total, neg_total, matched)
        if hard_reject:
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "url": normalize_url(getattr(entry, 'link', '')), "reason": hr_reason, "score": score, "pos": pos_total, "neg": neg_total, "matched": matched})
            continue
        category, cat_strength, top_trigger = detect_category(combined)
        category_threshold = 3
        if category == 'Sport':
            category_threshold = 8
        if category == 'Royals':
            category_threshold = 6
        if category == 'Notable International':
            category_threshold = 5
        has_uk_term = any(not str(k).startswith('NEG:') for k in matched)
        hybrid_flag = False
        if score < category_threshold or not has_uk_term:
            if category == 'Notable International' and pos_total >= 4 and neg_total <= pos_total * 1.2:
                hybrid_flag = True
            else:
                write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "url": normalize_url(getattr(entry, 'link', '')), "reason": "low_score_or_no_uk", "score": score, "pos": pos_total, "neg": neg_total})
                continue
        def relevance_level(s, matched):
            has_strong_uk = any(k in matched for k in ["uk", "united kingdom", "britain", "london", "parliament", "nhs"])
            if s >= 10:
                return "Very High"
            elif s >= 7 or has_strong_uk:
                return "High"
            elif s >= 4:
                return "Medium"
            elif s >= 2:
                return "Low"
            else:
                return "Very Low"
        level = relevance_level(score, matched)
        if level in ["Low", "Very Low"]:
            write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "reason": "low_relevance_level", "level": level, "score": score})
            continue
        ai_confirmed = False
        if level == "Medium":
            if is_uk_relevant_gemini(title, summary, full_paras):
                ai_confirmed = True
            else:
                write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "rejected", "source": name, "title": title, "url": normalize_url(getattr(entry, 'link', '')), "reason": "gemini_not_uk", "level": level, "score": score})
                continue
        if category_counts[category] < 3:
            candidates.append((score, name, entry, published_dt, pos_total, neg_total, matched, category, cat_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed))
            category_counts[category] += 1
    unique = []
    seen_links = set()
    seen_titles = set()
    seen_hashes = set()
    for item in candidates:
        score, source, entry, published_dt, pos_total, neg_total, matched, category, cat_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed = item
        link = normalize_url(getattr(entry, 'link', ''))
        ntitle = normalize_title(getattr(entry, 'title', ''))
        h = content_hash(entry)
        dup = False
        if link in seen_links or h in seen_hashes:
            dup = True
        else:
            for st in seen_titles:
                if difflib.SequenceMatcher(None, st, ntitle).ratio() > FUZZY_DUP_THRESHOLD:
                    dup = True
                    break
        if not dup:
            unique.append(item)
            seen_links.add(link)
            seen_titles.add(ntitle)
            seen_hashes.add(h)
    unique.sort(key=lambda x: x[0], reverse=True)
    selected = []
    temp_cat_counts = Counter()
    for item in unique:
        if len(selected) >= TARGET_POSTS:
            break
        score, source, entry, published_dt, pos_total, neg_total, matched, category, cat_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed = item
        if temp_cat_counts[category] < 3:
            selected.append(item)
            temp_cat_counts[category] += 1
    posts = 0
    skipped = 0
    for item in selected:
        score, source, entry, published_dt, pos_total, neg_total, matched, category, cat_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed = item
        try:
            post_success = post_with_flair_and_reply(source, entry, published_dt, score, pos_total, neg_total, matched, category, cat_strength, hybrid_flag, full_paras, top_trigger, ai_confirmed)
            if post_success:
                posts += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
        time.sleep(10)
    write_run_log({"timestamp": datetime.now(timezone.utc).isoformat(), "action": "run_summary", "attempted": len(selected), "posted": posts, "skipped": skipped})
if __name__ == "__main__":
    main()
