import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vm_arcade.telemetry import (Client, MonitorError, NodePoller, Rates,
                                load_config, numeric, section_current)
from vm_arcade.controls import Controls, Selection


class RateTests(unittest.TestCase):
    def sample(self, uptime=20, counter=100):
        return {"status": "running", "uptime": uptime, "netin": counter,
                "netout": counter * 2, "diskread": counter * 3}

    def test_rates_need_two_samples_and_keep_absent_metrics_unknown(self):
        rates = Rates()
        self.assertTrue(all(v is None for v in rates.sample("vm", self.sample(), 100).values()))
        result = rates.sample("vm", self.sample(25, 150), 105)
        self.assertEqual(result["netin"], 10)
        self.assertEqual(result["netout"], 20)
        self.assertIsNone(result["diskwrite"])

    def test_restart_and_counter_reset_do_not_make_spikes(self):
        rates = Rates()
        rates.sample("vm", self.sample(100, 1000), 10)
        self.assertIsNone(rates.sample("vm", self.sample(1, 2000), 15)["netin"])
        self.assertIsNone(rates.sample("vm", self.sample(6, 10), 20)["netin"])
        self.assertEqual(rates.sample("vm", self.sample(11, 30), 25)["netin"], 4)

    def test_long_outage_and_stopped_guest_have_no_rate(self):
        rates = Rates()
        rates.sample("vm", self.sample(), 0)
        self.assertIsNone(rates.sample("vm", self.sample(300, 10000), 200)["netin"])
        stopped = self.sample(0, 0)
        stopped["status"] = "stopped"
        self.assertIsNone(rates.sample("vm", stopped, 205)["netin"])

    def test_invalid_numbers_remain_unknown(self):
        for value in (None, "bad", "NaN", float("inf"), -1, True):
            self.assertIsNone(numeric(value))
        self.assertEqual(numeric("0"), 0)


class FakeAPI:
    name = "node-a"

    def __init__(self):
        self.fail = set()
        self.empty = False
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        section = path.split("/")[-1].split("?")[0]
        if section in self.fail:
            raise MonitorError("Permission denied")
        if section == "status":
            return {"cpu": .12, "uptime": 200, "memory": {"used": 1000, "total": 8000}}
        if section == "qemu":
            return [] if self.empty else [{"vmid": 100, "status": "running", "uptime": 20, "cpu": .5}]
        return []


