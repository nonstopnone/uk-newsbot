### autopaperposter.py
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
    articles = soup.find_all('figure')
    for fig in articles:
        img = fig.find('img')
        if not img or not img.get('alt'):
            continue
        name = img['alt'].strip()
        caption = fig.find('figcaption')
        summary = caption.get_text(strip=True) if caption else ''
        if name and summary:
            front_pages.append((name, summary))
    return front_pages

def post_to_reddit(entries, subreddit_name):
    reddit = praw.Reddit(
        client_id=os.environ['REDDIT_CLIENT_ID'],
        client_secret=os.environ['REDDIT_CLIENT_SECRET'],
        username=os.environ['REDDIT_USERNAME'],
        password=os.environ['REDDITPASSWORD'],  # Using REDDITPASSWORD from secrets
        user_agent=os.environ.get('USER_AGENT', 'newspaper-bot/1.0'),
    )
    subreddit = reddit.subreddit(subreddit_name)

    tomorrow = datetime.now() + timedelta(days=1)
    date_str = tomorrow.strftime('%d/%-m/%Y')
    day_name = tomorrow.strftime('%A')

    for name, summary in entries:
        title = f"{name} Front Page | {day_name} {date_str}"
        selftext = summary
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


### .github/workflows/autopaperposter.yml
# Save this as .github/workflows/autopaperposter.yml

name: Auto Paper Poster

on:
  schedule:
    - cron: '0 23 * * *'  # Every day at 23:00 UTC
  workflow_dispatch: {}

jobs:
  run-script:
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'

    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.x'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install praw requests beautifulsoup4

      - name: Run autopaperposter script
        env:
          REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}
          REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}
          REDDIT_USERNAME: ${{ secrets.REDDIT_USERNAME }}
          REDDITPASSWORD: ${{ secrets.REDDITPASSWORD }}
          USER_AGENT: 'newspaper-bot/1.0'
          SUBREDDIT: ${{ secrets.SUBREDDIT }}
        run: |
          python autopaperposter.py
