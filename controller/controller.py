# ============================================================
# TCP BACKUP SYSTEM
# SDN CONTROLLER (OS-Ken, OpenFlow 1.3)
#
# What it does
#   1. Installs forwarding rules so h1 (client) can reach h2/h3 (servers).
#   2. PATH SELECTION: there are two paths, A (fast) and B (slow). Every second it
#      gives each path a cost and picks the cheaper one:
#          cost of a link = delay (ms) + 100 x background utilisation (0..1)
#          cost of a path = sum of its links;  a path with a DOWN link costs infinity
#      The client's own traffic is not counted as "load", otherwise a backup would
#      think it is causing congestion and keep jumping between paths.
#   3. REROUTING: when a link goes down (or the other path becomes clearly cheaper)
#      it rewrites the rules on s1 and s4 so traffic moves to the other path.
#
# Run:   osken-manager controller/controller.py
# Baseline without rerouting (for the experiments):
#        ROUTING_MODE=static osken-manager controller/controller.py
# ============================================================

import os
import time

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3


# ============================================================
# NETWORK DESCRIPTION  (must match topology/topology.py)
# ============================================================

# host IP -> (switch datapath id, port the host is plugged into)
HOSTS = {"10.0.0.1": (1, 1), "10.0.0.2": (4, 1), "10.0.0.3": (4, 2)}
CLIENT_IPS = ["10.0.0.1"]
SERVER_IPS = ["10.0.0.2", "10.0.0.3"]
CLIENT_SWITCH = 1            # s1: where clients connect and where the paths split
SERVER_SWITCH = 4            # s4: where the paths join and the servers connect
CLIENT_PORT = 1              # s1 port of h1 (used to measure the client's own traffic)

# link name -> (switch a, port a, switch b, port b, delay ms, bandwidth Mbps)
# "a" is always the side closer to the client.
LINKS = {
    "A1": (1, 2, 2, 1, 5, 10),
    "A2": (2, 2, 4, 3, 5, 10),
    "B1": (1, 3, 3, 1, 30, 4),
    "B2": (3, 2, 4, 4, 30, 4),
}

# the two possible routes from s1 to s4
PATHS = {"A": ["A1", "A2"], "B": ["B1", "B2"]}


# ============================================================
# SETTINGS
# ============================================================

STATIC_MODE = os.environ.get("ROUTING_MODE", "dynamic") == "static"

POLL_INTERVAL = 1.0          # seconds between statistics requests
LOAD_WEIGHT = 100.0          # cost of a completely busy link (in "ms")
NOISE_LEVEL = 0.10           # background below 10% of a link is treated as zero
HYSTERESIS = 15.0            # another path must be this much cheaper before we move
MIN_SWITCH_INTERVAL = 3.0    # seconds between two voluntary moves (avoids flapping)
INFINITY = float("inf")

# which (switch, port) belongs to which link
PORT_TO_LINK = {}
for _name, (_sa, _pa, _sb, _pb, _d, _b) in LINKS.items():
    PORT_TO_LINK[(_sa, _pa)] = _name
    PORT_TO_LINK[(_sb, _pb)] = _name


def say(message):
    print(f"[{time.strftime('%H:%M:%S')}] CONTROLLER: {message}", flush=True)


# ============================================================
# THE CONTROLLER APPLICATION
# ============================================================

