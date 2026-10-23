import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foia_archive.engine import run_once
from foia_archive.ingestion_journal import (
    exclusive_writer, latest_receipt, policy_fingerprint, RunJournal,
)
from foia_archive.scraper_core import get_reading_rooms_to_crawl
from foia_archive.storage import (
    get_connection, init_db, record_reading_room_crawl_failure,
    record_reading_room_crawl_success, upsert_reading_room,
)
from foia_archive.utils import Config


def hold_lock(path, pipe):
    with exclusive_writer(path):
        pipe.send('locked')
        pipe.recv()


class JournalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'archive.db'
        self.cfg = Config({'storage': {'db_path': str(self.db), 'files_dir': self.temp.name + '/files'},
                           'crawler': {'dry_run': True, 'per_host_delay_seconds': 0}})
        init_db(self.db, self.cfg.storage['files_dir'])
        conn = get_connection(self.db)
        self.ids = [upsert_reading_room(conn, f'https://agency.gov/{i}', str(i), 'agency', None, None) for i in range(3)]
        conn.close()

    def succeeded(self, source_id, *args, **kwargs):
        conn = get_connection(self.db)
        try:
            record_reading_room_crawl_success(conn, source_id, '2026-10-18T12:00:00+00:00')
        finally:
            conn.close()

    def run_cycle(self, crawl=None, backup=None):
        with patch('foia_archive.engine.load_config', return_value=self.cfg), \
             patch('foia_archive.engine.crawl_reading_room', side_effect=crawl or self.succeeded), \
             patch('foia_archive.engine.backup_database_to_b2', side_effect=backup, return_value=None):
            return run_once(refresh_metadata_enabled=False)

    def test_interrupted_wave_resumes_same_id_and_skips_completed_source(self):
        calls = []
        def interrupted(source_id, *args, **kwargs):
            calls.append(source_id)
            if source_id == self.ids[1]:
                raise KeyboardInterrupt()
            self.succeeded(source_id)
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle(interrupted)
        old = latest_receipt(self.db)
        self.assertEqual(old['state'], 'INTERRUPTED')
        calls.clear()
        def resumed(source_id, *args, **kwargs):
            calls.append(source_id)
            self.succeeded(source_id)
        self.run_cycle(resumed)
        new = latest_receipt(self.db)
        self.assertEqual(new['id'], old['id'])
        self.assertEqual(new['attempt'], 2)
        self.assertEqual(calls, self.ids[1:])
        self.assertEqual(new['source_counts']['completed'], 3)
        self.assertEqual(new['state'], 'COMPLETED')
        self.assertEqual(new['deltas']['archived'], 0)
        self.assertIsNone(new['metrics_after']['actual_b2_versioned_bytes'])
        self.assertIsNone(new['metrics_after']['cost_cents'])

    def test_source_failure_is_persisted_without_exposing_exception_text(self):
        def failed(source_id, *args, **kwargs):
            if source_id == self.ids[0]:
                raise RuntimeError('credential=never-publish-this')
            self.succeeded(source_id)
        self.run_cycle(failed)
        receipt = latest_receipt(self.db)
        self.assertEqual(receipt['state'], 'DEGRADED')
        self.assertTrue(receipt['escalation_required'])
        self.assertEqual(receipt['source_counts']['failed'], 1)
        self.assertEqual(receipt['source_counts']['completed'], 2)
        self.assertNotIn('never-publish-this', str(receipt))

    def test_crawler_recorded_error_is_not_mislabeled_success(self):
        def failed(source_id, *args, **kwargs):
            conn = get_connection(self.db)
            try:
                record_reading_room_crawl_failure(conn, source_id, '2026-10-18T12:00:00', 'HTTP 403')
            finally:
                conn.close()
        self.run_cycle(failed)
        receipt = latest_receipt(self.db)
        self.assertEqual(receipt['source_counts']['failed'], 3)
        self.assertEqual(receipt['state'], 'DEGRADED')

    def test_checkpoint_failure_remains_degraded_and_lock_releases(self):
        self.run_cycle(backup=RuntimeError('offline'))
        receipt = latest_receipt(self.db)
        self.assertEqual(receipt['checkpoint_state'], 'FAILED')
        self.assertEqual(receipt['stop_reason'], 'checkpoint_failure')
        self.assertEqual(receipt['state'], 'DEGRADED')
        with exclusive_writer(self.db):
            pass

    def test_quota_stop_does_not_claim_unvisited_sources_were_crawled(self):
        def capped(source_id, *args, **kwargs):
            self.succeeded(source_id)
            self.cfg.data['_storage_quota_reached'] = True
        self.run_cycle(capped)
        receipt = latest_receipt(self.db)
        self.assertEqual(receipt['stop_reason'], 'storage_cap_reached')
        self.assertEqual(receipt['source_counts']['completed'], 1)
        self.assertEqual(receipt['source_counts']['not_attempted'], 2)

    def test_policy_change_does_not_reuse_interrupted_plan(self):
        fingerprint = policy_fingerprint(self.cfg)
        with exclusive_writer(self.db):
            journal = RunJournal(self.db, fingerprint, self.ids)
        self.cfg.data['crawler']['max_docs_per_source'] = 1
        with exclusive_writer(self.db):
            new = RunJournal(self.db, policy_fingerprint(self.cfg), self.ids)
        self.assertNotEqual(new.id, journal.id)
        conn = get_connection(self.db)
        self.assertEqual(conn.execute('SELECT state FROM ingestion_runs WHERE id=?', (journal.id,)).fetchone()[0], 'INTERRUPTED')
        conn.close()

    def test_same_database_alias_cannot_acquire_a_second_writer(self):
        with exclusive_writer(self.db):
            with self.assertRaisesRegex(RuntimeError, 'Another ingestion writer'):
                with exclusive_writer(self.db.parent / '.' / self.db.name):
                    self.fail('second writer entered')

    def test_killed_process_releases_lock_without_deleting_lock_inode(self):
        ctx = multiprocessing.get_context('spawn')
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=hold_lock, args=(str(self.db), child))
        proc.start()
        try:
            self.assertTrue(parent.poll(5), 'child failed to acquire lock')
            self.assertEqual(parent.recv(), 'locked')
            inode = Path(str(self.db) + '.writer.lock').stat().st_ino
            with self.assertRaisesRegex(RuntimeError, 'Another ingestion writer'):
                with exclusive_writer(self.db):
                    self.fail('concurrent writer entered')
            proc.terminate()
            proc.join(5)
            self.assertFalse(proc.is_alive())
            with exclusive_writer(self.db):
                self.assertEqual(Path(str(self.db) + '.writer.lock').stat().st_ino, inode)
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(5)
            parent.close()
            child.close()

    def test_bounded_selection_rotates_never_crawled_and_oldest_sources(self):
        self.succeeded(self.ids[0])
        selected = get_reading_rooms_to_crawl(self.cfg, limit=1)
        self.assertEqual([row['id'] for row in selected], [self.ids[1]])
        self.succeeded(self.ids[1])
        selected = get_reading_rooms_to_crawl(self.cfg, limit=1)
        self.assertEqual([row['id'] for row in selected], [self.ids[2]])

    def test_failed_nested_lock_preserves_outer_owner(self):
        with exclusive_writer(self.db):
            with self.assertRaises(RuntimeError):
                with exclusive_writer(self.db):
                    self.fail('unexpected acquisition')
            with exclusive_writer(self.db, allow_nested=True):
                pass

    def test_restart_preserves_existing_database_without_contacting_cloud(self):
        from scripts.seed_presentation_corpus import _restore_latest_if_available
        original = self.db.read_bytes()
        with patch('scripts.seed_presentation_corpus.load_config', return_value=self.cfg), \
             patch('scripts.seed_presentation_corpus.list_database_backups') as listing, \
             patch('scripts.seed_presentation_corpus.restore_database_from_b2') as restore:
            self.assertTrue(_restore_latest_if_available('unused'))
        listing.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(self.db.read_bytes(), original)

    def test_corrupt_existing_database_is_preserved_for_recovery(self):
        from scripts.seed_presentation_corpus import _restore_latest_if_available
        self.db.write_bytes(b'broken database content')
        with patch('scripts.seed_presentation_corpus.load_config', return_value=self.cfg), \
             patch('scripts.seed_presentation_corpus.restore_database_from_b2') as restore:
            with self.assertRaises(Exception):
                _restore_latest_if_available('unused')
        restore.assert_not_called()
        self.assertEqual(self.db.read_bytes(), b'broken database content')

    def test_secret_configuration_does_not_enter_fingerprint_payload(self):
        original = policy_fingerprint(self.cfg)
        self.cfg.data['foia_hub'] = {'api_key': 'secret'}
        self.assertEqual(policy_fingerprint(self.cfg), original)

    def test_receipt_read_does_not_create_missing_database(self):
        with self.assertRaises(Exception):
            latest_receipt(self.db.parent / 'missing.db')
        self.assertFalse((self.db.parent / 'missing.db').exists())


if __name__ == '__main__':
    unittest.main()
