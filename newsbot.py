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
# Section: Global Setup
# =========================
REQUIRED_ENV = [
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDIT_PASSWORD",
    "GEMINI_API_KEY"
]

def console_log(msg):
    """Prints a timestamped message to stdout."""
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def verbose_log(title, score, pos, neg, matched, status, reason):
    """Prints a readable block for every article checked."""
    print("-" * 60, flush=True)
    print(f"Checking: {title}", flush=True)
    
    # Format matched keywords for readability
    keywords = ", ".join([f"{k}({v})" for k, v in matched.items()])
    if not keywords:
        keywords = "None"
        
    print(f"   Math:   Score {score} (Pos: {pos} | Neg: {neg})", flush=True)
    print(f"   Keys:   {keywords}", flush=True)
    print(f"   Result: {status.upper()} -> {reason}", flush=True)
    print("-" * 60, flush=True)

# Environment Check
for v in REQUIRED_ENV:
    if v not in os.environ:
        sys.exit(f"Missing env var: {v}")

# Gemini & Reddit Setup
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDIT_PASSWORD"],
    user_agent="BreakingUKNewsBot/2.4"
)
subreddit = reddit.subreddit("BreakingUKNews")
model_name = 'gemini-1.5-flash'

# =========================
# Section: Files and Constants
# =========================
DEDUP_FILE = "posted_urls.txt"
RUN_LOG_FILE = "run_log.txt"
DAILY_PREFIX = "posted_urls_"
FUZZY_DUP_THRESHOLD = 0.40
TARGET_POSTS = 10           # Increased target
PROCESS_LIMIT = 100         # Check more articles
TIME_WINDOW_HOURS = 12      # Look back 12 hours instead of 6

# =========================
# Section: Keywords (Same as before, abbreviated for space)
# =========================
UK_KEYWORDS = {
    "uk": 6, "united kingdom": 6, "britain": 6, "england": 5, "scotland": 5, "wales": 5, "northern ireland": 5,
    "london": 5, "westminster": 5, "parliament": 5, "downing street": 5, "starmer": 5, "rishi sunak": 5,
    "prime minister": 5, "home office": 4, "foreign office": 4, "treasury": 4, "chancellor": 4,
    "nhs": 6, "met police": 4, "bbc": 4, "labour": 4, "tory": 4, "conservative": 4, "lib dem": 4,
    "manchester": 4, "birmingham": 4, "liverpool": 4, "glasgow": 4, "edinburgh": 4, "cardiff": 4, "belfast": 4,
    "brexit": 5, "royal": 4, "king charles": 4, "prince william": 4, "princess kate": 4,
    "general election": 5, "ministry of defence": 4, "hmrc": 4, "dvla": 3, "dwp": 3
}

NEGATIVE_KEYWORDS = {
    "clinton": -15, "biden": -12, "trump": -12, "harris": -10,
    "white house": -8, "congress": -8, "senate": -8, "fbi": -6, "cia": -6,
    "nfl": -6, "nba": -6, "mlb": -6, "super bowl": -6,
    "ukraine war": -2, "gaza": -2, "israel": -2, # Mild penalty to force strong UK connection
    "putin": -6, "zelensky": -4, "netanyahu": -4
}

# Regex Compilation
def compile_keywords_dict(d):
    return [(k, w, re.compile(r"\b" + re.escape(k) + r"\b", re.I)) for k, w in d.items()]

UK_PATTERNS = compile_keywords_dict(UK_KEYWORDS)
NEG_PATTERNS = compile_keywords_dict(NEGATIVE_KEYWORDS)
PROMO_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in ["deal","discount","voucher","offer","buy","sale"]]
OPINION_PATTERNS = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in ["opinion","comment","editorial","viewpoint"]]
SPORTS_PREVIEW_REGEX = re.compile(r"\b(?:preview|odds|prediction|fight night|upcoming|vs)\b", re.IGNORECASE)

