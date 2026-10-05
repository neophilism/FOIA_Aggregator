import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from foia_archive.engine import run_once
from foia_archive.scheduler import (
    DEFAULT_ERROR_RETRY_SECONDS,
    run_forever,
)
from foia_archive.scraper_core import (
    crawl_reading_room,
    get_reading_rooms_to_crawl,
)
from foia_archive.utils import Config


class EngineResilienceTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            {
                "crawler": {
                    "dry_run": True,
                    "max_docs_per_source": 10,
                    "per_host_delay_seconds": 0,
                    "interval_hours": 6,
                },
                "storage": {
                    "db_path": "data/test.db",
                    "files_dir": "data/test-files",
                },
            }
        )

    def test_metadata_failure_still_crawls_known_sources(self):
        rooms = [{"id": 1}, {"id": 2}]
        with (
            patch("foia_archive.engine.load_config", return_value=self.config),
            patch("foia_archive.engine.init_db"),
            patch(
                "foia_archive.engine.refresh_metadata",
                side_effect=requests.HTTPError("HTTP 503"),
            ),
            patch(
                "foia_archive.engine.get_reading_rooms_to_crawl",
                return_value=rooms,
            ),
            patch("foia_archive.engine.crawl_reading_room") as crawl,
        ):
            metadata_ok = run_once()

        self.assertFalse(metadata_ok)
        self.assertEqual(
            [call.args[0] for call in crawl.call_args_list],
            [1, 2],
        )

    def test_metadata_can_be_intentionally_skipped(self):
        with (
            patch("foia_archive.engine.load_config", return_value=self.config),
            patch("foia_archive.engine.init_db"),
            patch("foia_archive.engine.refresh_metadata") as refresh,
            patch(
                "foia_archive.engine.get_reading_rooms_to_crawl",
                return_value=[],
            ),
        ):
            metadata_ok = run_once(refresh_metadata_enabled=False)

        self.assertTrue(metadata_ok)
        refresh.assert_not_called()

    def test_one_unexpected_source_failure_does_not_stop_remaining_sources(self):
        rooms = [{"id": 1}, {"id": 2}, {"id": 3}]
        with (
            patch("foia_archive.engine.load_config", return_value=self.config),
            patch("foia_archive.engine.init_db"),
            patch("foia_archive.engine.refresh_metadata"),
            patch(
                "foia_archive.engine.get_reading_rooms_to_crawl",
                return_value=rooms,
            ),
            patch(
                "foia_archive.engine.crawl_reading_room",
                side_effect=[RuntimeError("boom"), None, None],
            ) as crawl,
            patch(
                "foia_archive.engine._record_unexpected_source_failure"
            ) as record_failure,
        ):
            run_once()

        self.assertEqual(crawl.call_count, 3)
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[1], 1)


    def test_database_failure_aborts_cycle_for_daemon_backoff(self):
        rooms = [{"id": 1}, {"id": 2}]
        with (
            patch("foia_archive.engine.load_config", return_value=self.config),
            patch("foia_archive.engine.init_db"),
            patch("foia_archive.engine.refresh_metadata"),
            patch(
                "foia_archive.engine.get_reading_rooms_to_crawl",
                return_value=rooms,
            ),
            patch(
                "foia_archive.engine.crawl_reading_room",
                side_effect=sqlite3.OperationalError("database is locked"),
            ) as crawl,
            patch(
                "foia_archive.engine._record_unexpected_source_failure"
            ) as record_failure,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                run_once()

        self.assertEqual(crawl.call_count, 1)
        record_failure.assert_not_called()


class SchedulerResilienceTests(unittest.TestCase):
    def test_failed_cycle_uses_short_error_retry_without_exiting(self):
        config = Config(
            {
                "crawler": {
                    "interval_hours": 6,
                    "daemon_error_retry_seconds": 45,
                },
            }
        )
        with (
            patch(
                "foia_archive.scheduler.load_config",
                return_value=config,
            ),
            patch(
                "foia_archive.scheduler.run_once",
                side_effect=RuntimeError("cycle failed"),
            ) as run,
            patch(
                "foia_archive.scheduler.time.sleep",
                side_effect=KeyboardInterrupt,
            ) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever()

        run.assert_called_once()
        sleep.assert_called_once_with(45.0)

    def test_missing_config_at_startup_does_not_terminate_daemon(self):
        with (
            patch(
                "foia_archive.scheduler.load_config",
                side_effect=FileNotFoundError("missing config"),
            ),
            patch("foia_archive.scheduler.run_once") as run,
            patch(
                "foia_archive.scheduler.time.sleep",
                side_effect=KeyboardInterrupt,
            ) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever("missing.yaml")

        run.assert_not_called()
        sleep.assert_called_once_with(DEFAULT_ERROR_RETRY_SECONDS)

    def test_successful_cycle_uses_normal_interval(self):
        config = Config(
            {
                "crawler": {
                    "interval_hours": 0.5,
                    "daemon_error_retry_seconds": 10,
                },
            }
        )
        with (
            patch(
                "foia_archive.scheduler.load_config",
                return_value=config,
            ),
            patch(
                "foia_archive.scheduler.run_once",
                return_value=True,
            ) as run,
            patch(
                "foia_archive.scheduler.time.sleep",
                side_effect=KeyboardInterrupt,
            ) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever()

        run.assert_called_once()
        sleep.assert_called_once_with(1800.0)

    def test_rapid_cycles_do_not_refresh_metadata_every_cycle(self):
        config = Config(
            {
                "crawler": {
                    "interval_hours": 0.001,
                    "daemon_error_retry_seconds": 10,
                },
                "foia_hub": {
                    "refresh_interval_minutes": 360,
                    "failure_retry_minutes": 15,
                },
            }
        )
        with (
            patch(
                "foia_archive.scheduler.load_config",
                return_value=config,
            ),
            patch(
                "foia_archive.scheduler.run_once",
                return_value=True,
            ) as run,
            patch(
                "foia_archive.scheduler.time.sleep",
                side_effect=[None, KeyboardInterrupt],
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever()

        self.assertEqual(run.call_count, 2)
        self.assertTrue(
            run.call_args_list[0].kwargs["refresh_metadata_enabled"]
        )
        self.assertFalse(
            run.call_args_list[1].kwargs["refresh_metadata_enabled"]
        )

    def test_invalid_timing_values_fall_back_without_killing_cycle(self):
        config = Config(
            {
                "crawler": {
                    "interval_hours": "bad",
                    "daemon_error_retry_seconds": "also-bad",
                },
                "foia_hub": {
                    "refresh_interval_minutes": "bad",
                    "failure_retry_minutes": "bad",
                },
            }
        )
        with (
            patch(
                "foia_archive.scheduler.load_config",
                return_value=config,
            ),
            patch(
                "foia_archive.scheduler.run_once",
                return_value=True,
            ) as run,
            patch(
                "foia_archive.scheduler.time.sleep",
                side_effect=KeyboardInterrupt,
            ) as sleep,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever()

        run.assert_called_once()
        sleep.assert_called_once_with(6.0 * 3600)

    def test_keyboard_interrupt_is_not_swallowed(self):
        config = Config(
            {"crawler": {"interval_hours": 6}}
        )
        with (
            patch(
                "foia_archive.scheduler.load_config",
                return_value=config,
            ),
            patch(
                "foia_archive.scheduler.run_once",
                side_effect=KeyboardInterrupt,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_forever()


class SourceCooldownTests(unittest.TestCase):
    def config(self, cooldown=60):
        return Config(
            {
                "crawler": {
                    "failed_source_retry_minutes": cooldown,
                },
                "storage": {"db_path": "unused.db"},
            }
        )

    def test_recently_failed_source_is_skipped(self):
        now = datetime.now(timezone.utc)
        rooms = [
            {
                "id": 1,
                "last_error_at": (
                    now - timedelta(minutes=10)
                ).isoformat(),
            },
            {
                "id": 2,
                "last_error_at": None,
            },
        ]
        conn = Mock()

        with (
            patch(
                "foia_archive.scraper_core.get_connection",
                return_value=conn,
            ),
            patch(
                "foia_archive.scraper_core.list_reading_rooms",
                return_value=rooms,
            ),
        ):
            result = get_reading_rooms_to_crawl(self.config())

        self.assertEqual([room["id"] for room in result], [2])
        conn.close.assert_called_once()

    def test_old_failure_is_retried_after_cooldown(self):
        now = datetime.now(timezone.utc)
        rooms = [
            {
                "id": 1,
                "last_error_at": (
                    now - timedelta(minutes=90)
                ).isoformat(),
            },
        ]
        conn = Mock()

        with (
            patch(
                "foia_archive.scraper_core.get_connection",
                return_value=conn,
            ),
            patch(
                "foia_archive.scraper_core.list_reading_rooms",
                return_value=rooms,
            ),
        ):
            result = get_reading_rooms_to_crawl(self.config())

        self.assertEqual([room["id"] for room in result], [1])

    def test_invalid_cooldown_config_uses_safe_default(self):
        now = datetime.now(timezone.utc)
        rooms = [
            {
                "id": 1,
                "last_error_at": (
                    now - timedelta(minutes=10)
                ).isoformat(),
            },
        ]
        conn = Mock()

        with (
            patch(
                "foia_archive.scraper_core.get_connection",
                return_value=conn,
            ),
            patch(
                "foia_archive.scraper_core.list_reading_rooms",
                return_value=rooms,
            ),
        ):
            result = get_reading_rooms_to_crawl(
                self.config(cooldown="not-a-number")
            )

        self.assertEqual(result, [])


class CrawlConnectionTests(unittest.TestCase):
    def test_source_connection_closes_even_if_internal_crawl_raises(self):
        config = Config(
            {
                "storage": {"db_path": "unused.db"},
            }
        )
        conn = Mock()
        with (
            patch(
                "foia_archive.scraper_core.get_connection",
                return_value=conn,
            ),
            patch(
                "foia_archive.scraper_core._crawl_reading_room_with_connection",
                side_effect=RuntimeError("parser bug"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                crawl_reading_room(
                    1,
                    config,
                    dry_run=False,
                    max_docs=None,
                )

        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
