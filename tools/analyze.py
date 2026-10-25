import argparse
import csv
import glob
import os
import statistics
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def jain(values):
    values = [v for v in values if v > 0]
    if not values:
        return 0.0
    return sum(values) ** 2 / (len(values) * sum(v * v for v in values))


def load(paths):
    groups = defaultdict(list)
    for path in paths:
        with open(path) as f:
            for row in csv.DictReader(f):
                groups[row["label"]].append(row)
    return groups


def summarize(rows):
    ok = [r for r in rows if r["avg_rtt_ms"]]
    avg = [float(r["avg_rtt_ms"]) for r in ok]
    p95 = [float(r["p95_rtt_ms"]) for r in ok if r["p95_rtt_ms"]]
    conn = [float(r["connect_ms"]) for r in ok if r["connect_ms"]]
    return {
        "clients": len(rows),
        "completed": sum(1 for r in rows if r["status"] in ("completed", "timeout")),
        "avg_rtt": statistics.mean(avg) if avg else 0,
        "p95_rtt": statistics.mean(p95) if p95 else 0,
        "max_rtt": max((float(r["max_rtt_ms"]) for r in ok), default=0),
        "connect": statistics.mean(conn) if conn else 0,
        "fairness": jain(avg),
    }


def main():
    ap = argparse.ArgumentParser(description="Summarize client metrics per experiment label")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--plot", default=None)
    args = ap.parse_args()
    files = args.files or sorted(glob.glob(os.path.join(BASE, "results", "metrics_*.csv")))
    if not files:
        print("no metrics files found")
        return
    groups = load(files)
    header = "%-28s %7s %9s %11s %11s %11s %11s %8s" % ("label", "clients", "finished", "connect_ms",
                                                          "avg_rtt_ms", "p95_rtt_ms", "max_rtt_ms", "jain")
    print(header)
    print("-" * len(header))
    summary = {}
    for label, rows in groups.items():
        s = summarize(rows)
        summary[label] = s
        print("%-28s %7d %9d %11.2f %11.2f %11.2f %11.2f %8.3f" % (label, s["clients"], s["completed"], s["connect"],
                                                                 s["avg_rtt"], s["p95_rtt"], s["max_rtt"],
                                                                 s["fairness"]))
    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        labels = list(summary)
        x = range(len(labels))
        fig, ax = plt.subplots(figsize=(max(6, 1.6 * len(labels)), 4))
        ax.bar([i - 0.2 for i in x], [summary[l]["avg_rtt"] for l in labels], 0.4, label="avg RTT")
        ax.bar([i + 0.2 for i in x], [summary[l]["p95_rtt"] for l in labels], 0.4, label="p95 RTT")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=20, ha="right")
        ax.set_ylabel("application RTT (ms)")
        ax.set_title("Exam request/response latency per scenario")
        ax.legend()
        fig.tight_layout()
        fig.savefig(args.plot, dpi=150)
        print("plot saved to %s" % args.plot)


if __name__ == "__main__":
    main()
