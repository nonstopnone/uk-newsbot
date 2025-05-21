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
import html

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

# --- Load posted URLs, titles, content hashes, and timestamps ---
posted_urls = {}
posted_titles = {}
posted_content_hashes = {}
if os.path.exists('posted_timestamps.txt'):
    with open('posted_timestamps.txt', 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                timestamp, url, title, content_hash = line.strip().split('|')
                post_time = datetime.fromisoformat(timestamp)
                posted_urls[url] = post_time
                posted_titles[title] = post_time
                posted_content_hashes[content_hash] = post_time

# Remove entries older than 7 days
now_utc = datetime.now(timezone.utc)
seven_days_ago = now_utc - timedelta(days=7)
posted_urls = {k: v for k, v in posted_urls.items() if v > seven_days_ago}
posted_titles = {k: v for k, v in posted_titles.items() if v > seven_days_ago}
posted_content_hashes = {k: v for k, v in posted_content_hashes.items() if v > seven_days_ago}

def save_duplicate_files():
    try:
        with open('posted_urls.txt', 'w', encoding='utf-8') as f:
            for url in posted_urls:
                f.write(url + '\n')
        with open('posted_titles.txt', 'w', encoding='utf-8') as f:
            for title in posted_titles:
                f.write(title + '\n')
        with open('posted_content_hashes.txt', 'w', encoding='utf-8') as f:
            for ch in posted_content_hashes:
                f.write(ch + '\n')
        with open('posted_timestamps.txt', 'w', encoding='utf-8') as f:
            for url, ts in posted_urls.items():
                title = next((t for t, t_ts in posted_titles.items() if t_ts == ts), "")
                ch = next((c for c, c_ts in posted_content_hashes.items() if c_ts == ts), "")
                f.write(f"{ts.isoformat()}|{url}|{title}|{ch}\n")
    except Exception as e:
        print(f"Error saving duplicate files: {e}")

first_run = not bool(posted_urls)

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
    title = html.unescape(title)
    title = re.sub(r'[^\w\s£$€]', '', title)
    title = re.sub(r'\s+', ' ', title).strip().lower()
    return title

def get_content_hash(entry):
    summary = getattr(entry, "summary", "")[:100]
    return hashlib.md5(summary.encode('utf-8')).hexdigest()

def is_duplicate(entry, title_threshold=0.85):
    if first_run:
        return False, ""
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
    "BBC News": "http://feeds.bbci.co.uk/news/rss.xml",
    "BBC News UK": "http://feeds.bbci.co.uk/news/uk/rss.xml",
    "BBC Sport Football": "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
    "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    "Times": "https://www.thetimes.co.uk/rss",
    "ITV": "https://www.itv.com/news/rss",
    "ITV Granada": "https://www.itv.com/news/granada/rss",
    "ITV UTV": "https://www.itv.com/news/utv/rss",
    "ITV West Country": "https://www.itv.com/news/westcountry/rss"
}
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Keywords for filtering ---
BREAKING_KEYWORDS = [
    "breaking", "urgent", "just in", "developing", "update", "live", "alert", "emergency", 
    "crisis", "disaster", "catastrophe", "motorway pile-up", "national security", 
    "terror attack", "major incident", "evacuation", "lockdown"
]

UK_KEYWORDS = [
    "uk", "united kingdom", "britain", "england", "scotland", "wales", "northern ireland",
    "london", "manchester", "birmingham", "glasgow", "edinburgh", "cardiff", "belfast",
    "liverpool", "leeds", "bristol", "sheffield", "newcastle", "nottingham", "southampton",
    "portsmouth", "oxford", "cambridge", "yorkshire", "lancashire", "devon", "cornwall",
    "kent", "sussex", "essex", "surrey", "hampshire", "norfolk", "suffolk", "cumbria",
    "northumberland", "merseyside", "cheshire", "dorset", "somerset", "uk government",
    "parliament", "house of commons", "house of lords", "prime minister", "nhs",
    "british", "brexit", "bbc", "premier league", "fa cup", "wimbledon"
]

PROMO_KEYWORDS = [
    "giveaway", "win", "promotion", "contest", "advert", "sponsor", "deal", "offer",
    "competition", "prize", "free", "discount"
]

