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

# Initialize Gemini
try:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
except Exception as e:
    log("ERROR", f"Failed to init Gemini: {e}", Col.RED)
    sys.exit(1)

# Initialize Reddit
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDITPASSWORD"],
    user_agent="RoyalNewsBot/1.0"
)

# Verify Auth
try:
    log("SYSTEM", f"Logged in as: {reddit.user.me()}", Col.GREEN)
except Exception as e:
    log("CRITICAL", f"Login failed: {e}", Col.RED)
    sys.exit(1)

model_name = 'gemini-2.5-flash'
subreddit = reddit.subreddit("UKRoyalNews")

# =========================
# Section: Files and Constants
# =========================
DEDUP_FILE = "posted_urls_royal.txt" # Separate dedup file
AI_CACHE_FILE = "ai_cache_royal.json" # Separate cache
METRICS_FILE = "metrics_royal.json"

TARGET_POSTS = 5
INITIAL_ARTICLES = 60
TIME_WINDOW_HOURS = 24 # Royals news moves slower, look back further

# =========================
# Section: Keyword Definitions (Strictly Royal)
# =========================
ROYAL_KEYWORDS = {
    "king charles": 10, "queen camilla": 10, 
    "prince william": 10, "princess of wales": 10, "kate middleton": 10,
    "prince harry": 10, "duke of sussex": 10, "duchess of sussex": 10,
    "buckingham palace": 8, "kensington palace": 8, "windsor castle": 8,
    "royal family": 6, "british monarchy": 6,
    "prince george": 8, "princess charlotte": 8, "prince louis": 8,
    "sandringham": 5, "balmoral": 5,
    "princess anne": 8, "prince edward": 8, "duke of edinburgh": 8
}

# Negative keywords to filter out non-UK royals and historical/irrelevant topics
NEGATIVE_KEYWORDS = {
    "spanish": -20, "spain": -10, "letizia": -20,
    "dutch": -20, "netherlands": -10, "maxima": -20,
    "danish": -20, "denmark": -10, "frederik": -20,
    "monaco": -20, "albert": -10,
    "saudi": -20, "arabia": -10,
    "ancient": -15, "archaeology": -15, "excavation": -15, "skeleton": -15,
    "tomb": -15, "burial site": -15, "medieval": -10, "viking": -15, "roman": -15,
    "netflix": -5, "the crown": -5, # Filter out TV show reviews unless specific
    "burger king": -20, "prince" : -2, # Generic prince penalty to force specific name match
    "review": -10
}

BANNED_PHRASES = [
    "princely burial", "ancient tomb", "found dead", "body found", 
    "game of thrones", "house of the dragon", "disney princess",
    "prince of persia", "fresh prince", "harry potter", "harry kane", "harry styles"
]

# =========================
# Section: Compile Keyword Patterns
# =========================
def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

ROYAL_PATTERNS = compile_keywords_dict(ROYAL_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)

# =========================
# Section: Utilities
# =========================
def normalize_url(u):
    if not u: return ""
    p = urllib.parse.urlparse(u)
    return urllib.parse.urlunparse((p.scheme, p.netloc, p.path.rstrip('/'), '', '', ''))

