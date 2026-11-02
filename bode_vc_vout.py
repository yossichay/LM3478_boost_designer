"""Small-signal Bode plots for the LM3478 boost: control-to-output Gvc = vout/vcomp, and loop gain.

Averaged peak-current-mode model, with constants taken from LM3478_TRANS.lib:
  comparator   R1*i_pk + Vsl*d = 1.08*Vcomp - 1.48     (Vsl = slope-comp ramp at ISEN per cycle)
  DCM  : reduced-order model (one pole set by the output node), valid up to ~fsw/10
  CCM  : Erickson averaged PCM model (states iL, v) x Ridley sampling term He(s)
  load : current source (as I1 in the netlist), plus the feedback divider
Losses are ignored (ideal switch and diode), so the DCM/CCM boundary and peak current are slightly low.
Loads below roughly 1-2 mA run at the 325 ns minimum on-time and skip pulses, so they have no
valid small-signal model and are left out.

usage: python3 bode_vc_vout.py [out.png]
"""
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- circuit (values from sweep_example.toml)
Vg = 12.0
L = 44.161e-6                      # PD_1260_7447706470_47u model inductance
R5 = 56e3
fsw = 544e3 * (26.7e3 / R5) ** (1 / 1.26)
R1 = 0.068
R3, R8 = 792e3, 2000e3
R6, C4, C3 = 33e3, 3.3e-9, 47e-12
# output caps: C10 22u (10.8m ESR, 4.3n ESL) || 0.1u || 2.2u || 2.2u
C10, ESR10, ESL10, Cmlcc = 22e-6, 0.0108, 4.313e-9, 0.1e-6 + 2.2e-6 + 2.2e-6
# LM3478 model
Kc, Voff = 1.08, 1.48
Vsl = 0.0685 * 1.3
gm, Ro = 25e-6 * 31, 48e3
Vref, Ksfb = 1.26, 0.97            # Vout regulates ~3% low (EA DC gain only gm*Ro)

R4_LIST = ["25k", "17k", "12.7k"]
LOADS = [5e-3, 10e-3, 40e-3, 100e-3]
COLORS = ["#6da7ec", "#2a78d6", "#1c5cab", "#0d366b"]   # light -> dark = light -> heavy load

f = np.logspace(1, np.log10(fsw / 2), 3000)
s = 2j * np.pi * f


def val(x):
    return float(x[:-1]) * 1e3 if x.endswith("k") else float(x)


def ycap():
    return s * Cmlcc + 1 / (ESR10 + s * ESL10 + 1 / (s * C10))


def gvc(V, Io, Rdiv):
    """Return (Gvc(s), mode) for output V and load Io."""
    Ts = 1 / fsw
    Itot = Io + V / Rdiv                               # diode current delivers load + divider
    # DCM operating point
    ip = np.sqrt(2 * Itot * (V - Vg) / (L * fsw))
    d1, d2 = ip * L * fsw / Vg, ip * L * fsw / (V - Vg)
    if d1 + d2 < 1:
        Fm = Kc / (R1 + Vsl * L * fsw / Vg)            # d(ip)/d(vcomp)
        k_vc = 2 * Itot / ip * Fm                      # d(i2)/d(vcomp)
        g_v = Itot / (V - Vg)                          # -d(i2)/dv
        return k_vc / (g_v + 1 / Rdiv + ycap()), "DCM"
    # CCM: Erickson model, <iL> from the peak-current law
    D = 1 - Vg / V; Dp = 1 - D
    IL = Itot / Dp
    Ma = Vsl / (R1 * Ts)

    def avg_iL(ic, d, vg, v):
        return ic - Ma * d * Ts - vg / L * d**2 * Ts / 2 - (v - vg) / L * (1 - d)**2 * Ts / 2

    ic0 = IL - avg_iL(0, D, Vg, V)
    e = 1e-6
    dI_dd = (avg_iL(ic0, D + e, Vg, V) - avg_iL(ic0, D - e, Vg, V)) / (2 * e)
    dI_dv = (avg_iL(ic0, D, Vg, V + e) - avg_iL(ic0, D, Vg, V - e)) / (2 * e)
    a = 1 / dI_dd
    A11 = L * s - V * a; A12 = Dp + V * a * dI_dv
    A21 = -Dp + IL * a;  A22 = ycap() + 1 / Rdiv - IL * a * dI_dv
    b1 = -V * a; b2 = IL * a
    Gvic = (A11 * b2 - A21 * b1) / (A11 * A22 - A12 * A21)
    mc = 1 + (Vsl / Ts) / (R1 * Vg / L)
    Q = 1 / (np.pi * (mc * Dp - 0.5))
    wn = np.pi * fsw
    He = 1 / (1 + s / (wn * Q) + (s / wn) ** 2)
    return Gvic * He * Kc / R1, f"CCM, mc·D'={mc * Dp:.2f}"


