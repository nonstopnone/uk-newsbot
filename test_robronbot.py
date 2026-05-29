"""Built-in test suite for robronbot. Offline by default; LIVE_TESTS=1 hits the real site."""
import os
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import robronbot as rb

UK = ZoneInfo("Europe/London")


# Fixture pages -----------------------------------------------------------

SPOILER_INDEX = """
<html><body>
<a href="https://x.test/emmerdale-insider/spoilers/today/"><img/> Fri, May 29</a>
<h3><a href="https://x.test/emmerdale-insider/spoilers/today/">3 spoilers for tonight</a></h3>
<a href="https://x.test/emmerdale-insider/spoilers/next-week-roundup/"><img/> Jun 1 - 5</a>
<h3><a href="https://x.test/emmerdale-insider/spoilers/next-week-roundup/">11 spoilers for next week</a></h3>
</body></html>
"""

NEWS_INDEX = """
<html><body>
<a href="https://x.test/emmerdale-insider/news/robron-clue-fires/">Fans spot huge clue revealing who's starting farm fires (Robert and Aaron)</a>
<a href="https://x.test/emmerdale-insider/news/cain-arrested/">Cain arrested in shock twist</a>
<a href="https://x.test/emmerdale-insider/category/news/">News</a>
</body></html>
"""

RECAPS_INDEX = """
<html><body>
<a href="https://x.test/emmerdale-insider/episode-recaps/sugdens-united/">Robert and Aaron reunite at the Woolpack</a>
<a href="https://x.test/emmerdale-insider/episode-recaps/old-thing/">Old article about Charity</a>
</body></html>
"""

def mkarticle(title, body, pubdate="2026-05-29T08:00:00Z"):
    return f"""<html><head><title>{title} | TV Guide</title>
    <meta property="article:published_time" content="{pubdate}"/>
    <meta name="description" content="{body[:200]}"/></head>
    <body><article><p>{body}</p></article></body></html>"""

ARTICLES = {
    "https://x.test/emmerdale-insider/spoilers/today/":
        mkarticle("3 spoilers for tonight",
                  "Cain confronts Ross at the Woolpack."),
    "https://x.test/emmerdale-insider/spoilers/next-week-roundup/":
        mkarticle("Spoilers for next week",
                  "Todd exposes a secret."),
    "https://x.test/emmerdale-insider/news/robron-clue-fires/":
        mkarticle("Fans spot huge clue about farm fires",
                  "Robert and Aaron are desperately hunting the culprit."),
    "https://x.test/emmerdale-insider/news/cain-arrested/":
        mkarticle("Cain arrested",
                  "Cain Dingle taken into custody after the fire."),
    "https://x.test/emmerdale-insider/episode-recaps/sugdens-united/":
        mkarticle("Sugdens united",
                  "Robert Sugden and Aaron Dingle share a tender moment."),
    "https://x.test/emmerdale-insider/episode-recaps/old-thing/":
        mkarticle("Old Charity story",
                  "Charity reflects on the year.",
                  pubdate="2026-04-01T08:00:00Z"),
}

class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

def fake_http(url):
    if url == rb.SPOILER_INDEX_URL or url.endswith("/spoilers/"):
        return _FakeResp(200, SPOILER_INDEX)
    if url.endswith("/news/"):
        return _FakeResp(200, NEWS_INDEX)
    if url.endswith("/episode-recaps/"):
        return _FakeResp(200, RECAPS_INDEX)
    if url in ARTICLES:
        return _FakeResp(200, ARTICLES[url])
    return _FakeResp(404, "")


# --- core unit tests ------------------------------------------------------

class TestNameDetection(unittest.TestCase):
    def test_positive(self):
        self.assertTrue(rb.has_robron("Robert and Aaron catch Kammy"))
        self.assertTrue(rb.has_robron("Robron reunite"))
        self.assertTrue(rb.has_robron("aaron dingle and his mum"))

    def test_negative(self):
        self.assertFalse(rb.has_robron("Kammy arrested and Laurel can't stay away from Ross"))
        # Dingle alone shouldn't trigger (Cain, Belle, Sam, etc.)
        self.assertFalse(rb.has_robron("Cain Dingle leaves Victoria devastated"))


class TestForwardSpoiler(unittest.TestCase):
    def test_finds_forward(self):
        url, _ = rb.get_forward_spoiler(SPOILER_INDEX, date(2026, 5, 29))
        self.assertIn("next-week-roundup", url)

    def test_none_when_only_past(self):
        past_only = '<a href="https://x.test/emmerdale-insider/spoilers/old/"><img/> Mon, May 25</a><h3><a href="https://x.test/emmerdale-insider/spoilers/old/">Cain arrested</a></h3>'
        self.assertIsNone(rb.get_forward_spoiler(past_only, date(2026, 5, 29))[0])


class TestScheduling(unittest.TestCase):
    def test_weekday_7am(self):
        jobs = rb.decide_jobs(datetime(2026, 5, 29, 7, 5, tzinfo=UK))  # Fri
        self.assertIn("post_episode", jobs)
        self.assertIn("sweep", jobs)

    def test_weekend_7am_no_post_ep(self):
        jobs = rb.decide_jobs(datetime(2026, 5, 30, 7, 5, tzinfo=UK))  # Sat
        self.assertNotIn("post_episode", jobs)
        self.assertIn("sweep", jobs)

    def test_evening_only_sweep(self):
        jobs = rb.decide_jobs(datetime(2026, 5, 29, 18, 20, tzinfo=UK))
        self.assertEqual(jobs, ["sweep"])

    def test_midday_nothing(self):
        self.assertEqual(rb.decide_jobs(datetime(2026, 5, 29, 12, 0, tzinfo=UK)), [])


