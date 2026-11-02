"""Interactive loop-gain explorer for the LM3478 boost (AN-1286 model + DCM extension).

Plots loop gain |T|, phase and phase margin (180 + angle T) against frequency for every combination
of the grid values, plus fc / PM per case. RC1 (R6), CC1 (C4) and CC2 (C3) are set with log sliders
or by typing a value ("8.2k", "22n", "220p") in the box next to each slider.

Models (per case):
  CCM : AN-1286 Eq. (17): Acm from Eq. (7) (or the full Eq. (2), toggle), RHP zero Eq. (9),
        output pole Eq. (10), ESR zero Eq. (8), fs/2 pole pair with Q from Eq. (11).
  DCM : reduced-order peak-current DCM model (used when Iout < Iout,crit), resistive load,
        same comparator gain convention as AN Eq. (7): d(ipk)/d(Vc) = 1/(RSN + VSL_tot*L*fs/Vin).
  Compensator : exact gm-amplifier network gm * (ROUT || (RC1 + 1/sCC1) || 1/sCC2),
        or the AN-1286 Eq. (12)-(15) approximation plus the CC2 pole (toggle).
Cases with Q < 0 (subharmonic oscillation) or below the minimum on-time are flagged.

usage:
  python3 loop_explorer.py                                   # default grid (below)
  python3 loop_explorer.py --set vout=41.68,60.46,80.34 --set iout=10m,40m,100m --set vin=11,13
  python3 loop_explorer.py --set L=44.16u,100u --set rsn=33m,68m --rc1 8.2k --cc1 22n --cc2 220p
  python3 loop_explorer.py --no-gui --save loop.png          # no window: print the table, save a PNG

Grid / fixed parameters (any can be given a list with --set name=v1,v2,...):
"""
import argparse
import itertools
import re
import sys

import numpy as np
import matplotlib

PARAMS = {                     # name: (default list, description)
    "vin":  ([11.0],                    "input voltage [V]"),
    "vout": ([41.68, 60.46, 80.34],     "output voltage [V] (R4 = 25k / 17k / 12.7k)"),
    "iout": ([10e-3, 100e-3],           "load current [A] (resistive load, as in AN-1286)"),
    "L":    ([44.161e-6],               "inductance [H] (WE-PD 7447706470 model value)"),
    "fs":   ([302.2e3],                 "switching frequency [Hz] (R5 = 56k)"),
    "rsn":  ([0.033],                   "current-sense resistor R1 [ohm]"),
    "rsl":  ([1.2e3],                   "external slope-comp resistor R2 [ohm]"),
    "cout": ([26.5e-6],                 "output capacitance [F]"),
    "esr":  ([0.0108],                  "output capacitor ESR [ohm]"),
    "vsl":  ([0.092],                   "internal slope ramp VSL [V] (datasheet 52-132 mV)"),
    "ksl":  ([40e-6],                   "external slope factor K [A] (datasheet)"),
    "gm":   ([800e-6],                  "error amp transconductance [S] (600-1000 uS)"),
    "aea":  ([38.0],                    "error amp gain A_EA = gm*ROUT [V/V] (26-44)"),
    "vfb":  ([1.26],                    "feedback reference [V]"),
    "tmin": ([325e-9],                  "minimum on-time [s]"),
}
__doc__ += "\n".join(f"  {k:5s} {v[1]}  (default {v[0]})" for k, v in PARAMS.items())

COMP0 = {"rc1": 8.2e3, "cc1": 22e-9, "cc2": 220e-12}
COMP_RANGE = {"rc1": (1e3, 100e3), "cc1": (1e-9, 470e-9), "cc2": (10e-12, 4.7e-9)}
COMP_LABEL = {"rc1": "RC1 (R6)", "cc1": "CC1 (C4)", "cc2": "CC2 (C3)"}
PM_TARGET = 45.0
F = np.logspace(1, 6, 3000)

SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
      "meg": 1e6, "M": 1e6, "g": 1e9, "G": 1e9}          # m = milli, M / meg = mega
UNITS = {"", "v", "a", "hz", "h", "f", "ω", "ohm", "ohms", "r", "s", "w", "°", "deg", "%"}


