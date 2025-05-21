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
import html  # For HTML entity decoding

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

# --- Load posted URLs, titles, content hashes, and records ---
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
first_run = not (os.path.exists('posted_urls.txt') or os.path.exists('posted_titles.txt') or os.path.exists('posted_content_hashes.txt'))

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

def is_duplicate(entry, posted_urls, posted_titles, posted_content_hashes, title_threshold=0.85):
    if first_run:  # Skip duplicate check on first run
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
    "BBC": "http://feeds.bbci.co.uk/news/uk/rss.xml",
    "Sky": "https://feeds.skynews.com/feeds/rss/home.xml",
    "Telegraph": "https://www.telegraph.co.uk/rss.xml",
    "Times": "https://www.thetimes.co.uk/rss",
    "ITV": "https://www.itv.com/news/rss"
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
    "National Newspapers Front Pages": ["front pages", "newspaper", "telegraph", "guardian", "times"]
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

# --- Breaking news window (last 6 hours to ensure more articles) ---
now_utc = datetime.now(timezone.utc)
six_hours_ago = now_utc - timedelta(hours=6)

def is_recent(entry):
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > six_hours_ago

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

def check_messages(posted_titles):
    """Check the bot's inbox for private messages related to the subreddit or posts."""
    messages = []
    try:
        for message in reddit.inbox.unread(limit=10):
            if isinstance(message, praw.models.Message):
                msg_body = message.body.lower()
                msg_relevant = ('breakinguknews' in msg_body or
                                any(title.lower() in msg_body for title in posted_titles))
                if msg_relevant:
                    messages.append({
                        'subject': message.subject,
                        'body': message.body,
                        'author': message.author.name if message.author else '[deleted]',
                        'time': datetime.fromtimestamp(message.created_utc, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                    })
                message.mark_read()
    except Exception as e:
        print(f"Error checking messages: {e}")
    return messages

def log_to_records(timestamp, source, title, category, post_link, comment_link=None, message=None):
    """Log post, comment, and message details to posted_records.txt."""
    try:
        with open('posted_records.txt', 'a', encoding='utf-8') as f:
            if message:
                f.write(f"{timestamp} | Message | {message['subject']} | {message['author']} | {message['body'][:100]} | {message['time']}\n")
            else:
                comment_field = comment_link if comment_link else "No comment posted"
                f.write(f"{timestamp} | {source} | {title} | {category} | {post_link} | {comment_field}\n")
    except Exception as e:
        print(f"Error writing to posted_records.txt: {e}")

def display_posted_records(current_posts):
    """Display current run's post links and contents of posted_records.txt."""
    print("\n--- Posts Created in This Run ---")
    if current_posts:
        for post in current_posts:
            print(f"Post: {post['title']} | Link: {post['post_link']}")
    else:
        print("No posts created in this run.")
    print("\n--- Posted Records (Historical) ---")
    try:
        if os.path.exists('posted_records.txt'):
            with open('posted_records.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    print(line.strip())
        else:
            print("No historical posted records found.")
    except Exception as e:
        print(f"Error reading posted_records.txt: {e}")
    print("--- End of Records ---")

# --- Gather recent entries ---
recent_entries = []
for source, feed_url in feed_sources.items():
    try:
        feed = feedparser.parse(feed_url)
        source_entries = [
            entry for entry in feed

.entries
            if is_recent(entry) and is_uk_relevant(entry) and not is_promotional(entry)
        ]
        for entry in source_entries:
            category = get_category(entry)
            if category:
                recent_entries.append((source, entry, category))
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

# --- Check if enough entries are available ---
if len(recent_entries) < 2:
    print(f"ERROR: Found only {len(recent_entries)} eligible articles, need at least 2.")
    sys.exit(1)

# --- Sort all entries by breaking-ness ---
recent_entries.sort(key=lambda tup: breaking_score(tup[1]), reverse=True)

# --- Select up to 10 stories, ensuring at least 2 ---
selected_entries = recent_entries[:max(10, len(recent_entries))]

# --- Posting to Reddit with simplified comment and title suffix ---
current_posts = []  # Track posts made in this run
posted_titles_for_check = set()
for source, entry, category in selected_entries:
    is_dup, reason = is_duplicate(entry, posted_urls, posted_titles, posted_content_hashes)
    if is_dup:
        print(f"Skipping duplicate: {reason}")
        continue
    posted_titles_for_check.add(html.unescape(entry.title))
    norm_link = normalize_url(entry.link)
    norm_title = normalize_title(entry.title)
    content_hash = get_content_hash(entry)

    # Assign flair
    flair_text = FLAIR_MAPPING.get(category, "No Flair")
    flair_id = None
    try:
        for flair in subreddit.flair.link_templates:
            if flair['text'] == flair_text:
                flair_id = flair['id']
                break
    except Exception as e:
        print(f"Warning: Could not retrieve flair templates: {e}")

    # Decode HTML entities in the title and append "| UK News"
    clean_title = html.unescape(entry.title) + " | UK News"
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        submission = subreddit.submit(
            title=clean_title,
            url=entry.link,
            flair_id=flair_id
        )
        post_link = submission.shortlink
        print(f"Posted: {post_link}")
        current_posts.append({'title': clean_title, 'post_link': post_link})
    except Exception as e:
        print(f"Error posting to Reddit: {e}")
        continue

    # Extract and format the body (first 3 paragraphs)
    quoted_body = extract_first_three_paragraphs(entry.link)
    comment_link = None
    if quoted_body:
        quoted_body = html.unescape(quoted_body)
        quoted_lines = [f"> {line}" if line.strip() else "" for line in quoted_body.split('\n')]
        quoted_comment = "\n".join(quoted_lines)
        quoted_comment += f"\n\nRead more at the [source]({entry.link})"
        try:
            comment = submission.reply(quoted_comment)
            comment_link = f"https://www.reddit.com{comment.permalink}"
            print("Added quoted body as comment.")
        except Exception as e:
            print(f"Error posting comment: {e}")

    # Save posted info
    try:
        with open('posted_urls.txt', 'a', encoding='utf-8') as f:
            f.write(norm_link + '\n')
        with open('posted_titles.txt', 'a', encoding='utf-8') as f:
            f.write(norm_title + '\n')
        with open('posted_content_hashes.txt', 'a', encoding='utf-8') as f:
            f.write(content_hash + '\n')
        log_to_records(timestamp, source, clean_title, category, post_link, comment_link)
    except Exception as e:
        print(f"Error saving posted info: {e}")

    # Stop after posting at least 2 posts
    if len(current_posts) >= 2:
        break

    # Add a delay to avoid rate limits
    time.sleep(30)

# --- Check if at least 2 posts were made ---
if len(current_posts) < 2:
    print(f"ERROR: Only {len(current_posts)} posts made, required at least 2.")
    sys.exit(1)

# --- Check messages after posting ---
messages = check_messages(posted_titles_for_check)
for message in messages:
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    log_to_records(timestamp, "N/A", "N/A", "Message", "N/A", None, message)

# --- Display posted records and current run's posts ---
display_posted_records(current_posts)
