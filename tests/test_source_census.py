import unittest
from unittest.mock import patch

from foia_archive.scraper_core import FileTooLarge
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
                    "resources": "https://agency.gov/records/foia-archive",
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
                    "field_path": "resources",
                    "url": "https://agency.gov/records/foia-archive",
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

    def test_curated_component_closes_metadata_gap(self):
        agencies = [
            {
                "id": "agency-curated",
                "attributes": {"name": "Election Assistance Commission"},
            }
        ]
        components = [
            {
                "id": "c1efb796-3bb7-4747-a8b7-415992834318",
                "attributes": {
                    "title": "U.S. Election Assistance Commission",
                    "website": {"uri": "https://www.eac.gov/"},
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-curated"}}
                },
            }
        ]

        census = build_component_census(
            agencies,
            components,
            agencies,
        )

        self.assertEqual(
            census["summary"]["current_agencies_without_recognized_source"],
            0,
        )
        self.assertEqual(
            census["components"][0]["recognized_sources"][0]["url"],
            "https://www.eac.gov/foia/foia-reading-room",
        )
        self.assertEqual(
            census["components"][0]["recognized_sources"][0]["source_type"],
            "curated_foia",
        )

    def test_historical_component_is_not_counted_as_current_gap(self):
        agencies = [
            {
                "id": "agency-historical",
                "attributes": {
                    "name": "Recovery Accountability and Transparency Board"
                },
            }
        ]
        components = [
            {
                "id": "034ea4e5-220d-497d-9e69-1889f005bc81",
                "attributes": {
                    "title": "Recovery Accountability and Transparency Board"
                },
                "relationships": {
                    "agency": {"data": {"id": "agency-historical"}}
                },
            }
        ]

        census = build_component_census(
            agencies,
            components,
            agencies,
        )

        self.assertEqual(census["summary"]["historical_agencies"], 1)
        self.assertEqual(census["summary"]["current_agencies"], 0)
        self.assertEqual(
            census["summary"]["current_agencies_without_recognized_source"],
            0,
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

    def test_oversized_probe_page_is_not_labeled_as_safety_block(self):
        response = FakeResponse(
            headers={"Content-Type": "text/html"},
            html="<html></html>",
        )
        with (
            patch(
                "foia_archive.source_census._request_with_safe_redirects",
                return_value=(response, "https://agency.gov/foia"),
            ),
            patch(
                "foia_archive.source_census._read_limited_text",
                side_effect=FileTooLarge("page exceeds census probe limit"),
            ),
        ):
            result = probe_source(
                "https://agency.gov/foia",
                user_agent="test",
            )

        self.assertEqual(result.category, "probe_page_too_large")
        self.assertIn("FileTooLarge", result.error)

    def test_transient_503_is_retried_before_probe_classification(self):
        first = FakeResponse(
            status_code=503,
            headers={"Retry-After": "0"},
        )
        second = FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            html='<a href="/foia/report.pdf">Report</a>',
        )
        with (
            patch(
                "foia_archive.source_census._request_with_safe_redirects",
                side_effect=[
                    (first, "https://agency.gov/foia"),
                    (second, "https://agency.gov/foia"),
                ],
            ) as request,
            patch("foia_archive.source_census._retry_sleep") as retry_sleep,
        ):
            result = probe_source(
                "https://agency.gov/foia",
                user_agent="test",
                max_retries=1,
                retry_backoff_seconds=0,
            )

        self.assertEqual(result.category, "document_producing")
        self.assertEqual(request.call_count, 2)
        retry_sleep.assert_called_once()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_repeated_429_is_classified_after_retry_budget(self):
        first = FakeResponse(
            status_code=429,
            headers={"Retry-After": "0"},
        )
        second = FakeResponse(
            status_code=429,
            headers={"Retry-After": "0"},
        )
        with (
            patch(
                "foia_archive.source_census._request_with_safe_redirects",
                side_effect=[
                    (first, "https://agency.gov/foia"),
                    (second, "https://agency.gov/foia"),
                ],
            ) as request,
            patch("foia_archive.source_census._retry_sleep") as retry_sleep,
        ):
            result = probe_source(
                "https://agency.gov/foia",
                user_agent="test",
                max_retries=1,
                retry_backoff_seconds=0,
            )

        self.assertEqual(result.category, "rate_limited")
        self.assertEqual(request.call_count, 2)
        retry_sleep.assert_called_once()

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
