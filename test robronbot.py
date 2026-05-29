"""
Built-in test suite for robronbot.

Offline tests run with no network and no Reddit/Gemini secrets -- they exercise
the real detection, scheduling and post-building code against fixtures. Live
tests (gated by LIVE_TESTS=1) actually fetch the spoiler site and TVMaze to
confirm the real integrations still parse.

Run:
    python -m unittest test_robronbot -v          # offline only
    LIVE_TESTS=1 python -m unittest test_robronbot -v   # + live checks
"""

import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import robronbot as rb

UK = ZoneInfo("Europe/London")

# A synthetic index mirroring the real Emmerdale Insider page structure:
# a dated anchor + a headline anchor pointing at the same spoiler URL.
INDEX = """
<html><body>
<a href="https://x/emmerdale-insider/spoilers/gabby-ross-jai/"><img/> Fri, May 29</a>
<h3><a href="https://x/emmerdale-insider/spoilers/gabby-ross-jai/">2 spoilers for tonight: Gabby puts it all out there for Ross and Jai grovels</a></h3>

<a href="https://x/emmerdale-insider/spoilers/robron-find-out/"><img/> Thu, May 28</a>
<h3><a href="https://x/emmerdale-insider/spoilers/robron-find-out/">3 spoilers: Robert and Aaron are determined to find out the truth</a></h3>

<a href="https://x/emmerdale-insider/spoilers/kammy-belle/"><img/> Wed, May 27</a>
<h3><a href="https://x/emmerdale-insider/spoilers/kammy-belle/">3 spoilers for Wed, May 27: Kammy caught out and Ross wants Laurel</a></h3>

<a href="https://x/emmerdale-insider/spoilers/next-week-roundup/"><img/> Jun 1 - 5</a>
<h3><a href="https://x/emmerdale-insider/spoilers/next-week-roundup/">11 spoilers for next week</a></h3>

<a href="https://x/emmerdale-insider/category/spoilers/">Spoilers</a>
</body></html>
"""

ARTICLES = {
    "https://x/emmerdale-insider/spoilers/gabby-ross-jai/":
        "<p>Gabby confronts Ross at the cafe. Jai apologises to Laurel.</p>",
    "https://x/emmerdale-insider/spoilers/kammy-belle/":
        "<p>Kammy snaps at Belle. Robert overhears and tells Aaron that evening.</p>",
}


def fake_fetch(url):
    return ARTICLES.get(url, "<p>nothing here</p>")


class TestDateParsing(unittest.TestCase):
    def test_simple(self):
        ref = date(2026, 5, 29)
        self.assertEqual(rb.parse_label_date("Fri, May 29", ref), date(2026, 5, 29))
        self.assertEqual(rb.parse_label_date("Thu, May 28", ref), date(2026, 5, 28))

    def test_ranges_and_junk_rejected(self):
        ref = date(2026, 5, 29)
        self.assertIsNone(rb.parse_label_date("Jun 1 - 5", ref))
        self.assertIsNone(rb.parse_label_date("Upcoming", ref))
        self.assertIsNone(rb.parse_label_date("", ref))

    def test_year_rollover(self):
        self.assertEqual(rb.parse_label_date("Sat, Jan 2", date(2026, 12, 31)), date(2027, 1, 2))


class TestNameDetection(unittest.TestCase):
    def test_positive(self):
        self.assertTrue(rb.has_robron("Robert and Aaron catch Kammy"))
        self.assertTrue(rb.has_robron("aaron's big secret"))
        self.assertTrue(rb.has_robron("Robron reunite"))

    def test_negative(self):
        self.assertFalse(rb.has_robron("Kammy arrested and Laurel can't stay away from Ross"))
        self.assertFalse(rb.has_robron("Cain Dingle leaves Victoria devastated"))


class TestSpoilerIndexParsing(unittest.TestCase):
    def test_entries_and_dates(self):
        entries = rb.parse_spoiler_index(INDEX, date(2026, 5, 29))
        by_date = {e["date"]: u for u, e in entries.items() if e["date"]}
        self.assertIn(date(2026, 5, 29), by_date)
        self.assertIn(date(2026, 5, 28), by_date)
        # The category link must not be treated as a spoiler entry.
        self.assertNotIn("https://x/emmerdale-insider/category/spoilers/", entries)


