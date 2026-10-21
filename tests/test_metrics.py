import copy
import unittest

from vm_arcade.cores import CoreRates, command
from vm_arcade.demo import DemoFleet
from vm_arcade.metrics import Metrics, TrafficParticles, cluster, host_cpu
from vm_arcade.topology import graph_host
from vm_arcade.telemetry import NodePoller
from test_telemetry import FakeAPI


class CoreTests(unittest.TestCase):
    def payload(self, at, user, idle, guest=0, boot=1):
        return {'at': at, 'stat': ['cpu %d 0 0 %d 0 0 0 0 %d 0' % (user,idle,guest),
                                  'cpu0 %d 0 0 %d 0 0 0 0 %d 0' % (user,idle,guest),
                                  'btime %d' % boot]}

    def test_per_core_delta_and_guest_time_not_counted_twice(self):
        rates = CoreRates()
        self.assertIsNone(rates.sample(self.payload(10,100,100,90))['cpu0'])
        result = rates.sample(self.payload(10.5,110,140,100))
        self.assertEqual(result, {'cpu': 20, 'cpu0': 20})

    def test_reboot_gap_and_rollback_clear_readings(self):
        for payload in (self.payload(11,200,200,boot=2), self.payload(14,200,200), self.payload(11,1,1)):
            rates = CoreRates()
            rates.sample(self.payload(10,100,100))
            self.assertIsNone(rates.sample(payload)['cpu0'])

    def test_fixed_read_only_command_requires_pinned_host(self):
        argv = command({'url':'https://192.0.2.1:8006','cpu_ssh':
                        {'user':'monitor','identity_file':'/key','known_hosts_file':'/known'}})
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('monitor@192.0.2.1',argv)
        self.assertIn("/proc/stat", argv[-1])
        self.assertNotIn('sudo',argv[-1])

    def test_jump_connection_keeps_identity_checks_on_both_hops(self):
        node = {'url':'https://192.0.2.2:8006', 'cpu_ssh':
                {'user':'monitor', 'identity_file':'/private key', 'known_hosts_file':'/known',
                 'jump_host':'192.0.2.1'}}
        argv = command(node)
        proxy = next(a for a in argv if a.startswith('ProxyCommand='))
        self.assertIn('StrictHostKeyChecking=yes', proxy)
        self.assertIn('monitor@192.0.2.1', proxy)
        self.assertIn("'/private key'", proxy)
        self.assertIn('monitor@192.0.2.2', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = DemoFleet().read()
        self.now = self.snapshots[0]['sections']['status']['at']

    def test_cluster_cpu_is_capacity_weighted_and_missing_is_not_zero(self):
        nodes = self.snapshots[:2]
        for node, cpu, count in zip(nodes,(.2,.8),(2,6)):
            node['sections'].pop('cores',None)
            node['sections']['status']['data']['cpu'] = cpu
            node['sections']['status']['data']['cpuinfo']['cpus'] = count
        hosts = [graph_host(s,self.now,20) for s in nodes]
        result = cluster(nodes,hosts,self.now,20)
        self.assertEqual(result['cpu'],65)
        nodes[1]['errors']['status']='offline'
        self.assertIsNone(cluster(nodes,hosts,self.now,20)['cpu'])

    def test_smoothing_cannot_overshoot_and_unknown_clears(self):
        m = Metrics()
        self.assertEqual(m.smooth('cpu',10,10),10)
        value = m.smooth('cpu',90,10.1)
        self.assertGreater(value,10)
        self.assertLess(value,90)
        self.assertIsNone(m.smooth('cpu',None,10.2))
        self.assertEqual(m.smooth('cpu',0,10.3),0)

    def test_histories_deduplicate_and_insert_an_outage_gap(self):
        m = Metrics()
        m.add('node-a/cpu',10,(20,None),10)
        m.add('node-a/cpu',10,(20,None),10.5)
        self.assertEqual(len(m.history.read('node-a/cpu',10.5)),1)
        m.add('node-a/cpu',10,(None,None),11)
        self.assertEqual(m.history.read('node-a/cpu',11)[-1],(11,None,None))
        m.add('node-a/cpu',12,(30,None),12)
        m.add('node-b/cpu',12,(90,None),12)
        self.assertEqual(m.peak('node-a/cpu',12),30)

    def test_core_source_falls_back_without_fabricating_thread_values(self):
        node = self.snapshots[0]
        node['sections']['cores']={'at':self.now,'data':{'cpu':53,'cpu0':71}}
        self.assertEqual(host_cpu(node,self.now,20)[0],53)
        node['errors']['cores']='offline'
        self.assertEqual(host_cpu(node,self.now,20)[0],node['sections']['status']['data']['cpu']*100)

    def test_selected_network_uses_fast_nic_samples_not_slow_guest_rates(self):
        node = self.snapshots[0]
        node['sections']['traffic']={'at':self.now,'data':{'qemu:201/net0':{'rx':777,'tx':111},
                                                          'qemu:202/net0':{'rx':222,'tx':333}}}
        m = Metrics()
        m.observe([node],self.now,20)
        self.assertEqual(m.history.read('node-a/qemu:201/net',self.now)[-1][1:],(777,111))
        self.assertEqual(m.history.read('node-a/net',self.now)[-1][1:],(999,444))
        node['errors']['traffic']='offline'
        m.observe([node],self.now+.1,20)
        self.assertEqual(m.history.read('node-a/qemu:201/net',self.now+.1)[-1][1:],(None,None))

    def test_fast_metrics_do_not_repoll_storage_or_clear_its_failure(self):
        api = FakeAPI()
        api.fail.add('storage')
        poller = NodePoller(api)
        first = poller.poll(100)
        second = poller.poll(100.5)
        self.assertIn('storage',second['errors'])
        self.assertEqual(sum(p.endswith('/storage') for p in api.paths),1)
        self.assertEqual(second['sections']['status']['at'],100.5)
        self.assertEqual(sum(p.endswith('/status') for p in api.paths),2)

    def test_parallel_worker_errors_do_not_leak_into_metric_poller_cache(self):
        poller = NodePoller(FakeAPI())
        snapshot = poller.poll(100)
        snapshot['errors']['cores'] = 'Transient stream failure'
        snapshot['errors']['traffic'] = 'Transient network failure'
        recovered = poller.poll(100.5)
        self.assertNotIn('cores', recovered['errors'])
        self.assertNotIn('traffic', recovered['errors'])

    def test_traffic_enters_at_endpoints_and_travels_in_opposite_directions(self):
        flow = TrafficParticles()
        self.assertEqual(flow.positions(4096, 4096, 0), [[], []])
        self.assertEqual(flow.positions(4096, 4096, .5), [[], []])
        self.assertEqual(flow.positions(0, 0, 1), [[0], [1]])
        self.assertEqual(flow.positions(0, 0, 1.25), [[.25], [.75]])
        self.assertEqual(flow.positions(0, 0, 1.75), [[.75], [.25]])
        self.assertEqual(flow.positions(0, 0, 2), [[], []])

    def test_ten_times_the_rate_emits_ten_times_the_particles(self):
        flow = TrafficParticles()
        emitted = [set(), set()]
        for frame in range(61):
            flow.positions(40960, 4096, frame / 30)
            for direction in (0, 1):
                emitted[direction].update(identity for identity, _ in flow.particles[direction])
        self.assertEqual([len(ids) for ids in emitted], [20, 2])

    def test_bursts_and_reversal_change_emissions_without_replacing_inflight_dots(self):
        flow = TrafficParticles()
        flow.positions(40960, 4096, 0)
        flow.positions(40960, 4096, .5)
        before = copy.deepcopy(flow.particles)
        lanes = flow.positions(4096, 81920, .5)
        self.assertEqual(flow.particles, before)  # No dots created/deleted by the new sample.
        after = flow.positions(4096, 81920, .75)
        self.assertEqual(flow.particles[0], before[0])
        for a, b in zip(lanes[0], after[0]):
            self.assertAlmostEqual(b - a, .25)
        self.assertEqual(len(flow.particles[1]), 5)  # Higher TX rate launches new dots.
        self.assertTrue(all(.5 < birth <= .75 for _, birth in flow.particles[1]))

    def test_zero_and_unknown_stop_emissions_but_particles_finish_their_journey(self):
        for stopped in (0, None):
            flow = TrafficParticles()
            flow.positions(40960, 40960, 0)
            flow.positions(stopped, stopped, .5)
            before = copy.deepcopy(flow.particles)
            self.assertEqual([len(dots) for dots in before], [5, 5])
            flow.positions(stopped, stopped, 1)
            self.assertEqual(flow.particles, before)
            flow.positions(stopped, stopped, 1.25)
            self.assertEqual(flow.particles, [[dot for dot in lane if dot[1] > .25]
                                              for lane in before])
            self.assertEqual(flow.positions(stopped, stopped, 1.5), [[], []])

    def test_low_traffic_accumulates_bytes_without_a_forced_minimum_particle_rate(self):
        flow = TrafficParticles()
        for frame in range(120):
            self.assertEqual(flow.positions(1024, 0, frame / 30), [[], []])
        lanes = flow.positions(1024, 0, 4)
        self.assertEqual(len(lanes[0]), 1)
        self.assertAlmostEqual(lanes[0][0], 0)
        self.assertEqual(lanes[1], [])

    def test_hidden_or_disconnected_interval_does_not_create_a_catchup_burst(self):
        flow = TrafficParticles()
        flow.positions(40960, 40960, 0)
        flow.positions(40960, 40960, .5)
        self.assertEqual(flow.positions(4096, 0, 10), [[], []])
        self.assertEqual(flow.positions(4096, 0, 10.5), [[], []])
        self.assertEqual(flow.positions(4096, 0, 11), [[0], []])

    def test_extreme_traffic_bounds_emissions_and_preserves_existing_particles(self):
        flow = TrafficParticles()
        flow.positions(1e9, 1e9, 0)
        for frame in range(1, 61):
            flow.positions(1e9, 1e9, frame / 30)
            self.assertTrue(flow.limited)
            self.assertTrue(all(len(lane) <= 513 for lane in flow.particles))
        before = copy.deepcopy(flow.particles)
        flow.positions(0, 0, 2)
        self.assertEqual(flow.particles, before)
        self.assertFalse(flow.limited)

    def test_traffic_panel_labels_bars_and_particles_use_identical_rates(self):
        from vm_arcade.display import Display
        from vm_arcade.network_view import rate
        display = object.__new__(Display)
        display.metrics, display.metric_flows = Metrics(), {}
        display.metric_host = {'uplink': {'rx': 20_000, 'tx': 2000}, 'traffic_current': True}
        texts, bars = [], []
        display.text = lambda *args, **kwargs: texts.append(args[0])
        display.meter = lambda *args, **kwargs: bars.append(args)
        display.line = display.rect = display.panel = lambda *args, **kwargs: None
        node = self.snapshots[0]
        display.host_panels(node, node['sections']['status']['data'], self.now, 20)
        flow = display.metric_flows[node['name']]
        self.assertEqual(flow.rates, [20_000, 2000])
        traffic_bars = [bar for bar in bars if bar[0] == 610]
        self.assertEqual([bar[3] for bar in traffic_bars], [20_000, 2000])
        self.assertEqual([bar[4] for bar in traffic_bars], [flow.scale, flow.scale])
        self.assertIn(rate(20_000), texts)
        self.assertIn(rate(2000), texts)
