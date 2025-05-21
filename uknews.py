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

# --- UK news RSS feeds ---
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
    """Return True if the entry is published within the last hour."""
    time_struct = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not time_struct:
        return False
    pub_time = datetime.fromtimestamp(time.mktime(time_struct), tz=timezone.utc)
    return pub_time > one_hour_ago

def extract_first_three_paragraphs(url):
    """Extract the first three non-empty, substantial paragraphs from the article URL."""
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = []
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            # Exclude very short lines (standfirsts/summaries often < 40 chars)
            if len(text) > 40:
                paragraphs.append(text)
            if len(paragraphs) == 3:
                break
        if paragraphs:
            return '\n\n'.join(paragraphs)
        else:
            # Fallback: first 500 chars of main text
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

# --- Shuffle entries for random cycling ---
random.shuffle(all_entries)

# --- Open log file for this run ---
log_filename = f"run_log_{now_utc.strftime('%Y%m%d_%H%M%S')}.txt"
with open(log_filename, 'w', encoding='utf-8') as log_file:
    new_posts = 0
    for entry in all_entries:
        title = entry.title
        link = entry.link

        if link in posted_urls:
            continue

        quote = extract_first_three_paragraphs(link)
        body = (
            f"{quote}\n\n"
            f"*Quoted from the link*\n\n"
            f"**What do you think about this news story? Join the conversation in the comments.**"
        )

        # --- Log the post attempt ---
        log_file.write("="*60 + "\n")
        log_file.write(f"TITLE: {title}\n")
        log_file.write(f"LINK: {link}\n")
        log_file.write(f"BODY:\n{body}\n")
        log_file.write("="*60 + "\n\n")

        # --- Post to Reddit as a link post with body ---
        try:
            subreddit.submit(title, url=link, selftext=body)
            print(f"Posted link post to Reddit: {title}")
            posted_urls.add(link)
            new_posts += 1
            time.sleep(10)  # Avoid Reddit rate limits[1][5][7]
            if new_posts >= 5:  # Limit per run (adjust as needed)
                break
        except Exception as e:
            print(f"Failed to post to Reddit: {e}")
            log_file.write(f"FAILED TO POST: {e}\n\n")
            continue

    # --- Save posted URLs ---
    with open('posted_urls.txt', 'w') as f:
        for url in posted_urls:
            f.write(url + '\n')

    log_file.write(f"\nTotal new posts this run: {new_posts}\n")

    if new_posts == 0:
        print("No new UK breaking news stories found in the last hour.")
    else:
        print(f"Posted {new_posts} new link posts to Reddit.")
