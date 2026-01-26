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
# Section: Console Colors & Logging
# =========================
class Col:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'

def log(tag, msg, color=Col.RESET):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    print(f"{color}[{ts}] [{tag}] {msg}{Col.RESET}", flush=True)

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
    user_agent="BreakingUKNewsBot/6.3"
)

try:
    log("SYSTEM", f"Logged in as: {reddit.user.me()}", Col.GREEN)
except Exception as e:
    log("CRITICAL", f"Login failed: {e}", Col.RED)
    sys.exit(1)

model_name = 'gemini-2.5-flash'
subreddit_uk = reddit.subreddit("BreakingUKNews")
subreddit_intl = reddit.subreddit("InternationalBulletin")

# =========================
# Section: Files and Constants
# =========================
DEDUP_FILE = "posted_urls.txt"
RUN_LOG_FILE = "run_log.txt"
AI_CACHE_FILE = "ai_cache.json"
METRICS_FILE = "metrics.json"

DAILY_PREFIX = "posted_urls_"
FUZZY_DUP_THRESHOLD = 0.40 # Historic check
IN_RUN_FUZZY_THRESHOLD = 0.55 # Stricter check for same-day dupes across sources
TARGET_POSTS = 8 
MAX_PER_SOURCE = 3 
INITIAL_ARTICLES = 80
TIME_WINDOW_HOURS = 12

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
    "reform uk": 4, "green party": 3,
    "manchester": 4, "birmingham": 4, "leeds": 4, "liverpool": 4,
    "sheffield": 4, "nottingham": 4, "bristol": 4,
    "glasgow": 4, "edinburgh": 4, "dundee": 4, "aberdeen": 4,
    "cardiff": 4, "newport": 4, "swansea": 4,
    "belfast": 4, "derry": 4,
    "brexit": 5, "ofsted": 3, "dvla": 3, "hmrc": 4, "dwp": 3,
    "heathrow": 4, "gatwick": 4, "stansted": 4,
    "royal": 4, "monarchy": 4, "king charles": 5, "queen camilla": 4,
    "prince william": 5, "princess kate": 5,
    "high court": 4, "supreme court uk": 4,
    "general election": 5, "by-election": 4,
    "british army": 3, "ministry of defence": 4, "moj": 3
}

NEGATIVE_KEYWORDS = {
    "clinton": -15, "bill clinton": -15, "hillary clinton": -15,
    "biden": -12, "joe biden": -12,
    "trump": -12, "donald trump": -12,
    "kamala harris": -10,
    "white house": -8, "congress": -8, "senate": -8,
    "washington": -6, "washington dc": -6,
    "california": -6, "texas": -6, "new york": -6,
    "florida": -6,
    "fbi": -6, "cia": -6, "pentagon": -6,
    "supreme court us": -8, "wall street": -6,
    "cnn": -5, "fox news": -5,
    "nfl": -6, "nba": -6, "mlb": -6,
    "super bowl": -6,
    "eu commission": -4, "european commission": -4,
    "brussels": -4, "germany": -4, "france": -4,
    "beijing": -6, "china": -6, "xi jinping": -8,
    "moscow": -6, "russia": -6, "putin": -8,
    "justin trudeau": -4, "ottawa": -4
}

BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone", "where to watch",
    "fantasy football", "fantasy premier league", "fpl", "dream team",
    "opinion:", "comment:", "analysis:", "view:", "letters:", "reader's view",
    "wordle", "quordle", "crossword", "sudoku", "horoscope"
]

FLUFF_PATTERNS = [
    re.compile(r"^Why\s", re.I),
    re.compile(r"^How\s", re.I),
    re.compile(r"^Here'?s\s", re.I),
    re.compile(r"^\d+\s(ways|things|reasons)", re.I),
    re.compile(r"what\s.*means\sfor\syou", re.I)
]

SPORTS_PREVIEW_REGEX = re.compile(r"\b(?:preview|odds|prediction|fight night|upcoming|vs|line-up|team news)\b", re.IGNORECASE)

MAJOR_EVENT_KEYWORDS = ["final", "semi-final", "champion", "trophy", "gold", "won", "wins", "victory", "defeat", "knockout", "dead", "died", "oscar", "bafta"]

FLAIR_TEXTS = {
    "Breaking News": "Breaking News",
    "Culture": "Culture",
    "Sport": "Sport",
    "Crime & Legal": "Crime & Legal",
    "Royals": "Royals",
    "Immigration": "Immigration",
    "Politics": "Politics",
    "Economy": "Economy",
    "Environment": "Environment",
    "Notable International": "Notable International News🌍",
    "Trade and Diplomacy": "Trade and Diplomacy"
}
FLAIR_CACHE = {}

