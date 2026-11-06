# ============================================================
# TCP BACKUP SYSTEM
# MEASUREMENT: throughput and recovery time during a link failure
#
#   sudo python3 -m experiments.measure
#   sudo python3 -m experiments.measure --repeat 3 --fail-at 8 --fail-for 10
#
# For each mode it starts the controller and the network, sends TCP data from
# h1 to h2, breaks link A2 (the fast path) and repairs it later:
#
#   static   = controller never reroutes (baseline)
#   dynamic  = controller moves the traffic to the other path
#
# Results go to experiments/results/  (then run experiments.plot_results)
# ============================================================

import argparse
import csv
import os
import shutil
import socket
import subprocess
import sys
import time

from mininet.log import setLogLevel

from topology.topology import start_network, set_link

RESULTS = os.path.join("experiments", "results")
BIN = 0.5                      # seconds per sample in the graph
FINE_BIN = 0.1                 # seconds per sample used to measure the recovery time


# ============================================================
# START / STOP THE CONTROLLER
# ============================================================

def start_controller(mode):
    program = shutil.which("osken-manager")
    if program is None:
        sys.exit("osken-manager not found. Install it with:  pip install 'os-ken<4'")

    log = open(os.path.join(RESULTS, f"controller_{mode}.log"), "w")
    process = subprocess.Popen([program, "controller/controller.py"],
                               env=dict(os.environ, ROUTING_MODE=mode),
                               stdout=log, stderr=subprocess.STDOUT)

    for _ in range(40):                                   # wait until it listens on 6653
        try:
            socket.create_connection(("127.0.0.1", 6653), timeout=1).close()
            return process
        except OSError:
            time.sleep(0.5)
    process.kill()
    sys.exit("The controller did not start - see " + log.name)


def stop_controller(process):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


# ============================================================
# ONE EXPERIMENT
# ============================================================

def run_once(mode, args):
    controller = start_controller(mode)
    network = start_network(with_delay=not args.no_delay)
    h1, h2 = network.get("h1"), network.get("h2")

    try:
        for _ in range(20):                               # wait until the rules are installed
            if "1 received" in h1.cmd("ping -c1 -W1 10.0.0.2"):
                break
            time.sleep(1)
        else:
            sys.exit("h1 cannot reach h2 - is the controller working?")

        csv_path = os.path.join(RESULTS, f"raw_{mode}.csv")
        receiver = h2.popen([sys.executable, "-m", "experiments.traffic",
                             "receiver", "10.0.0.2", "6000", csv_path])
        time.sleep(1)

        start = time.time()
        sender = h1.popen([sys.executable, "-m", "experiments.traffic",
                           "sender", "10.0.0.2", "6000", str(args.duration)])

        time.sleep(args.fail_at)
        fail_time = time.time() - start
        print(f"    link A2 DOWN at t={fail_time:.1f}s")
        set_link(network, "A2", "down")

        time.sleep(args.fail_for)
        up_time = time.time() - start
        print(f"    link A2 UP   at t={up_time:.1f}s")
        set_link(network, "A2", "up")

        sender.wait(timeout=args.duration + 60)
        receiver.wait(timeout=30)
    finally:
        network.stop()
        stop_controller(controller)

    return (read_series(csv_path, start, args.duration, BIN),
            read_series(csv_path, start, args.duration, FINE_BIN), fail_time, up_time)


# ============================================================
# TURN THE RAW LOG INTO THROUGHPUT (Mbps) PER 0.5 SECOND
# ============================================================

def read_series(csv_path, start, duration, width):
    bins = [0] * (int(duration / width) + 1)
    with open(csv_path) as file:
        for row in csv.DictReader(file):
            index = int((float(row["timestamp"]) - start) / width)
            if 0 <= index < len(bins):
                bins[index] += int(row["bytes"])
    return [(i * width, count * 8 / width / 1e6) for i, count in enumerate(bins)]


def average(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def analyse(series, fail_time, up_time, duration):
    before = average(v for t, v in series if fail_time - 4 <= t < fail_time)
    during = average(v for t, v in series if fail_time + 1 <= t < up_time)

    # Outage = throughput drops below 10% of normal (measured in 0.1 s steps) within
    # 1.5 s of the failure. Recovery time = from the moment the link broke until the
    # throughput is above that level again. 0 means there was no visible outage.
    threshold = max(0.1 * before, 0.3)
    outage_start = None
    for t, value in series:
        if fail_time <= t <= fail_time + 1.5 and value < threshold:
            outage_start = t
            break

    recovery = 0.0
    if outage_start is not None:
        recovery = duration - fail_time                  # until proven otherwise: never recovered
        for t, value in series:
            if t > outage_start and value >= threshold:
                recovery = t - fail_time
                break
    return before, during, recovery


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=26, help="seconds of traffic")
    parser.add_argument("--fail-at", type=float, default=7, help="seconds until the link breaks")
    parser.add_argument("--fail-for", type=float, default=10, help="seconds the link stays broken")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-delay", action="store_true", help="skip netem delay emulation")
    parser.add_argument("--modes", default="static,dynamic")
    args = parser.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    setLogLevel("warning")

    summary = []
    for run in range(1, args.repeat + 1):
        for mode in args.modes.split(","):
            print(f"run {run}/{args.repeat}  mode = {mode}")
            series, fine_series, fail_time, up_time = run_once(mode, args)
            before, during, recovery = analyse(fine_series, fail_time, up_time, args.duration)
            text = f"{recovery:.1f} s" if recovery > 0 else "no visible outage (< 0.1 s)"
            print(f"    before failure: {before:.2f} Mbps   during failure: {during:.2f} Mbps   "
                  f"RECOVERY TIME: {text}")

            with open(os.path.join(RESULTS, f"series_{mode}_run{run}.csv"), "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["time_s", "throughput_mbps"])
                writer.writerows((f"{t:.1f}", f"{v:.3f}") for t, v in series)

            summary.append({"mode": mode, "run": run, "before_mbps": round(before, 2),
                            "during_mbps": round(during, 2), "recovery_s": round(recovery, 1),
                            "fail_start_s": round(fail_time, 1), "fail_end_s": round(up_time, 1)})

    with open(os.path.join(RESULTS, "summary.csv"), "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)
    print(f"\nResults saved in {RESULTS}/   ->  now run:  python3 -m experiments.plot_results")


if __name__ == "__main__":
    main()
