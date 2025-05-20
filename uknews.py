import feedparser
import requests
from bs4 import BeautifulSoup
import os

# Load posted URLs
posted_urls = set()
if os.path.exists('posted_urls.txt'):
    with open('posted_urls.txt', 'r') as f:
        posted_urls = set(line.strip() for line in f)

# RSS feeds
feed_urls = [
    'http://feeds.bbci.co.uk/news/rss.xml',         # BBC News
    'https://feeds.skynews.com/feeds/rss/home.xml', # Sky News
    'https://www.itv.com/news/rss',                 # ITV News
    'https://www.telegraph.co.uk/rss.xml',          # The Telegraph
    'https://www.thetimes.co.uk/rss',               # The Times
]

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

for feed_url in feed_urls:
    try:
        # Parse RSS feed
        feed = feedparser.parse(feed_url)
        # Process the latest 20 entries
        for entry in feed.entries[:20]:
            title = entry.title
            link = entry.link

            if link in posted_urls:
                continue

            # Fetch the full article
            try:
                response = requests.get(link, headers=headers, timeout=10)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')

                # Extract first three paragraphs (assuming <p> tags)
                paragraphs = soup.find_all('p')
                quote = '\n\n'.join(p.get_text(strip=True) for p in paragraphs[:3]) if len(paragraphs) >= 3 else soup.get_text(strip=True)[:500]

                # Format post body
                body = (
                    f"**Link:** [{title}]({link})\n\n"
                    f"**Quote:**\n\n{quote}\n\n"
                    f"*Quoted from the link*\n\n"
                    f"**What do you think about this news story? Comment below.**"
                )

                # TEST CASE PRINT OUT
                print("\n" + "="*50)
                print(f"TITLE: {title}\n")
                print(f"BODY:\n{body}")
                print("="*50 + "\n")
                posted_urls.add(link)

            except requests.RequestException as e:
                print(f"Failed to fetch article {link}: {e}")
                continue

    except Exception as e:
        print(f"Error processing feed {feed_url}: {e}")

# Save posted URLs
with open('posted_urls.txt', 'w') as f:
    for url in posted_urls:
        f.write(url + '\n')
