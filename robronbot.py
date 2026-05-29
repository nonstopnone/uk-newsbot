import os
import re
import sys
import html
import time
from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import praw


def _env(key, default):
    val = os.environ.get(key)
    return val if val not in (None, "") else default


TVMAZE_SHOW_ID = int(_env("TVMAZE_SHOW_ID", "2548"))
TVMAZE_BASE = "https://api.tvmaze.com"
LOCAL_TZ = _env("LOCAL_TZ", "Europe/London")
SUBREDDIT = _env("SUBREDDIT", "robronaddicts")

SPOILER_INDEX_URL = _env(
    "SPOILER_INDEX_URL",
    "https://www.tvguide.co.uk/emmerdale-insider/category/spoilers/",
)
SPOILER_PATH_HINT = _env("SPOILER_PATH_HINT", "/emmerdale-insider/spoilers/")
ROBRON_TERMS = [t.strip().lower() for t in _env("ROBRON_TERMS", "robron,aaron,robert").split(",") if t.strip()]
ROBRON_OVERRIDE = _env("ROBRON_OVERRIDE", "auto").lower()

EPISODE_HOUR = int(_env("EPISODE_HOUR", "7"))
EPISODE_MINUTE = int(_env("EPISODE_MINUTE", "0"))
EPISODE_WINDOW_MIN = int(_env("EPISODE_WINDOW_MIN", "90"))

MIDNIGHT_WINDOW_MIN = int(_env("MIDNIGHT_WINDOW_MIN", "90"))
ARTICLES_WEEKDAY = int(_env("ARTICLES_WEEKDAY", "1"))
CLIPS_WEEKDAY = int(_env("CLIPS_WEEKDAY", "5"))

JOB = _env("JOB", "auto")
FORCE_WINDOW = _env("FORCE_WINDOW", "false").lower() == "true"

POST_FLAIR_EPISODE = _env("POST_FLAIR_EPISODE", "Episode Discussion")
POST_FLAIR_SPOILER = _env("POST_FLAIR_SPOILER", "Spoilers")
STICKY = _env("STICKY", "true").lower() == "true"
STICKY_SLOT = int(_env("STICKY_SLOT", "2"))
SUGGESTED_SORT = _env("SUGGESTED_SORT", "new")

MARKER_PREFIX = _env("MARKER_PREFIX", "emmerbot")
DEDUPE_SCAN_LIMIT = int(_env("DEDUPE_SCAN_LIMIT", "60"))

USE_GEMINI_INTRO = _env("USE_GEMINI_INTRO", "false").lower() == "true"
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-1.5-flash")

DRY_RUN = _env("DRY_RUN", "false").lower() == "true"
USER_AGENT = _env("USER_AGENT", "python:robronaddicts-episode-bot:v3.0 (by /u/robronaddicts mods)")

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.google.com/",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], start=1)}


@dataclass
class Episode:
    season: int
    number: int
    airdate: str
    name: str
    summary_text: str

    @property
    def code(self):
        return f"S{self.season}E{self.number}"


def strip_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def now_local():
    return datetime.now(ZoneInfo(LOCAL_TZ))


def _in_window(when, hour, minute, window_min):
    minutes_now = when.hour * 60 + when.minute
    start = hour * 60 + minute
    return start <= minutes_now <= start + window_min


def _http_get(url, timeout=20):
    return requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)


def parse_label_date(label, ref):
    if not label:
        return None
    low = label.lower()
    if "-" in low or "\u2013" in low:
        return None
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})\b", low)
    if not m:
        return None
    month, day = MONTHS[m.group(1)], int(m.group(2))
    best = None
    for yr in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = date(yr, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - ref).days) < abs((best - ref).days):
            best = cand
    return best


def has_robron(text):
    if not text:
        return False
    t = text.lower()
    return any(re.search(r"\b" + re.escape(term) + r"\b", t) for term in ROBRON_TERMS)


def parse_spoiler_index(html_text, ref):
    soup = BeautifulSoup(html_text, "html.parser")
    entries = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if SPOILER_PATH_HINT not in href or href.rstrip("/").endswith("category/spoilers"):
            continue
        txt = a.get_text(" ", strip=True)
        e = entries.setdefault(href, {"date": None, "headline": ""})
        d = parse_label_date(txt, ref)
        if d:
            e["date"] = d
        elif len(txt) > len(e["headline"]):
            e["headline"] = txt
    return entries


