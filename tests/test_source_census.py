import unittest
from unittest.mock import patch

from foia_archive.source_census import (
    ProbeResult,
    attach_probe_results,
    build_component_census,
    looks_like_unclassified_source,
    probe_source,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, html=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = html.encode("utf-8")
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index:index + chunk_size]

    def close(self):
        self.closed = True


class SourceCensusCoverageTests(unittest.TestCase):
    def setUp(self):
        self.agencies = [
            {
                "id": "agency-1",
                "attributes": {"name": "Agency One"},
            }
        ]
        self.components = [
            {
                "id": "component-1",
                "attributes": {
                    "title": "Office With Library",
                    "reading_rooms": [
                        "https://agency.gov/foia/reading-room"
                    ],
                    "website": "https://agency.gov/",
                    "request_form": "https://agency.gov/foia/request",
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-1"}}
                },
            },
            {
                "id": "component-2",
                "attributes": {
                    "title": "Likely Classifier Gap",
                    "website": "https://agency.gov/foia/library",
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-1"}}
                },
            },
            {
                "id": "component-3",
                "attributes": {
                    "title": "Request Only",
                    "request_form": "https://agency.gov/foia/request-only",
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-1"}}
                },
            },
        ]

    def test_census_separates_recognized_sources_from_gap_candidates(self):
        census = build_component_census(
            self.agencies,
            self.components,
            self.agencies,
        )

        summary = census["summary"]
        self.assertEqual(summary["components"], 3)
        self.assertEqual(
            summary["components_with_recognized_source"],
            1,
        )
        self.assertEqual(
            summary["components_without_recognized_source"],
            2,
        )
        self.assertEqual(
            summary["components_without_source_but_candidate_url"],
            1,
        )
        self.assertEqual(summary["unique_source_urls"], 1)

        gap = next(
            row
            for row in census["components"]
            if row["component_id"] == "component-2"
        )
        self.assertEqual(
            gap["candidate_ignored_urls"],
            [
                {
                    "field_path": "website",
                    "url": "https://agency.gov/foia/library",
                }
            ],
        )

        request_only = next(
            row
            for row in census["components"]
            if row["component_id"] == "component-3"
        )
        self.assertEqual(
            request_only["candidate_ignored_urls"],
            [],
        )

    def test_candidate_heuristic_excludes_request_forms(self):
        self.assertFalse(
            looks_like_unclassified_source(
                "request_form",
                "https://agency.gov/foia/library/request",
            )
        )
        self.assertTrue(
            looks_like_unclassified_source(
                "website",
                "https://agency.gov/foia/reading-room",
            )
        )


class SourceProbeTests(unittest.TestCase):
    def probe_with_response(self, response, final_url="https://agency.gov/foia"):
        with patch(
            "foia_archive.source_census._request_with_safe_redirects",
            return_value=(response, final_url),
        ):
            return probe_source(
                "https://agency.gov/foia",
                user_agent="test",
                rate_limiter=None,
            )

    def test_direct_document_links_classify_source_as_document_producing(self):
        response = FakeResponse(
            headers={"Content-Type": "text/html"},
            html='<a href="/foia/report.pdf">Report</a>',
        )

        result = self.probe_with_response(response)

        self.assertEqual(result.category, "document_producing")
        self.assertEqual(result.direct_document_links, 1)
        self.assertTrue(response.closed)

    def test_dynamic_platform_hint_becomes_adapter_candidate(self):
        response = FakeResponse(
            headers={"Content-Type": "text/html"},
            html=(
                '<form action="/app/search"><input name="q"></form>'
                '<input type="hidden" name="__VIEWSTATE" value="abc">'
            ),
        )

        result = self.probe_with_response(
            response,
            "https://agency.gov/app/ReadingRoom.aspx",
        )

        self.assertEqual(result.category, "adapter_candidate")
        self.assertIn("__viewstate", result.adapter_hints)
        self.assertIn("readingroom.aspx", result.adapter_hints)

    def test_blocked_source_is_separate_from_discovery_coverage(self):
        response = FakeResponse(
            status_code=403,
            headers={"Content-Type": "text/html"},
        )

        result = self.probe_with_response(response)

        self.assertEqual(result.category, "blocked")
        self.assertEqual(result.status_code, 403)

    def test_attach_probe_results_counts_categories(self):
        census = {
            "summary": {},
            "components": [],
            "source_instances": [],
            "ignored_url_field_counts": {},
            "candidate_ignored_field_counts": {},
        }
        probes = {
            "https://one.gov/": ProbeResult(
                url="https://one.gov/",
                final_url="https://one.gov/",
                category="document_producing",
                status_code=200,
                mime_type="text/html",
                direct_document_links=2,
                crawlable_page_links=1,
                adapter_hints=(),
                error=None,
            ),
            "https://two.gov/": ProbeResult(
                url="https://two.gov/",
                final_url="https://two.gov/",
                category="blocked",
                status_code=403,
                mime_type="text/html",
                direct_document_links=0,
                crawlable_page_links=0,
                adapter_hints=(),
                error=None,
            ),
        }

        enriched = attach_probe_results(census, probes)

        self.assertEqual(
            enriched["probe_summary"]["category_counts"],
            {"blocked": 1, "document_producing": 1},
        )


if __name__ == "__main__":
    unittest.main()
