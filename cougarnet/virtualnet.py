#!/usr/bin/python3

import argparse
import ipaddress
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

from pygraphviz import AGraph

MAC_RE = re.compile(r'^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$')

TERM="xfce4-terminal"
HOSTPREP_MODULE="cougarnet.hostprep"
TMPDIR="./tmp"

class HostNotStarted(Exception):
    pass

class Host(object):
    def __init__(self, hostname, gw4=None, gw6=None, type='node'):
        self.hostname = hostname
        self.pid = None
        self.pidfile = None
        self.config_file = None
        self.next_int_num = 0
        self.int_to_neighbor = {}
        self.neighbor_to_int = {}
        self.int_to_mac = {}
        self.int_to_ip4 = {}
        self.int_to_ip6 = {}
        self.gw4 = gw4
        self.gw6 = gw6
        self.type = type

    def create_config(self, commsock):
        cmd = ['mkdir', '-p', TMPDIR]
        subprocess.run(cmd)
        fd, self.config_file = tempfile.mkstemp(suffix='.cfg',
                prefix=f'{self.hostname}-', dir=TMPDIR)

        with os.fdopen(fd, 'w') as fh:
            gw4 = self.gw4 if self.gw4 is not None else ''
            gw6 = self.gw6 if self.gw6 is not None else ''
            fh.write(f'{self.hostname},{self.type},{gw4},{gw6},{commsock}\n')
            for intf in self.int_to_neighbor:
                mac = self.int_to_mac[intf] if self.int_to_mac[intf] is not None else ''
                fh.write(f'{intf},{mac}')
                for addr in self.int_to_ip4[intf]:
                    fh.write(f',{addr}')
                for addr in self.int_to_ip6[intf]:
                    fh.write(f',{addr}')
                fh.write('\n')

    def create_hosts_file_entries(self, fh):
        for intf in self.int_to_neighbor:
            for addr in self.int_to_ip4[intf]:
                slash = addr.find('/')
                if slash >= 0:
                    addr = addr[:slash]
                fh.write(f'{addr} {self.hostname}\n')
            for addr in self.int_to_ip6[intf]:
                slash = addr.find('/')
                if slash >= 0:
                    addr = addr[:slash]
                fh.write(f'{addr} {self.hostname}\n')

    def start(self, hosts_file):
        assert self.config_file is not None, \
                "create_config() must be called before start()"

        cmd = ['sudo', 'mkdir', '-p', '/run/netns']
        subprocess.run(cmd, check=True)

        cmd = ['sudo', 'touch', f'/run/netns/{self.hostname}']
        subprocess.run(cmd)

        cmd = ['mkdir', '-p', TMPDIR]
        subprocess.run(cmd)
        fd, self.pidfile = tempfile.mkstemp(suffix='.pid',
                prefix=f'{self.hostname}-', dir=TMPDIR)
        os.close(fd)

        cmd = [TERM, '-e', f'sudo -E unshare '+ \
                f'--mount ' + \
                f'--net=/run/netns/{self.hostname} ' + \
                f'--uts ' + \
                f'{sys.executable} -m {HOSTPREP_MODULE} ' + \
                f'--hosts-file {hosts_file} ' + \
                f'--disable-native-stack ' + \
                f'--user {os.environ.get("USER")} ' + \
                f'{self.pidfile} {self.config_file}']
        p = subprocess.Popen(cmd)

        while True:
            with open(self.pidfile, 'r') as fh:
                try:
                    self.pid = int(fh.read())
                    break
                except ValueError:
                    pass

                if p.poll() is not None:
                    cmd = ['rm', self.pidfile]
                    subprocess.run(cmd)
                    raise HostNotStarted(f'{self.hostname} did not start properly.')
                time.sleep(1)

    def add_int(self, intf, host):
        self.int_to_neighbor[intf] = host
        self.neighbor_to_int[host] = intf

    def next_int(self):
        int_next = self.next_int_num
        self.next_int_num += 1
        return int_next

    def signal(self, signal_type):
        if self.pid is None:
            return
        cmd = ['sudo', 'kill', f'-{signal_type}', str(self.pid)]
        subprocess.run(cmd)

    def cleanup(self):
        self.signal('KILL')

        #XXX not sure why this while loop is necessary (i.e., why we need to
        #XXX umount several times)
        while True:
            cmd = ['sudo', 'umount', f'/run/netns/{self.hostname}']
            try:
                subprocess.run(cmd, stderr=subprocess.DEVNULL, check=True)
            except subprocess.CalledProcessError:
                break

        cmd = ['sudo', 'rm', f'/run/netns/{self.hostname}']
        subprocess.run(cmd)

        cmd = ['rm', self.config_file]
        subprocess.run(cmd)

    def label_for_int(self, intf):
        label = ''
        m = re.search(r'\d+$', intf)
        if m is not None:
            label += m.group(0)
        if self.int_to_ip4[intf]:
            if label:
                label += '<BR/>'
            label += '<BR/>'.join(self.int_to_ip4[intf])
        if self.int_to_ip6[intf]:
            if label:
                label += '<BR/>'
            label += '<BR/>\n'.join(self.int_to_ip6[intf])
        return label