def parse_si(text):
    """'8.2k' -> 8200, '792K' -> 792000, '22nF' -> 22e-9, '1Meg' -> 1e6. Unknown suffixes raise an error."""
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*((?i:meg)|[pnuµmkKMgG])?\s*(\S*)\s*", text)
    if not m or m.group(3).lower() not in UNITS:
        raise ValueError(f"cannot parse value '{text}' (use p n u m k M G, e.g. 8.2k, 22n, 1.5M)")
    return float(m.group(1)) * SI[m.group(2) if m.group(2) in SI else m.group(2).lower()] if m.group(2) else float(m.group(1))


def fmt_si(x, unit=""):
    for s, k in (("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(x) >= k * 0.9995:
            return f"{x / k:.3g}{s}{unit}"
    return f"{x:.3g}{unit}"


def power_stage(p, f, full_acm=False):
    """Vc -> Vout transfer function for one case. Returns (G(s), info dict)."""
    s = 2j * np.pi * f
    vin, vout, iout, L, fs = p["vin"], p["vout"], p["iout"], p["L"], p["fs"]
    R = vout / iout
    D = 1 - vin / vout; Dp = 1 - D
    vsl_tot = p["vsl"] + p["ksl"] * p["rsl"]
    Se, Sn = vsl_tot * fs / p["rsn"], vin / L                      # Eq. (5), (6)
    Zc = p["esr"] + 1 / (s * p["cout"])                          # output capacitor branch
    icrit = vout * D * Dp**2 / (2 * L * fs)                       # CCM/DCM boundary, resistive load
    info = dict(D=D, R=R, Q=np.nan, mode="CCM", flag="")
    if iout >= icrit:
        Q = 1 / (np.pi * (Dp * Se / Sn + 0.5 - D))               # Eq. (11)
        if full_acm:                                              # Eq. (2)-(4)
            n = 1 + 2 * Se / Sn
            wcLe = 2 * np.pi * fs * L / (Dp**3 * np.pi * n)
            Acm = Dp / p["rsn"] / (1 / wcLe + 2 / R)
        else:                                                     # Eq. (7)
            Acm = Dp * R / (2 * p["rsn"])
        wz1 = 1 / (p["cout"] * p["esr"])                          # Eq. (8)
        wz2 = R * (vin / vout) ** 2 / L                           # Eq. (9)
        wp1 = 2 / (p["cout"] * R)                                 # Eq. (10)
        wn = np.pi * fs
        G = Acm * (1 + s / wz1) * (1 - s / wz2) / ((1 + s / wp1) * (1 + s / (Q * wn) + (s / wn) ** 2))
        info.update(Q=Q, fz2=wz2 / 2 / np.pi)
        ipk = iout / Dp + vin * D / (2 * L * fs)
        if Q < 0:
            info["flag"] = "Q<0 subharmonic"
    else:
        info["mode"] = "DCM"
        ipk = np.sqrt(2 * iout * (vout - vin) / (L * fs))         # i2_avg = ipk^2 L fs / (2 (Vout - Vin)) = Iout
        Fm = 1 / (p["rsn"] + vsl_tot * L * fs / vin)              # d(ipk)/d(Vc), same convention as Eq. (7)
        k_vc = 2 * iout / ipk * Fm                                # d(i2)/d(Vc)
        g_v = iout / (vout - vin)                                 # -d(i2)/d(Vout)
        G = k_vc / (g_v + 1 / R + 1 / Zc)
        info["fz2"] = np.inf
        if ipk * L / vin < p["tmin"]:
            info["flag"] = "min on-time: pulse skipping"
    info["ipk"] = ipk
    info["vsen"] = p["rsn"] * ipk + p["ksl"] * p["rsl"] * D       # ISEN peak incl. external ramp
    if info["vsen"] > 0.125:
        info["flag"] = (info["flag"] + ", " if info["flag"] else "") + f"ISEN {info['vsen']*1e3:.0f} mV > 125 mV"
    return G, info


def compensator(p, comp, f, an_approx=False):
    s = 2j * np.pi * f
    rout = p["aea"] / p["gm"]
    if an_approx:                                                 # Eq. (12)-(15) + CC2 pole
        wz3, wp2 = 1 / (comp["cc1"] * comp["rc1"]), 1 / (comp["cc1"] * rout)
        return p["aea"] * (1 + s / wz3) / (1 + s / wp2) / (1 + s * comp["cc2"] * comp["rc1"])
    return p["gm"] / (1 / rout + s * comp["cc2"] + 1 / (comp["rc1"] + 1 / (s * comp["cc1"])))


def loop(p, comp, f=F, full_acm=False, an_approx=False):
    G, info = power_stage(p, f, full_acm)
    T = G * compensator(p, comp, f, an_approx) * p["vfb"] / p["vout"]    # Eq. (16), (17)
    mag = 20 * np.log10(np.abs(T))
    ph = np.degrees(np.unwrap(np.angle(T)))
    ph -= 360 * np.round(ph[0] / 360)                             # start the phase near 0 deg
    k = np.where((mag[:-1] >= 0) & (mag[1:] < 0))[0]
    if len(k):
        i = k[0]
        a = mag[i] / (mag[i] - mag[i + 1])                        # interpolate the 0 dB crossing
        fc = f[i] * (f[i + 1] / f[i]) ** a
        pm = 180 + ph[i] + a * (ph[i + 1] - ph[i])                # Eq. (19), (20)
    else:
        fc, pm = np.nan, np.nan
    info.update(fc=fc, pm=pm)
    return mag, ph, info


def build_cases(overrides):
    grid = {k: v[0] for k, v in PARAMS.items()}
    for item in overrides:
        name, _, vals = item.partition("=")
        if name not in grid:
            sys.exit(f"unknown parameter '{name}'. Known: {', '.join(grid)}")
        grid[name] = [parse_si(v) for v in vals.split(",")]
    varying = [k for k, v in grid.items() if len(v) > 1]
    cases = [dict(zip(grid, combo)) for combo in itertools.product(*grid.values())]
    return cases, varying


def case_label(p, varying):
    if not varying:
        return "case"
    unit = {"vin": "V", "vout": "V", "iout": "A", "L": "H", "fs": "Hz", "rsn": "Ω", "rsl": "Ω",
            "cout": "F", "esr": "Ω", "vsl": "V", "ksl": "A", "gm": "S", "tmin": "s"}
    return ", ".join(f"{k}={fmt_si(p[k], unit.get(k, ''))}" for k in varying)


def table(cases, varying, comp, full_acm, an_approx):
    lines = [f"RC1={fmt_si(comp['rc1'], 'Ω')} CC1={fmt_si(comp['cc1'], 'F')} CC2={fmt_si(comp['cc2'], 'F')}",
             f"{'case':40s} {'mode':4s} {'Q':>6s} {'fc':>9s} {'fz2/10':>9s} {'PM':>6s}  flags"]
    for p in cases:
        _, _, i = loop(p, comp, full_acm=full_acm, an_approx=an_approx)
        fz = "" if not np.isfinite(i["fz2"]) else fmt_si(i["fz2"] / 10, "Hz")
        lines.append(f"{case_label(p, varying):40s} {i['mode']:4s} {i['Q']:6.2f} {fmt_si(i['fc'], 'Hz'):>9s} "
                     f"{fz:>9s} {i['pm']:5.0f}°  {i['flag']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=V1,V2,...",
                    help="grid values for a parameter (SI suffixes allowed: 10m, 44u, 1.2k)")
    for k in COMP0:
        ap.add_argument(f"--{k}", default=None, help=f"initial {COMP_LABEL[k]} (default {fmt_si(COMP0[k])})")
    ap.add_argument("--full-acm", action="store_true", help="start with the full Eq. (2) instead of Eq. (7)")
    ap.add_argument("--an-approx", action="store_true", help="start with the AN Eq. (12)-(15) compensator")
    ap.add_argument("--no-gui", action="store_true", help="print the table and exit (with --save: also a PNG)")
    ap.add_argument("--save", metavar="PNG", help="save the figure to this file")
    args = ap.parse_args()

    cases, varying = build_cases(args.set)
    if len(cases) > 40:
        print(f"warning: {len(cases)} cases - the plot will be crowded")
    comp = {k: parse_si(getattr(args, k)) if getattr(args, k) else v for k, v in COMP0.items()}
    state = {"full_acm": args.full_acm, "an_approx": args.an_approx}

    if args.no_gui:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Slider, TextBox, CheckButtons, Button

    fig = plt.figure(figsize=(15, 9.5))
    gs = fig.add_gridspec(3, 1, left=0.06, right=0.66, top=0.95, bottom=0.27, hspace=0.12)
    gr = fig.add_gridspec(2, 1, left=0.73, right=0.98, top=0.95, bottom=0.27, hspace=0.3, height_ratios=[2, 1])
    ax_mag = fig.add_subplot(gs[0]); ax_ph = fig.add_subplot(gs[1], sharex=ax_mag)
    ax_pm = fig.add_subplot(gs[2], sharex=ax_mag)
    ax_bar = fig.add_subplot(gr[0]); ax_fc = fig.add_subplot(gr[1])
    colors = plt.cm.tab10(np.arange(len(cases)) % 10) if len(cases) <= 10 else plt.cm.turbo(np.linspace(0, 1, len(cases)))
    labels = [f"{n + 1}: {case_label(p, varying)}" for n, p in enumerate(cases)]
    nums = [str(n + 1) for n in range(len(cases))]

    lines = []
    for c, lab in zip(colors, labels):
        lm, = ax_mag.semilogx(F, F * 0, color=c, lw=1.6, label=lab)
        lp, = ax_ph.semilogx(F, F * 0, color=c, lw=1.6)
        lq, = ax_pm.semilogx(F, F * 0, color=c, lw=1.6)
        dot_m, = ax_mag.plot([], [], "o", color=c, ms=6, mec="white")
        dot_q, = ax_pm.plot([], [], "o", color=c, ms=6, mec="white")
        lines.append((lm, lp, lq, dot_m, dot_q))
    ax_mag.axhline(0, color="grey", lw=1); ax_pm.axhline(0, color="grey", lw=1)
    ax_pm.axhline(PM_TARGET, color="grey", lw=1, ls=":")
    ax_ph.axhline(-180, color="grey", lw=1, ls=":")
    fs_line = [a.axvline(cases[0]["fs"] / 2, color="grey", ls="--", lw=0.8) for a in (ax_mag, ax_ph, ax_pm)]
    ax_mag.set_ylabel("|T| (dB)"); ax_ph.set_ylabel("∠T (°)"); ax_pm.set_ylabel("180° + ∠T (°)")
    ax_pm.set_xlabel("Frequency (Hz)"); ax_mag.set_xlim(F[0], F[-1])
    ax_mag.set_ylim(-60, 90); ax_ph.set_ylim(-400, 30); ax_pm.set_ylim(-220, 210)
    for a in (ax_mag, ax_ph, ax_pm):
        a.grid(True, which="both", alpha=0.3)
    plt.setp(ax_mag.get_xticklabels(), visible=False); plt.setp(ax_ph.get_xticklabels(), visible=False)
    ax_mag.legend(fontsize=7, loc="lower left", ncol=1 + len(cases) // 8)
    title = ax_mag.set_title("", fontsize=10, loc="left")

    def redraw(_=None):
        pms, fcs, flags = [], [], []
        for p, (lm, lp, lq, dm, dq) in zip(cases, lines):
            mag, ph, i = loop(p, comp, full_acm=state["full_acm"], an_approx=state["an_approx"])
            lm.set_ydata(mag); lp.set_ydata(ph); lq.set_ydata(180 + ph)
            dm.set_data([i["fc"]], [0]); dq.set_data([i["fc"]], [i["pm"]])
            pms.append(i["pm"]); fcs.append((i["fc"], i.get("fz2", np.inf))); flags.append((i["mode"], i["flag"]))
        ax_bar.clear(); ax_fc.clear()
        y = np.arange(len(cases))
        bar_c = [c if (np.isfinite(pm) and not fl[1].startswith("Q<0")) else "lightgrey" for c, pm, fl in zip(colors, pms, flags)]
        ax_bar.barh(y, np.nan_to_num(pms), color=bar_c)
        for yi, pm, (mode, fl) in zip(y, pms, flags):
            txt = f" {pm:.0f}° {mode}" if np.isfinite(pm) else " no crossover"
            if fl:
                txt += f"  ⚠ {fl}"
            ax_bar.text(max(np.nan_to_num(pm), 0), yi, txt, va="center", fontsize=7)
        ax_bar.axvline(PM_TARGET, color="grey", ls=":", lw=1)
        ax_bar.set_yticks(y, nums, fontsize=8); ax_bar.set_ylabel("case"); ax_bar.invert_yaxis()
        ax_bar.set_xlim(min(0, np.nanmin(pms + [0])) - 5, 200); ax_bar.set_xlabel("Phase margin at fc (°)")
        ax_bar.set_title("Phase margin per case (dotted: 45° target)", fontsize=10, loc="left")
        fc_v = np.array([a for a, _ in fcs]); fz_v = np.array([b for _, b in fcs]) / 10
        ax_fc.barh(y - 0.2, np.nan_to_num(fc_v) / 1e3, height=0.4, color=colors, label="fc")
        ax_fc.barh(y + 0.2, np.where(np.isfinite(fz_v), fz_v, 0) / 1e3, height=0.4, color="lightgrey", label="fz2/10 (CCM)")
        ax_fc.set_yticks(y, nums, fontsize=7); ax_fc.invert_yaxis(); ax_fc.set_xlabel("kHz")
        ax_fc.legend(fontsize=7, loc="lower right"); ax_fc.set_title("Crossover vs RHP-zero limit", fontsize=10, loc="left")
        rout = cases[0]["aea"] / cases[0]["gm"]
        title.set_text(f"RC1 = {fmt_si(comp['rc1'], 'Ω')}, CC1 = {fmt_si(comp['cc1'], 'F')}, CC2 = {fmt_si(comp['cc2'], 'F')}"
                       f"   |  fz3 = {fmt_si(1 / (2 * np.pi * comp['cc1'] * comp['rc1']), 'Hz')}, "
                       f"fp2 ≈ {fmt_si(1 / (2 * np.pi * comp['cc1'] * (rout + comp['rc1'])), 'Hz')}, "
                       f"fp3 = {fmt_si(1 / (2 * np.pi * comp['cc2'] * comp['rc1']), 'Hz')}   |  "
                       f"{'Eq. (2)' if state['full_acm'] else 'Eq. (7)'}, "
                       f"{'AN compensator' if state['an_approx'] else 'exact gm network'}")
        fig.canvas.draw_idle()

    # ---- controls: one log slider + one text box per compensation part
    sliders, boxes, busy = {}, {}, [False]
    for row, k in enumerate(COMP0):
        lo, hi = COMP_RANGE[k]
        ys = 0.17 - row * 0.05
        s_ax = fig.add_axes([0.10, ys, 0.50, 0.03]); t_ax = fig.add_axes([0.66, ys, 0.08, 0.03])
        sl = Slider(s_ax, COMP_LABEL[k], np.log10(lo), np.log10(hi), valinit=np.log10(comp[k]))
        sl.valtext.set_visible(False)
        tb = TextBox(t_ax, "", initial=fmt_si(comp[k]))

        def on_slide(v, k=k):
            if busy[0]:
                return
            comp[k] = 10 ** v
            busy[0] = True; boxes[k].set_val(fmt_si(comp[k])); busy[0] = False
            redraw()

        def on_text(text, k=k):
            if busy[0]:
                return
            try:
                comp[k] = parse_si(text)
            except ValueError as e:
                print(e); return
            busy[0] = True; sliders[k].set_val(np.clip(np.log10(comp[k]), *sliders[k].valmin_valmax)); busy[0] = False
            redraw()

        sl.valmin_valmax = (sl.valmin, sl.valmax)
        sl.on_changed(on_slide); tb.on_submit(on_text)
        sliders[k], boxes[k] = sl, tb

    cb_ax = fig.add_axes([0.78, 0.07, 0.11, 0.13])
    cb = CheckButtons(cb_ax, ["full Eq. (2)", "AN EA approx"], [state["full_acm"], state["an_approx"]])

    def on_check(label):
        key = "full_acm" if label.startswith("full") else "an_approx"
        state[key] = not state[key]; redraw()
    cb.on_clicked(on_check)

    def on_print(_):
        print(table(cases, varying, comp, state["full_acm"], state["an_approx"]))

    def on_reset(_):
        for k, v in COMP0.items():
            sliders[k].set_val(np.log10(v))
    b1 = Button(fig.add_axes([0.90, 0.13, 0.08, 0.04]), "Print table"); b1.on_clicked(on_print)
    b2 = Button(fig.add_axes([0.90, 0.07, 0.08, 0.04]), "Reset"); b2.on_clicked(on_reset)

    redraw()
    print(table(cases, varying, comp, state["full_acm"], state["an_approx"]))
    if args.save:
        fig.savefig(args.save, dpi=110); print("saved", args.save)
    if not args.no_gui:
        plt.show()


if __name__ == "__main__":
    main()
