import feedparser
import requests
from bs4 import BeautifulSoup
import praw
from datetime import datetime, timedelta, timezone
import time
import os
import sys
import random

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

# --- Load posted URLs ---
posted_urls = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r') as f:
        posted_urls = set(line.strip() for line in f if line.strip())

# --- Trusted UK news RSS feeds ---
feed_urls = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://feeds.skynews.com/feeds/rss/home.xml',
    'https://www.telegraph.co.uk/rss.xml',
    # Only UK major outlets, no generic feeds
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Define the breaking news window (last 7 hours) ---
now_utc = datetime.now(timezone.utc)
seven_hours_ago = now_utc - timedelta(hours=7)

def is_recent(entry):
    """Return True if the entry is published within the last 7 hours."""
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > seven_hours_ago

def extract_article_paragraphs(url, title):
    """Extract the first three meaningful paragraphs, skipping short 'top line' blurbs, standfirst, and author lines."""
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)]

        filtered = []
        for p in paragraphs:
            if len(p) < 60:
                continue
            if title.strip().lower() in p.strip().lower():
                continue
            if p.isupper():
                continue
            if p.strip().startswith("By "):
                continue
            filtered.append(p)
            if len(filtered) == 3:
                break

        return '\n\n'.join(filtered) if filtered else ""
    except Exception as e:
        return ""

# --- Collect all eligible articles from all feeds ---
articles = []
for feed_url in feed_urls:
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = getattr(entry, 'title', '')
            link = getattr(entry, 'link', '')
            if not link or not title or not isinstance(link, str) or not link.startswith(('http://', 'https://')):
                continue
            if link in posted_urls:
                continue
            if not is_recent(entry):
                continue
            articles.append({
                "title": title,
                "link": link
            })
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

random.shuffle(articles)

log_filename = f"run_log_{now_utc.strftime('%Y%m%d_%H%M%S')}.txt"
with open(log_filename, 'w', encoding='utf-8') as log_file:
    new_posts = 0
    for article in articles:
        title = article["title"]
        link = article["link"]

        paragraphs = extract_article_paragraphs(link, title)
        if not paragraphs.strip():
            continue

        body = (
            f"{paragraphs}\n\n"
            f"*Quoted from the link*\n\n"
            f"**What are your thoughts on this story? Join the discussion in the comments.**"
        )

        post_title = f"{title}| UK News"

        log_file.write("="*60 + "\n")
        log_file.write(f"TITLE: {post_title}\n")
        log_file.write(f"LINK: {link}\n")
        log_file.write(f"BODY:\n{body}\n")
        log_file.write("="*60 + "\n\n")

        try:
            submission = subreddit.submit(post_title, url=link, selftext=body)
            print(f"Posted link post to Reddit: {post_title}")
        except Exception as e:
            print(f"Unexpected error posting to Reddit: {e}")
            log_file.write(f"UNEXPECTED ERROR: {e}\n\n")
            continue

        posted_urls.add(link)
        new_posts += 1
        time.sleep(10)
        if new_posts >= 5:
            break

    with open('posted_urls.txt', 'w') as f:
        for url in posted_urls:
            f.write(url + '\n')

    log_file.write(f"\nTotal new posts this run: {new_posts}\n")

    if new_posts == 0:
        print("No new UK breaking news stories found in the last 7 hours.")
    else:
        print(f"Posted {new_posts} new link posts to Reddit from the last 7 hours.")
