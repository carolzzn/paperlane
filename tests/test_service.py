"""Run in the service image; isolated temporary library and fake printer only."""
import io
import json
import devices
from unittest.mock import MagicMock
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from fastapi import HTTPException
import main
from imaging import render_photo, render_sheet
from ipp import attribute, decode


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / 'data'
        self.photos = Path(self.tmp.name) / 'photos'
        self.completed = Path(self.tmp.name) / 'completed'
        self.photos.mkdir()
        self.completed.mkdir()
        self.patchers = [patch.object(main, 'DATA', self.data), patch.object(main, 'ROOT', self.photos), patch.object(main, 'COMPLETED', self.completed), patch.object(main, 'stop', threading.Event())]
        for p in self.patchers:
            p.start()
        main.init()
        self.printer_patch = patch.object(main, 'Printer')
        self.printer_mock = self.printer_patch.start()
        self.caps = json.loads((Path(__file__).parent / 'fixtures/fuji-ipp.json').read_text())
        self.caps.update({'printer-state': [3], 'printer-is-accepting-jobs': [True]})
        self.printer_mock.return_value.capabilities.return_value = self.caps
        with main.db() as c:
            if not devices.all_devices(c):
                devices.save(c, 'Test printer', 'ipp://192.168.1.10:631/ipp/print', self.caps)
            else:
                devices.refresh(c, devices.get(c)['id'], self.caps)
        main.printer_state = {'online': True, 'accepting': True}

    def tearDown(self):
        main.stop.set()
        self.printer_patch.stop()
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def prepare(self, amount=1):
        folder = self.photos / '2026/11/03'
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(amount):
            Image.new('RGB', (1600, 1200), (80, 120, 160)).save(folder / ('photo%d.heic' % i), format='HEIF')
        (folder / 'live.mov').write_bytes(b'fake video')
        main.scan()
        photos = main.photos(limit=10)['photos']
        self.assertEqual(len(photos), amount)
        return main.prepare(main.Prepare(photos=[{'id': p['id']} for p in photos]))['prepared']

    def submit(self, prepared, request_id=None):
        return main.submit(main.Submit(request_id=request_id or uuid.uuid4(), prepared_ids=[p['id'] for p in prepared], copies=1, confirmed=True))

    def test_heic_render_fit_and_source_preserved(self):
        prepared = self.prepare()
        blob = (self.data / 'prepared' / (prepared[0]['id'] + '.jpg')).read_bytes()
        with Image.open(io.BytesIO(blob)) as image:
            self.assertEqual(image.size, (1200, 1800))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        path = next(self.photos.rglob('*.heic'))
        before = path.read_bytes()
        cover, landscape, _ = render_photo(path, 'cover', 90)
        with Image.open(io.BytesIO(cover)) as image:
            self.assertEqual(image.size, (1200, 1800))
            self.assertNotEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertFalse(landscape)
        self.assertEqual(path.read_bytes(), before)

    def test_source_symlinks_and_path_escape_blocked(self):
        external = Path(self.tmp.name) / 'outside.jpg'
        Image.new('RGB', (50, 50)).save(external)
        (self.photos / 'link.jpg').symlink_to(external)
        main.scan()
        self.assertEqual(main.photos()['total'], 0)
        with self.assertRaises(HTTPException):
            main.photo_path({'rel': '../outside.jpg'})

    def test_submission_idempotency_and_limits(self):
        prepared = self.prepare()
        key = uuid.uuid4()
        self.assertFalse(self.submit(prepared, key)['duplicate'])
        self.assertTrue(self.submit(prepared, key)['duplicate'])
        with main.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        with self.assertRaises(HTTPException):
            main.submit(main.Submit(request_id=uuid.uuid4(), prepared_ids=[prepared[0]['id']] * 3, copies=5, confirmed=True))
        with self.assertRaises(HTTPException):
            main.submit(main.Submit(request_id=uuid.uuid4(), prepared_ids=[prepared[0]['id']], copies=1, confirmed=False))

    def test_ambiguous_send_blocks_rest_and_never_retries(self):
        self.submit(self.prepare(2))
        calls = []
        class FakePrinter:
            def __init__(self, *args, **kwargs): pass
            def capabilities(self): return {'printer-is-accepting-jobs': [True], 'printer-state': [3]}
            def validate_ticket(self, *args): pass
            def print_ticket(self, *args): calls.append(1); raise TimeoutError('simulated loss after send')
        with patch.object(main, 'Printer', FakePrinter):
            thread = threading.Thread(target=main.worker)
            thread.start()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if any(j['status'] == 'unknown' for j in main.jobs()): break
                time.sleep(.05)
            time.sleep(.1)
            main.stop.set()
            thread.join(3)
        self.assertEqual(len(calls), 1)
        jobs = main.jobs()
        unknown = next(j for j in jobs if j['status'] == 'unknown')
        self.assertEqual(sum(j['status'] == 'pending' for j in jobs), 1)
        main.resolve(unknown['id'], main.Resolve(action='acknowledge'))
        self.assertFalse(any(j['status'] == 'pending' for j in main.jobs()))

    def test_cancel_during_validation_does_not_print(self):
        self.submit(self.prepare())
        calls = []
        class FakePrinter:
            def __init__(self, *args, **kwargs): pass
            def capabilities(self): return {'printer-is-accepting-jobs': [True], 'printer-state': [3]}
            def validate_ticket(self, *args):
                job = main.jobs()[0]
                main.resolve(job['id'], main.Resolve(action='cancel'))
                main.stop.set()
            def print_ticket(self, *args): calls.append(1)
        with patch.object(main, 'Printer', FakePrinter): main.worker()
        self.assertEqual(calls, [])
        self.assertEqual(main.jobs()[0]['status'], 'cancelled')

    def test_successful_send_waits_for_printer_completion(self):
        self.submit(self.prepare())
        calls = []
        class FakePrinter:
            def __init__(self, *args, **kwargs): pass
            def capabilities(self): return {'printer-is-accepting-jobs': [True], 'printer-state': [3]}
            def validate_ticket(self, *args): pass
            def print_ticket(self, data, ticket):
                calls.append(data)
                return {'job-id': [123]}
            def job(self, job_id):
                self_test.assertEqual(job_id, 123)
                return {'job-state': [9]}
        self_test = self
        with patch.object(main, 'Printer', FakePrinter):
            thread = threading.Thread(target=main.worker)
            thread.start()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if main.jobs()[0]['status'] == 'completed': break
                time.sleep(.05)
            main.stop.set()
            thread.join(3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(main.jobs()[0]['status'], 'completed')
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 0)
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 1)

    def test_all_copies_must_finish_before_archive(self):
        prepared = self.prepare()
        main.submit(main.Submit(request_id=uuid.uuid4(), prepared_ids=[prepared[0]['id']], copies=2, confirmed=True))
        jobs = main.jobs()
        main.set_job(jobs[0]['id'], 'completed', 'fake complete')
        main.archive_completed(prepared[0]['id'])
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 1)
        main.set_job(jobs[1]['id'], 'completed', 'fake complete')
        main.archive_completed(prepared[0]['id'])
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 0)
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 1)
        main.archive_completed(prepared[0]['id'])
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 1)

    def test_failed_or_changed_source_is_not_archived(self):
        prepared = self.prepare()
        self.submit(prepared)
        main.set_job(main.jobs()[0]['id'], 'failed', 'fake failure')
        main.archive_completed(prepared[0]['id'])
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 1)
        main.set_job(main.jobs()[0]['id'], 'completed', 'fake complete')
        next(self.photos.rglob('*.heic')).write_bytes(b'changed after preview')
        main.archive_completed(prepared[0]['id'])
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 1)
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 0)
        with main.db() as c:
            self.assertEqual(c.execute('SELECT status FROM archives').fetchone()[0], 'failed')

    def test_collage_sheet_archives_every_source(self):
        self.prepare(4)
        rows = main.photos(limit=10)['photos']
        result = main.prepare(main.Prepare(photos=[{'id': p['id']} for p in rows], layout='grid4', fit='cover'))
        self.assertEqual(len(result['prepared']), 1)
        self.assertEqual(len(result['prepared'][0]['photo_ids']), 4)
        self.submit(result['prepared'])
        main.set_job(main.jobs()[0]['id'], 'completed', 'fake complete')
        main.archive_completed(result['prepared'][0]['id'])
        self.assertEqual(len(list(self.photos.rglob('*.heic'))), 0)
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 4)

    def test_atomic_archive_never_overwrites(self):
        source = self.photos / 'one.jpg'
        target = self.completed / 'one.jpg'
        source.write_bytes(b'source')
        target.write_bytes(b'existing completed original')
        with self.assertRaises(ValueError):
            main.move_no_replace(source, target)
        self.assertEqual(target.read_bytes(), b'existing completed original')
        self.assertEqual(source.read_bytes(), b'source')

    def test_archive_recovers_after_file_move_before_database_update(self):
        prepared = self.prepare()
        self.submit(prepared)
        main.set_job(main.jobs()[0]['id'], 'completed', 'fake complete')
        with main.db() as c:
            source = c.execute('SELECT * FROM prepared_sources WHERE prepared_id=?', (prepared[0]['id'],)).fetchone()
            c.execute('INSERT INTO archives VALUES(?,?,?,?,?,?)', (prepared[0]['id'], source['photo_id'], 'recovery/photo.heic', 'moving', 'fake interrupted move', time.time()))
        target = self.completed / 'recovery/photo.heic'
        target.parent.mkdir()
        main.move_no_replace(self.photos / source['source_rel'], target)
        main.archive_completed(prepared[0]['id'])
        self.assertTrue(target.is_file())
        with main.db() as c:
            self.assertEqual(c.execute('SELECT status FROM archives').fetchone()[0], 'archived')
        self.assertEqual(len(list(self.completed.rglob('*.heic'))), 1)

    def test_three_inch_and_small_photo_collage_dimensions(self):
        source = self.photos / 'photo.jpg'
        Image.new('RGB', (1400, 1000), 'red').save(source)
        items = [{'path': source}]
        jpeg, landscape, _ = render_sheet(items, paper='3inch')
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (900, 900))
        jpeg, _, _ = render_sheet(items * 2, layout='square3', fit='cover')
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (1200, 1800))
            self.assertGreater(image.getpixel((600, 450))[0], 200)
            self.assertGreater(image.getpixel((600, 1350))[0], 200)
        with self.assertRaises(ValueError): render_sheet(items, paper='3inch', layout='square3')

    def test_restart_never_requeues_inflight_job(self):
        self.submit(self.prepare())
        with main.db() as c: c.execute("UPDATE jobs SET status='sending'")
        main.init()
        self.assertEqual(main.jobs()[0]['status'], 'unknown')

    def test_ipp_nested_collection_and_truncation(self):
        packet = struct.pack('>BBHI', 2, 0, 0, 1) + b'\x04' + attribute(0x34, 'media-col', {'media-size': (0x34, {'x-dimension': (0x21, 10160)})}) + b'\x03'
        parsed = decode(packet)['attributes']['media-col'][0]
        self.assertEqual(parsed['media-size'][0]['x-dimension'][0], 10160)
        with self.assertRaises(ValueError): decode(packet[:-5])


    def test_switch_target_rejects_preview(self):
        prepared=self.prepare()
        with main.db() as c:
            other=devices.save(c,'Second','ipp://192.168.1.11/ipp/print',self.caps)
        with self.assertRaises(main.HTTPException):
            main.submit(main.Submit(request_id=__import__('uuid').uuid4(),prepared_ids=[prepared[0]['id']],copies=1,confirmed=True,printer_id=other['id']))
        self.submit(prepared)
        with main.db() as c:
            self.assertNotEqual(c.execute('SELECT printer_id FROM jobs').fetchone()[0],other['id'])

    def test_capability_change_expires_preview(self):
        prepared=self.prepare()
        changed=__import__('copy').deepcopy(self.caps)
        changed['printer-resolution-supported']=[{'x':600,'y':600,'units':3}]
        with main.db() as c:
            devices.refresh(c,devices.get(c)['id'],changed)
        with self.assertRaises(main.HTTPException): self.submit(prepared)

    def test_migration_preserves_password_history_and_printer(self):
        self.submit(self.prepare())
        main.set_job(main.jobs()[0]['id'],'completed','old history')
        secret=(self.data/'credentials.json').read_bytes()
        with main.db() as c:
            before=dict(c.execute('SELECT * FROM jobs').fetchone())
        main.init()
        self.assertEqual((self.data/'credentials.json').read_bytes(),secret)
        with main.db() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM jobs').fetchone()),before)
            self.assertEqual(len(devices.all_devices(c)),1)

    def test_calibration_is_preview_only_and_no_photo_archival(self):
        response=main.prepare(main.Prepare(calibration=True))
        self.assertEqual(main.jobs(),[])
        self.submit(response['prepared'])
        self.assertEqual(len(main.jobs()),1)
        with main.db() as c:
            self.assertEqual(c.execute('SELECT kind FROM prepared').fetchone()[0],'calibration')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM prepared_sources').fetchone()[0],0)

    def test_real_v1_schema_migrates_history_without_credentials_reset(self):
        self.submit(self.prepare())
        main.set_job(main.jobs()[0]['id'], 'completed', 'v1 completed')
        credentials = (self.data / 'credentials.json').read_bytes()
        with main.db() as c:
            for field in ('printer_id', 'ticket', 'kind'):
                c.execute('ALTER TABLE prepared DROP COLUMN ' + field)
            c.execute('ALTER TABLE jobs DROP COLUMN printer_id')
            c.execute('DROP TABLE printers')
        with patch.object(main, 'URI', 'ipp://192.168.1.10/ipp/print'):
            main.init()
        self.assertEqual((self.data / 'credentials.json').read_bytes(), credentials)
        with main.db() as c:
            self.assertEqual(devices.get(c)['id'], 'legacy')
            self.assertEqual(c.execute('SELECT printer_id,status FROM jobs').fetchone()[:], ('legacy','completed'))
            self.assertIsNone(c.execute('SELECT ticket FROM prepared').fetchone()[0])

    def test_worker_uses_original_device_after_default_switch(self):
        self.submit(self.prepare())
        with main.db() as c:
            original = devices.get(c)['uri']
            other = devices.save(c,'Second','ipp://192.168.1.11/ipp/print', self.caps)
        main.printer_action(other['id'], main.DeviceAction(action='default'))
        destinations=[]
        class FakePrinter:
            def __init__(self, uri, **kwargs): destinations.append(uri)
            def capabilities(self): return {'printer-state':[3], 'printer-is-accepting-jobs':[True]}
            def validate_ticket(self, ticket): pass
            def print_ticket(self, data, ticket): return {'job-id':[42]}
            def job(self, job_id):
                main.stop.set()
                return {'job-state':[9]}
        with patch.object(main,'Printer',FakePrinter): main.worker()
        self.assertEqual(destinations,[original])
        self.assertEqual(main.jobs()[0]['status'],'completed')


    def test_same_source_on_two_preview_pages_is_rejected(self):
        first = self.prepare()
        second = main.prepare(main.Prepare(photos=[{'id':first[0]['photo_id']}]))['prepared']
        with self.assertRaises(HTTPException) as caught:
            self.submit(first + second)
        self.assertIn('同一源照片', caught.exception.detail)
        self.assertEqual(main.jobs(), [])

if __name__ == '__main__':
    unittest.main(verbosity=2)
