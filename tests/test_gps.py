import copy
import unittest

from vm_arcade.controls import Controls, Selection
from vm_arcade.gps import DemoGPS, current, decode, validate
from vm_arcade.gps_view import position


class GPSTests(unittest.TestCase):
    def test_read_only_origin_config_rejects_credentials_and_paths(self):
        self.assertEqual(validate({'url': 'http://192.0.2.20:8080'})['url'], 'http://192.0.2.20:8080')
        for url in ('ftp://example.org', 'http://user:secret@example.org',
                    'http://example.org/api', 'http://example.org?cmd=offline', 'http://example.org:70000'):
            with self.assertRaises((ValueError, KeyError)):
                validate({'url': url})

    def test_source_stall_and_failure_stop_live_display(self):
        snapshot = {'at': 10, 'advanced_at': 10, 'data': {'collector_age_s': .2}}
        self.assertTrue(current(snapshot, 12))
        self.assertFalse(current(snapshot, 14))
        snapshot['at'] = 15  # Another HTTP reply containing the same old sample.
        self.assertFalse(current(snapshot, 15))
        snapshot.update(at=16, advanced_at=16, error='Unavailable')
        self.assertFalse(current(snapshot, 16))
        self.assertFalse(current(None, 16))

    def test_malformed_or_missing_reports_do_not_crash_renderer(self):
        original = DemoGPS().read()['data']
        for section, key in (('gps', 'sky'), ('ntp', 'tracking')):
            payload = copy.deepcopy(original)
            payload[section][key] = []
            with self.assertRaises(ValueError):
                decode(payload)
        payload = copy.deepcopy(original)
        payload['history'][0]['time'] = 'invalid'
        payload['ntp']['tracking']['last_offset_s'] = float('nan')
        result = decode(payload)
        self.assertIsNone(result['history'][0]['time'])
        self.assertIsNone(result['ntp']['tracking']['last_offset_s'])
        payload['gps']['sky']['satellites'] = [None]
        with self.assertRaises(ValueError):
            decode(payload)

    def test_sky_plot_uses_actual_azimuth_and_elevation(self):
        self.assertEqual(position({'az': 0, 'el': 0}, 100, 100, 50), (100, 50))
        x, y = position({'az': 90, 'el': 0}, 100, 100, 50)
        self.assertAlmostEqual(x, 150)
        self.assertAlmostEqual(y, 100)
        self.assertEqual(position({'az': 123, 'el': 90}, 100, 100, 50), (100, 100))
        self.assertIsNone(position({'az': 123}, 100, 100, 50))

    def test_third_view_is_opt_in_and_exit_mapping_is_preserved(self):
        selection = Selection(gps_enabled=True)
        for expected in ('metrics', 'gps', 'network'):
            self.assertTrue(selection.act('toggle_view', [], []))
            self.assertEqual(selection.view, expected)
        self.assertFalse(selection.act('exit', [], []))
        controls = Controls('/nonexistent', exit_key=115)
        self.assertEqual(controls.keys[115], 'exit')
        self.assertNotEqual(controls.keys.get(1073742049), 'exit')
        selection = Selection()
        selection.act('gps', [], [])
        self.assertEqual(selection.view, 'network')
        selection.act('toggle_view', [], [])
        selection.act('toggle_view', [], [])
        self.assertEqual(selection.view, 'network')


if __name__ == '__main__':
    unittest.main()
