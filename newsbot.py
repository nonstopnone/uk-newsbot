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
    user_agent="BreakingUKNewsBot/7.1"
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
HISTORY_FILE = "history.json" 
AI_CACHE_FILE = "ai_cache.json"
METRICS_FILE = "metrics.json"

FUZZY_DUP_THRESHOLD = 0.40 
IN_RUN_FUZZY_THRESHOLD = 0.55 
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
    "treasury": 4, "bank of england": 4, "chancellor": 4, "keir starmer": 5,
    "nhs": 6, "national health service": 6, "junior doctors": 4,
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
    "prince william": 5, "princess kate": 5, "buckingham palace": 4,
    "high court": 4, "supreme court uk": 4, "crown court": 3,
    "general election": 5, "by-election": 4,
    "british army": 3, "ministry of defence": 4, "moj": 3, "royal navy": 3, "raf": 3
}

NEGATIVE_KEYWORDS = {
    "clinton": -15, "bill clinton": -15, "hillary clinton": -15,
    "biden": -12, "joe biden": -12,
    "trump": -12, "donald trump": -12,
    "kamala harris": -10, "white house": -8, "congress": -8, "senate": -8,
    "washington": -6, "washington dc": -6, "fbi": -6, "cia": -6, "pentagon": -6,
    "supreme court us": -8, "wall street": -6, "cnn": -5, "fox news": -5,
    "nfl": -6, "nba": -6, "mlb": -6, "super bowl": -6,
    "eu commission": -4, "european commission": -4, "brussels": -4,
    "beijing": -6, "china": -6, "xi jinping": -8, "moscow": -6, "russia": -6, "putin": -8
}

BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone", "where to watch",
    "fantasy football", "fantasy premier league", "fpl", "dream team",
    "opinion:", "comment:", "analysis:", "view:", "letters:", "reader's view",
    "wordle", "quordle", "crossword", "sudoku", "horoscope",
    "shopping list", "deal of the day", "amazon prime day"
]

FLUFF_PATTERNS = [
    re.compile(r"^Why\s", re.I),
    re.compile(r"^How\s", re.I),
    re.compile(r"^Here'?s\s", re.I),
    re.compile(r"^\d+\s(ways|things|reasons|places)", re.I),
    re.compile(r"what\s.*means\sfor\syou", re.I),
    re.compile(r"everything\syou\sneed\sto\sknow", re.I)
]

MAJOR_EVENT_KEYWORDS = ["final", "semi-final", "champion", "trophy", "gold", "won", "wins", "victory", "defeat", "knockout", "dead", "died", "oscar", "bafta", "world cup", "euro 20", "olympics"]

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
# Section: Utility Classes
# =========================
class NewsEntry:
    def __init__(self, source, title, link, summary, published, entry_obj=None):
        self.source = source
        self.title = title
        self.link = link
        self.summary = summary
        self.published = published
        self.entry_obj = entry_obj

def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

UK_PATTERNS = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)

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

def save_to_history(entry, target_sub, category, result_status):
    data = load_json_data(HISTORY_FILE, [])
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "title": entry.title,
        "url": entry.link,
        "source": entry.source,
        "subreddit": target_sub.display_name if target_sub else "N/A",
        "category": category,
        "status": result_status
    }
    data.append(record)
    if len(data) > 1000: data = data[-1000:]
    save_json_data(HISTORY_FILE, data)

# =========================
# Section: Analysis Logic
# =========================
def calculate_score(text):
    text_l = text.lower()
    score, pos, neg = 0, 0, 0
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
    return False, ""

