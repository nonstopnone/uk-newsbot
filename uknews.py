import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import random
import urllib.parse
import difflib
import re
import hashlib
import html  # <-- Added for HTML entity decoding

# --- Environment variable check ---
required_env_vars = [
    'REDDIT_CLIENT_ID',
    'REDDIT_CLIENT_SECRET',
    'REDDIT_USERNAME',
    'REDDITPASSWORD'
]
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    print(f"ERROR: Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

# --- Reddit API credentials ---
REDDIT_CLIENT_ID = os.environ['REDDIT_CLIENT_ID']
REDDIT_CLIENT_SECRET = os.environ['REDDIT_CLIENT_SECRET']
REDDIT_USERNAME = os.environ['REDDIT_USERNAME']
REDDIT_PASSWORD = os.environ['REDDITPASSWORD']

reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    username=REDDIT_USERNAME,
    password=REDDIT_PASSWORD,
    user_agent='BreakingUKNewsBot/1.0'
)
subreddit = reddit.subreddit('BreakingUKNews')

# --- Load posted URLs, titles, and content hashes ---
posted_urls = set()
posted_titles = set()
posted_content_hashes = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r', encoding='utf-8') as f:
        posted_urls = set(line.strip() for line in f if line.strip())
if os.path.exists('posted_titles.txt'):
    with open('posted_titles.txt', 'r', encoding='utf-8') as f:
        posted_titles = set(line.strip().lower() for line in f if line.strip())
if os.path.exists('posted_content_hashes.txt'):
    with open('posted_content_hashes.txt', 'r', encoding='utf-8') as f:
        posted_content_hashes = set(line.strip() for line in f if line.strip())

def normalize_url(url):
    parsed = urllib.parse.urlparse(url)
    normalized_path = parsed.path.rstrip('/')
    normalized_url = urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        normalized_path, '', '', ''
    ))
    return normalized_url

def normalize_title(title):
    title = html.unescape(title)  # Decode HTML entities
    # Remove punctuation except numbers and currency symbols (£, $, €)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_content_hash(entry):
    summary = getattr(entry, "summary", "")[:100]
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def is_duplicate(entry, posted_urls, posted_titles, posted_content_hashes, title_threshold=0.85):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)
    
    if norm_link in posted_urls or content_hash in posted_content_hashes:
        return True, "URL or content hash already posted"
    
    for posted_title in posted_titles:
        similarity = difflib.SequenceMatcher(None, norm_title, posted_title).ratio()
        if similarity > title_threshold:
            return True, f"Title too similar to existing post (similarity: {similarity:.2f})"
    
    return False, ""

# --- Major UK news RSS feeds ---
feed_sources = {
    "BBC": "http://feeds.bbci.co.uk/news/uk/rss.xml",
    "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
    "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    "Times": "https://www.thetimes.co.uk/rss",
}

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Keywords for filtering ---
BREAKING_KEYWORDS = [
    "breaking", "urgent", "just in", "developing", "update", "live", "alert"
]
UK_KEYWORDS = [
    "uk", "united kingdom", "britain", "england", "scotland", "wales", "northern ireland",
    "london", "manchester", "birmingham", "glasgow", "cardiff", "belfast"
]
PROMO_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor", "deal", "offer",
    "competition", "prize", "free", "discount"
]
CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "urgent", "alert", "emergency", "crisis", "motorway pile-up", "national security"],
    "Crime & Legal": ["crime", "murder", "arrest", "robbery", "assault", "police", "court", "trial", "judge", "lawsuit", "verdict", "fraud", "manslaughter"],
    "Sport": ["sport", "football", "cricket", "rugby", "tennis", "athletics", "premier league", "championship", "cyclist"],
    "Royals": ["royal", "monarch", "queen", "king", "prince", "princess", "buckingham", "jubilee"],
    "Culture": ["arts", "music", "film", "theatre", "festival", "heritage", "literary", "tv series"],
    "Immigration": ["immigration", "asylum", "migrant", "border", "home office", "channel crossing"],
    "Politics": ["parliament", "election", "government", "policy", "house of commons", "by-election"],
    "Economy": ["economy", "finance", "business", "taxes", "employment", "energy prices", "retail"],
    "Notable International News": ["international", "uk-us", "un climate", "global", "foreign"],
    "Trade and Diplomacy": ["trade", "diplomacy", "eu", "brexit", "uk-eu", "foreign policy"],
    "National Newspapers Front Pages": ["front page", "newspaper", "telegraph", "guardian", "times"]
}

