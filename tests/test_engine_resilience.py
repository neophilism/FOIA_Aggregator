import unittest
from unittest.mock import patch

import requests

from foia_archive.engine import run_once
from foia_archive.scheduler import run_forever
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
            run_once()

        self.assertEqual(
            [call.args[0] for call in crawl.call_args_list],
            [1, 2],
        )

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
        ):
            run_once()

        self.assertEqual(crawl.call_count, 3)


class SchedulerResilienceTests(unittest.TestCase):
    def test_daemon_survives_failed_cycle_and_reaches_sleep(self):
        config = Config(
            {
                "crawler": {"interval_hours": 6},
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
        sleep.assert_called_once_with(6.0 * 3600)


if __name__ == "__main__":
    unittest.main()