class VirtualNetwork(object):
    def __init__(self):
        self.host_by_name = {}
        self.hosts_file = None

    def import_int(self, hostname_addr):
        parts = hostname_addr.split(',')
        hostname = parts[0]
        mac = None
        addrs = parts[1:]
        addrs4 = []
        addrs6 = []
        subnet4 = None
        subnet6 = None
        for addr in addrs:

            # MAC address
            m = MAC_RE.search(addr)
            if m is not None:
                if mac is not None:
                    raise ValueError(f'Only one MAC address is allowed')
                mac = addr
                continue

            # IP address
            slash = addr.find('/')
            if slash < 0:
                raise ValueError(f'Address for {hostname} interface must include prefix length!')
            if ':' in addr:
                # IPv6 address
                subnet = str(ipaddress.IPv6Network(addr, strict=False))
                if subnet6 is None:
                    subnet6 = subnet
                if subnet6 != subnet:
                    raise ValueError(f'All connected IP addresses must be on the same subnet!')
                addrs6.append(addr)
            else:
                # IPv4 address
                subnet = str(ipaddress.IPv4Network(addr, strict=False))
                if subnet4 is None:
                    subnet4 = subnet
                if subnet4 != subnet:
                    raise ValueError(f'All connected IP addresses must be on the same subnet!')
                addrs4.append(addr)

        if hostname not in self.host_by_name:
            self.host_by_name[hostname] = Host(hostname)
        host = self.host_by_name[hostname]
        return host, mac, addrs4, addrs6, subnet4, subnet6

    def import_link(self, line):
        parts = line.split()
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f'Invalid link format.')

        int1_info, int2_info = parts[:2]
        host1, mac1, addrs41, addrs61, subnet41, subnet61 = \
                self.import_int(int1_info)
        host2, mac2, addrs42, addrs62, subnet42, subnet62 = \
                self.import_int(int2_info)

        if set(addrs41).intersection(set(addrs42)):
            raise ValueError(f'Addresses for {host1.hostname} and {host2.hostname} cannot be the same!')
        if subnet41 is not None and subnet42 is not None and subnet41 != subnet42:
            raise ValueError(f'Addresses for {host1.hostname} and {host2.hostname} must be in the same subnet!')
        if set(addrs61).intersection(set(addrs62)):
            raise ValueError(f'Addresses for {host1.hostname} and {host2.hostname} cannot be the same!')
        if subnet61 is not None and subnet62 is not None and subnet61 != subnet62:
            raise ValueError(f'Addresses for {host1.hostname} and {host2.hostname} must be in the same subnet!')

        if len(parts) > 2:
            attrs = dict([p.split('=', maxsplit=1) for p in parts[2].split(',')])
        else:
            attrs = {}

        self.add_link(host1, host2, **attrs)
        host1.int_to_mac[host1.neighbor_to_int[host2]] = mac1
        host2.int_to_mac[host2.neighbor_to_int[host1]] = mac2
        host1.int_to_ip4[host1.neighbor_to_int[host2]] = addrs41
        host2.int_to_ip4[host2.neighbor_to_int[host1]] = addrs42
        host1.int_to_ip6[host1.neighbor_to_int[host2]] = addrs61
        host2.int_to_ip6[host2.neighbor_to_int[host1]] = addrs62

    @classmethod
    def from_file(cls, fh):
        net = cls()
        mode = None
        for line in fh:
            line = line.strip()

            if line == 'NODES':
                mode = 'node'
                continue
            elif line == 'LINKS':
                mode = 'link'
                continue
            elif not line:
                continue
            elif line.startswith('#'):
                continue

            if mode == 'node':
                net.import_node(line)
            elif mode == 'link':
                net.import_link(line)
            else:
                pass

        return net

    def import_node(self, line):
        parts = line.split()
        if len(parts) < 1 or len(parts) > 2:
            raise ValueError(f'Invalid node format.')

        hostname = parts[0]
        if len(parts) > 1:
            attrs = dict([p.split('=', maxsplit=1) for p in parts[1].split(',')])
        else:
            attrs = {}

        self.host_by_name[hostname] = Host(hostname, **attrs)

    def add_link(self, host1, host2, bw=None, delay=None):
        if isinstance(host1, str):
            host1 = self.host_by_name[host1]
        if isinstance(host2, str):
            host2 = self.host_by_name[host2]

        num1 = host1.next_int()
        num2 = host2.next_int()
        int1 = f'{host1.hostname}-eth{num1}'
        int2 = f'{host2.hostname}-eth{num2}'
        host1.add_int(int1, host2)
        host2.add_int(int2, host1)

    def apply_links(self):
        done = set()
        for hostname, host in self.host_by_name.items():
            for intf, neighbor in host.int_to_neighbor.items():
                host1 = host
                host2 = neighbor
                int1 = intf
                int2 = host2.neighbor_to_int[host]
                if (host1, host2, int1, int2) in done:
                    continue
                done.add((host1, host2, int1, int2))
                done.add((host2, host1, int2, int1))
                cmd = ['sudo', 'ip', 'link', 'add',
                        int1, 'netns', host1.hostname,
                        'type', 'veth', 'peer', 'name',
                        int2, 'netns', host2.hostname]
                subprocess.run(cmd, check=True)

    def create_hosts_file(self):
        cmd = ['mkdir', '-p', TMPDIR]
        subprocess.run(cmd)
        fd, self.hosts_file = tempfile.mkstemp(prefix=f'hosts-', dir=TMPDIR)

        with os.fdopen(fd, 'w') as fh:
            fh.write('127.0.0.1 localhost\n')

            for hostname, host in self.host_by_name.items():
                host.create_hosts_file_entries(fh)

    def config(self):
        cmd = ['mkdir', '-p', TMPDIR]
        subprocess.run(cmd)
        fd, self.commsock_file = tempfile.mkstemp(suffix='.sock', dir=TMPDIR)
        os.close(fd)
        subprocess.run(['rm', self.commsock_file])
        self.commsock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM, 0)
        self.commsock.bind(self.commsock_file)

        self.create_hosts_file()
        for hostname, host in self.host_by_name.items():
            host.create_config(self.commsock_file)

    def signal_hosts(self, signal):
        for hostname, host in self.host_by_name.items():
            host.signal(signal)

    def wait_for_startup(self):
        while True:
            none_exists = True
            for hostname, host in self.host_by_name.items():
                if os.path.exists(host.pidfile):
                    pid = open(host.pidfile, 'r').read()
                    cmd = ['ps', '-p', pid]
                    if subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode != 0:
                        cmd = ['rm', host.pidfile]
                        subprocess.run(cmd)
                        raise HostNotStarted(f'{hostname} did not start properly.')
                    none_exists = False
            if none_exists:
                break
            time.sleep(1)

    def start(self):
        for hostname, host in self.host_by_name.items():
            host.start(self.hosts_file)

        self.apply_links()
        self.signal_hosts('HUP')
        self.wait_for_startup()
        self.signal_hosts('HUP')

    def cleanup(self):
        for hostname, host in self.host_by_name.items():
            host.cleanup()

        cmd = ['rm', self.hosts_file]
        subprocess.run(cmd)

        self.commsock.close()
        cmd = ['rm', self.commsock_file]
        subprocess.run(cmd)

    def display(self, to_screen, output_file):
        G = AGraph()

        done = set()
        for hostname, host in self.host_by_name.items():
            for intf, neighbor in host.int_to_neighbor.items():
                host1 = host
                host2 = neighbor
                int1 = intf
                int2 = host2.neighbor_to_int[host]
                if (host1, host2, int1, int2) in done:
                    continue
                done.add((host1, host2, int1, int2))
                done.add((host2, host1, int2, int1))
                G.add_edge(host1.hostname, host2.hostname,
                        headlabel=f'<{host1.label_for_int(int1)}>',
                        taillabel=f'<{host2.label_for_int(int2)}>')
        img = G.draw(prog='dot')
        if to_screen:
            subprocess.run(['graph-easy', '--from', 'graphviz'], input=img,
                    stderr=subprocess.DEVNULL)
        if output_file:
            G.draw(output_file, prog='dot')

    def message_loop(self):
        start_time = time.time()
        while True:
            data, peer = self.commsock.recvfrom(4096)
            try:
                hostname, msg = data.decode('utf-8').split(',', maxsplit=1)
            except:
                sys.stderr.write('Malformed message: %s' % str(data))
            ts = time.time() - start_time
            print('%000.3f \033[1m%4s\033[0m  %s' % (ts, hostname, msg))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--display',
            action='store_const', const=True, default=False,
            help='Display the network configuration as text')
    parser.add_argument('--output-file', '-o',
            type=argparse.FileType('w'), action='store',
            help='Print the network configuration to a file')
    parser.add_argument('config_file',
            type=argparse.FileType('r'), action='store',
            help='File containing the network configuration')
    args = parser.parse_args(sys.argv[1:])

    net = VirtualNetwork.from_file(args.config_file)
    if args.display:
        net.display(args.output_file)
    try:
        net.config()
        net.start()
        sys.stdout.write('Ctrl-c to quit\n')
        net.message_loop()
    except KeyboardInterrupt:
        pass
    finally:
        net.cleanup()

if __name__ == '__main__':
    main()