class BackupController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}                          # switch id -> connection
        self.current = None                          # "A" or "B"
        self.last_switch = 0.0
        self.port_down = {}                          # (switch, port) -> True when link is down
        self.last_stats = {}                         # (switch, port) -> (time, tx_bytes, rx_bytes)
        self.tx_rate = {}                            # (switch, port) -> bits per second
        self.rx_rate = {}

        say("mode = " + ("STATIC (no rerouting)" if STATIC_MODE else "DYNAMIC"))
        hub.spawn(self.monitor)

    # --------------------------------------------------------
    # A SWITCH CONNECTS  ->  install its rules
    # --------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_connected(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        say(f"switch s{datapath.id} connected")
        self.choose_path("startup")
        self.program_all()

    # --------------------------------------------------------
    # A LINK GOES DOWN / UP  ->  react immediately
    # --------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_changed(self, ev):
        message = ev.msg
        datapath = message.datapath
        ofproto = datapath.ofproto
        key = (datapath.id, message.desc.port_no)
        if key not in PORT_TO_LINK:
            return

        down = bool(message.desc.state & ofproto.OFPPS_LINK_DOWN) or \
               bool(message.desc.config & ofproto.OFPPC_PORT_DOWN)
        if self.port_down.get(key, False) != down:
            self.port_down[key] = down
            say(f"link {PORT_TO_LINK[key]} is {'DOWN' if down else 'UP'} (seen by s{key[0]})")
            self.choose_path("link change")

    def link_is_up(self, name):
        sa, pa, sb, pb, _, _ = LINKS[name]
        return not (self.port_down.get((sa, pa), False) or self.port_down.get((sb, pb), False))

    # --------------------------------------------------------
    # STATISTICS  ->  how busy is each link?
    # --------------------------------------------------------
    def monitor(self):
        while True:
            for datapath in list(self.datapaths.values()):
                parser = datapath.ofproto_parser
                datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)          # replies arrive while we sleep
            self.choose_path("periodic check")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_received(self, ev):
        switch = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            key = (switch, stat.port_no)
            previous = self.last_stats.get(key)
            if previous and now > previous[0]:
                seconds = now - previous[0]
                self.tx_rate[key] = (stat.tx_bytes - previous[1]) * 8 / seconds
                self.rx_rate[key] = (stat.rx_bytes - previous[2]) * 8 / seconds
            self.last_stats[key] = (now, stat.tx_bytes, stat.rx_bytes)

    # --------------------------------------------------------
    # PATH SELECTION
    # --------------------------------------------------------
    def link_cost(self, name):
        if not self.link_is_up(name):
            return INFINITY

        sa, pa, _, _, delay, bandwidth = LINKS[name]
        traffic = self.tx_rate.get((sa, pa), 0.0)            # bits/s going towards the servers

        if self.current and name in PATHS[self.current]:
            # the client's own traffic is on this link: do not count it as background load
            traffic = max(0.0, traffic - self.rx_rate.get((CLIENT_SWITCH, CLIENT_PORT), 0.0))

        utilisation = traffic / (bandwidth * 1e6)
        if utilisation < NOISE_LEVEL:
            utilisation = 0.0
        return delay + LOAD_WEIGHT * min(1.0, utilisation)

    def path_cost(self, path_name):
        return sum(self.link_cost(link) for link in PATHS[path_name])

    def choose_path(self, reason):
        if STATIC_MODE:
            if self.current is None:
                self.switch_to("A", "static mode")
            return                                           # never reroute in the baseline

        costs = {name: self.path_cost(name) for name in PATHS}
        best = min(costs, key=costs.get)

        if costs[best] == INFINITY:
            if self.current is not None:
                say("NO PATH AVAILABLE - all links down")
                self.current = None
            return

        if self.current is None or costs[self.current] == INFINITY:
            self.switch_to(best, reason)                     # current path is dead: move at once
        elif (best != self.current
              and costs[self.current] - costs[best] > HYSTERESIS
              and time.time() - self.last_switch >= MIN_SWITCH_INTERVAL):
            self.switch_to(best, f"path {best} is cheaper ({costs[self.current]:.0f} -> {costs[best]:.0f})")

    def switch_to(self, path_name, reason):
        old = self.current
        self.current = path_name
        self.last_switch = time.time()
        say(f"PATH {old or '-'} -> {path_name}   reason: {reason}")
        self.program_all()

    # --------------------------------------------------------
    # FLOW RULES
    # --------------------------------------------------------
    def build_rules(self):
        """List of (switch, destination IP, output port) for the current path."""
        chosen = self.current or "A"
        rules = []

        for ip, (switch, port) in HOSTS.items():             # deliver to a directly attached host
            rules.append((switch, ip, port))

        for path_name, links in PATHS.items():
            for name in links:
                sa, pa, sb, pb, _, _ = LINKS[name]
                # towards the servers; the choice is made on s1 only
                if sa != CLIENT_SWITCH or path_name == chosen:
                    for ip in SERVER_IPS:
                        rules.append((sa, ip, pa))
                # towards the client; the choice is made on s4 only
                if sb != SERVER_SWITCH or path_name == chosen:
                    for ip in CLIENT_IPS:
                        rules.append((sb, ip, pb))
        return rules

    def program_all(self):
        for switch, ip, port in self.build_rules():
            datapath = self.datapaths.get(switch)
            if datapath is not None:
                self.add_flow(datapath, ip, port)

    def add_flow(self, datapath, ip, out_port):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        actions = [parser.OFPActionOutput(out_port)]
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        # IPv4 traffic and ARP for this destination. Adding a rule with the same
        # match replaces the old one - that is how a flow is moved to another path.
        for match in (parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip),
                      parser.OFPMatch(eth_type=0x0806, arp_tpa=ip)):
            datapath.send_msg(parser.OFPFlowMod(datapath=datapath, priority=10,
                                                match=match, instructions=instructions))
