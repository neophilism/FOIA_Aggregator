import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.discovery import extract_reading_room_sources, refresh_metadata
from foia_archive.intelligence_community_sources import (
    normalized_ic_elements,
    unique_ic_source_urls,
)
from foia_archive.source_census import (
    ProbeResult,
    attach_probe_results,
)
from foia_archive.storage import get_connection, init_db
from foia_archive.utils import Config


class IntelligenceCommunityRegistryTests(unittest.TestCase):
    def test_registry_contains_exactly_18_unique_elements(self):
        elements = normalized_ic_elements()

        self.assertEqual(len(elements), 18)
        self.assertEqual(len({item["slug"] for item in elements}), 18)
        self.assertTrue(all(item["sources"] for item in elements))

    def test_key_subelements_have_explicit_sources(self):
        elements = {
            item["slug"]: item
            for item in normalized_ic_elements()
        }

        army_urls = {
            source["url"]
            for source in elements["army-intelligence"]["sources"]
        }
        navy_urls = {
            source["url"]
            for source in elements["naval-intelligence"]["sources"]
        }
        state_urls = {
            source["url"]
            for source in elements["state-inr"]["sources"]
        }

        self.assertIn("https://www.usainscom.army.mil/FOIA/", army_urls)
        self.assertIn("https://foia.army.mil/", army_urls)
        self.assertIn(
            "https://www.oni.navy.mil/Contact-Us/Freedom-of-Information-Act/Reading-Room/",
            navy_urls,
        )
        self.assertIn(
            "https://foia.state.gov/FOIALIBRARY/SearchResults.aspx",
            state_urls,
        )

    def test_daf_reading_room_is_shared_by_air_and_space_force(self):
        elements = {
            item["slug"]: item
            for item in normalized_ic_elements()
        }
        air = {
            source["url"]
            for source in elements["air-force-intelligence"]["sources"]
        }
        space = {
            source["url"]
            for source in elements["space-force-intelligence"]["sources"]
        }

        self.assertEqual(
            air,
            {"https://efoia.cce.af.mil/app/ReadingRoom.aspx"},
        )
        self.assertEqual(air, space)

    def test_stale_foia_urls_normalize_to_current_sources(self):
        sources = extract_reading_room_sources(
            {
                "reading_rooms": [
                    "https://www.cia.gov/library/readingroom/",
                    "https://www.cia.gov/library/readingroom/what-electronic-reading-room",
                    "https://www.rmda.army.mil/readingroom/",
                ]
            }
        )

        self.assertEqual(
            {item["url"] for item in sources},
            {
                "https://www.cia.gov/readingroom/",
                "https://foia.army.mil/",
            },
        )

    def test_unique_ic_source_urls_are_http_sources(self):
        urls = unique_ic_source_urls()

        self.assertGreaterEqual(len(urls), 18)
        self.assertTrue(all(url.startswith("https://") for url in urls))


class IntelligenceCommunityDiscoveryTests(unittest.TestCase):
    def test_refresh_adds_supplemental_ic_sources_even_when_foia_gov_lacks_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "archive.db"
            files_dir = root / "files"
            init_db(db_path, files_dir)
            config = Config(
                {
                    "crawler": {"user_agent": "test"},
                    "foia_hub": {
                        "base_url": "https://api.foia.gov/api",
                        "timeout_seconds": 1,
                        "api_key": "test",
                        "max_retries": 0,
                    },
                    "storage": {
                        "db_path": str(db_path),
                        "files_dir": str(files_dir),
                    },
                }
            )

            with (
                patch(
                    "foia_archive.discovery.fetch_agencies",
                    return_value=[],
                ),
                patch(
                    "foia_archive.discovery.fetch_agency_components",
                    return_value=([], []),
                ),
            ):
                refresh_metadata(config)

            conn = get_connection(db_path)
            try:
                urls = {
                    row["url"]
                    for row in conn.execute(
                        "SELECT url FROM reading_rooms WHERE active = 1"
                    ).fetchall()
                }
            finally:
                conn.close()

        self.assertIn("https://www.usainscom.army.mil/FOIA/", urls)
        self.assertIn(
            "https://www.oni.navy.mil/Contact-Us/Freedom-of-Information-Act/Reading-Room/",
            urls,
        )
        self.assertIn(
            "https://foia.state.gov/FOIALIBRARY/SearchResults.aspx",
            urls,
        )


class IntelligenceCommunityCensusTests(unittest.TestCase):
    def test_probe_summary_reports_element_level_reachability(self):
        elements = normalized_ic_elements()
        probe_results = {}
        for url in unique_ic_source_urls():
            probe_results[url] = ProbeResult(
                url=url,
                final_url=url,
                category="document_producing",
                status_code=200,
                mime_type="text/html",
                direct_document_links=1,
                crawlable_page_links=1,
                adapter_hints=(),
                error=None,
            )

        nsa_url = next(
            source["url"]
            for element in elements
            if element["slug"] == "nsa"
            for source in element["sources"]
        )
        probe_results[nsa_url] = ProbeResult(
            url=nsa_url,
            final_url=nsa_url,
            category="blocked",
            status_code=403,
            mime_type="text/html",
            direct_document_links=0,
            crawlable_page_links=0,
            adapter_hints=(),
            error=None,
        )

        census = {
            "summary": {},
            "components": [],
            "source_instances": [],
            "ignored_url_field_counts": {},
            "candidate_ignored_field_counts": {},
            "intelligence_community": elements,
        }
        enriched = attach_probe_results(census, probe_results)

        self.assertEqual(enriched["ic_summary"]["elements"], 18)
        self.assertEqual(
            enriched["ic_summary"]["elements_with_sources"],
            18,
        )
        self.assertEqual(
            enriched["ic_summary"]["reachable_elements"],
            17,
        )
        self.assertEqual(
            enriched["ic_summary"]["known_unreachable_elements"],
            1,
        )
        nsa = next(
            row
            for row in enriched["intelligence_community"]
            if row["slug"] == "nsa"
        )
        self.assertEqual(nsa["coverage_status"], "known_unreachable")
        self.assertEqual(nsa["probe_categories"], ["blocked"])


if __name__ == "__main__":
    unittest.main()
