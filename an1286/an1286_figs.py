"""AN-1286 figures 5-1 to 5-4 redone for the HV booster (Vin = 11 V, Iout = 100 mA, Vout = 41.7/60.5/80.3 V)."""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from an1286_calc import *

RC1, CC1, CC2, RSL = 8.2e3, 22e-9, 220e-12, 1.2e3
VOUTS = (41.68, 60.46, 80.34)
COLS = ("#86b6ef", "#2a78d6", "#104281")      # light -> dark = low -> high Vout
INK, MUTED, GRID = "#0b0b0b", "#898781", "#e1e0d9"
f = np.logspace(1, 6, 4000)
fcut = f <= FS / 2


def style(ax, ylab):
    ax.set_xscale("log"); ax.grid(True, which="both", color=GRID, lw=0.6)
    ax.set_xlabel("Frequency (Hz)", color=INK); ax.set_ylabel(ylab, color=INK)
    ax.tick_params(colors=MUTED)
    for s in ax.spines.values():
        s.set_color("#c3c2b7")


def loop(vout, comp, acm="Acm", vin=11):
    c = corner(vin, vout, 0.1, VSL + K_SL * RSL)
    ADC = eq18_ADC(c[acm], eq13_AEA(), c["AFB"])
    if comp == "none":
        return c, T_eq17(f, ADC, c["wz1"], c["wz2"], c["wp1"], c["Q"])
    if comp == "pole":       # AN Fig. 5-2: dominant pole only
        T = T_eq17(f, ADC, c["wz1"], c["wz2"], c["wp1"], c["Q"])
        return c, T / (1 + 2j * np.pi * f / eq15_wp2(CC1))
    return c, T_eq17(f, ADC, c["wz1"], c["wz2"], c["wp1"], c["Q"],
                     eq14_wz3(CC1, RC1), eq15_wp2(CC1), CC2, RC1)


def fig_mag(comp, title, out, mark_target=False):
    fig, ax = plt.subplots(figsize=(8, 4.6))
    for v, col in zip(VOUTS, COLS):
        c, T = loop(v, comp)
        lab = f"Vout = {v:.1f} V"
        if mark_target:
            ft = c["wz2"] / 2 / np.pi / 10
            gt = 20 * np.log10(abs(T[np.argmin(abs(f - ft))]))
            lab += f"  (fz2/10 = {ft/1e3:.1f} kHz, |T| = {gt:.1f} dB there)"
            ax.plot(ft, gt, "D", color=col, ms=7, mec="white", mew=1.5)
        ax.plot(f[fcut], 20 * np.log10(abs(T[fcut])), color=col, lw=2, label=lab)
        fc, pm = crossover(f, T)
        if not np.isnan(fc) and comp != "none":
            ax.plot(fc, 0, "o", color=col, ms=7, mec="white", mew=1.5)
    ax.axhline(0, color=MUTED, lw=1)
    ax.axvline(FS / 2, color=MUTED, lw=1, ls="--"); ax.text(FS / 2 * 1.1, 50, "fs/2", color=MUTED, fontsize=9)
    ax.set_ylim(-60, 70); ax.set_xlim(10, 1e6)
    style(ax, "|T| (dB)"); ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


def fig_final(out):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.6))
    for v, col in zip(VOUTS, COLS):
        c, T = loop(v, "full")
        fc, pm = crossover(f, T)
        ph = np.degrees(np.unwrap(np.angle(T)))
        a1.plot(f[fcut], 20 * np.log10(abs(T[fcut])), color=col, lw=2, label=f"{v:.1f} V: fc {fc/1e3:.1f} kHz")
        a1.plot(fc, 0, "o", color=col, ms=7, mec="white", mew=1.5)
        a2.plot(f[fcut], 180 + ph[fcut], color=col, lw=2, label=f"{v:.1f} V: PM {pm:.0f}°")
        a2.plot(fc, pm, "o", color=col, ms=7, mec="white", mew=1.5)
    a1.axhline(0, color=MUTED, lw=1); a1.set_ylim(-60, 70)
    a2.axhline(0, color=MUTED, lw=1); a2.axhspan(30, 100, color="#f0efec", zorder=0); a2.axhline(45, color=MUTED, lw=1, ls=":")
    a2.text(250, 108, "AN range 30–100°, min 45° dotted", color=MUTED, fontsize=9); a2.set_ylim(-100, 200)
    for a in (a1, a2):
        a.set_xlim(10, FS / 2); a.legend(frameon=False, fontsize=9)
    style(a1, "|T| (dB)"); style(a2, "Phase margin, 180° + ∠T (°)")
    a1.set_title("Figure 5-3. Loop gain magnitude", color=INK, fontsize=11, loc="left")
    a2.set_title("Figure 5-4. Phase margin of loop gain", color=INK, fontsize=11, loc="left")
    fig.tight_layout(); fig.savefig(out, dpi=130); plt.close(fig)


if __name__ == "__main__":
    fig_mag("none", "Figure 5-1. Uncompensated loop gain (A_comp = A_EA), Vin = 11 V, 100 mA", "fig5-1.png", True)
    fig_mag("pole", "Figure 5-2. Loop gain with dominant compensation pole (CC1 = 22 nF)", "fig5-2.png")
    fig_final("fig5-3_5-4.png")
    print("ok")