class TestSweep(unittest.TestCase):
    def test_finds_robron_articles_only(self):
        mentions = rb.sweep_robron_mentions(date(2026, 5, 29), http_get=fake_http)
        urls = sorted(m.url for m in mentions)
        self.assertIn("https://x.test/emmerdale-insider/news/robron-clue-fires/", urls)
        self.assertIn("https://x.test/emmerdale-insider/episode-recaps/sugdens-united/", urls)
        self.assertNotIn("https://x.test/emmerdale-insider/news/cain-arrested/", urls)
        # Old article filtered out by age.
        self.assertNotIn("https://x.test/emmerdale-insider/episode-recaps/old-thing/", urls)

    def test_handles_section_404(self):
        def http(url):
            if url == rb.SPOILER_INDEX_URL or url.endswith("/spoilers/"):
                return _FakeResp(403, "")
            return fake_http(url)
        mentions = rb.sweep_robron_mentions(date(2026, 5, 29), http_get=http)
        # Still finds the news + recaps mentions even if spoilers section is blocked.
        self.assertTrue(any("robron-clue-fires" in m.url for m in mentions))


class TestBuilds(unittest.TestCase):
    def test_post_episode_build(self):
        title, body = rb.build_post_episode(datetime(2026, 5, 29, 7, 0, tzinfo=UK))
        self.assertEqual(title, "Post-Episode Discussion for Friday 29 May 2026")
        self.assertIn("early release on YouTube", body)

    def test_spoilers_has_working_cover(self):
        _, body = rb.build_spoilers("Todd exposes a secret",
                                    "https://x.test/emmerdale-insider/spoilers/next-week-roundup/",
                                    datetime(2026, 5, 29, 18, 0, tzinfo=UK))
        self.assertIn(">!", body)
        self.assertIn("!<", body)
        self.assertIn("next-week-roundup", body)

    def test_mentions_comment_format(self):
        m = rb.RobronMention(
            url="https://x.test/emmerdale-insider/news/robron-clue-fires/",
            title="Fans spot huge clue about farm fires",
            snippet="Robert and Aaron are desperately hunting the culprit.",
            pubdate=date(2026, 5, 29),
        )
        body = rb.build_mentions_comment([m])
        self.assertIn(m.url, body)
        self.assertIn(">!", body)  # snippet covered
        self.assertIn("Robert and Aaron", body)


class _FakeSub:
    def __init__(self, title="", selftext=""):
        self.title = title
        self.selftext = selftext
        self.subreddit = type("S", (), {"display_name": rb.SUBREDDIT})()

class _FakeMe:
    def __init__(self, subs): self._subs = subs
    @property
    def submissions(self):
        outer = self
        class _S:
            def new(self, limit=80): return list(outer._subs)
        return _S()

class _FakeReddit:
    def __init__(self, subs): self._me = _FakeMe(subs)
    @property
    def user(self):
        outer = self
        class _U:
            def me(self): return outer._me
        return _U()


class TestDedupe(unittest.TestCase):
    def test_post_episode_dedupe(self):
        now = datetime(2026, 5, 29, 7, 0, tzinfo=UK)
        prefix = rb.post_episode_title_prefix(now)
        self.assertTrue(rb.post_episode_already_posted(
            _FakeReddit([_FakeSub(title=prefix)]), prefix))
        self.assertFalse(rb.post_episode_already_posted(
            _FakeReddit([_FakeSub(title="something else")]), prefix))

    def test_find_today_post_episode(self):
        now = datetime(2026, 5, 29, 7, 0, tzinfo=UK)
        prefix = rb.post_episode_title_prefix(now)
        target = _FakeSub(title=prefix)
        found = rb.find_today_post_episode(_FakeReddit([target]), now)
        self.assertIs(found, target)
        self.assertIsNone(rb.find_today_post_episode(_FakeReddit([_FakeSub(title="x")]), now))

    def test_spoiler_dedupe_by_url(self):
        url = "https://x.test/emmerdale-insider/spoilers/next-week-roundup/"
        self.assertTrue(rb.spoiler_already_posted(
            _FakeReddit([_FakeSub(selftext=f"...({url})...")]), url))
        self.assertFalse(rb.spoiler_already_posted(
            _FakeReddit([_FakeSub(selftext="nothing")]), url))


@unittest.skipUnless(os.environ.get("LIVE_TESTS") == "1", "set LIVE_TESTS=1")
class TestLive(unittest.TestCase):
    def test_tvmaze(self):
        eps = rb.fetch_episodes()
        self.assertGreater(len(eps), 100)

    def test_spoiler_index(self):
        resp = rb._http_get(rb.SPOILER_INDEX_URL)
        self.assertEqual(resp.status_code, 200)
        recs = rb._index_records(resp.text, date.today())
        self.assertTrue([r for r in recs if r["date"]])

    def test_sweep_runs_cleanly(self):
        mentions = rb.sweep_robron_mentions(date.today())
        print(f"\n[LIVE] sweep found {len(mentions)} Robron mention(s):")
        for m in mentions[:5]:
            print(f"  - {m.title[:70]}")
            print(f"    {m.url}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
