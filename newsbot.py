import feedparser
import requests
import praw
import os
import sys
import re
import time
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from google import genai

# ==========================================
# ⚙️ Configuration & Secrets
# ==========================================
# Ensure these are set in your GitHub Repo Secrets
REQUIRED_VARS = [
    "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", 
    "REDDIT_USERNAME", "REDDITPASSWORD", 
    "GEMINI_API_KEY"
]

for var in REQUIRED_VARS:
    if not os.environ.get(var):
        print(f"[CRITICAL] Missing environment variable: {var}")
        sys.exit(1)

# Initialize APIs
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDIT_PASSWORD"],
    user_agent="UKNewsBot/v4.0 (Auth Verified)"
)

# Authentication Verification
try:
    print(f"[SYSTEM] Logged in as: {reddit.user.me()}")
except Exception as e:
    print(f"[CRITICAL] LOGIN FAILED: {e}. Check your username/password secrets.")
    sys.exit(1)

subreddit = reddit.subreddit("BreakingUKNews")
DEDUP_FILE = "posted_urls.txt"
TIME_WINDOW = 12 # Hours

# ==========================================
# 🇬🇧 Categorization & Keywords
# ==========================================
# Exact names from your list
FLAIR_MAP = {
    "Breaking News": ["breaking", "urgent", "just in", "developing story"],
    "Culture": ["art", "music", "film", "movie", "theatre", "museum", "festival", "celebrity", "tv show"],
    "Sport": ["football", "cricket", "rugby", "league", "cup", "match", "premier league", "tennis", "olympics"],
    "Crime & Legal": ["police", "court", "arrest", "murder", "sentence", "jail", "scotland yard", "met police", "lawyer"],
    "Royals": ["royal", "king charles", "buckingham palace", "prince william", "princess kate", "meghan", "harry"],
    "Immigration": ["asylum", "migrant", "small boats", "borders", "home office", "visa", "deportation"],
    "Politics": ["parliament", "starmer", "westminster", "election", "mp", "minister", "downing street", "labour", "tory"],
    "Economy": ["inflation", "tax", "budget", "bank of england", "interest rates", "growth", "recession", "pounds", "sterling"],
    "Notable International News🌍": ["international", "world news", "overseas", "foreign policy", "global"]
}

UK_WEIGHTS = {
    "uk": 6, "united kingdom": 6, "britain": 6, "london": 4, "england": 4, "scotland": 4, "wales": 4, "belfast": 4, "nhs": 5
}

# Negative signals to catch things like the "Smartphone not in UK" review
BANNED_PHRASES = [
    "not coming to the uk", "isn't coming to the uk", "won't be available in the uk",
    "i tried the", "review:", "hands-on with", "best smartphone"
]

# ==========================================
# 🛠️ Helper Functions
# ==========================================
def load_seen():
    if not os.path.exists(DEDUP_FILE): return set()
    with open(DEDUP_FILE, "r") as f: return set(line.strip() for line in f)

def save_seen(url):
    with open(DEDUP_FILE, "a") as f: f.write(url + "\n")

def get_flair_id(name):
    try:
        for f in subreddit.flair.link_templates:
            if f['text'] == name: return f['id']
    except: return None
    return None

def detect_category(text):
    text = text.lower()
    scores = {cat: 0 for cat in FLAIR_MAP}
    for cat, keywords in FLAIR_MAP.items():
        for k in keywords:
            if k in text: scores[cat] += 1
    
    best_cat = max(scores, key=scores.get)
    if scores[best_cat] == 0:
        return "Notable International News🌍" if any(x in text for x in ["world", "global", "foreign"]) else "Breaking News"
    return best_cat

def gemini_check(title, summary):
    prompt = f"Is this article primarily about the UK or does it have a direct impact on the UK? Answer YES or NO.\nTitle: {title}\nSummary: {summary}"
    try:
        response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
        return "YES" in response.text.upper()
    except: return False

# ==========================================
# 🚀 Execution
# ==========================================
def run():
    print(f"[START] Starting Run at {datetime.now()}")
    seen_urls = load_seen()
    feeds = [
     "BBC": "https://feeds.bbci.co.uk/news/uk/rss.xml",
        "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
        "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    ]
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TIME_WINDOW)
    
    for url in feeds:
        feed = feedparser.parse(url)
        print(f"[FETCH] Checking feed: {url}")
        
        for entry in feed.entries[:20]:
            title = entry.title
            link = entry.link
            summary = getattr(entry, 'summary', '')

            # 1. Deduplication
            if link in seen_urls: continue

            # 2. Hard Filter (Smartphone review fix)
            if any(p in title.lower() for p in BANNED_PHRASES):
                print(f"[REJECTED] Banned phrase/Review: {title[:50]}...")
                continue

            # 3. UK Relevance Weighting
            uk_score = sum(weight for word, weight in UK_WEIGHTS.items() if word in (title + summary).lower())
            
            # 4. AI Verification
            print(f"[AI-CHECK] Verifying: {title[:50]}...")
            if uk_score < 4 and not gemini_check(title, summary):
                print(f"[REJECTED] No UK relevance: {title[:50]}...")
                continue

            # 5. Categorize & Post
            category = detect_category(title + " " + summary)
            flair_id = get_flair_id(category)
            
            try:
                print(f"[POSTED] {category} -> {title[:60]}")
                post = subreddit.submit(title=title, url=link, flair_id=flair_id)
                post.reply(f"**Category**: {category}\n\n*This article was automatically curated for r/BreakingUKNews*")
                save_seen(link)
                time.sleep(5) # Rate limiting
            except Exception as e:
                print(f"[ERROR] Failed to post: {e}")

    print("[FINISHED] Run complete.")

if __name__ == "__main__":
    run()
