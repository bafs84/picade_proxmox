import json
import unittest

from vm_arcade.controls import Controls, Selection
from vm_arcade.telemetry import MonitorError, NodePoller
from vm_arcade.topology import interfaces, graph_host, sample_interfaces
from vm_arcade.wan import decode, current, query, validate


class NetworkAPI:
    name = "node-a"

    def __init__(self):
        self.paths = []
        self.counter = 100
        self.uptime = 50
        self.fail = set()
        self.second = True

    def get(self, path):
        self.paths.append(path)
        section = path.split("/")[-1].split("?")[0]
        if section in self.fail:
            raise MonitorError("Permission denied")
        if section == "status":
            return {"uptime": 200}
        if section == "qemu":
            return [{"vmid": 100, "status": "running", "uptime": self.uptime}]
        if section == "network":
            return [{"iface": "vmbr0", "type": "bridge", "active": 1, "gateway": "192.0.2.1"}]
        if section == "config":
            return {"net0": "virtio=PRIVATE-MAC,bridge=vmbr0,tag=20", "net1": "virtio=x,bridge=vmbr0,tag=25",
                    "cipassword": "PRIVATE-NOT-RETAINED"}
        if section == "netstat":
            rows = [{"vmid": 100, "dev": "net0", "in": self.counter, "out": self.counter * 2}]
            if self.second:
                rows.append({"vmid": 100, "dev": "net1", "in": self.counter * 3, "out": self.counter * 4})
            return rows
        return []


class TopologyTests(unittest.TestCase):
    def test_nic_directions_and_multi_nic_sum_without_double_counting(self):
        api = NetworkAPI()
        poller = NodePoller(api)
        initial = poller.poll(100)
        self.assertIsNone(graph_host(initial, 100, 20)["guests"][0]["rx"])
        api.counter += 50
        api.uptime += 5
        sample = poller.poll(105)
        guest = graph_host(sample, 105, 20)["guests"][0]
        self.assertEqual([(n["rx"], n["tx"]) for n in guest["nics"]], [(10, 20), (30, 40)])
        self.assertEqual((guest["rx"], guest["tx"]), (40, 60))
        self.assertNotIn("PRIVATE", json.dumps(sample))
        self.assertTrue(any(path.endswith("/config?current=1") for path in api.paths))
        self.assertEqual(sum("/config?" in p for p in api.paths), 1)

    def test_counter_loss_reset_and_network_failure_stop_animation(self):
        api = NetworkAPI()
        poller = NodePoller(api)
        poller.poll(100)
        api.counter += 50
        sample = poller.poll(105)
        self.assertIsNotNone(graph_host(sample, 105, 20)["guests"][0]["rx"])
        api.second = False
        lost = poller.poll(110)
        self.assertIsNone(graph_host(lost, 110, 20)["guests"][0]["rx"])
        api.second = True
        regained = poller.poll(115)
        self.assertIsNone(graph_host(regained, 115, 20)["guests"][0]["rx"])
        api.uptime = 1
        reset = poller.poll(120)
        self.assertTrue(all(n["rx"] is None for n in graph_host(reset, 120, 20)["guests"][0]["nics"]))
        api.fail.add("netstat")
        failed = poller.poll(125)
        self.assertTrue(all(n["rx"] is None for n in graph_host(failed, 125, 20)["guests"][0]["nics"]))
        self.assertTrue(all(n["tx"] is None for n in graph_host(sample, 180, 20)["guests"][0]["nics"]))

    def test_failed_configuration_is_not_relabelled_as_live(self):
        api = NetworkAPI()
        poller = NodePoller(api)
        poller.poll(100)
        api.counter += 100
        api.fail.add("config")
        sample = poller.poll(160)
        guest = graph_host(sample, 160, 20)["guests"][0]
        self.assertFalse(guest["configured"])
        self.assertEqual(guest["nics"][0]["segment"], "VLAN 20")
        self.assertIsNone(guest["tx"])
        # A failed config retry stays failed between the minute-spaced attempts.
        self.assertFalse(graph_host(poller.poll(165), 165, 20)["guests"][0]["configured"])

    def test_host_totals_are_guest_sums_not_physical_host_rrd(self):
        api = NetworkAPI()
        poller = NodePoller(api)
        poller.poll(100)
        api.counter += 50
        snapshot = poller.poll(105)
        snapshot["sections"]["rrd"] = {"at": 105, "data": [{"time": 1000, "netin": 999999, "netout": 999999}]}
        host = graph_host(snapshot, 105, 20)
        self.assertEqual(host["uplink"], {"rx": 40, "tx": 60})
        for guest in snapshot["sections"]["qemu"]["data"]:
            guest["status"] = "stopped"
        self.assertEqual(graph_host(snapshot, 105, 20)["uplink"], {"rx": 0, "tx": 0})
        snapshot["errors"]["qemu"] = "Unavailable"
        self.assertEqual(graph_host(snapshot, 105, 20)["uplink"], {"rx": None, "tx": None})

    def test_fast_samples_override_slow_samples_and_fail_closed(self):
        api = NetworkAPI()
        snapshot = NodePoller(api).poll(100)
        snapshot["sections"]["traffic"] = {"at": 105, "data": {
            "qemu:100/net0": {"rx": 1, "tx": 2}, "qemu:100/net1": {"rx": 3, "tx": 4}}}
        self.assertEqual(graph_host(snapshot, 105, 20)["uplink"], {"rx": 4, "tx": 6})
        self.assertIsNone(graph_host(snapshot, 109, 20)["uplink"]["rx"])
        snapshot["errors"]["traffic"] = "Unavailable"
        self.assertIsNone(graph_host(snapshot, 105, 20)["uplink"]["rx"])

    def test_trunks_are_one_nic_not_fabricated_vlan_splits(self):
        rows = interfaces({"net0": "virtio=x,bridge=vmbr0,trunks=20;25,tag=7,link_down=1",
                           "net1": "virtio=y,bridge=vmbr0", "sshkeys": "PRIVATE"})
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["down"])
        self.assertEqual(rows[0]["trunks"], "20;25")
        self.assertIsNone(rows[1]["tag"])


