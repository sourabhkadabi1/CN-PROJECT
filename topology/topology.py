# ============================================================
# TCP BACKUP SYSTEM
# MININET TOPOLOGY  (two alternative paths between client and servers)
#
#                  +---- s2 ----+            PATH A: fast  (10 Mbps, 5 ms per link)
#   h1 --- s1 -----+            +----- s4 --- h2   (backup server 1)
#  (client)        +---- s3 ----+        \--- h3   (backup server 2)
#                                          PATH B: slow  ( 4 Mbps, 30 ms per link)
#
# Run it alone:   sudo python3 -m topology.topology
# (start the controller first:  osken-manager controller/controller.py)
# ============================================================

import os

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController


# ============================================================
# SETTINGS
# ============================================================

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

HOST_BANDWIDTH = 15          # Mbps, faster than the core so hosts are never the bottleneck

# name of link, (switch, port), (switch, port), bandwidth Mbps, delay ms
CORE_LINKS = [
    ("A1", ("s1", 2), ("s2", 1), 10, 5),
    ("A2", ("s2", 2), ("s4", 3), 10, 5),
    ("B1", ("s1", 3), ("s3", 1), 4, 30),
    ("B2", ("s3", 2), ("s4", 4), 4, 30),
]

# host, IP address, (switch, port)
HOSTS = [
    ("h1", "10.0.0.1", ("s1", 1)),
    ("h2", "10.0.0.2", ("s4", 1)),
    ("h3", "10.0.0.3", ("s4", 2)),
]


# ============================================================
# BUILD THE NETWORK
# ============================================================

def build_network(with_delay=True):
    # Containers / WSL without the OVS kernel module: export MININET_USERSPACE=1
    userspace = os.environ.get("MININET_USERSPACE") == "1"
    switch_options = {"datapath": "user"} if userspace else {}

    def make_switch(name, **params):
        # OpenFlow 1.3, and "secure" = never forward without a rule from the controller
        return OVSSwitch(name, protocols="OpenFlow13", failMode="secure",
                         **dict(switch_options, **params))

    # autoStaticArp: hosts already know each other's MAC, so no ARP broadcasts are needed
    net = Mininet(controller=None, switch=make_switch, link=TCLink,
                  autoSetMacs=True, autoStaticArp=True)

    net.addController("c0", controller=RemoteController,
                      ip=CONTROLLER_IP, port=CONTROLLER_PORT)

    for number in range(1, 5):
        net.addSwitch(f"s{number}")            # s1 gets datapath id 1, s2 gets 2, ...

    for name, ip, (switch, port) in HOSTS:
        host = net.addHost(name, ip=f"{ip}/24")
        net.addLink(host, net.get(switch), port2=port, bw=HOST_BANDWIDTH,
                    delay="1ms" if with_delay else None)

    for name, (sw_a, port_a), (sw_b, port_b), bandwidth, delay in CORE_LINKS:
        net.addLink(net.get(sw_a), net.get(sw_b), port1=port_a, port2=port_b,
                    bw=bandwidth, delay=f"{delay}ms" if with_delay else None)

    return net


def start_network(with_delay=True):
    net = build_network(with_delay)
    net.start()

    if os.environ.get("MININET_USERSPACE") == "1":
        # The OVS userspace datapath forwards packets with unfinished (offloaded)
        # TCP checksums, which the receiver drops. Not needed with the kernel datapath.
        for host in net.hosts:
            for interface in host.intfNames():
                host.cmd(f"ethtool -K {interface} tx off rx off tso off gso off gro off")
    return net


def set_link(net, link_name, state):
    """Bring one core link 'up' or 'down', e.g. set_link(net, 'A2', 'down')."""
    for name, (sw_a, _), (sw_b, _), _, _ in CORE_LINKS:
        if name == link_name:
            net.configLinkStatus(sw_a, sw_b, state)
            return
    raise ValueError(f"Unknown link {link_name}")


# ============================================================
# RUN ALONE (interactive Mininet CLI)
# ============================================================

if __name__ == "__main__":
    setLogLevel("info")
    network = start_network(with_delay=os.environ.get("NO_DELAY") != "1")
    print("\nTry:  h1 ping -c 3 h2        link s2 s4 down        link s2 s4 up\n")
    CLI(network)
    network.stop()
