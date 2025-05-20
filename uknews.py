import feedparser
import requests
from bs4 import BeautifulSoup
import praw
import os
from datetime import datetime, timedelta, timezone
import time

# --- Reddit setup ---
reddit = praw.Reddit(
    client_id=os.environ['REDDIT_CLIENT_ID'],
    client_secret=os.environ['REDDIT_CLIENT_SECRET'],
    username=os.environ['REDDIT_USERNAME'],
    password=os.environ['REDDIT_PASSWORD'],
    user_agent='BreakingUKNewsBot/1.0'
)
subreddit = reddit.subreddit('BreakingUKNews')

# --- Load posted URLs ---
posted_urls = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r') as f:
        posted_urls = set(line.strip() for line in f if line.strip())

# --- RSS feeds (UK news only) ---
feed_urls = [
    'http://feeds.bbci.co.uk/news/rss.xml',
    'https://feeds.skynews.com/feeds/rss/home.xml',
    'https://www.itv.com/news/rss',
    'https://www.telegraph.co.uk/rss.xml',
    'https://www.thetimes.co.uk/rss',
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

# --- Time window for "breaking" news: last hour ---
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
        paragraphs = [p for p in soup.find_all('p') if p.get_text(strip=True)]
        if len(paragraphs) >= 3:
            return '\n\n'.join(p.get_text(strip=True) for p in paragraphs[:3])
        elif paragraphs:
            return '\n\n'.join(p.get_text(strip=True) for p in paragraphs)
        else:
            return soup.get_text(strip=True)[:500]
    except Exception as e:
        return f"(Could not extract article text: {e})"

# --- Open log file for this run ---
log_filename = f"run_log_{now_utc.strftime('%Y%m%d_%H%M%S')}.txt"
log_file = open(log_filename, 'w', encoding='utf-8')

new_posts = 0
for feed_url in feed_urls:
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link

            if link in posted_urls:
                continue
            if not is_recent(entry):
                continue

            quote = extract_first_three_paragraphs(link)
            body = (
                f"**Link:** [{title}]({link})\n\n"
                f"**Quote:**\n\n{quote}\n\n"
                f"*Quoted from the link*\n\n"
                f"**What do you think about this news story? Comment below.**"
            )

            # --- Log the post attempt ---
            log_file.write("==="*20 + "\n")
            log_file.write(f"TITLE: {title}\n")
            log_file.write(f"LINK: {link}\n")
            log_file.write(f"BODY:\n{body}\n")
            log_file.write("==="*20 + "\n\n")

            # --- Post to Reddit ---
            try:
                subreddit.submit(title, selftext=body)
                print(f"Posted to Reddit: {title}")
                posted_urls.add(link)
                new_posts += 1
                time.sleep(10)  # Avoid Reddit rate limits
                if new_posts >= 5:  # Limit per run (adjust as needed)
                    break
            except Exception as e:
                print(f"Failed to post to Reddit: {e}")
                log_file.write(f"FAILED TO POST: {e}\n\n")
                continue
        if new_posts >= 5:
            break
    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")
        log_file.write(f"ERROR PROCESSING FEED: {feed_url} - {e}\n\n")

# --- Save posted URLs ---
with open('posted_urls.txt', 'w') as f:
    for url in posted_urls:
        f.write(url + '\n')

log_file.write(f"\nTotal new posts this run: {new_posts}\n")
log_file.close()

if new_posts == 0:
    print("No new UK breaking news stories found in the last hour.")
else:
    print(f"Posted {new_posts} new stories to Reddit.")