class WanTests(unittest.TestCase):
    def payload(self, age=12, stamp=995, up=1):
        values = {"unpoller_device_wan_receive_rate_bytes": 2000,
                  "unpoller_device_wan_transmit_rate_bytes": 3000,
                  "unpoller_prometheus_cache_age_seconds": age, "source_timestamp": stamp, "up": up}
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"__name__": k}, "value": [1000, str(v)]} for k, v in values.items()]}}

    def test_upstream_age_and_local_age_both_limit_freshness(self):
        result = decode(self.payload(), 100, 1000)
        self.assertEqual((result["rx"], result["tx"]), (2000, 3000))
        self.assertEqual(result["source_age"], 17)
        self.assertTrue(current(result, 115))
        self.assertFalse(current(result, 141))
        for args in ({"age": 100}, {"stamp": 500}, {"up": 0}, {"stamp": 1100}):
            with self.assertRaises(MonitorError):
                decode(self.payload(**args), 100, 1000)

    def test_missing_duplicate_nan_and_wrong_result_fail_closed(self):
        for mutate in (lambda r: r["data"]["result"].pop(),
                       lambda r: r["data"]["result"].append(r["data"]["result"][0]),
                       lambda r: r["data"]["result"][0].update(value=[1000, "NaN"]),
                       lambda r: r["data"].update(resultType="matrix")):
            payload = self.payload()
            mutate(payload)
            with self.assertRaises(MonitorError):
                decode(payload, 100, 1000)

    def test_fixed_selectors_and_no_credentials_in_url(self):
        config = dict(url="http://192.0.2.63:9090", site="example-site", instance="unifi-example", device='gateway"x', port="eth4")
        validate(config)
        self.assertIn('name="gateway\\"x"', query(config))
        # Metric-name matching preserves the timestamp series alongside the rate series.
        self.assertIn('or on (__name__, instance, site, name, port)', query(config))
        for url in ("http://secret@host", "file:///tmp/x", "http://host/?secret=x"):
            with self.assertRaises(ValueError):
                validate(dict(config, url=url))


