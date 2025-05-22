### post_front_pages.py
```python
import os
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import praw

def fetch_front_pages(url):
    """
    Fetches newspaper names and summaries from the given Sky News page.
    Returns a list of tuples: (paper_name, summary)
    """
    resp = requests.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    front_pages = []
    # On Sky News front pages story, each paper is in a div with class 'sdc-article-body'
    articles = soup.find_all('figure')
    for fig in articles:
        # Paper name is in img alt
        img = fig.find('img')
        if not img or not img.get('alt'):
            continue
        name = img['alt'].strip()
        # Summary is in the next sibling <figcaption> or <p>
        caption = fig.find('figcaption')
        summary = caption.get_text(strip=True) if caption else ''
        if name and summary:
            front_pages.append((name, summary))
    return front_pages


def post_to_reddit(entries, subreddit_name):
    """
    Posts each newspaper front page summary to Reddit.
    """
    # Initialize Reddit client using environment variables
    reddit = praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDITPASSWORD'],
        user_agent=os.environ.get('USER_AGENT', 'newspaper-bot/1.0'),
    )
    subreddit = reddit.subreddit(subreddit_name)

    # Calculate tomorrow's date in DD/M/YYYY and weekday name
    tomorrow = datetime.now() + timedelta(days=1)
    date_str = tomorrow.strftime('%d/%-m/%Y')
    day_name = tomorrow.strftime('%A')

    for name, summary in entries:
        title = f"{name} Front Page | {day_name} {date_str}"
        selftext = summary
        # Submit text post with flair
        submission = subreddit.submit(
            title,
            selftext=selftext,
            flair_text='National Newspaper Front Pages'
        )
        print(f"Posted: {submission.id} - {title}")


if __name__ == '__main__':
    SKY_URL = 'https://news.sky.com/story/saturdays-national-newspaper-front-pages-12427754'
    SUBREDDIT = os.environ.get('SUBREDDIT', 'your_subreddit_here')

    pages = fetch_front_pages(SKY_URL)
    if not pages:
        print("No front pages found, exiting.")
    else:
        post_to_reddit(pages, SUBREDDIT)
``` 

---

### .github/workflows/daily_post.yml
```yaml
name: Daily Newspaper Front Pages

on:
  schedule:
    # Runs every day at 23:00 UTC (00:00 BST)
    - cron: '0 23 * * *'
  workflow_dispatch: {}

jobs:
  post:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.x'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install praw requests beautifulsoup4

      - name: Run posting script
        env:
          REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}
          REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}
          REDDIT_USERNAME: ${{ secrets.REDDIT_USERNAME }}
          REDDIT_PASSWORD: ${{ secrets.REDDIT_PASSWORD }}
          USER_AGENT: 'newspaper-bot/1.0'
          SUBREDDIT: ${{ secrets.SUBREDDIT }}
        run: |
          python post_front_pages.py