class TestDetectRobron(unittest.TestCase):
    def test_named_in_headline(self):
        state, _, reason = rb.detect_robron(date(2026, 5, 28), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, True)
        self.assertIn("headline", reason)

    def test_named_in_body(self):
        state, _, reason = rb.detect_robron(date(2026, 5, 27), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, True)
        self.assertIn("body", reason)

    def test_found_but_not_named(self):
        state, _, reason = rb.detect_robron(date(2026, 5, 29), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIs(state, False)

    def test_no_entry(self):
        state, _, _ = rb.detect_robron(date(2026, 5, 30), index_html=INDEX, article_fetcher=fake_fetch)
        self.assertIsNone(state)


class TestOverride(unittest.TestCase):
    def setUp(self):
        self._orig = rb.ROBRON_OVERRIDE

    def tearDown(self):
        rb.ROBRON_OVERRIDE = self._orig

    def test_on(self):
        rb.ROBRON_OVERRIDE = "on"
        self.assertTrue(rb.is_robron_day(date(2030, 1, 1))[0])

    def test_off(self):
        rb.ROBRON_OVERRIDE = "off"
        self.assertFalse(rb.is_robron_day(date(2030, 1, 1))[0])


class TestScheduling(unittest.TestCase):
    def test_episode_window(self):
        self.assertEqual(rb.decide_jobs(datetime(2026, 5, 29, 7, 10, tzinfo=UK)), ["episode"])

    def test_outside_window(self):
        self.assertEqual(rb.decide_jobs(datetime(2026, 5, 29, 12, 0, tzinfo=UK)), [])

    def test_tuesday_articles(self):
        d = datetime(2026, 6, 2, 0, 20, tzinfo=UK)
        self.assertEqual(d.weekday(), 1)
        self.assertEqual(rb.decide_jobs(d), ["articles"])

    def test_saturday_clips(self):
        d = datetime(2026, 5, 30, 0, 30, tzinfo=UK)
        self.assertEqual(d.weekday(), 5)
        self.assertEqual(rb.decide_jobs(d), ["clips"])

    def test_midnight_window_closes(self):
        self.assertEqual(rb.decide_jobs(datetime(2026, 6, 2, 2, 0, tzinfo=UK)), [])


class TestBuilds(unittest.TestCase):
    def test_episode_with_enrichment(self):
        eps = [rb.Episode(55, 108, "2026-05-29", "Friday", "Robert makes a decision that stuns Aaron.")]
        title, body = rb.build_episode(eps, datetime(2026, 5, 29, 7, 0, tzinfo=UK), source_url="https://x/spoiler/")
        self.assertIn("S55E108", title)
        self.assertIn(">!", body)
        self.assertIn("Robert makes a decision that stuns Aaron.", body)
        self.assertIn("YouTube", body)
        self.assertIn("https://x/spoiler/", body)

    def test_episode_double_bill(self):
        eps = [rb.Episode(55, 108, "2026-05-29", "Ep1", ""), rb.Episode(55, 109, "2026-05-29", "Ep2", "")]
        title, _ = rb.build_episode(eps, datetime(2026, 5, 29, 7, 0, tzinfo=UK))
        self.assertIn("S55E108 & S55E109", title)

    def test_episode_no_enrichment(self):
        title, body = rb.build_episode([], datetime(2026, 5, 29, 7, 0, tzinfo=UK))
        self.assertIn("Emmerdale Discussion", title)
        self.assertNotIn("S55", title)

    def test_spoiler_articles(self):
        title, _ = rb.build_spoiler("articles", datetime(2026, 6, 2, 0, 0, tzinfo=UK))
        self.assertIn("Magazine & Online Articles", title)
        self.assertIn("Week of", title)

    def test_spoiler_clips(self):
        title, _ = rb.build_spoiler("clips", datetime(2026, 5, 30, 0, 0, tzinfo=UK))
        self.assertIn("Spoiler Clips", title)

    def test_markers_distinct(self):
        self.assertEqual(rb.marker("2026-05-29", "episode"), "emmerbot:2026-05-29:episode")
        self.assertNotEqual(rb.marker("2026-05-29", "episode"), rb.marker("2026-05-29", "articles"))


@unittest.skipUnless(os.environ.get("LIVE_TESTS") == "1", "set LIVE_TESTS=1 to run live network checks")
class TestLive(unittest.TestCase):
    """Hits the real spoiler site and TVMaze. Run on demand to confirm parsing."""

    def test_tvmaze_returns_episodes(self):
        eps = rb.fetch_episodes()
        self.assertGreater(len(eps), 100)
        self.assertTrue(any(e.season >= 50 for e in eps), "expected a recent Emmerdale season")

    def test_spoiler_index_parses_recent_dates(self):
        resp = rb._http_get(rb.SPOILER_INDEX_URL)
        self.assertEqual(resp.status_code, 200, f"index returned HTTP {resp.status_code}")
        entries = rb.parse_spoiler_index(resp.text, date.today())
        dated = [e["date"] for e in entries.values() if e["date"]]
        self.assertTrue(dated, "no dated spoiler entries parsed -- the page layout may have changed")
        # At least one entry should be within ~2 weeks of today.
        near = [d for d in dated if abs((d - date.today()).days) <= 14]
        self.assertTrue(near, f"no spoiler dates near today; parsed dates: {sorted(dated)[:5]}")

    def test_detect_today_runs_cleanly(self):
        # We don't assert the outcome (depends on the schedule) -- only that the
        # full detection path executes against live data without error.
        state, url, reason = rb.detect_robron(date.today())
        print(f"\n[LIVE] today detection -> state={state} reason={reason!r} url={url}")
        self.assertIn(state, (True, False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