def margins(T):
    mag = np.abs(T)
    k = np.where(mag < 1)[0]
    if len(k) == 0:
        return None, None
    k = k[0]
    return f[k], 180 + np.degrees(np.unwrap(np.angle(T)))[k]


def main(out):
    Zc = 1 / (1 / Ro + s * C3 + 1 / (R6 + 1 / (s * C4)))
    fig, ax = plt.subplots(4, 3, figsize=(15, 12), sharex=True,
                           gridspec_kw={"height_ratios": [3, 2, 3, 2]})
    ink, muted, grid = "#0b0b0b", "#898781", "#e1e0d9"
    print(f"fsw = {fsw/1e3:.0f} kHz, L = {L*1e6:.1f} uH, R1 = {R1*1e3:.0f} mOhm")
    print(f"{'R4':>6} {'Vout':>6} {'Io':>6} {'mode':>15} | {'Gvc@1k':>7} {'fc':>7} {'PM':>5}")
    for j, r4 in enumerate(R4_LIST):
        R4p = 1 / (1 / val(r4) + 1 / R8)
        V = Ksfb * Vref * (1 + R3 / R4p)
        Rdiv = R3 + R4p
        H = R4p / Rdiv
        for Io, col in zip(LOADS, COLORS):
            G, mode = gvc(V, Io, Rdiv)
            T = G * H * gm * Zc
            fc, pm = margins(T)
            ls = "-" if mode == "DCM" else "--"
            lab = f"{Io*1e3:g} mA ({mode})"
            for row, X in ((0, G), (2, T)):
                ax[row, j].semilogx(f, 20 * np.log10(np.abs(X)), ls, color=col, lw=2, label=lab)
                ax[row + 1, j].semilogx(f, np.degrees(np.unwrap(np.angle(X))), ls, color=col, lw=2)
            if fc:
                ax[2, j].plot(fc, 0, "o", color=col, ms=8, mec="#fcfcfb", mew=2)
            g1k = 20 * np.log10(abs(G[np.argmin(abs(f - 1e3))]))
            print(f"{r4:>6} {V:6.1f} {Io*1e3:4.0f}mA {mode:>15} | {g1k:6.1f}dB "
                  f"{(fc or 0)/1e3:6.2f}k {pm if pm is not None else float('nan'):4.0f}°")
        ax[0, j].set_title(f"R4 = {r4}  →  Vout ≈ {V:.0f} V", color=ink, fontsize=12)
    ax[0, 0].set_ylabel("|Gvc| = vout/vcomp [dB]")
    ax[1, 0].set_ylabel("∠Gvc [°]")
    ax[2, 0].set_ylabel("|T| loop gain [dB]")
    ax[3, 0].set_ylabel("∠T [°]")
    for a in ax.flat:
        a.grid(True, which="both", color=grid, lw=0.6)
        a.axvspan(fsw / 10, f[-1], color="#f0efec", zorder=0)
        a.tick_params(colors=muted)
        for sp in a.spines.values():
            sp.set_color("#c3c2b7")
    for j in range(3):
        ax[2, j].axhline(0, color=muted, lw=1)
        ax[3, j].axhline(-180, color=muted, lw=1, ls=":")
        ax[-1, j].set_xlabel("Frequency [Hz]")
    ax[0, 2].legend(loc="upper right", fontsize=9, title="Load (solid DCM, dashed CCM)", title_fontsize=9)
    fig.suptitle(f"LM3478 boost: control-to-output Gvc (top) and loop gain T (bottom)\n"
                 f"L={L*1e6:.1f} µH, fsw≈{fsw/1e3:.0f} kHz, R1={R1*1e3:.0f} mΩ, "
                 f"R6={R6/1e3:g}k C4={C4*1e9:g}n C3={C3*1e12:g}p, Vin={Vg:g} V. "
                 f"Shaded: above fsw/10 (averaged model less accurate). Dots: crossover.\n"
                 f"CCM needs mc·D' > 0.5, otherwise subharmonic oscillation (the peak at fsw/2).",
                 fontsize=11, color=ink)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print("saved", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bode_vc_vout.png")