# =========================
# Section: Core Logic
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

def load_dedup():
    urls, titles, hashes = set(), set(), set()
    if os.path.exists(DEDUP_FILE):
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 2:
                    urls.add(parts[1])
                    if len(parts) >= 3: titles.add(parts[2])
                    if len(parts) >= 4: hashes.add(parts[3])
    return urls, titles, hashes

POSTED_URLS, POSTED_TITLES, POSTED_HASHES = load_dedup()

def add_to_dedup(entry):
    ts = datetime.now(timezone.utc).isoformat()
    norm_link = normalize_url(getattr(entry, 'link', ''))
    norm_title = normalize_title(getattr(entry, 'title', ''))
    h = content_hash(entry)
    with open(DEDUP_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{ts}|{norm_link}|{norm_title}|{h}\n")
    POSTED_URLS.add(norm_link)
    POSTED_TITLES.add(norm_title)
    POSTED_HASHES.add(h)

def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.content, 'html.parser')
        paras = [p.get_text(" ", strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 50]
        return paras
    except:
        return []

def calculate_score(text):
    text_l = text.lower()
    score, pos, neg = 0, 0, 0
    matched = {}
    
    for k, w, pat in UK_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c
            pos += w * c
            matched[k] = matched.get(k, 0) + c
            
    for k, w, pat in NEG_PATTERNS:
        c = len(pat.findall(text_l))
        if c:
            score += w * c
            neg += abs(w) * c # store as positive number for logging
            matched[f"NEG:{k}"] = matched.get(f"NEG:{k}", 0) + c
            
    return score, pos, neg, matched

def is_hard_reject(text, pos, neg):
    # If negatives outweigh positives significantly
    if neg > max(5, 1.5 * pos):
        return True, "Negative Dominance"
    # Immediate ban on US politics without UK context
    for banned in ["clinton", "biden", "trump", "harris"]:
        if banned in text.lower():
            uk_context = any(x in text.lower() for x in ["uk", "britain", "london", "starmer", "sunak"])
            if not uk_context:
                return True, f"Banned Topic: {banned}"
    return False, ""

def detect_category(text):
    cats = {
        "Politics": ["parliament", "government", "minister", "election", "labour", "tory"],
        "Economy": ["economy", "inflation", "budget", "bank of england", "prices", "tax"],
        "Crime": ["police", "court", "arrest", "murder", "stab", "jail"],
        "Sport": ["football", "cricket", "rugby", "league", "cup", "race"],
        "Royals": ["royal", "king", "queen", "prince", "palace"],
        "World": ["ukraine", "gaza", "russia", "china", "usa", "eu"]
    }
    scores = {cat: sum(text.lower().count(k) for k in keys) for cat, keys in cats.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General"

def gemini_check(title, summary):
    console_log(f"AI CHECK: Verifying '{title[:40]}...'")
    prompt = f"Is this news article primarily about the UK or does it have significant UK impact? Answer YES or NO. Title: {title}. Summary: {summary}"
    try:
        resp = client.models.generate_content(model=model_name, contents=prompt)
        res = resp.text.strip().lower().startswith('yes')
        console_log(f"AI RESULT: {'YES' if res else 'NO'}")
        return res
    except Exception as e:
        console_log(f"AI ERROR: {e}")
        return False # Fail safe

# =========================
# Section: Main Run
# =========================
def main():
    console_log("Starting Newsbot Run (Verbose Mode)...")
    
    # 1. Expanded Feed List
    feeds = {
        "BBC": "https://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    }
    
    entries = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=TIME_WINDOW_HOURS)
    
    console_log(f"Fetching feeds (looking back {TIME_WINDOW_HOURS} hours)...")
    
    for name, url in feeds.items():
        try:
            f = feedparser.parse(url)
            count = 0
            for e in f.entries:
                # Date parsing
                dt = None
                for field in ['published', 'updated', 'created']:
                    if hasattr(e, field):
                        try:
                            dt = dateparser.parse(getattr(e, field))
                            if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
                            break
                        except: pass
                
                if dt and dt > cutoff:
                    entries.append((name, e, dt))
                    count += 1
            print(f" -> {name}: Found {count} recent items.")
        except Exception as e:
            print(f" -> {name}: Error {e}")

    console_log(f"Total raw entries found: {len(entries)}")
    
    # Sort by newest first
    entries.sort(key=lambda x: x[2], reverse=True)
    
    candidates = []
    
    # 2. Process Entries with Verbose Logging
    for i, (source, entry, dt) in enumerate(entries):
        if i >= PROCESS_LIMIT: break
        
        title = getattr(entry, 'title', '').strip()
        link = normalize_url(getattr(entry, 'link', ''))
        summary = getattr(entry, 'summary', '')
        
        # Deduplication Check
        if link in POSTED_URLS:
            verbose_log(title, 0, 0, 0, {}, "REJECTED", "Duplicate URL")
            continue
        if normalize_title(title) in POSTED_TITLES:
            verbose_log(title, 0, 0, 0, {}, "REJECTED", "Duplicate Title")
            continue

        # Fetch Content
        paras = fetch_article_text(link)
        if not paras:
            verbose_log(title, 0, 0, 0, {}, "REJECTED", "Could not fetch text")
            continue
            
        full_text = title + " " + summary + " " + " ".join(paras)
        
        # Scoring
        score, pos, neg, matched = calculate_score(full_text)
        
        # Hard Reject Logic
        is_hard, hard_reason = is_hard_reject(full_text, pos, neg)
        if is_hard:
            verbose_log(title, score, pos, neg, matched, "REJECTED", f"HARD NEGATIVE ({hard_reason})")
            continue
            
        # Category Logic
        category = detect_category(full_text)
        
        # Thresholds
        threshold = 4
        if category == "Sport": threshold = 8
        if category == "Royals": threshold = 6
        
        status = "CANDIDATE"
        reason = "Passes Threshold"
        
        if score < threshold:
            status = "REJECTED"
            reason = f"Score {score} < Threshold {threshold}"
        elif any(x in title.lower() for x in ["odds", "prediction", "vs", "live stream"]):
            status = "REJECTED"
            reason = "Sports Preview/Liveblog"
        
        # If it's a candidate but score is borderline, ask AI
        ai_checked = False
        if status == "CANDIDATE" and score < 8:
            is_relevant = gemini_check(title, summary)
            if not is_relevant:
                status = "REJECTED"
                reason = "AI Vetoed Relevance"
            ai_checked = True

        verbose_log(title, score, pos, neg, matched, status, reason)
        
        if status == "CANDIDATE":
            candidates.append({
                "source": source, "entry": entry, "score": score,
                "category": category, "ai_checked": ai_checked
            })

    # 3. Posting Logic
    console_log(f"Processing {len(candidates)} candidates...")
    candidates.sort(key=lambda x: x['score'], reverse=True)
    
    posted_count = 0
    for item in candidates[:TARGET_POSTS]:
        entry = item['entry']
        title = getattr(entry, 'title', '')
        link = getattr(entry, 'link', '')
        
        console_log(f"Posting: {title}")
        try:
            # Post to Reddit
            sub = subreddit.submit(title=title, url=link)
            
            # Reply
            reply = f"**{item['source']}** | Score: {item['score']} | Category: {item['category']}\n\n"
            reply += "This article was automatically curated and posted because it contains strong UK-related signals.\n\n"
            if item['ai_checked']:
                reply += "*Relevance confirmed by AI check.*"
            sub.reply(reply)
            
            add_to_dedup(entry)
            posted_count += 1
            time.sleep(5) # Be nice to API
        except Exception as e:
            console_log(f"Failed to post {title}: {e}")

    console_log(f"Run Complete. Posted {posted_count} articles.")

if __name__ == "__main__":
    main()
