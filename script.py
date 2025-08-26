import requests
from bs4 import BeautifulSoup
import json
import os
from datetime import datetime
import sys
import praw

url = 'https://www.gov.uk/government/publications/migrants-detected-crossing-the-english-channel-in-small-boats/migrants-detected-crossing-the-english-channel-in-small-boats-last-7-days'
response = requests.get(url)
soup = BeautifulSoup(response.text, 'html.parser')
table = soup.find('table')
rows = table.find('tbody').find_all('tr')
last_row = rows[-1]
cells = last_row.find_all('td')
date_str = cells[0].text.strip()
migrants = int(cells[1].text.strip())
boats = int(cells[2].text.strip())
date = datetime.strptime(date_str, '%d %B %Y')
date_formatted = date.strftime('%d %B')
average = round(migrants / boats, 1) if boats > 0 else 0

try:
    with open('totals.json', 'r') as f:
        totals = json.load(f)
except FileNotFoundError:
    totals = {
        "total_2025": 0,
        "total_since_gov": 51918,
        "last_migrants": 0,
        "last_image": None,
        "last_posted_date": "1900-01-01"
    }

last_date = datetime.fromisoformat(totals['last_posted_date'])
if date <= last_date:
    print("Already posted or old data")
    sys.exit(0)

totals['total_2025'] += migrants
totals['total_since_gov'] += migrants

if migrants == 0:
    image_path = 'empty.png'
else:
    if totals['last_migrants'] > 0 and totals['last_image']:
        image_path = 'arrival2.png' if totals['last_image'] == 'arrival1.png' else 'arrival1.png'
    else:
        image_path = 'arrival1.png'

title = f"UK Illegal Migration Tracker: {migrants} Illegal Migrants Arrived on {boats} Small Boats to the UK on {date_formatted}"

body = f"""On {date_formatted}, {migrants} illegal migrants arrived in the UK via {boats} small boats across the English Channel, according to provisional Home Office data.
That’s an average of {average} people per boat.
All were escorted by French authorities to English waters and then picked up by Border Force.
Updated provisional totals:
Total in 2025 so far: {totals['total_2025']}
Since the current government took office: {totals['total_since_gov']}
International law recognises that each state decides its own laws for entry. A non-national who enters the UK without leave to do so commits an offence, regardless of whether they are seeking asylum. Illegal refers to the method of arrival. However, asylum claims, where an individual is a genuine refugee under international law, can provide protection from prosecution, even if their initial entry was unlawful.
This post is automated and may contain errors, see the government data here {url}"""

reddit = praw.Reddit(client_id=os.environ['REDDIT_CLIENT_ID'], client_secret=os.environ['REDDIT_CLIENT_SECRET'], username=os.environ['REDDIT_USERNAME'], password=os.environ['REDDITPASSWORD'], user_agent='tracker/1.0')
subreddit = reddit.subreddit('SUBREDDIT_NAME')  # Replace with actual subreddit
subreddit.submit_image(title, image_path, selftext=body)

totals['last_migrants'] = migrants
totals['last_image'] = image_path
totals['last_posted_date'] = date.isoformat()
with open('totals.json', 'w') as f:
    json.dump(totals, f)
