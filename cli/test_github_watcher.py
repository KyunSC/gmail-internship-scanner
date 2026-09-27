"""Checks for github_watcher: schema normalization, the listing filter, and
the seen-set / ETag state across runs. No network — requests.get is faked.

    venv/bin/python -m unittest test_github_watcher -v
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import github_watcher as gw

NOW = time.time()
HOUR = 3600
SIMPLIFY, VANSH = gw.SOURCES


def simplify_row(title, company, locations, *, terms=("Summer 2027",),
                 category="Software", active=True, age=HOUR):
    return {"id": f"s-{company}-{title}", "company_name": company, "title": title,
            "locations": list(locations), "terms": list(terms), "category": category,
            "active": active, "is_visible": True, "date_posted": int(NOW - age),
            "url": f"https://jobs.example/{company}"}


def vansh_row(title, company, locations, *, season="Summer", age=HOUR):
    return {"id": f"v-{company}-{title}", "company_name": company, "title": title,
            "locations": list(locations), "season": season, "active": True,
            "is_visible": True, "date_posted": int(NOW - age),
            "url": f"https://jobs.example/{company}"}


SIMPLIFY_ROWS = [
    simplify_row("Software Engineer Intern", "Acme", ["Toronto, ON, Canada"]),        # keep
    simplify_row("Backend Developer Intern", "Beta", ["Remote in Canada"]),           # keep
    simplify_row("Firmware Engineer Intern", "Delta", ["Markham, ON, Canada"],
                 category="Hardware"),                                                # keep
    simplify_row("Software Engineer Intern", "Usco", ["Remote in USA"]),              # --anywhere only
    simplify_row("Software Engineer Intern", "Oldterm", ["Toronto, ON, Canada"],
                 terms=("Spring 2026",)),
    simplify_row("Software Engineer Intern", "Closed", ["Toronto, ON, Canada"], active=False),
    simplify_row("QA Automation Intern", "Testco", ["Toronto, ON, Canada"]),
    simplify_row("Analog Design Intern", "Chipco", ["Ottawa, ON, Canada"], category="Hardware"),
    simplify_row("Reporting Analyst Intern", "Insureco", ["Toronto, ON, Canada"],
                 category="AI/ML/Data"),
    simplify_row("Software Engineer Intern", "Stale", ["Toronto, ON, Canada"], age=60 * 86400),
]
VANSH_ROWS = [
    vansh_row("Software Engineer Intern", "Gamma", ["Montreal, Canada"]),             # keep
    vansh_row("Software Engineer Intern", "Acme", ["Toronto, ON"]),                   # dup of Acme
    vansh_row("Software Engineer Intern", "Fallco", ["Toronto, ON"], season="Fall"),
]
EXPECTED = {"Acme", "Beta", "Delta", "Gamma"}


def response(status, data=None, etag=None):
    resp = MagicMock(status_code=status, headers={"ETag": etag} if etag else {})
    resp.json.return_value = data
    resp.raise_for_status.side_effect = (
        requests.HTTPError(f"{status}") if status >= 400 else None)
    return resp


class FakeGitHub:
    """Serves each source's rows with a fixed ETag, and 304 when it's echoed."""

    def __init__(self, rows=None, etags=None, fail=()):
        self.rows = rows or {SIMPLIFY["url"]: SIMPLIFY_ROWS, VANSH["url"]: VANSH_ROWS}
        self.etags = etags or {SIMPLIFY["url"]: '"e-simplify"', VANSH["url"]: '"e-vansh"'}
        self.fail = set(fail)
        self.sent = []

    def __call__(self, url, headers=None, timeout=None):
        sent = (headers or {}).get("If-None-Match")
        self.sent.append((url, sent))
        if url in self.fail:
            raise requests.ConnectionError("network down")
        if sent and sent == self.etags[url]:
            return response(304)
        return response(200, self.rows[url], self.etags[url])


class NormalizeAndFilterTests(unittest.TestCase):
    def test_simplify_schema(self):
        l = gw.normalize_listing(SIMPLIFY_ROWS[0], SIMPLIFY)
        self.assertEqual(l["terms"], ["Summer 2027"])
        self.assertEqual((l["company"], l["category"], l["source"]), ("Acme", "Software", "simplify"))

    def test_vansh_season_gets_repo_year(self):
        l = gw.normalize_listing(VANSH_ROWS[0], VANSH)
        self.assertEqual(l["terms"], ["Summer 2027"])
        self.assertIsNone(l["category"])

    def wanted(self, anywhere=False):
        rows = [(r, SIMPLIFY) for r in SIMPLIFY_ROWS] + [(r, VANSH) for r in VANSH_ROWS]
        return {l["company"] for l in (gw.normalize_listing(r, s) for r, s in rows)
                if gw.is_wanted(l, 30, anywhere, NOW)}

    def test_filter_keeps_canadian_software_summer_2027(self):
        self.assertEqual(self.wanted(), EXPECTED)

    def test_anywhere_drops_only_the_location_rule(self):
        self.assertEqual(self.wanted(anywhere=True), EXPECTED | {"Usco"})


class WatchStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "seen.json"
        patcher = patch.object(gw, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_watch(self, fake, **kwargs):
        with patch.object(gw.requests, "get", side_effect=fake):
            return gw.watch(**kwargs)

    def test_first_run_shows_all_then_304_shows_none(self):
        fake = FakeGitHub()
        shown, statuses = self.run_watch(fake)
        self.assertEqual({l["company"] for l in shown}, EXPECTED)
        self.assertEqual(len(shown), len(EXPECTED), "cross-repo duplicate shown twice")
        self.assertEqual(statuses, {"simplify": "updated", "vansh": "updated"})

        fake.sent.clear()
        shown, statuses = self.run_watch(fake)
        self.assertEqual(shown, [])
        self.assertEqual(statuses, {"simplify": "unchanged", "vansh": "unchanged"})
        self.assertEqual({s for _, s in fake.sent}, {'"e-simplify"', '"e-vansh"'})

    def test_only_new_postings_after_repo_update(self):
        self.run_watch(FakeGitHub())
        new_row = simplify_row("Software Developer Intern", "Newco", ["Vancouver, BC, Canada"])
        fake = FakeGitHub(rows={SIMPLIFY["url"]: SIMPLIFY_ROWS + [new_row], VANSH["url"]: VANSH_ROWS},
                          etags={SIMPLIFY["url"]: '"e-simplify-2"', VANSH["url"]: '"e-vansh"'})
        shown, _ = self.run_watch(fake)
        self.assertEqual([l["company"] for l in shown], ["Newco"])

    def test_show_all_ignores_seen_and_etag(self):
        self.run_watch(FakeGitHub())
        fake = FakeGitHub()
        shown, _ = self.run_watch(fake, show_all=True)
        self.assertEqual({l["company"] for l in shown}, EXPECTED)
        self.assertEqual({s for _, s in fake.sent}, {None})

    def test_no_mark_writes_nothing(self):
        shown, _ = self.run_watch(FakeGitHub(), mark=False)
        self.assertEqual(len(shown), len(EXPECTED))
        self.assertFalse(self.state_path.exists())

    def test_failed_source_keeps_no_etag(self):
        fake = FakeGitHub(fail={VANSH["url"]})
        shown, statuses = self.run_watch(fake)
        self.assertTrue(statuses["vansh"].startswith("error"))
        self.assertEqual({l["company"] for l in shown}, EXPECTED - {"Gamma"})
        state = gw.load_state()
        self.assertIn("simplify", state["sources"])
        self.assertNotIn("vansh", state["sources"])

        # Next run retries vansh from scratch and surfaces what it missed.
        fake = FakeGitHub()
        shown, _ = self.run_watch(fake)
        self.assertEqual([l["company"] for l in shown], ["Gamma"])
        self.assertIn((VANSH["url"], None), fake.sent)

    def test_changed_filters_refetch_instead_of_trusting_304(self):
        self.run_watch(FakeGitHub())
        fake = FakeGitHub()
        shown, _ = self.run_watch(fake, anywhere=True)
        self.assertEqual({s for _, s in fake.sent}, {None})
        self.assertEqual([l["company"] for l in shown], ["Usco"])


if __name__ == "__main__":
    unittest.main()
