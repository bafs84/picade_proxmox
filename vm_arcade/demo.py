"""Explicit demo mode only. No fallback from failed live connections."""
import math
import time

GIB = 1024 ** 3


class DemoFleet:
    def read(self):
        now, wall = math.floor(time.monotonic() * 2) / 2, time.time()
        result = []
        for index, name in enumerate(("node-a", "node-b", "node-c")):
            core_loads = {"cpu" + str(i): max(0, min(100, [18, 42, 11][index] + 18 * math.sin(now * .6 + i * .7 + index))) for i in range(20)}
            cpu = sum(core_loads.values()) / len(core_loads)
            mem = [9.8, 18.7, 8.1][index] + .25 * math.sin(now * .1 + index)
            vms = []
            names = [("edge-gateway", "research-api"), ("analysis-lab", "sandbox"), ("lab-notebook", "metrics")][index]
            for i, label in enumerate(names):
                vmid = 201 + index * 10 + i
                stopped = index == 1 and i == 1
                vms.append({"key": "qemu:" + str(vmid), "id": vmid, "kind": "qemu", "name": label,
                            "status": "stopped" if stopped else "running", "cpu": 0 if stopped else 12 + i * 16,
                            "cpus": [2, 4][i], "mem": 0 if stopped else [0.8, 3.2][i] * GIB,
                            "maxmem": [2, 8][i] * GIB, "maxdisk": [32, 64][i] * GIB,
                            "uptime": 0 if stopped else 271600,
                            "rates": {k: None if stopped else v * 1024 ** 2 for k, v in
                                      zip(("netin", "netout", "diskread", "diskwrite"), (3.2, 2.4, 0.2, 0.8))}})
            status = {"cpu": cpu / 100, "wait": .006, "uptime": 1052280,
                      "cpuinfo": {"model": "Intel i7-12700T", "cpus": 20, "cores": 12},
                      "loadavg": [[2.64, 3.18, 2.91], [8.72, 7.48, 5.32], [1.26, 1.44, 1.62]][index],
                      "memory": {"used": mem * GIB, "total": 31.2 * GIB, "available": (30.2 - mem) * GIB},
                      "swap": {"used": .2 * GIB, "total": 8 * GIB}}
            rrd = [{"time": wall - (60 - i) * 60, "cpu": (cpu + math.sin(i * .31) * 5) / 100,
                    "netin": (8.4 + math.sin(i * .2)) * 1024 ** 2,
                    "netout": (3.1 + math.sin(i * .3) * .7) * 1024 ** 2} for i in range(61)]
            result.append({"name": name, "errors": {}, "sections": {
                "cores": {"at": now, "data": dict(core_loads, cpu=cpu)},
                "status": {"at": now, "data": status}, "qemu": {"at": now, "data": vms},
                "lxc": {"at": now, "data": []}, "rrd": {"at": now, "data": rrd},
                "storage": {"at": now, "data": [{"storage": "local-btrfs", "type": "btrfs",
                                              "used": [238, 516, 184][index] * GIB, "total": 894 * GIB,
                                              "avail": (894 - [238, 516, 184][index]) * GIB, "active": 1}]}}})
        for index, snapshot in enumerate(result):
            guests = snapshot["sections"]["qemu"]["data"]
            attachments, traffic = {}, {}
            for i, guest in enumerate(guests):
                attachments[guest["key"]] = {"at": now, "data": [{"dev": "net0", "bridge": "vmbr0",
                    "tag": "20" if i == 0 else "25" if index == 1 else None, "trunks": None, "down": False}]}
                traffic[guest["key"] + "/net0"] = {"rx": (1 + math.sin(now * .4 + index)) * (i + 1) * 125000,
                                                      "tx": (1 + math.cos(now * .3 + i)) * (index + 1) * 65000}
            snapshot["sections"]["topology"] = {"at": now, "data": {
                "network": {"at": now, "data": [{"iface": "vmbr0", "type": "bridge", "active": 1,
                    "cidr": "192.0.2.{}/24".format(101 + index), "gateway": "192.0.2.1", "bridge_ports": "eth0"}]},
                "attachments": attachments, "traffic": traffic}}
        return result

    def wan(self):
        now = time.monotonic()
        return {"at": now, "source_age": 0, "rx": (2 + math.sin(now * .4)) * 3800000,
                "tx": (2 + math.cos(now * .3)) * 520000, "device": "DEMO GATEWAY", "port": "WAN1",
                "gateway": "192.0.2.1"}

    def close(self):
        pass