def detect_category(text):
    t_lower = text.lower()
    cats = {
        "Politics": ["parliament", "government", "minister", "mp", "prime minister", "election", "reform uk", "labour", "tory", "downing street", "legislation", "whitehall"],
        "Economy": ["economy", "chancellor", "bank of england", "inflation", "budget", "sterling", "tax", "fiscal", "gdp", "recession", "mortgage", "pension"],
        "Crime & Legal": ["police", "court", "trial", "arrest", "murder", "charged", "prison", "jailed", "sentenced", "stabbed", "crime", "judge", "jury", "manslaughter"],
        "Sport": ["football", "cricket", "tennis", "match", "premier league", "wimbledon", "cup", "trophy", "rugby", "f1", "lewis hamilton", "harry kane"],
        "Royals": ["royal", "monarchy", "king", "queen", "prince", "princess", "palace", "buckingham", "windsor", "harry", "meghan"],
        "Culture": ["culture", "art", "music", "film", "festival", "museum", "tv", "concert", "glastonbury", "oasis", "theatre", "actor", "author"],
        "Immigration": ["immigration", "asylum", "refugee", "border", "home office", "migrant", "channel crossing", "rwanda", "deportation"],
        "Environment": ["storm", "weather", "flood", "climate", "met office", "rain", "wind", "snow", "temperature", "alert", "warning", "sewage", "water"],
        "Trade and Diplomacy": ["trade", "diplomacy", "ambassador", "summit", "treaty", "foreign policy", "sanctions"]
    }
    
    scores = {c: 0 for c in cats}
    trigger_words = {c: [] for c in cats}
    for cat, keys in cats.items():
        for k in keys:
            if k in t_lower: 
                scores[cat] += 1
                trigger_words[cat].append(k)
            
    if all(v == 0 for v in scores.values()):
        return "Breaking News", 0.0, "general"

    # Fix Weather/Storm misclassification as Politics
    if scores["Environment"] > 0 and scores["Politics"] > 0:
        if not any(x in t_lower for x in ["prime minister", "parliament", "election", "legislation"]):
            scores["Politics"] = 0 

    best = max(scores, key=scores.get)
    if scores["Crime & Legal"] > 0 and best == "Politics":
        if scores["Crime & Legal"] >= scores["Politics"] - 1:
            best = "Crime & Legal"
    
    triggers = trigger_words[best]
    best_trigger = Counter(triggers).most_common(1)[0][0] if triggers else "keyword"
    return best, 1.0, best_trigger

