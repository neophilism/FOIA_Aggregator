import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import requests

from foia_archive.discovery import (
    _metadata_retry_delay,
    fetch_json,
)
from foia_archive.scraper_core import _retry_delay


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        *,
        headers=None,
        payload=None,
        json_error=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = {} if payload is None else payload
        self.json_error = json_error
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def close(self):
        self.closed = True


class MetadataRetryTests(unittest.TestCase):
    def test_429_retry_after_then_success(self):
        first = FakeResponse(
            429,
            headers={"Retry-After": "3"},
        )
        second = FakeResponse(200, payload={"data": ["ok"]})

        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[first, second],
            ) as request,
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            result = fetch_json(
                "https://api.foia.gov/api/agency",
                30,
                {},
                max_retries=2,
                retry_backoff_seconds=1,
                max_retry_delay_seconds=10,
            )

        self.assertEqual(result, {"data": ["ok"]})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_503_retries_with_exponential_backoff(self):
        first = FakeResponse(503)
        second = FakeResponse(200, payload={"data": []})

        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[first, second],
            ),
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            fetch_json(
                "https://api.foia.gov/api/agency",
                30,
                {},
                max_retries=2,
                retry_backoff_seconds=2,
            )

        sleep.assert_called_once_with(2.0)

    def test_transport_failure_is_retried(self):
        response = FakeResponse(200, payload={"ok": True})
        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[requests.Timeout("timeout"), response],
            ),
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            result = fetch_json(
                "https://api.foia.gov/api/agency",
                30,
                {},
                max_retries=1,
                retry_backoff_seconds=0.5,
            )

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(0.5)

    def test_invalid_json_is_retried(self):
        first = FakeResponse(
            200,
            json_error=ValueError("bad json"),
        )
        second = FakeResponse(200, payload={"ok": True})

        with (
            patch(
                "foia_archive.discovery.requests.get",
                side_effect=[first, second],
            ),
            patch("foia_archive.discovery.time.sleep") as sleep,
        ):
            result = fetch_json(
                "https://api.foia.gov/api/agency",
                30,
                {},
                max_retries=1,
                retry_backoff_seconds=0.25,
            )

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(0.25)

    def test_retry_after_http_date_is_honored_but_capped(self):
        retry_at = format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        response = FakeResponse(
            429,
            headers={"Retry-After": retry_at},
        )

        delay = _metadata_retry_delay(
            response,
            attempt=0,
            base_seconds=1,
            max_delay_seconds=7,
        )

        self.assertEqual(delay, 7)

    def test_rate_limit_reset_is_honored_but_capped(self):
        response = FakeResponse(
            429,
            headers={"X-RateLimit-Reset": "110"},
        )
        with patch("foia_archive.discovery.time.time", return_value=100):
            delay = _metadata_retry_delay(
                response,
                attempt=0,
                base_seconds=1,
                max_delay_seconds=4,
            )

        self.assertEqual(delay, 4)


class CrawlerRetryDelayTests(unittest.TestCase):
    def test_numeric_retry_after_overrides_shorter_backoff(self):
        self.assertEqual(
            _retry_delay(
                0,
                1,
                retry_after="5",
                max_delay_seconds=60,
            ),
            5,
        )

    def test_http_date_retry_after_is_capped(self):
        retry_at = format_datetime(
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        self.assertEqual(
            _retry_delay(
                0,
                1,
                retry_after=retry_at,
                max_delay_seconds=9,
            ),
            9,
        )

    def test_rate_limit_reset_is_supported(self):
        with patch(
            "foia_archive.scraper_core.time.time",
            return_value=100,
        ):
            self.assertEqual(
                _retry_delay(
                    0,
                    1,
                    rate_limit_reset="106",
                    max_delay_seconds=60,
                ),
                6,
            )


if __name__ == "__main__":
    unittest.main()
