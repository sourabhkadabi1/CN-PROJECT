# ============================================================
# TCP BACKUP SYSTEM
# GRAPHS FROM THE MEASUREMENTS
#
#   python3 -m experiments.plot_results
#
# Reads  experiments/results/summary.csv  and  series_<mode>_run1.csv
# Writes experiments/results/throughput.png  and  recovery.png
# ============================================================

import csv
import os

import matplotlib
matplotlib.use("Agg")                      # draw to a file, no window needed
import matplotlib.pyplot as plt

RESULTS = os.path.join("experiments", "results")

COLOURS = {"static": "tab:red", "dynamic": "tab:green"}
LABELS = {"static": "Static path (no rerouting)", "dynamic": "SDN controller (rerouting)"}


def read_csv(name):
    with open(os.path.join(RESULTS, name)) as file:
        return list(csv.DictReader(file))


# ============================================================
# GRAPH 1: THROUGHPUT OVER TIME
# ============================================================

def plot_throughput(summary):
    plt.figure(figsize=(10, 5))

    for mode in COLOURS:
        path = f"series_{mode}_run1.csv"
        if not os.path.exists(os.path.join(RESULTS, path)):
            continue
        rows = read_csv(path)
        plt.plot([float(r["time_s"]) for r in rows],
                 [float(r["throughput_mbps"]) for r in rows],
                 color=COLOURS[mode], label=LABELS[mode], linewidth=2)

    first = summary[0]
    plt.axvspan(float(first["fail_start_s"]), float(first["fail_end_s"]),
                color="grey", alpha=0.2, label="Link A2 (fast path) is down")

    plt.xlabel("Time (seconds)")
    plt.ylabel("Throughput (Mbps)")
    plt.title("TCP throughput during a link failure")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(RESULTS, "throughput.png"), dpi=130)
    plt.close()


# ============================================================
# GRAPH 2: RECOVERY TIME (average over all runs)
# ============================================================

def plot_recovery(summary):
    modes = [m for m in COLOURS if any(r["mode"] == m for r in summary)]
    averages = []
    for mode in modes:
        values = [float(r["recovery_s"]) for r in summary if r["mode"] == mode]
        averages.append(sum(values) / len(values))

    plt.figure(figsize=(6, 5))
    bars = plt.bar([LABELS[m] for m in modes], averages, color=[COLOURS[m] for m in modes])
    for bar, value in zip(bars, averages):
        label = f"{value:.1f} s" if value > 0 else "no visible\noutage (<0.1 s)"
        plt.text(bar.get_x() + bar.get_width() / 2, value, label,
                 ha="center", va="bottom", fontsize=11)

    plt.ylabel("Recovery time (seconds)")
    plt.title("Time to recover from the link failure")
    plt.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(RESULTS, "recovery.png"), dpi=130)
    plt.close()


if __name__ == "__main__":
    summary_rows = read_csv("summary.csv")
    plot_throughput(summary_rows)
    plot_recovery(summary_rows)
    print(f"graphs saved in {RESULTS}/ (throughput.png, recovery.png)")
