import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('probe_printer', Path(__file__).resolve().parents[1] / 'scripts/probe_printer.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProbeTest(unittest.TestCase):
    def test_probe_is_read_only_requests_media_database_and_omits_identity(self):
        calls = []

        class FakePrinter:
            def __init__(self, *args, **kwargs): pass
            def request(self, operation, attributes):
                calls.append((operation, attributes))
                return {'status': 0, 'attributes': {
                    'media-supported': ['na_index-4x6_4x6in'],
                    'media-col-database': [{'media-size': [{'x-dimension': [10160], 'y-dimension': [15240]}]}],
                    'printer-name': ['PRIVATE DEVICE NAME'],
                    'printer-uri-supported': ['ipp://private.example.test/ipp/print'],
                    'printer-uuid': ['PRIVATE UUID'],
                }}

        with patch.object(module, 'Printer', FakePrinter):
            report = module.probe('ipp://printer.example.test/ipp/print')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 0x000b)
        requested = calls[0][1]
        self.assertEqual(requested[0][1], 'requested-attributes')
        self.assertTrue(all(attr[1] == '' for attr in requested[1:]))
        self.assertIn('media-col-database', [attr[2] for attr in requested])
        serialized = json.dumps(report)
        self.assertNotIn('PRIVATE', serialized)
        self.assertNotIn('private.example.test', serialized)
        self.assertIn('media-col-database', report['attributes'])


if __name__ == '__main__':
    unittest.main()