def normalize_title(t):
    if not t: return ""
    t = html.unescape(t)
    t = re.sub(r"[^\w\s£$€]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()

def content_hash(entry):
    blob = (getattr(entry, 'title', '') + " " + getattr(entry, 'summary', ''))[:700]
    return hashlib.md5(blob.encode('utf-8')).hexdigest()

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

def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    norm_link = normalize_url(getattr(entry, 'link', ''))
    norm_title = normalize_title(getattr(entry, 'title', ''))
    h = content_hash(entry)
    try:
        with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    except: pass
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
    except:
        return []

# =========================
# Section: Scoring
# =========================
def calculate_royal_score(text):
    text_l = text.lower()
    score = 0
    matched = {}
    
    for k, w, pat in ROYAL_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c
            matched[k] = matched.get(k, 0) + c
            
    for k, w, pat in NEG_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c # w is negative
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + c
            
    return score, matched

def is_hard_reject(text):
    text_l = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in text_l:
            return True, f"banned_phrase:{phrase}"
    
    # Historical filter: If "archaeology" words appear without "king charles" or "william" etc.
    if any(x in text_l for x in ["ancient", "tomb", "burial", "skeleton"]):
        if not any(x in text_l for x in ["king charles", "prince william", "princess of wales", "buckingham palace"]):
            return True, "historical_noise"

    return False, ""

# =========================
# Section: Gemini AI Check (Specialized)
# =========================
def is_royal_relevant_gemini(title, summary, excerpt_200, entry_hash):
    cache = load_json_data(AI_CACHE_FILE, {})
    
    # Prune old cache
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    clean_cache = {k: v for k, v in cache.items() if v.get('timestamp', 0) > cutoff}
    
    if entry_hash in clean_cache:
        cached_result = clean_cache[entry_hash]['is_relevant']
        log("DETAIL", f"AI Cache Hit: {'RELEVANT' if cached_result else 'IRRELEVANT'}", Col.BLUE)
        return cached_result

    log("DETAIL", f"Requesting Royal AI check for: {title[:40]}...", Col.YELLOW)
    
    prompt = f"""You are a strict filter for the r/UKRoyalNews subreddit.
Task: Determine if this article is about the CURRENT British Royal Family (Windsors).

Rules for YES (Relevant):
1. Primarily about King Charles, Camilla, William, Kate, or their children.
2. Official Palace announcements or significant events involving the monarchy.

Rules for NO (Irrelevant):
1. Articles about "Prince" the musician, Harry Potter, Harry Styles, etc.
2. Historical articles about ancient kings (Richard III, Henry VIII) unless linked to current events.
3. Archaeology stories (e.g., "Princely burial site found in Germany").
4. Stories about foreign royals (Spanish, Dutch, etc.) unless visiting the UK.
5. TV Show reviews (e.g The Crown)

Output: Respond ONLY with 'YES' or 'NO'.

Article:
Title: {title}
Summary: {summary}
Excerpt: {excerpt_200}
"""
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
        decision = response.text.strip().lower()
        is_relevant = decision.startswith('yes')
        
        clean_cache[entry_hash] = {
            "is_relevant": is_relevant,
            "timestamp": datetime.now(timezone.utc).timestamp()
        }
        save_json_data(AI_CACHE_FILE, clean_cache)

        if is_relevant:
            log("DETAIL", f"AI Result: RELEVANT ({decision})", Col.GREEN)
        else:
            log("DETAIL", f"AI Result: IRRELEVANT ({decision})", Col.RED)
            
        return is_relevant
    except Exception as e:
        log("ERROR", f"AI Error: {e}", Col.RED)
        return False

# =========================
# Section: Orchestration
# =========================
def is_duplicate(entry):
    norm_link = normalize_url(getattr(entry, 'link', ''))
    if not norm_link: return True
    if norm_link in POSTED_URLS: return True
    if content_hash(entry) in POSTED_HASHES: return True
    return False

def post_to_reddit(source, entry, score, matched, ai_confirmed):
    title = getattr(entry, 'title', '')
    url = getattr(entry, 'link', '')

    try:
        log("POSTING", f"Attempting to post: {title[:50]}...", Col.CYAN)
        submission = subreddit.submit(title=title, url=url)
    except Exception as e:
        log("ERROR", f"Post failed: {e}", Col.RED)
        return False

    # Simple Comment
    lines = []
    lines.append(f"**Source:** {source}")
    lines.append(f"[Read more]({url})")
    lines.append("")
    if ai_confirmed:
        lines.append("*Relevance confirmed by AI analysis.*")
    
    try:
        submission.reply('\n'.join(lines))
    except: pass
    
    add_to_dedup(entry)
    log("SUCCESS", f"Posted: {title[:50]}...", Col.GREEN)
    return True

def main():
    log("START", "Starting Royal Newsbot Run...", Col.CYAN)
    
    feeds = [
        ("BBC", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
        ("Sky", "https://feeds.skynews.com/feeds/rss/home.xml"),
        ("Telegraph", "https://www.telegraph.co.uk/rss.xml"),
    ]
    
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=TIME_WINDOW_HOURS)
    entries = []
    
    for name, url in feeds:
        try:
            log("FETCH", f"Checking {name}...", Col.BLUE)
            feed = feedparser.parse(url)
            for entry in feed.entries:
                # Date check
                dt = None
                for field in ['published', 'updated', 'created', 'date']:
                    if hasattr(entry, field):
                        try:
                            dt = dateparser.parse(getattr(entry, field))
                            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
                            break
                        except: pass
                
                if dt and dt > cutoff:
                    entries.append((name, entry, dt))
        except: continue
        
    entries.sort(key=lambda x: x[2], reverse=True)
    log("INFO", f"Found {len(entries)} recent articles.", Col.RESET)
    
    candidates = []
    ai_check_count = 0
    
    for name, entry, dt in entries:
        if len(candidates) >= INITIAL_ARTICLES: break
        
        if is_duplicate(entry): continue
        
        title = getattr(entry, 'title', '')
        summary = getattr(entry, 'summary', '')
        h = content_hash(entry)
        
        full_paras = fetch_article_text(getattr(entry, 'link', ''))
        article_text = ' '.join(full_paras)
        combined = title + ' ' + summary + ' ' + article_text
        
        # 200 word excerpt
        combined_words = (title + " " + summary + " " + article_text).split()
        excerpt_200 = " ".join(combined_words[:200])

        # Filters
        reject, reason = is_hard_reject(combined)
        if reject:
            log("REJECTED", f"Hard Filter ({reason}): {title[:40]}...", Col.RED)
            continue
            
        score, matched = calculate_royal_score(combined)
        
        # Thresholds
        is_candidate = False
        ai_confirmed = False
        
        # Must have positive royal score
        if score >= 10:
            # Check for generic "Harry" without "Prince" or "Styles"
            if "harry" in combined.lower() and "prince" not in combined.lower() and "duke" not in combined.lower():
                 # Risky (Harry Kane, Harry Styles), Force AI
                 pass
            elif score >= 25:
                # Very high confidence (full names used)
                is_candidate = True
                log("DETAIL", f"High Score ({score}): {title[:40]}...", Col.GREEN)
            else:
                # Moderate score, verify context
                ai_check_count += 1
                if is_royal_relevant_gemini(title, summary, excerpt_200, h):
                    is_candidate = True
                    ai_confirmed = True
                else:
                    log("REJECTED", f"AI Veto: {title[:40]}...", Col.RED)
                    add_to_dedup(entry) # Dedup rejected
        else:
            log("REJECTED", f"Low Score ({score}): {title[:40]}...", Col.RED)
            
        if is_candidate:
            candidates.append((name, entry, score, matched, ai_confirmed))
            
    log("INFO", f"Processing {len(candidates)} candidates for posting...", Col.CYAN)
    posted = 0
    
    for item in candidates:
        if posted >= TARGET_POSTS: break
        if post_to_reddit(*item):
            posted += 1
            time.sleep(10)
            
    log("FINISHED", f"Run Complete. Posted: {posted}. AI Checks: {ai_check_count}", Col.GREEN)

if __name__ == "__main__":
    main()
