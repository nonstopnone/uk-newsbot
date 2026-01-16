import os
import sys
import time
import re
import requests
import praw
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# --- Configuration ---
# Target URL for the rolling blog
SKY_NEWS_URL = "https://news.sky.com/story/tuesdays-national-newspaper-front-pages-12427754"
SUBREDDIT = "uknews_approvals"
TEMP_DIR = "temp_images"

def clean_text(text):
    if not text: return ""
    return text.strip()

def parse_post_time(post_text):
    """
    Attempts to find a time string (e.g., 22:30) in the post text.
    Returns a datetime object representing that time in the recent past, or None.
    """
    # Regex for HH:MM
    match = re.search(r'\b([0-1]?[0-9]|2[0-3]):([0-5][0-9])\b', post_text)
    if not match:
        return None

    hour, minute = map(int, match.groups())
    now = datetime.now()
    
    # Construct potential times: Today at HH:MM, Yesterday at HH:MM
    dt_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    dt_yesterday = dt_today - timedelta(days=1)
    
    # Logic: The post time is likely the one closest to 'now' but in the past.
    if dt_today > now:
        return dt_yesterday
    else:
        # If dt_today is in the past, return it
        return dt_today

def generate_post_title(paper_name, page_title_day=None):
    now = datetime.now()
    
    # Logic to handle "Tomorrow's papers" appearing tonight
    # If it's after 8PM (20:00), we assume the papers are for tomorrow
    if now.hour >= 20:
        paper_date = now + timedelta(days=1)
    else:
        paper_date = now

    day_name = paper_date.strftime("%A").upper()
    date_str = paper_date.strftime("%d/%m/%Y")
    
    # If we extracted a specific day from the Sky News title (e.g. "Tuesday's Papers"), use that
    if page_title_day:
        day_name = page_title_day.upper()

    return f"{paper_name.upper()} Front Page | {day_name} {date_str}"

def main():
    # 1. Validate Env
    REQUIRED_ENV = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDITPASSWORD"]
    missing = [var for var in REQUIRED_ENV if var not in os.environ]
    if missing:
        print(f"Missing env vars: {missing}")
        sys.exit(1)

    # 2. Setup Reddit
    try:
        reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            username=os.environ["REDDIT_USERNAME"],
            password=os.environ["REDDITPASSWORD"],
            user_agent=os.environ.get("USER_AGENT", "NewspaperBot/2.0")
        )
        subreddit = reddit.subreddit(SUBREDDIT)
    except Exception as e:
        print(f"Reddit Auth Error: {e}")
        sys.exit(1)

    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)

    # 3. Scrape Sky News
    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(SKY_NEWS_URL)
        
        try:
            page.wait_for_selector('[data-testid="live-blog-post"]', timeout=30000)
        except:
            print("Timeout waiting for posts.")
            browser.close()
            sys.exit(1)

        # Get Page Title to guess the Day
        page_main_title = page.title()
        extracted_day = None
        days_of_week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for day in days_of_week:
            if day in page_main_title:
                extracted_day = day
                break

        # Get all posts
        all_posts = page.query_selector_all('[data-testid="live-blog-post"]')
        
        # --- LIMIT SCAN TO LAST 15 ---
        scan_limit = 15
        posts_to_scan = all_posts[:scan_limit]
        print(f"Found {len(all_posts)} total posts. Scanning the top {len(posts_to_scan)}...")

        for post in posts_to_scan:
            text_content = post.inner_text()
            
            # A. Check for "End of coverage"
            end_phrases = ["that's all for today", "that concludes our coverage", "check back tomorrow"]
            if any(phrase in text_content.lower() for phrase in end_phrases):
                print("Found end-of-coverage marker. Stopping script.")
                break 

            # B. Check Age (Stale Check > 18 Hours)
            post_dt = parse_post_time(text_content)
            if post_dt:
                age_hours = (datetime.now() - post_dt).total_seconds() / 3600
                if age_hours > 18:
                    print(f"Skipping stale post (Age: {age_hours:.1f} hours).")
                    continue
            
            # C. Extract Data
            lines = [l.strip() for l in text_content.split('\n') if l.strip()]
            if not lines: continue

            paper_name = lines[0]
            # Heuristic: If the first line is just a time (e.g. "22:30"), the paper name is the second line.
            if re.match(r'^[0-1]?[0-9]:[0-5][0-9]$', paper_name):
                if len(lines) > 1:
                    paper_name = lines[1]
                    blurb = "\n\n".join(lines[2:])
                else:
                    continue # Malformed post
            else:
                blurb = "\n\n".join(lines[1:])
            
            # Check for Image
            img_element = post.query_selector('img')
            if not img_element:
                continue 

            img_url = img_element.get_attribute('src')
            
            # D. Construct Reddit Post Data
            title = generate_post_title(paper_name, extracted_day)
            
            # E. Deduplication Check
            already_exists = False
            for submission in subreddit.new(limit=25):
                # Check loosely for Paper Name matches
                if paper_name.upper() in submission.title.upper() and datetime.now().strftime("%d/%m") in submission.title:
                    already_exists = True
                    break
            
            if already_exists:
                print(f"Skipping {paper_name} (Already posted).")
                continue

            # F. Process New Paper
            print(f"Processing new paper: {paper_name}")
            
            try:
                img_data = requests.get(img_url).content
                safe_filename = "".join(x for x in paper_name if x.isalnum())
                local_path = f"{TEMP_DIR}/{safe_filename}.jpg"
                
                with open(local_path, 'wb') as f:
                    f.write(img_data)
                
                print(f"Uploading {paper_name} to Reddit...")
                submission = subreddit.submit_image(title=title, image_path=local_path)
                
                if blurb:
                    comment_text = f"{blurb}\n\nVia: Sky News"
                    submission.reply(comment_text)
                
                print(f"Success! {title}")
                
                if os.path.exists(local_path):
                    os.remove(local_path)
                    
                time.sleep(5)

            except Exception as e:
                print(f"Error processing {paper_name}: {e}")

        browser.close()

if __name__ == "__main__":
    main()