def check_ai_relevance(title, summary, excerpt, entry_hash):
    cache = load_json_data(AI_CACHE_FILE, {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    clean_cache = {k: v for k, v in cache.items() if v.get('timestamp', 0) > cutoff}
    if entry_hash in clean_cache: return clean_cache[entry_hash]['classification']

    prompt = f"""You are a strict news editor. Classify this article:
    1. UK_NEWS: Vital hard news for a UK audience (Politics, Economy, Crime, Major Social issues).
    2. GLOBAL_MAJOR: World-changing events (War, major international disasters, globally relevant results).
    3. DISCARD: Local news, minor royals, weather warnings (unless severe), minor sport, or fluff.
    
    Respond ONLY: UK_NEWS, GLOBAL_MAJOR, or DISCARD.
    
    Title: {title}
    Summary: {summary}
    Excerpt: {excerpt}"""
    
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
        res = response.text.strip().upper()
        classification = "DISCARD"
        if "UK_NEWS" in res: classification = "UK_NEWS"
        elif "GLOBAL_MAJOR" in res: classification = "GLOBAL_MAJOR"
        
        clean_cache[entry_hash] = {"classification": classification, "timestamp": datetime.now(timezone.utc).timestamp()}
        save_json_data(AI_CACHE_FILE, clean_cache)
        return classification
    except: return "DISCARD"

# =========================
# Section: Posting & Execution
# =========================
def post_article(target_sub, entry, category, score, matched, ai_checked, full_paras, trigger_word, is_intl=False):
    flair_label = category
    if is_intl: flair_label = "Notable International News🌍"
    flair_id = get_flair_id(target_sub, flair_label)
    
    try:
        sub = target_sub.submit(title=entry.title, url=entry.link, flair_id=flair_id) if flair_id else target_sub.submit(title=entry.title, url=entry.link)
        lines = [f"**Source:** {entry.source}", ""]
        if full_paras:
            for para in full_paras[:3]: lines.append(f"> {para}"); lines.append("")
        if not is_intl:
            lines.append(f"**Category:** {category} (Trigger detected: *'{trigger_word}'*)")
            lines.append(f"**Relevance Score:** {score}")
            lines.append("")
        lines.append("This was posted automatically and validated by AI." if ai_checked else "This was posted automatically.")
        try: sub.reply('\n'.join(lines))
        except: pass
        
        add_to_dedup(entry)
        update_metrics(entry.source, category)
        save_to_history(entry, target_sub, category, "posted")
        return True
    except: return False

def get_flair_id(sub, text):
    key = f"{sub.display_name}:{text}"
    if key in FLAIR_CACHE: return FLAIR_CACHE[key]
    try:
        for t in sub.flair.link_templates:
            if t['text'] == text: FLAIR_CACHE[key] = t['id']; return t['id']
    except: pass
    return None

def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.content, 'html.parser')
        return [p.get_text(" ", strip=True) for p in soup.find_all('p') if len(p.get_text()) > 40]
    except: return []

def load_dedup():
    urls, titles, hashes = set(), set(), set()
    cleaned = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    if os.path.exists(DEDUP_FILE):
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                p = line.strip().split('|')
                if len(p) >= 4:
                    try:
                        ts = dateparser.parse(p[0])
                        if (ts.replace(tzinfo=timezone.utc) if not ts.tzinfo else ts) > cutoff:
                            urls.add(p[1]); titles.add(p[2]); hashes.add(p[-1]); cleaned.append(line)
                    except: continue
    with open(DEDUP_FILE, 'w', encoding='utf-8') as f: f.writelines(cleaned)
    return urls, titles, hashes

POSTED_URLS, POSTED_TITLES, POSTED_HASHES = load_dedup()

def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    h = content_hash(entry.title + entry.summary)
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f: f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    except: pass
    POSTED_URLS.add(norm_link); POSTED_TITLES.add(norm_title); POSTED_HASHES.add(h)

def main():
    log("START", "Newsbot Run 7.1...", Col.CYAN)
    if not os.path.exists(HISTORY_FILE): save_json_data(HISTORY_FILE, [])
    
    feeds = [("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml"), ("Sky", "https://feeds.skynews.com/feeds/rss/home.xml"), ("Telegraph", "https://www.telegraph.co.uk/rss.xml")]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW_HOURS)
    raw_entries = []
    
    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                dt = None
                for k in ['published', 'updated', 'created']:
                    if hasattr(e, k):
                        try: dt = dateparser.parse(getattr(e, k)); dt = dt.replace(tzinfo=timezone.utc) if not dt.tzinfo else dt; break
                        except: pass
                if dt and dt > cutoff: raw_entries.append(NewsEntry(source, e.title, e.link, getattr(e, 'summary', ''), dt, e))
        except: continue
        
    raw_entries.sort(key=lambda x: x.published, reverse=True)
    candidates, in_run_titles = [], set()

    for entry in raw_entries:
        if len(candidates) >= INITIAL_ARTICLES: break
        norm_l, norm_t = normalize_url(entry.link), normalize_title(entry.title)
        h = content_hash(entry.title + entry.summary)
        
        if norm_l in POSTED_URLS or h in POSTED_HASHES: continue
        if any(difflib.SequenceMatcher(None, norm_t, et).ratio() > IN_RUN_FUZZY_THRESHOLD for et in in_run_titles): continue

        paras = fetch_article_text(entry.link)
        full_text = f"{entry.title} {entry.summary} {' '.join(paras)}"
        excerpt = " ".join(full_text.split()[:200])
        
        score, pos, neg, matched = calculate_score(full_text)
        reject, reason = is_hard_reject(full_text, pos, neg)
        if reject: continue
            
        cat, _, trigger = detect_category(full_text)
        if cat in ["Sport", "Culture"] and not any(w in full_text.lower() for w in MAJOR_EVENT_KEYWORDS): continue
        
        ai_res = check_ai_relevance(entry.title, entry.summary, excerpt, h)
        if ai_res in ["UK_NEWS", "GLOBAL_MAJOR"]:
            candidates.append({
                "entry": entry, "score": score, "cat": cat, "matched": matched, 
                "ai": True, "target": "UK" if ai_res == "UK_NEWS" else "INTL", 
                "paras": paras, "trigger": trigger
            })
            in_run_titles.add(norm_t)

    # Source balancing
    grouped = {}
    for c in candidates:
        grouped.setdefault(c['entry'].source, []).append(c)
    
    final = []
    source_counts = {s: 0 for s in grouped}
    while len(final) < TARGET_POSTS:
        added = False
        for s in grouped:
            if source_counts[s] < MAX_PER_SOURCE and grouped[s]:
                final.append(grouped[s].pop(0)); source_counts[s] += 1; added = True
                if len(final) >= TARGET_POSTS: break
        if not added: break
        
    for item in final:
        target_sub = subreddit_uk if item['target'] == "UK" else subreddit_intl
        log("POSTING", f"To {target_sub.display_name}: {item['entry'].title}", Col.CYAN)
        post_article(target_sub, item['entry'], item['cat'], item['score'], item['matched'], item['ai'], item['paras'], item['trigger'], is_intl=(item['target']=="INTL"))
        time.sleep(5)
    log("FINISHED", f"Run Complete. Posted: {len(final)}", Col.GREEN)

if __name__ == "__main__": main()