# =========================
# Section: Compilation & Utilities
# =========================
def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

UK_PATTERNS = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)

PROMO_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in [
    "discount","voucher","sale","promo","competition","giveaway"]]

class NewsEntry:
    def __init__(self, source, title, link, summary, published, entry_obj=None):
        self.source = source
        self.title = title
        self.link = link
        self.summary = summary
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
    data["sources"][source] = data["sources"].get(source, 0) + 1
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
        link = entry_obj.link
        title = entry_obj.title
        summary = getattr(entry_obj, 'summary', '')
    else:
        link = url_override
        title = title_override
        summary = ""

    norm_link = normalize_url(link)
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
# Section: Fetching & Scraping
# =========================
def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.content, 'html.parser')
        paras = []
        for p in soup.find_all('p'):
            text = p.get_text(" ", strip=True)
            if len(text) > 40:
                paras.append(text)
        return paras
    except: return []

def scrape_manual_entry(url):
    try:
        log("MANUAL", f"Scraping {url}...", Col.CYAN)
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.content, 'html.parser')
        
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title: title = og_title.get("content", "")
        elif soup.title: title = soup.title.string
        
        summary = ""
        og_desc = soup.find("meta", property="og:description")
        if og_desc: summary = og_desc.get("content", "")
        
        if not title: return None
        
        return NewsEntry("Manual", title.strip(), url, summary.strip(), datetime.now(timezone.utc))
    except: return None

# =========================
# Section: Analysis Logic
# =========================
def calculate_score(text):
    text_l = text.lower()
    score = 0
    pos = 0
    neg = 0
    matched = {}
    
    for k, w, pat in UK_PATTERNS:
        count = len(pat.findall(text_l))
        if count:
            score += w * count
            pos += w * count
            matched[k] = matched.get(k, 0) + count
            
    for k, w, pat in NEG_PATTERNS:
        count = len(pat.findall(text_l))
        if count:
            score += w * count
            neg += abs(w) * count
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + count
            
    return score, pos, neg, matched

def is_hard_reject(text, pos, neg):
    t_lower = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in t_lower: return True, f"banned: {phrase}"
    for pat in FLUFF_PATTERNS:
        if pat.search(text): return True, "fluff/opinion"
    if neg > max(6, 1.5 * pos): return True, "negative dominance"
    if "fantasy football" in t_lower: return True, "fantasy sport"
    return False, ""

def detect_category(text):
    t_lower = text.lower()
    cats = {
        "Politics": ["parliament", "government", "minister", "mp", "prime minister", "election", "brexit", "reform uk", "labour", "tory"],
        "Economy": ["economy", "chancellor", "bank of england", "inflation", "budget", "sterling", "tax", "fiscal"],
        "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "charged", "prison", "jailed", "sentenced", "stabbed"],
        "Sport": ["football", "cricket", "tennis", "match", "premier league", "wimbledon", "cup", "trophy"],
        "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess", "palace"],
        "Culture": ["culture", "art", "music", "film", "festival", "museum", "tv"],
        "Immigration": ["immigration", "asylum", "refugee", "border", "home office", "migrant"],
        "Environment": ["storm", "weather", "flood", "climate", "met office", "rain", "wind", "snow", "temperature", "alert", "warning"],
        "Trade and Diplomacy": ["trade", "diplomacy", "ambassador", "summit", "treaty"]
    }
    
    scores = {c: 0 for c in cats}
    for cat, keys in cats.items():
        for k in keys:
            if k in t_lower: scores[cat] += 1
            
    if all(v == 0 for v in scores.values()):
        return "Notable International", 0.0, "general"

    # Weather Conflict Check: Prevent weather alerts from being classed as Politics
    if scores["Environment"] > 0 and scores["Politics"] > 0:
         if not any(x in t_lower for x in ["prime minister", "parliament", "election", "legislation"]):
            scores["Politics"] = 0 

    best = max(scores, key=scores.get)
    
    if scores["Crime & Legal"] > 0 and best == "Politics":
        if scores["Crime & Legal"] >= scores["Politics"] - 1:
            return "Breaking News", 1.0, "crime_override"
            
    return best, 1.0, "keyword"

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