class PollingTests(unittest.TestCase):
    def test_partial_failure_preserves_inventory_but_marks_it_stale(self):
        api = FakeAPI()
        poller = NodePoller(api)
        initial = poller.poll(100)
        self.assertTrue(section_current(initial, "qemu", 100))
        self.assertEqual(initial["sections"]["qemu"]["data"][0]["cpu"], 50)
        self.assertIsNone(initial["sections"]["qemu"]["data"][0]["mem"])
        api.fail.add("qemu")
        failed = poller.poll(105)
        self.assertEqual(len(failed["sections"]["qemu"]["data"]), 1)
        self.assertFalse(section_current(failed, "qemu", 105))
        self.assertTrue(section_current(failed, "status", 105))
        self.assertEqual(failed["sections"]["qemu"]["at"], 100)
        api.fail.clear()
        api.empty = True
        recovered = poller.poll(110)
        self.assertTrue(section_current(recovered, "qemu", 110))
        self.assertEqual(recovered["sections"]["qemu"]["data"], [])

    def test_wall_clock_does_not_control_freshness(self):
        snapshot = NodePoller(FakeAPI()).poll(100)
        self.assertTrue(section_current(snapshot, "status", 110))
        self.assertFalse(section_current(snapshot, "status", 121))
        self.assertFalse(section_current(snapshot, "status", 99))

    def test_rrd_is_fetched_once_per_minute(self):
        api = FakeAPI()
        poller = NodePoller(api)
        for now in (100, 105, 110, 160):
            poller.poll(now)
        self.assertEqual(sum("/rrddata?" in path for path in api.paths), 2)
        self.assertTrue(any(path.endswith("/qemu?full=1") for path in api.paths))


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = self.root / "test.token"
        self.token.write_text("TEST-TOKEN-NOT-A-REAL-SECRET")
        self.token.chmod(0o600)
        self.node = {"name": "node-a", "url": "https://192.0.2.1:8006", "token_id": "test@pve!monitor",
                     "token_file": str(self.token), "tls_sha256": hashlib.sha256(b"test-cert").hexdigest()}

    def tearDown(self):
        self.temp.cleanup()

    def test_configuration_is_private_and_disallows_insecure_urls(self):
        target = self.root / "config.json"
        target.write_text(json.dumps({"nodes": [self.node]}))
        target.chmod(0o600)
        self.assertEqual(load_config(target)["nodes"][0]["name"], "node-a")
        self.assertEqual(load_config(target)["traffic_poll_seconds"], .5)
        target.chmod(0o644)
        with self.assertRaises(MonitorError):
            load_config(target)
        target.chmod(0o600)
        for url in ("http://192.0.2.1", "https://secret@192.0.2.1", "https://192.0.2.1/?token=x"):
            self.node["url"] = url
            target.write_text(json.dumps({"nodes": [self.node]}))
            with self.assertRaises(MonitorError):
                load_config(target)

    def test_exit_override_accepts_only_valid_integer_key_codes(self):
        target = self.root / "config.json"
        target.write_text(json.dumps({"nodes": [self.node], "exit_key": 115}))
        target.chmod(0o600)
        self.assertEqual(load_config(target)["exit_key"], 115)
        for key in (True, 0, -1, 2**31, "s", 1.5, None):
            with self.subTest(key=key):
                target.write_text(json.dumps({"nodes": [self.node], "exit_key": key}))
                with self.assertRaises(MonitorError):
                    load_config(target)

    def test_pin_checked_before_credentials_and_methods_are_get_only(self):
        class Connection:
            def __init__(self):
                self.sock = self
                self.calls = []
                self.cert = b"test-cert"
                self.status = 200
            def connect(self): pass
            def getpeercert(self, binary_form): return self.cert
            def request(self, *args, **kwargs): self.calls.append((args, kwargs))
            def getresponse(self): return self
            def read(self, limit): return b'{"data":{"uptime":42}}'
            def close(self): pass
        conn = Connection()
        with patch("vm_arcade.telemetry.http.client.HTTPSConnection", return_value=conn):
            client = Client(self.node)
            self.assertEqual(client.get("/nodes/node-a/status"), {"uptime": 42})
            self.assertEqual(conn.calls[0][0][0], "GET")
            self.assertIn("Authorization", conn.calls[0][1]["headers"])
            conn.calls.clear()
            conn.cert = b"unexpected-cert"
            with self.assertRaisesRegex(MonitorError, "pin changed"):
                client.get("/nodes/node-a/status")
            self.assertEqual(conn.calls, [])
            for path in ("/nodes/node-a/qemu/100/status/stop", "https://elsewhere/status", "/nodes/node-b/status"):
                with self.assertRaises(MonitorError):
                    client.get(path)
            self.assertEqual(conn.calls, [])


class ControlTests(unittest.TestCase):
    def test_custom_exit_replaces_default_and_configured_exit_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.xml"
            path.write_text('<inputList><inputConfig type="keyboard">'
                            '<input name="select" type="key" id="113" value="1"/>'
                            '<input name="a" type="key" id="99" value="1"/>'
                            '<input name="x" type="key" id="32" value="1"/>'
                            '</inputConfig></inputList>')
            controls = Controls(path, exit_key=115)
            state = Selection()
            self.assertTrue(state.act(controls.keys.get(1073742049), [1], []))
            self.assertTrue(state.act(controls.keys.get(113), [1], []))
            self.assertEqual(controls.keys[99], "details")
            self.assertEqual(controls.keys[32], "toggle_view")
            self.assertFalse(state.act(controls.keys[115], [1], []))

    def test_saved_picade_modifier_keys_and_start_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.xml"
            path.write_text('<inputList><inputConfig type="keyboard">'
                            '<input name="a" type="key" id="1073742048" value="1"/>'
                            '<input name="b" type="key" id="1073742050" value="1"/>'
                            '<input name="select" type="key" id="1073742049" value="1"/>'
                            '<input name="start" type="key" id="13" value="1"/>'
                            '</inputConfig></inputList>')
            controls = Controls(path)
            self.assertEqual(controls.keys[1073742048], "details")
            self.assertEqual(controls.keys[1073742050], "back")
            self.assertEqual(controls.keys[1073742049], "exit")
            self.assertEqual(controls.keys[13], "overview")

    def test_selection_tracks_vm_identity_when_inventory_changes(self):
        state = Selection()
        guests = [{"key": "qemu:100"}, {"key": "qemu:200"}, {"key": "lxc:300"}]
        state.act("down", [1, 2, 3], guests)
        self.assertEqual(state.guest_key, "qemu:200")
        self.assertEqual(state.resolve(guests[1:]), 0)
        state.act("details", [1], guests)
        self.assertTrue(state.detail)
        state.act("back", [1], guests)
        self.assertFalse(state.detail)
        self.assertTrue(state.act("right", [1, 2, 3], []))
        self.assertIsNone(state.guest_key)
        self.assertFalse(state.act("exit", [1], []))


if __name__ == "__main__":
    unittest.main()