def detect_robron(target_date, index_html=None, article_fetcher=None):
    try:
        if index_html is None:
            resp = _http_get(SPOILER_INDEX_URL)
            if resp.status_code != 200:
                return None, None, f"index HTTP {resp.status_code}"
            index_html = resp.text
    except Exception as e:
        return None, None, f"index fetch failed: {e}"

    entries = parse_spoiler_index(index_html, target_date)
    match = next((u for u, e in entries.items() if e["date"] == target_date), None)
    if not match:
        return None, None, "no per-day spoiler entry for this date yet"

    if has_robron(entries[match]["headline"]):
        return True, match, "named in spoiler headline"

    fetch = article_fetcher or (lambda u: _http_get(u).text)
    try:
        body_html = fetch(match)
        body_text = BeautifulSoup(body_html, "html.parser").get_text(" ", strip=True)
    except Exception as e:
        return None, match, f"article fetch failed: {e}"

    if has_robron(body_text):
        return True, match, "named in spoiler body"
    return False, match, "spoiler found but Robron not named"


def is_robron_day(target_date, **kw):
    if ROBRON_OVERRIDE == "on":
        return True, None, "override ON"
    if ROBRON_OVERRIDE == "off":
        return False, None, "override OFF"
    state, url, reason = detect_robron(target_date, **kw)
    return (state is True), url, reason


def decide_jobs(when=None):
    when = when or now_local()
    weekday = when.weekday()
    jobs = []
    if _in_window(when, EPISODE_HOUR, EPISODE_MINUTE, EPISODE_WINDOW_MIN):
        jobs.append("episode")
    if weekday == ARTICLES_WEEKDAY and _in_window(when, 0, 0, MIDNIGHT_WINDOW_MIN):
        jobs.append("articles")
    if weekday == CLIPS_WEEKDAY and _in_window(when, 0, 0, MIDNIGHT_WINDOW_MIN):
        jobs.append("clips")
    return jobs


def fetch_episodes(session=None, retries=4):
    sess = session or requests.Session()
    url = f"{TVMAZE_BASE}/shows/{TVMAZE_SHOW_ID}/episodes"
    last_err = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return [
                Episode(
                    season=int(e.get("season") or 0),
                    number=int(e.get("number") or 0),
                    airdate=e.get("airdate") or "",
                    name=e.get("name") or "",
                    summary_text=strip_html(e.get("summary")),
                )
                for e in resp.json()
            ]
        except Exception as err:
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch TVMaze episodes after {retries} tries: {last_err}")


def episodes_for_date(episodes, date_str):
    found = [e for e in episodes if e.airdate == date_str]
    return sorted(found, key=lambda e: (e.season, e.number))


def _spoiler(text):
    safe = text.replace("!<", "! <").replace(">!", "> !")
    return f">!{safe}!<"


SPOILER_RULE = (
    "**Please keep spoilers out of the title and use spoiler tags** "
    "`>!like this!<` **in comments.**"
)


def build_episode(eps, date_obj, source_url=None):
    weekday = date_obj.strftime("%A")
    date_s = date_obj.strftime("%-d %B %Y")
    if eps:
        codes = " & ".join(e.code for e in eps)
        title = f"Emmerdale Discussion \u2014 {weekday} {date_s} \u2014 {codes}"
    else:
        title = f"Emmerdale Discussion \u2014 {weekday} {date_s}"

    lines = [
        f"Episode discussion for **{weekday} {date_s}** \u2014 Robron are on today.",
        "",
        "Out now on **ITVX** and **YouTube** from 7am, and on **ITV1** tonight.",
        "",
        "Talk about everything Robron (and the wider episode) here. " + SPOILER_RULE,
        "",
        "---",
        "",
    ]
    for e in eps:
        lines.append(f"**{e.code}** \u2014 {e.name}".rstrip(" \u2014"))
        lines.append("")
        if e.summary_text:
            lines.append("Synopsis (spoiler-tagged): " + _spoiler(e.summary_text))
        else:
            lines.append("_No synopsis available yet._")
        lines.append("")
    if source_url:
        lines.append(f"[Spoiler preview for today]({source_url}) (external; spoilers).")
    return title, "\n".join(lines).strip()


def build_spoiler(kind, date_obj):
    week = date_obj.strftime("%-d %B %Y")
    if kind == "articles":
        title = f"Spoilers \u2014 Magazine & Online Articles \u2014 Week of {week}"
        intro = ("Weekly thread for **magazine and online article spoilers**, which drop "
                 "around now. Robron don't always feature \u2014 drop anything in that does "
                 "as you spot it.")
    else:
        title = f"Spoilers \u2014 Spoiler Clips \u2014 Week of {week}"
        intro = ("Weekly thread for **spoiler clips**, which drop around now. Robron don't "
                 "always feature \u2014 post clips here when they do.")
    lines = [intro, "", SPOILER_RULE, "", "Please credit/link the original source where you can."]
    return title, "\n".join(lines).strip()