class HistoryTests(unittest.TestCase):
    def test_history_deduplicates_samples_bounds_memory_and_preserves_gaps(self):
        from vm_arcade.history import Histories, segments
        h = Histories()
        h.add("one", 100, 1, 2, 100)
        h.add("one", 100, 999, 999, 101)
        h.add("one", 105, None, None, 105)
        h.add("one", 110, 3, 4, 110)
        self.assertEqual(len(h.read("one", 110)), 3)
        self.assertEqual(segments(h.read("one", 110), 1, 15), [[(100, 1)], [(110, 3)]])
        self.assertEqual(h.read("two", 110), [])
        self.assertEqual(h.read("one", 411), [])
        for i in range(1600):
            h.add("bounded", i / 4, i, i, i / 4)
        self.assertLessEqual(len(h.read("bounded", 400)), 1201)
        self.assertEqual(segments([(0, 1, 2), (30, 3, 4)], 1, 15), [[(0, 1)], [(30, 3)]])

    def test_fleet_history_is_the_same_sum_as_the_host_links(self):
        from vm_arcade.history import Histories
        h = Histories()
        hosts = [{"name": "node-a", "sampled_at": 100, "traffic_current": True, "uplink": {"rx": 10, "tx": 20}, "guests": []},
                 {"name": "node-b", "sampled_at": 100, "traffic_current": True, "uplink": {"rx": 30, "tx": 40}, "guests": []}]
        h.sample([], hosts, 100, 20)
        self.assertEqual(h.read("hosts", 100), [(100, 40, 60)])
        h.sample([], hosts, 100.1, 20)
        self.assertEqual(len(h.read("hosts", 100.1)), 1)
        hosts[0].update(sampled_at=100.5, uplink={"rx": None, "tx": None})
        h.sample([], hosts, 100.5, 20)
        self.assertEqual(h.read("hosts", 100.5)[-1], (100.5, None, None))


class GraphTests(unittest.TestCase):
    def test_linear_particle_density_and_smooth_motion_on_rate_changes(self):
        from vm_arcade.network_view import particle_positions, point_on_path, FlowParticles
        for value in (None, 0):
            self.assertEqual(particle_positions(value, 10), [])
        a = particle_positions(10000, 10, scale=10000)
        b = particle_positions(10000, 10.04, scale=10000)
        self.assertNotEqual(a, b)
        reverse = particle_positions(10000, 10, True, scale=10000)
        self.assertEqual(reverse, [1 - p for p in a])
        # Both directions use the same scale: ten times the rate, ten times the particles.
        self.assertEqual(len(a), 10 * len(particle_positions(1000, 10, scale=10000)))
        self.assertLessEqual(len(particle_positions(1e20, 10)), 12)
        flow = FlowParticles()
        before = flow.positions("edge", 10000, 10, 10000)
        after = flow.positions("edge", 1000, 10.04, 10000)
        self.assertAlmostEqual(after[0] - before[0], .04 / 1.6)
        self.assertEqual(flow.positions("edge", None, 10.08, 10000), [])
        self.assertEqual(point_on_path([(0, 0), (10, 0), (10, 30)], .5), (10, 10))

    def test_wan_particles_represent_bytes_without_normalizing_idle_traffic(self):
        from vm_arcade.network_view import VolumeParticles
        flow = VolumeParticles()
        self.assertEqual(flow.positions(32768, 3276.8, 0), [[], []])
        # Count emitted dots by accounting for elapsed bytes and fractional credit.
        for frame in range(1, 301):
            flow.positions(32768, 3276.8, frame / 30)
        self.assertEqual(flow.quantum, 32768)
        self.assertAlmostEqual(flow.lanes[0][1], 0, places=5)
        self.assertAlmostEqual(flow.lanes[1][1], 0, places=5)
        quiet = VolumeParticles()
        quiet.positions(100, 10, 0)
        self.assertEqual(quiet.positions(100, 10, .5), [[], []])
        self.assertEqual(quiet.quantum, flow.quantum)
        self.assertEqual(flow.positions(0, 0, 10.1), [[], []])
        self.assertEqual(flow.positions(None, 10000, 10.2), [[], []])

    def test_wan_shared_volume_unit_does_not_shrink_after_a_burst(self):
        from vm_arcade.network_view import VolumeParticles
        flow = VolumeParticles()
        flow.positions(10**8, 10**7, 0)
        unit = flow.quantum
        flow.positions(1000, 100, .5)
        self.assertEqual(flow.quantum, unit)
        self.assertGreater(unit, 32768)
        self.assertIn('MiB', flow.label())

    def test_keyboard_and_cabinet_can_toggle_without_onscreen_controls(self):
        controls = Controls("/nonexistent")
        self.assertEqual(controls.keys[110], "network")
        self.assertEqual(controls.keys[32], "toggle_view")
        selection = Selection()
        selection.act("toggle_view", [1], [])
        self.assertEqual(selection.view, "metrics")
        selection.act("toggle_view", [1], [])
        self.assertEqual(selection.view, "network")
        selection.act("overview", [1], [])
        self.assertEqual(selection.view, "metrics")
        selection.act("back", [1], [])
        self.assertEqual(selection.view, "network")


if __name__ == "__main__":
    unittest.main()
