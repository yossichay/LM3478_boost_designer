"""Figure 7-1: load-step transient, as-built network vs AN-1286 design (ngspice, Vin = 11 V)."""
import sys, glob
sys.path.insert(0, "..")
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import sweep

BASE = "../results/an1286_verify_20261101_131137"      # as built: 68m / 100R / 33k / 3.3n / 47p
NEW = sorted(glob.glob("../results/an1286_verify_revB_*"))[-1]
V40 = sorted(glob.glob("../results/an1286_verify_40V_*"))[-1]
GREY, BLUE, INK, MUTED, GRIDC = "#9a9893", "#2a78d6", "#0b0b0b", "#898781", "#e1e0d9"


def load(path, case):
    r = sweep.read_raw(f"{path}/case_{case:03d}.raw")
    return np.real(r["time"]), np.real(r["v(out)"]), np.real(r["i(vsense)"])


def peaks(t, il, t0, n=60, fs=302.2e3):
    T = 1 / fs
    return np.array([il[(t >= t0 + i * T) & (t < t0 + (i + 1) * T)].max() for i in range(n)])


def per_cycle(t, x, t0, t1, fn, fs=302.2e3):
    T = 1 / fs
    n = int((t1 - t0) / T)
    tc = t0 + (np.arange(n) + 0.5) * T
    return tc, np.array([fn(x[(t >= t0 + i * T) & (t < t0 + (i + 1) * T)]) for i in range(n)])


rows = [("41.7 V", (V40, 0), (V40, 1), 15e-3), ("60.5 V", (BASE, 1), (NEW, 1), 20e-3), ("80.3 V", (BASE, 2), (NEW, 2), 20e-3)]
fig, ax = plt.subplots(2, 3, figsize=(14, 7), sharex=True)
for j, (lab, b, n, tstep) in enumerate(rows):
    for (path, case), col, name, z in ((b, GREY, "as built", 3), (n, BLUE, "AN-1286 design", 2)):
        t, v, il = load(path, case)
        t0, t1 = tstep - 0.3e-3, tstep + 1.5e-3
        tc, va = per_cycle(t, v, t0, t1, np.mean)
        vref = va[tc < tstep].mean()
        ax[0, j].plot((tc - tstep) * 1e3, (va - vref) * 1e3, color=col, lw=1.8, label=name, zorder=z)
        tc, pk = per_cycle(t, il, t0, t1, np.max)
        ax[1, j].plot((tc - tstep) * 1e3, pk, "o", color=col, ms=2.2, label=name, zorder=z)
    ax[0, j].set_title(f"{lab}: 40 → 100 mA load step", color=INK, fontsize=11, loc="left")
    ax[1, j].set_xlabel("Time after step (ms)")
    ax[1, j].set_ylim(0, 1.8)
ax[0, 0].legend(frameon=False, fontsize=9, loc="lower right")
for a in ax.flat:
    a.grid(True, color=GRIDC, lw=0.6); a.tick_params(colors=MUTED)
    for s in a.spines.values():
        s.set_color("#c3c2b7")
ax[0, 0].set_ylabel("Vout, cycle average − pre-step (mV)")
ax[1, 0].set_ylabel("Peak inductor current per cycle (A)")
fig.suptitle("Figure 7-1. ngspice switching model, Vin = 11 V: grey = as built (68 mΩ / 100 Ω / 33k / 3.3n / 47p), "
             "blue = AN-1286 design (33 mΩ / 1.2k / 8.2k / 22n / 220p)", color=INK, fontsize=11, x=0.01, ha="left")
fig.tight_layout()
fig.savefig("fig7-1_transient.png", dpi=120)
print("ok", NEW, V40)
