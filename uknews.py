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

# --- Check for required environment variables ---
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

# --- Reddit API credentials from environment variables ---
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

# --- Load posted URLs and titles ---
posted_urls = set()
posted_titles = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r', encoding='utf-8') as f:
        posted_urls = set(line.strip() for line in f if line.strip())
if os.path.exists('posted_titles.txt'):
    with open('posted_titles.txt', 'r', encoding='utf-8') as f:
        posted_titles = set(line.strip().lower() for line in f if line.strip())

def normalize_url(url):
    """Remove query, fragment, and trailing slash for duplicate protection."""
    parsed = urllib.parse.urlparse(url)
    normalized_path = parsed.path.rstrip('/')
    normalized_url = urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        normalized_path,
        '', '', ''  # params, query, fragment
    ))
    return normalized_url

def normalize_title(title):
    return title.strip().lower()

# --- RSS feeds ---
feed_urls = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://feeds.skynews.com/feeds/rss/home.xml',
    'https://www.itv.com/news/rss',
    'https://www.telegraph.co.uk/rss.xml',
    'https://www.thetimes.co.uk/rss',
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Define the breaking news window (last hour) ---
now_utc = datetime.now(timezone.utc)
one_hour_ago = now_utc - timedelta(hours=1)

def is_recent(entry):
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > one_hour_ago

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

# --- Gather all recent entries from all feeds ---
all_entries = []
for feed_url in feed_urls:
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            if is_recent(entry):
                all_entries.append(entry)
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

random.shuffle(all_entries)

# --- Logging directory ---
os.makedirs('logs', exist_ok=True)
log_filename = f"logs/run_log_{now_utc.strftime('%Y%m%d_%H%M%S')}.txt"
with open(log_filename, 'w', encoding='utf-8') as log_file:
    new_posts = 0
    for entry in all_entries:
        title = entry.title
        link = entry.link

        norm_link = normalize_url(link)
        norm_title = normalize_title(title)

        if norm_link in posted_urls or norm_title in posted_titles:
            continue

        # Format title for Reddit post
        reddit_title = f"{title} | UK News"

        # Prepare comment body
        quote = extract_first_three_paragraphs(link)
        comment_body = (
            f"{quote}\n\n"
            f"*Quoted from the link*\n\n"
            f"**What do you think about this news story? Join the conversation in the comments.**"
        )

        log_file.write("="*60 + "\n")
        log_file.write(f"TITLE: {reddit_title}\n")
        log_file.write(f"LINK: {link}\n")
        log_file.write(f"COMMENT BODY:\n{comment_body}\n")
        log_file.write("="*60 + "\n\n")

        try:
            # Submit the link post
            submission = subreddit.submit(title=reddit_title, url=link)
            print(f"Posted link post to Reddit: {reddit_title}")
            # Immediately post the context as a comment
            submission.reply(comment_body)
            print(f"Posted context comment under: {reddit_title}")
            posted_urls.add(norm_link)
            posted_titles.add(norm_title)
            new_posts += 1
            time.sleep(10)
            if new_posts >= 5:
                break
        except Exception as e:
            print(f"Failed to post to Reddit: {e}")
            log_file.write(f"FAILED TO POST: {e}\n\n")
            continue

    with open('posted_urls.txt', 'w', encoding='utf-8') as f:
        for url in posted_urls:
            f.write(url + '\n')
    with open('posted_titles.txt', 'w', encoding='utf-8') as f:
        for title in posted_titles:
            f.write(title + '\n')

    log_file.write(f"\nTotal new posts this run: {new_posts}\n")

    if new_posts == 0:
        print("No new UK breaking news stories found in the last hour.")
    else:
        print(f"Posted {new_posts} new link posts to Reddit.")
