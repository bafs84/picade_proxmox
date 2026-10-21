"""Optional, pinned SSH stream of read-only /proc/stat counters; no remote install."""
import json
import hashlib
import math
import os
from pathlib import Path
import re
import select
import shlex
import subprocess
import time
from urllib.parse import urlsplit

# Constant command: configuration cannot supply a shell program. One connection
# per node, rather than two SSH handshakes every second. No persistent files.
PROGRAM = '''import json,time
while True:
    started=time.monotonic()
    with open('/proc/stat') as source:
        rows=[line.strip() for line in source if line.startswith(('cpu','btime '))]
    print(json.dumps({'at':started,'stat':rows}),flush=True)
    time.sleep(max(0,0.5-(time.monotonic()-started)))
'''


class CoreRates:
    def __init__(self):
        self.previous = None

    def sample(self, payload):
        timestamp = float(payload['at'])
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError('Invalid sample time')
        rows, boot = {}, None
        for line in payload['stat']:
            fields = line.split()
            if fields[0] == 'btime':
                boot = int(fields[1])
            elif re.fullmatch(r'cpu\d*', fields[0]) and len(fields) >= 9:
                # guest/guest_nice are already included in user/nice.
                values = tuple(int(v) for v in fields[1:9])
                if min(values) < 0:
                    raise ValueError('Invalid CPU counters')
                rows[fields[0]] = values
        if 'cpu' not in rows or boot is None or len(rows) > 4097:
            raise ValueError('Incomplete CPU counters')
        previous = self.previous
        self.previous = (timestamp, boot, rows)
        result = dict.fromkeys(rows)
        if not previous or boot != previous[1] or not 0 < timestamp - previous[0] <= 3:
            return result
        for name, values in rows.items():
            old = previous[2].get(name)
            if old is None:
                continue
            delta = [a - b for a, b in zip(values, old)]
            if min(delta) < 0 or not sum(delta):
                continue
            result[name] = 100 * (sum(delta) - delta[3] - delta[4]) / sum(delta)
        return result


def command(node):
    config = node['cpu_ssh']
    options = ['-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
               '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ServerAliveInterval=2', '-o', 'ServerAliveCountMax=2',
               '-o', 'UserKnownHostsFile=' + config['known_hosts_file'],
               '-i', config['identity_file']]
    def connection_options(host):
        result = list(options)
        if config.get('control_dir'):
            identity = host + '/' + config['user'] + '/' + config['identity_file']
            socket = str(Path(config['control_dir']) / hashlib.sha256(identity.encode()).hexdigest()[:20])
            result += ['-o', 'ControlMaster=auto', '-o', 'ControlPersist=60', '-o', 'ControlPath=' + socket]
        return result
    proxy = []
    if config.get('jump_host'):
        jump = ['ssh'] + connection_options(config['jump_host']) + ['-W', '%h:%p', config['user'] + '@' + config['jump_host']]
        proxy = ['-o', 'ProxyCommand=' + ' '.join(shlex.quote(arg) for arg in jump)]
    return ['ssh'] + connection_options(urlsplit(node['url']).hostname) + proxy + [
        config['user'] + '@' + urlsplit(node['url']).hostname,
        'python3 -u -c ' + shlex.quote(PROGRAM)]



def stream(node, stop, publish):
    failures = 0
    while not stop.is_set():
        process = None
        try:
            process = subprocess.Popen(command(node), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            rates, buffer, last = CoreRates(), b'', time.monotonic()
            while not stop.is_set():
                if not select.select([process.stdout], [], [], .25)[0]:
                    if time.monotonic() - last > 3:
                        raise ValueError('Stream timed out')
                    continue
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    raise ValueError('Stream ended')
                buffer += block
                if len(buffer) > 512 * 1024:
                    raise ValueError('Stream too large')
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    data = rates.sample(json.loads(raw))
                    last = time.monotonic()
                    publish(data, last)
                    failures = 0
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            publish(None, time.monotonic())
            failures += 1
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
        stop.wait(min(60, 2 ** min(failures, 6)))