# =========================
# Section: AI Check
# =========================
def check_ai_relevance(title, summary, excerpt, entry_hash):
    cache = load_json_data(AI_CACHE_FILE, {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    clean_cache = {k: v for k, v in cache.items() if v.get('timestamp', 0) > cutoff}
    
    if entry_hash in clean_cache:
        res = clean_cache[entry_hash]['is_relevant']
        log("DETAIL", f"AI Cache: {'PASS' if res else 'FAIL'}", Col.BLUE)
        return res

    log("DETAIL", f"Requesting AI Check: {title[:30]}...", Col.YELLOW)
    
    prompt = f"""You are a strict UK news filter.
Task: Determine if this article is hard news relevant to the UK.

Rules:
1. REJECT "Fluff": Opinion pieces, "5 ways to...", "Why X is happening", or lifestyle/travel advice.
2. REJECT Sports/Culture unless it is a major final, tournament win, or death of a legend.
3. REJECT US Politics unless it has direct, stated consequences for the UK.
4. ACCEPT Hard News: Crime, Politics, Economy, Major Accidents, Royal announcements.

Respond ONLY with 'YES' or 'NO'.

Article:
Title: {title}
Summary: {summary}
Excerpt: {excerpt}
"""
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
        text = response.text.strip().lower()
        is_relevant = "yes" in text
        
        clean_cache[entry_hash] = {
            "is_relevant": is_relevant,
            "timestamp": datetime.now(timezone.utc).timestamp()
        }
        save_json_data(AI_CACHE_FILE, clean_cache)
        
        log("DETAIL", f"AI Result: {text.upper()}", Col.GREEN if is_relevant else Col.RED)
        return is_relevant
    except Exception as e:
        log("ERROR", f"AI Failed: {e}", Col.RED)
        return False

# =========================
# Section: Posting Logic
# =========================
def post_article(target_sub, entry, category, score, matched, ai_checked, full_paras, is_intl=False):
    title = entry.title
    url = entry.link
    
    flair_label = category
    if is_intl: flair_label = "Notable International News🌍"
    elif category == "Breaking News": flair_label = "Breaking News"
    
    flair_id = get_flair_id(target_sub, flair_label)
    
    try:
        if flair_id:
            sub = target_sub.submit(title=title, url=url, flair_id=flair_id)
        else:
            sub = target_sub.submit(title=title, url=url)
            
        lines = []
        lines.append(f"**Source:** {entry.source}")
        lines.append("")
        
        if full_paras:
            for para in full_paras[:3]:
                lines.append(f"> {para}")
                lines.append("")
        
        if not is_intl:
            k_list = ", ".join([k for k in matched.keys() if not k.startswith("NEG:")][:5])
            lines.append(f"**UK Relevance Score:** {score}")
            lines.append(f"**Keywords:** {k_list}")
            lines.append("")
            
        if ai_checked:
            lines.append("This was posted automatically and validated by AI.")
        else:
            lines.append("This was posted automatically.")
            
        try: sub.reply('\n'.join(lines))
        except: pass
        
        add_to_dedup(entry)
        update_metrics(entry.source, category)
        return True
        
    except Exception as e:
        log("ERROR", f"Post failed: {e}", Col.RED)
        return False

# =========================
# Section: Main Run
# =========================
def main():
    log("START", "Starting Newsbot Run...", Col.CYAN)
    
    if not os.path.exists(AI_CACHE_FILE): save_json_data(AI_CACHE_FILE, {})
    if not os.path.exists(METRICS_FILE): save_json_data(METRICS_FILE, {"sources":{}, "categories":{}})
    
    feeds = [
        ("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("Sky", "https://feeds.skynews.com/feeds/rss/home.xml"),
        ("Telegraph", "https://www.telegraph.co.uk/rss.xml")
    ]
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    raw_entries = []
    
    manual_url = os.environ.get("MANUAL_URL", "").strip()
    if manual_url:
        m_entry = scrape_manual_entry(manual_url)
        if m_entry: raw_entries.append(m_entry)
        
    for source, url in feeds:
        try:
            log("FETCH", f"Checking {source}...", Col.BLUE)
            feed = feedparser.parse(url)
            for e in feed.entries:
                dt = None
                for k in ['published', 'updated', 'created']:
                    if hasattr(e, k):
                        try: 
                            dt = dateparser.parse(getattr(e, k))
                            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
                            break
                        except: pass
                
                if dt and dt > cutoff:
                    raw_entries.append(NewsEntry(source, e.title, e.link, getattr(e, 'summary', ''), dt, e))
        except: continue
        
    raw_entries.sort(key=lambda x: x.published, reverse=True)
    log("INFO", f"Found {len(raw_entries)} items to process.", Col.RESET)
    
    candidates = []
    posted_titles_this_run = set() 
    
    strong_geo = {'uk', 'united kingdom', 'britain', 'england', 'london', 'scotland', 'wales'}
    
    ai_checks_done = 0
    redirect_count = 0

    for entry in raw_entries:
        if len(candidates) >= INITIAL_ARTICLES: break
        
        norm_link = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        h = content_hash(entry.title + entry.summary)
        
        if norm_link in POSTED_URLS or h in POSTED_HASHES: continue
        
        is_in_run_dupe = False
        for existing_t in posted_titles_this_run:
            if difflib.SequenceMatcher(None, norm_title, existing_t).ratio() > IN_RUN_FUZZY_THRESHOLD:
                is_in_run_dupe = True
                break
        if is_in_run_dupe: continue

        paras = fetch_article_text(entry.link)
        full_text = entry.title + " " + entry.summary + " " + " ".join(paras)
        words = full_text.split()
        excerpt = " ".join(words[:200])
        
        score, pos, neg, matched = calculate_score(full_text)
        reject, reason = is_hard_reject(full_text, pos, neg)
        if reject:
            log("REJECTED", f"Hard Filter ({reason}): {entry.title[:30]}...", Col.RED)
            continue
            
        cat, cat_score, _ = detect_category(full_text)
        
        if cat in ["Sport", "Culture"]:
            if not any(w in full_text.lower() for w in MAJOR_EVENT_KEYWORDS):
                log("REJECTED", f"Minor Sport/Culture: {entry.title[:30]}...", Col.RED)
                continue
                
        is_candidate = False
        ai_confirmed = False
        target = "UK"
        
        threshold = 4
        if cat == 'Sport': threshold = 8
        if cat == 'Royals': threshold = 6
        
        strong_geo_match = any(g in full_text.lower() for g in strong_geo)
        
        if score >= 15 and strong_geo_match and cat != "Royals":
            is_candidate = True 
            log("DETAIL", f"Auto-Pass (High Score + Strong Geo): {entry.title[:40]}...", Col.GREEN)
        elif score >= threshold:
            ai_checks_done += 1
            if check_ai_relevance(entry.title, entry.summary, excerpt, h):
                is_candidate = True
                ai_confirmed = True
            else:
                if score >= 4:
                    target = "INTL"
                    is_candidate = True 
                    redirect_count += 1
                    log("REROUTE", f"Redirecting to Intl: {entry.title[:40]}...", Col.YELLOW)
                else:
                    log("REJECTED", f"AI Veto: {entry.title[:40]}...", Col.RED)
        else:
            log("REJECTED", f"Low Score ({score}): {entry.title[:40]}...", Col.RED)
            
        if is_candidate:
            candidates.append({
                "entry": entry,
                "source": entry.source,
                "score": score,
                "cat": cat,
                "matched": matched,
                "ai": ai_confirmed,
                "target": target,
                "paras": paras
            })
            posted_titles_this_run.add(norm_title) 

    grouped = {}
    for c in candidates:
        s = c['source']
        if s not in grouped: grouped[s] = []
        grouped[s].append(c)
        
    final_list = []
    source_counts = {s: 0 for s in grouped}
    
    added_count = 0
    while added_count < TARGET_POSTS:
        added_this_round = False
        for source in grouped:
            if source_counts[source] < MAX_PER_SOURCE and grouped[source]:
                pick = grouped[source].pop(0) 
                final_list.append(pick)
                source_counts[source] += 1
                added_count += 1
                added_this_round = True
                if added_count >= TARGET_POSTS: break
        if not added_this_round: break
        
    log("INFO", f"Processing {len(final_list)} candidates for UK posting...", Col.CYAN)
    
    count_uk = 0
    count_intl = 0
    
    for item in final_list:
        e = item['entry']
        if item['target'] == "UK":
            log("POSTING", f"Attempting to post UK: {e.title}...", Col.CYAN)
            if post_article(subreddit_uk, e, item['cat'], item['score'], item['matched'], item['ai'], item['paras']):
                log("SUCCESS", f"Posted UK: {e.title}", Col.GREEN)
                count_uk += 1
        elif item['target'] == "INTL":
            log("POSTING", f"Attempting to post Intl: {e.title}...", Col.CYAN)
            if post_article(subreddit_intl, e, item['cat'], 0, {}, False, item['paras'], is_intl=True):
                log("SUCCESS", f"Posted Intl: {e.title}", Col.GREEN)
                count_intl += 1
        
        time.sleep(5)
        
    log("FINISHED", f"Run Complete. UK Posted: {count_uk}. AI Checks: {ai_checks_done} | Redirected: {redirect_count}", Col.GREEN)

if __name__ == "__main__":
    main()
