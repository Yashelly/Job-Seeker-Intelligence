from __future__ import annotations

import unittest

from cvbankas_tracker.sources.base import CollectionCancelledError
from cvbankas_tracker.sources.career_seb import collect_seb


def _item(number: int) -> dict:
    return {
        "title": f"Engineer {number}",
        "location": "Vilnius",
        "url": f"/career/find-your-new-job/our-vacant-positions/engineer-{number}",
    }


def _detail(title: str = "Engineer") -> str:
    return f"""<html><div class='pw-lever-description__details'>
    <p>{title} builds secure daily vacancy collection systems for our teams.</p>
    <p>Responsibilities include maintaining reliable services, collaborating with colleagues,
    reviewing operational data, and improving the candidate experience across regions.</p>
    </div><footer>Footer navigation must not be treated as a job description.</footer></html>"""


class SebCareerTests(unittest.TestCase):
    def test_collects_post_pages_and_official_details(self) -> None:
        calls: list[tuple[str, dict | None]] = []

        def fetch_json(url: str, payload=None, headers=None):
            del headers
            calls.append((url, payload))
            offset = payload["offset"]
            return {"totalHits": 101, "leverListItems": [_item(index) for index in range(offset, min(offset + 100, 101))]}

        jobs, pages, incomplete = collect_seb({}, 2, fetch_json, lambda url: _detail(url.rsplit("-", 1)[-1]))

        self.assertEqual(len(jobs), 101)
        self.assertFalse(incomplete)
        self.assertEqual(calls[0][0], "https://sebgroup.com/api/lever-v2/list")
        self.assertEqual(calls[0][1]["hits"], 100)
        self.assertEqual(calls[1][1]["offset"], 100)
        self.assertEqual(jobs[0]["url"], "https://sebgroup.com/career/find-your-new-job/our-vacant-positions/engineer-0")
        self.assertNotIn("Footer navigation", jobs[0]["description"])
        self.assertEqual(len(pages), 103)

    def test_marks_failed_details_partial_and_reraises_cancellation(self) -> None:
        def fetch_json(url: str, payload=None, headers=None):
            del url, payload, headers
            return {"totalHits": 2, "leverListItems": [_item(1), _item(2)]}

        def one_failure(url: str) -> str:
            if url.endswith("engineer-2"):
                raise OSError("temporary failure")
            return _detail()

        jobs, _pages, incomplete = collect_seb({}, 1, fetch_json, one_failure)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(incomplete)

        with self.assertRaises(CollectionCancelledError):
            collect_seb({}, 1, fetch_json, lambda _url: (_ for _ in ()).throw(CollectionCancelledError("stop")))

    def test_listing_cap_is_incomplete(self) -> None:
        def fetch_json(url: str, payload=None, headers=None):
            del url, headers
            return {"totalHits": 101, "leverListItems": [_item(index) for index in range(payload["offset"], payload["offset"] + 100)]}

        jobs, _pages, incomplete = collect_seb({}, 1, fetch_json, lambda _url: _detail())
        self.assertEqual(len(jobs), 100)
        self.assertTrue(incomplete)
