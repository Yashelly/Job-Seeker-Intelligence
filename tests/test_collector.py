from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cvbankas_tracker.collector import CvbankasCollector


class CvbankasCollectorTests(unittest.TestCase):
    def test_collect_listing_urls_from_multiple_pages(self) -> None:
        collector = CvbankasCollector()
        delayed_urls = []
        page_one = """
        <a class="list_a" href="https://www.cvbankas.lt/job-one/1-11111111">Job one</a>
        <a class="list_a" href="https://www.cvbankas.lt/job-two/1-22222222">Job two</a>
        """
        page_two = """
        <a class="list_a" href="https://www.cvbankas.lt/job-two/1-22222222">Job two</a>
        <a class="list_a" href="https://www.cvbankas.lt/job-three/1-33333333">Job three</a>
        """

        with patch.object(collector, "fetch_page", side_effect=[page_one, page_two]) as mocked_fetch:
            urls, page_urls = collector.collect_listing_urls_from_pages(
                keyword="python",
                max_pages=2,
                before_listing_fetch=delayed_urls.append,
            )

        self.assertEqual(len(page_urls), 2)
        self.assertEqual(delayed_urls, page_urls)
        self.assertEqual(len(urls), 3)
        self.assertEqual(
            urls,
            [
                "https://www.cvbankas.lt/job-one/1-11111111",
                "https://www.cvbankas.lt/job-two/1-22222222",
                "https://www.cvbankas.lt/job-three/1-33333333",
            ],
        )
        self.assertEqual(mocked_fetch.call_count, 2)
        self.assertIn("keyw=python", page_urls[0])
        self.assertIn("/darbas-darbas-namuose", page_urls[0])
        self.assertIn("page=2", page_urls[1])

    def test_build_listing_url_uses_cvbankas_remote_section(self) -> None:
        collector = CvbankasCollector()

        url = collector.build_listing_url(keyword="AI automation", page=2)

        self.assertIn("https://www.cvbankas.lt/darbas-darbas-namuose", url)
        self.assertIn("keyw=AI+automation", url)
        self.assertIn("page=2", url)

    def test_manual_cvbankas_listing_url_is_forced_to_remote_section(self) -> None:
        collector = CvbankasCollector()

        with patch.object(collector, "fetch_page", return_value="") as mocked_fetch:
            _, page_urls = collector.collect_listing_urls_from_pages(
                listing_url="https://www.cvbankas.lt/?keyw=n8n",
                max_pages=1,
            )

        self.assertEqual(page_urls[0], "https://www.cvbankas.lt/darbas-darbas-namuose?keyw=n8n")
        mocked_fetch.assert_called_once_with(page_urls[0])


if __name__ == "__main__":
    unittest.main()