def _gemini_intro(kind):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    topic = {"episode": "tonight's episode discussion",
             "articles": "this week's magazine and online article spoilers",
             "clips": "this week's spoiler clips"}.get(kind, "the discussion")
    try:
        prompt = (f"Write ONE short, warm, spoiler-free sentence opening a fan thread for "
                  f"{topic} on the soap Emmerdale, focused on the couple 'Robron' (Robert "
                  f"Sugden and Aaron Dingle). Do NOT invent plot details, names or events. "
                  f"No emojis. Under 25 words.")
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_MODEL}:generateContent?key={key}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[warn] Gemini intro failed: {e}")
        return ""


def build_post(job, date_obj, eps=None, source_url=None):
    if job == "episode":
        title, body = build_episode(eps or [], date_obj, source_url)
    else:
        title, body = build_spoiler(job, date_obj)
    if USE_GEMINI_INTRO:
        intro = _gemini_intro(job)
        if intro:
            body = intro + "\n\n" + body
    return title, body


def make_reddit():
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDITPASSWORD"],
        user_agent=USER_AGENT,
    )
    reddit.validate_on_submit = True
    return reddit


def marker(date_str, job):
    return f"{MARKER_PREFIX}:{date_str}:{job}"


def already_posted(reddit, date_str, job):
    needle = marker(date_str, job)
    for submission in reddit.user.me().submissions.new(limit=DEDUPE_SCAN_LIMIT):
        if submission.subreddit.display_name.lower() != SUBREDDIT.lower():
            continue
        if needle in (submission.selftext or ""):
            return True
    return False


def _apply_flair(submission, flair_text):
    if not flair_text:
        return
    for template in submission.subreddit.flair.link_templates:
        if template.get("text", "").strip().lower() == flair_text.strip().lower():
            submission.flair.select(template["id"])
            return
    submission.mod.flair(text=flair_text)


def submit_thread(reddit, title, body, date_str, job):
    sub = reddit.subreddit(SUBREDDIT)
    full_body = f"{body}\n\n&#32;\n\n^({marker(date_str, job)})"
    submission = sub.submit(title=title, selftext=full_body, send_replies=False)
    try:
        submission.mod.suggested_sort(SUGGESTED_SORT)
    except Exception as e:
        print(f"[warn] suggested sort: {e}")
    flair = POST_FLAIR_EPISODE if job == "episode" else POST_FLAIR_SPOILER
    try:
        _apply_flair(submission, flair)
    except Exception as e:
        print(f"[warn] flair '{flair}': {e}")
    if STICKY:
        try:
            submission.mod.sticky(state=True, bottom=(STICKY_SLOT == 2))
        except Exception as e:
            print(f"[warn] sticky: {e}")
    return submission


def resolve_jobs(now):
    if JOB in ("episode", "articles", "clips"):
        if FORCE_WINDOW or JOB in decide_jobs(now):
            return [JOB]
        print(f"[skip] forced job '{JOB}' is outside its window/day (set FORCE_WINDOW=true).")
        return []
    if JOB == "auto":
        return decide_jobs(now)
    print(f"[error] invalid JOB={JOB!r}")
    return []


def run_job(reddit, job, now):
    date_str = now.strftime("%Y-%m-%d")
    source_url = None
    eps = None

    if job == "episode":
        on, source_url, reason = is_robron_day(now.date())
        print(f"[detect] Robron on {date_str}? {on} \u2014 {reason}")
        if not on:
            print("[skip] not detected as a Robron day; no episode thread.")
            return reddit
        try:
            eps = episodes_for_date(fetch_episodes(), date_str)
        except Exception as e:
            print(f"[warn] TVMaze enrichment failed, posting without it: {e}")
            eps = []

    title, body = build_post(job, now, eps, source_url)

    if DRY_RUN:
        print(f"=== DRY RUN [{job}] ===")
        print("TITLE:", title)
        print("---- BODY ----")
        print(body)
        return reddit

    if reddit is None:
        reddit = make_reddit()
    if already_posted(reddit, date_str, job):
        print(f"[skip] already posted {date_str}/{job}.")
        return reddit
    submission = submit_thread(reddit, title, body, date_str, job)
    print(f"[ok] posted [{job}]: https://redd.it/{submission.id}")
    return reddit


def main():
    now = now_local()
    jobs = resolve_jobs(now)
    if not jobs:
        print(f"[skip] {now:%a %H:%M %Z} \u2014 nothing scheduled.")
        return 0
    print(f"[info] UK {now:%a %Y-%m-%d %H:%M} \u2014 jobs: {', '.join(jobs)}")
    reddit = None
    for job in jobs:
        reddit = run_job(reddit, job, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