CATEGORY_KEYWORDS = {
    "Breaking News": ["breaking", "urgent", "alert", "emergency", "crisis", "disaster", "catastrophe", "motorway pile-up", "national security", "terror attack", "major incident", "evacuation", "lockdown"],
    "Crime & Legal": ["crime", "murder", "homicide", "arrest", "robbery", "burglary", "assault", "police", "court", "trial", "judge", "lawsuit", "verdict", "conviction", "acquittal", "fraud", "manslaughter", "inquest", "coroner", "prosecution", "defendant", "bail", "sentencing"],
    "Sport": ["sport", "football", "premier league", "championship", "fa cup", "cricket", "test match", "ashes", "rugby", "six nations", "tennis", "wimbledon", "athletics", "olympics", "commonwealth games", "cyclist", "grand national", "cheltenham festival", "boxing", "f1", "formula one"],
    "Royals": ["royal", "monarch", "queen", "king", "prince", "princess", "duke", "duchess", "buckingham palace", "windsor", "jubilee", "coronation", "royal family", "kate middleton", "prince william", "prince harry", "meghan markle", "king charles"],
    "Culture": ["arts", "music", "film", "cinema", "theatre", "west end", "festival", "edinburgh fringe", "glastonbury", "heritage", "literary", "booker prize", "tv series", "british television", "bbc drama", "art exhibition", "national gallery", "tate modern"],
    "Immigration": ["immigration", "asylum", "migrant", "refugee", "border", "home office", "channel crossing", "deportation", "visa", "citizenship", "illegal immigration", "rwanda policy", "small boats"],
    "Politics": ["parliament", "election", "general election", "by-election", "government", "policy", "legislation", "bill", "house of commons", "house of lords", "prime minister", "chancellor", "cabinet", "tory", "labour", "conservative", "liberal democrats", "snp", "plaid cymru", "dup", "sinn fein"],
    "Economy": ["economy", "finance", "business", "taxes", "budget", "employment", "unemployment", "inflation", "cost of living", "energy prices", "retail", "bank of england", "interest rates", "gdp", "trade deficit", "pound sterling"],
    "Notable International News": ["international", "global", "foreign", "uk-us", "un climate", "cop conference", "summit", "geopolitics", "sanctions", "war", "conflict", "peace talks"],
    "Trade and Diplomacy": ["trade", "diplomacy", "eu", "brexit", "uk-eu", "foreign policy", "ambassador", "trade deal", "export", "import", "tariffs", "bilateral agreement"],
    "National Newspapers Front Pages": ["front pages", "newspaper", "telegraph", "times", "daily mail", "sun", "mirror", "express", "independent", "ft", "financial times"]
}

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

def is_promotional(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    return any(kw in text for kw in PROMO_KEYWORDS)

def is_uk_relevant(entry):
    title = entry.title.lower()
    summary = getattr(entry, "summary", "").lower()
    combined_text = title + " " + summary
    uk_count = sum(combined_text.count(kw) for kw in UK_KEYWORDS)
    distinct_uk_keywords = len([kw for kw in UK_KEYWORDS if kw in combined_text])
    max_uk_freq = max((combined_text.count(kw) for kw in UK_KEYWORDS), default=0)
    breaking_count = sum(kw in combined_text for kw in BREAKING_KEYWORDS)
    if distinct_uk_keywords < 2 and max_uk_freq < 3:
        return False
    if breaking_count == 0:
        return False
    return True

def get_category(entry):
    text = (entry.title + " " + getattr(entry, "summary", "")).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None

def log_rejection(title, url, reason):
    try:
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        with open('rejected.txt', 'a', encoding='utf-8') as f:
            f.write(f"{timestamp}|{title}|{url}|{reason}\n")
    except Exception as e:
        print(f"Error logging rejection: {e}")

# --- Main loop: fetch, filter, post ---
def main():
    # Gather recent entries
    recent_entries = []
    now_utc = datetime.now(timezone.utc)
    two_hours_ago = now_utc - timedelta(hours=2)
    for source, feed_url in feed_sources.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                # Filter by recency
                time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
                if not time_struct:
                    continue
                pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
                if pub_time < two_hours_ago:
                    continue
                if is_promotional(entry):
                    continue
                if not is_uk_relevant(entry):
                    log_rejection(entry.title, entry.link, "Not UK relevant or not breaking")
                    continue
                category = get_category(entry)
                if not category:
                    log_rejection(entry.title, entry.link, "No category match")
                    continue
                recent_entries.append((source, entry, category))
        except Exception as e:
            print(f"Error processing feed {feed_url}: {e}")

    # Shuffle and sort by breaking-ness (simplified)
    random.shuffle(recent_entries)
    def breaking_score(entry):
        text = (entry.title + " " + getattr(entry, "summary", "")).lower()
        return sum(kw in text for kw in BREAKING_KEYWORDS)
    recent_entries.sort(key=lambda tup: breaking_score(tup[1]), reverse=True)

    # Select up to 10 stories, balancing categories and sources
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

    # Posting to Reddit with quoted body as comment
    for source, entry, category in selected_entries:
        is_dup, reason = is_duplicate(entry)
        if is_dup:
            log_rejection(entry.title, entry.link, f"Duplicate: {reason}")
            continue
        norm_link = normalize_url(entry.link)
        norm_title = normalize_title(entry.title)
        content_hash = get_content_hash(entry)
        flair_text = FLAIR_MAPPING.get(category, "No Flair")
        flair_id = None
        try:
            for flair in subreddit.flair.link_templates:
                if flair['text'] == flair_text:
                    flair_id = flair['id']
                    break
        except Exception:
            pass  # Flairs may not be set up as templates

        # Submit the link post
        try:
            submission = subreddit.submit(
                title=entry.title,
                url=entry.link,
                flair_id=flair_id
            )
            print(f"Posted: {submission.shortlink}")
        except Exception as e:
            print(f"Error posting to Reddit: {e}")
            continue

        # Extract and format the body (first 3 paragraphs)
        quoted_body = extract_first_three_paragraphs(entry.link)
        if quoted_body:
            quoted_lines = [f"> {line}" if line.strip() else "" for line in quoted_body.split('\n')]
            quoted_comment = "\n".join(quoted_lines)
            quoted_comment += f"\n\n[Read more at the source]({entry.link})"
            try:
                submission.reply(quoted_comment)
                print("Added quoted body as comment.")
            except Exception as e:
                print(f"Error posting comment: {e}")

        # Save posted info
        timestamp = datetime.now(timezone.utc)
        posted_urls[norm_link] = timestamp
        posted_titles[norm_title] = timestamp
        posted_content_hashes[content_hash] = timestamp
        save_duplicate_files()
        time.sleep(30)  # Avoid Reddit rate limits

if __name__ == "__main__":
    main()