# --- Flair mapping ---
FLAIR_MAPPING = {
    "Breaking News": "Breaking News",
    "Crime & Legal": "Crime & Legal",
    "Sport": "Sport",
    "Royals": "Royals",
    "Culture": "Culture",
    "Immigration": "Immigration",
    "Politics": "Politics",
    "Economy": "Economy",
    "Notable International News": "Notable International News",
    "Trade and Diplomacy": "Trade and Diplomacy",
    "National Newspapers Front Pages": "National Newspapers Front Pages",
    None: "No Flair"
}

# --- Breaking news window (last 2 hours) ---
now_utc = datetime.now(timezone.utc)
two_hours_ago = now_utc - timedelta(hours=2)

def is_recent(entry):
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > two_hours_ago

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMO_KEYWORDS)

def is_uk_relevant(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in UK_KEYWORDS)

def get_category(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None

def extract_first_three_paragraphs(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = []
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 40:
                paragraphs.append(text)
            if len(paragraphs) == 3:
                break
        if paragraphs:
            return '\n\n'.join(paragraphs)
        else:
            return soup.get_text(strip=True)[:500]
    except Exception as e:
        return f"(Could not extract article text: {e})"

def breaking_score(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    score = sum(kw in text for kw in BREAKING_KEYWORDS)
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            score += 1
    return score

# --- Gather recent entries ---
recent_entries = []
for source, feed_url in feed_sources.items():
    try:
        feed = feedparser.parse(feed_url)
        source_entries = [
            entry for entry in feed.entries
            if is_recent(entry) and is_uk_relevant(entry) and not is_promotional(entry)
        ]
        if source_entries:
            random.shuffle(source_entries)
            source_entries.sort(key=breaking_score, reverse=True)
            for entry in source_entries[:2]:
                category = get_category(entry)
                if category:
                    recent_entries.append((source, entry, category))
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

# --- Rank all entries by breaking-ness, then shuffle among equal scores ---
random.shuffle(recent_entries)
recent_entries.sort(key=lambda tup: breaking_score(tup[1]), reverse=True)

# --- Select up to 10 stories, balancing categories and sources ---
selected_entries = []
used_sources = set()
used_categories = {cat: 0 for cat in CATEGORY_KEYWORDS}
for source, entry, category in recent_entries:
    if len(selected_entries) >= 10:
        break
    if used_categories[category] < 2 and source not in used_sources:
        selected_entries.append((source, entry, category))
        used_sources.add(source)
        used_categories[category] += 1

# --- Posting to Reddit with quoted body as comment ---
def post_to_reddit(entry, category):
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)

    # Assign flair
    flair_text = FLAIR_MAPPING.get(category, "No Flair")
    flair_id = None
    # Get flair template id (if flairs are set up as templates)
    for flair in subreddit.flair.link_templates:
        if flair['text'] == flair_text:
            flair_id = flair['id']
            break

    # Decode HTML entities in the title before posting
    clean_title = html.unescape(entry.title)

    # Submit the link post
    submission = subreddit.submit(
        title=clean_title,
        url=entry.link,
        flair_id=flair_id
    )
    print(f"Posted: {submission.shortlink}")

    # Extract and format the body (first 3 paragraphs)
    quoted_body = extract_first_three_paragraphs(entry.link)
    if quoted_body:
        # Format as a blockquote for Reddit
        quoted_lines = [f"> {line}" if line.strip() else "" for line in quoted_body.split('\n')]
        quoted_comment = "\n".join(quoted_lines)
        quoted_comment += f"\n\n[Read more at the source]({entry.link})"
        # Add the required prompt
        quoted_comment += "\n\n---\n\nWhat do you think of this news story? Join the conversation in the comments."
        submission.reply(quoted_comment)
        print("Added quoted body as comment.")

    # Save posted info
    with open('posted_urls.txt', 'a', encoding='utf-8') as f:
        f.write(norm_link + '\n')
    with open('posted_titles.txt', 'a', encoding='utf-8') as f:
        f.write(norm_title + '\n')
    with open('posted_content_hashes.txt', 'a', encoding='utf-8') as f:
        f.write(content_hash + '\n')

# --- Main posting loop ---
for source, entry, category in selected_entries:
    is_dup, reason = is_duplicate(entry, posted_urls, posted_titles, posted_content_hashes)
    if is_dup:
        print(f"Skipping duplicate: {reason}")
        continue
    post_to_reddit(entry, category)
    # Add a delay to avoid rate limits
    time.sleep(30)
