"""LM3478 boost converter design calculations.

Power stage: LM3478 datasheet SNVS085Y, section 7.2 (equation numbers "DS (n)").
Compensation: AN-1286 (SNVA067D), auto-tuned on the loop model of ../an1286/loop_explorer.py.

    from booster_design import Spec, design
    d = design(Spec(vin=12, vouts=(41.7, 80.3), iout_min=0.01, iout_max=0.1, fs=300e3))
    print(d.report())

Overrides (the parts the user picks) go in `choice`: L, dcr, cout, esr, and comp = {rc1, cc1, cc2} to
switch the auto-tuning off. Everything downstream (RSN, RSL, compensation) is recomputed from them.
"""
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "an1286"))
from loop_explorer import loop as loop_tf, F  # noqa: E402

# ---- LM3478 datasheet values (typ unless noted)
VFB = 1.26
VSENSE, VSENSE_MIN = 0.156, 0.125        # current-sense threshold [V]
VSL, VSL_MIN = 0.092, 0.052              # internal slope ramp [V]
VSL_RATIO = 0.49                         # VSL / VSENSE, DS (19)
K_SL = 40e-6                             # external slope factor [A], DS (24)
GM, AEA = 800e-6, 38.0                   # error amplifier
I_EA = 110e-6                            # error amp source current (slews CC1)
TMIN = 325e-9                            # minimum on-time / blanking
VSC = 0.343                              # short-circuit sense threshold
VDRV_MAX, I_DRV = 7.2, 1.0               # gate drive clamp [V], peak drive current [A]
FA_K, FA_EXP = 4.503e11, -1.26           # DS (7): RFA = 4.503e11 * fs^-1.26
I_SUPPLY = 2.7e-3                        # LM3478 supply current, non-switching
LOSS_PARTS = {  # part parameters of the loss estimate: key -> (label, unit)
    "rds": ("MOSFET RDS(on) (25 °C)", "Ω"), "qg": ("MOSFET Qg at the drive voltage", "C"),
    "coss": ("MOSFET Coss(er) at Vout", "F"), "tsw": ("MOSFET switching edge (rise ≈ fall)", "s"),
    "cj": ("Diode Cj at Vout", "F"),
}
LOSS_COLS = ["L DCR", "L core", "Q cond", "Q switch", "Q Coss", "Q gate", "D cond", "D Cj", "RSN", "Cout ESR", "FB divider", "LM3478"]
CC1_MAX = 47e-9                          # larger CC1 slews COMP too slowly (AN-1286 study, sec. 5.4)
RC1_MIN = 4.7e3                          # below this the EA mid-band gain gm*RC1 < ~4: Cout is raised instead

E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6,
       6.2, 6.8, 7.5, 8.2, 9.1]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40, 1.43,
       1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10,
       2.15, 2.21, 2.26, 2.32, 2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
       3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53,
       4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49, 5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65,
       6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76]
V_CLASSES = [20, 25, 30, 40, 50, 60, 63, 75, 80, 100, 120, 150, 200, 250, 300, 400, 450, 600]


def e_round(x, series=E24, mode="nearest"):
    """Round x to a standard E-series value: mode 'nearest' | 'up' | 'down'."""
    if x <= 0 or not math.isfinite(x):
        return x
    dec = 10 ** math.floor(math.log10(x))
    cands = [v * d for d in (dec / 10, dec, dec * 10) for v in series]
    if mode == "up":
        return min(c for c in cands if c >= x * (1 - 1e-9))
    if mode == "down":
        return max(c for c in cands if c <= x * (1 + 1e-9))
    return min(cands, key=lambda c: abs(math.log(c / x)))


def v_class(v):
    return next((c for c in V_CLASSES if c >= v), math.ceil(v / 100) * 100)


def si(x, unit="", digits=3):
    if x is None or not np.isfinite(x):
        return "–"
    for s, k in (("M", 1e6), ("k", 1e3), ("", 1), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(x) >= k * 0.9995:
            return f"{x / k:.{digits}g} {s}{unit}"
    return f"{x:.{digits}g} {unit}"


@dataclass
class Spec:
    vin: float = 12.0
    vcc: float = 0.0                     # LM3478 VIN pin supply (IC + gate driver); 0 = the regulator input Vin
    vouts: tuple = (41.7, 60.5, 80.3)    # output settings [V]
    iout_min: float = 0.010
    iout_max: float = 0.100
    fs: float = 300e3
    eff: float = 0.88                    # efficiency estimate for currents
    ripple_ratio: float = 0.30           # inductor ripple dIL / IL at full load (DS: 30 %)
    vout_ripple: float = 0.20            # output ripple target [V pp]
    vin_ripple: float = 0.01             # input ripple target, fraction of Vin
    cl_margin: float = 1.6               # current limit / peak current. DS says 120 %, but 1.2-1.4 locked
                                         # up in current limit after a load step in simulation (1.6 ok)
    q_target: float = 1.0               # max Q of the fs/2 pole pair (AN-1286 eq. 11)
    pm_target: float = 45.0              # CCM phase margin target [deg]
    pm_target_dcm: float = 40.0          # light-load (DCM) phase margin target [deg]
    vd: float = 0.7                      # diode forward drop [V]
    vq: float = 0.1                      # MOSFET on-state drop [V]
    rf1: float = 1e6                     # top feedback resistor RF1 [ohm]
    loss_budget: float = 0.01            # per-part loss target, fraction of Pout (sets DCR, Rds, Cj targets)


@dataclass
class Design:
    spec: Spec
    values: dict = field(default_factory=dict)       # chosen part values
    reqs: list = field(default_factory=list)         # (ref, part, value / requirement, notes)
    corners: list = field(default_factory=list)      # operating-point table
    loops: list = field(default_factory=list)        # (label, p dict, info) for the loop plots
    losses: list = field(default_factory=list)       # (label, losses dict) per Vout x {Iout min, max}
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def params(self, vin, vout, iout):
        """Loop-model parameter dict (see loop_explorer.PARAMS)."""
        v = self.values
        return dict(vin=vin, vout=vout, iout=iout, L=v["L"], fs=v["fs"], rsn=v["rsn"], rsl=v["rsl"],
                    cout=v["cout"], esr=v["esr"], vsl=VSL, ksl=K_SL, gm=GM, aea=AEA, vfb=VFB, tmin=TMIN)

    def report(self):
        v, s = self.values, self.spec
        out = ["LM3478 boost design", "=" * 60,
               f"Vin {s.vin:g} V{f' (LM3478 VIN {s.vcc:g} V)' if s.vcc else ''}, Vout {', '.join(f'{x:g}' for x in s.vouts)} V, "
               f"Iout {si(s.iout_min, 'A')}..{si(s.iout_max, 'A')}, fs {si(v['fs'], 'Hz')}", ""]
        w = max(len(r[0]) for r in self.reqs)
        wp = max(len(r[1]) for r in self.reqs)
        for ref, part, val, note in self.reqs:
            out.append(f"{ref:<{w}}  {part:<{wp}}  {val}")
            if note:
                out.append(f"{'':<{w}}  {'':<{wp}}  ({note})")
        out += ["", "Operating points", "-" * 60]
        out += [c["text"] for c in self.corners]
        out += ["", "Estimated losses [mW] (part values: " + ", ".join(
            f"{k} {si(v[k], u)}" for k, (_, u) in LOSS_PARTS.items()) + f", L DCR {si(v['dcr'], 'Ω')}"
            + (f", L Rp {si(v['rp'], 'Ω')}, Cp {si(v['cp'] or 0, 'F')}" if v.get("rp") else "")
            + (", Co(er) / Cj(er) at each Vout from the model curves" if v.get("coss_curve") else "") + ")", "-" * 60]
        out += loss_table(self.losses)
        out += ["", "Loop (AN-1286 model)", "-" * 60]
        wl = max([len(lab) for lab, _, _ in self.loops] + [4])
        out.append(f"{'case':<{wl}}  mode  {'fc':>10}  {'PM':>5}  flags")
        for lab, _, i in self.loops:
            out.append(f"{lab:<{wl}}  {i['mode']:<4}  {si(i['fc'], 'Hz'):>10}  {i['pm']:4.0f}°  {i['flag']}")
        if self.notes:
            out += ["", "Notes"] + [f"- {n}" for n in self.notes]
        if self.warnings:
            out += ["", "WARNINGS"] + [f"! {n}" for n in self.warnings]
        return "\n".join(out)


# ---------------------------------------------------------------- power stage helpers

def duty(s, vin, vout):
    return 1 - (vin - s.vq) / (vout + s.vd)                      # DS (9)


def stage(s, vin, vout, iout, L, fs):
    """Currents at one operating point (CCM or DCM)."""
    D = duty(s, vin, vout)
    il = iout / ((1 - D) * s.eff)                                # DS (13), with efficiency
    dil = vin * D / (fs * L)                                     # peak-to-peak (DS (14) is half of it)
    if il >= dil / 2:
        mode, ipk, ton = "CCM", il + dil / 2, D / fs             # DS (12)
        t2 = (1 - D) / fs
        il_rms = math.sqrt(il**2 + dil**2 / 12)
        sw_rms = math.sqrt(D) * il_rms
    else:
        mode = "DCM"
        ipk = math.sqrt(2 * iout * (vout + s.vd - vin) / (s.eff * L * fs))
        ton = ipk * L / vin
        t2 = ipk * L / (vout + s.vd - vin)
        il_rms = ipk * math.sqrt((ton + t2) * fs / 3)
        sw_rms = ipk * math.sqrt(ton * fs / 3)
        D = ton * fs
    return dict(vin=vin, vout=vout, iout=iout, D=D, il=il, dil=dil, ipk=ipk, il_rms=il_rms,
                sw_rms=sw_rms, mode=mode, ton=ton, toff=t2)


def ic_vin(s, vin):
    """Voltage on the LM3478 VIN pin: its own supply when given, else the regulator input."""
    return s.vcc or vin


def c_er(curve, v_at, ref):
    """Energy-equivalent capacitance 0 -> v_at from a measured C(V) curve [[V...], [C...]]:
    Co(er) = 2/V^2 * integral v C(v) dv. Scaled so the curve's top voltage gives `ref` (the measured or
    typed Co(er)); above the curve it stays at that value."""
    V, C = (np.asarray(a, float) for a in curve)
    V, C = np.concatenate([[0.0], V]), np.concatenate([[C[0]], C])
    E = np.concatenate([[0.0], np.cumsum(0.5 * (V[1:] * C[1:] + V[:-1] * C[:-1]) * np.diff(V))])
    er = lambda x: 2 * np.interp(x, V, E) / x ** 2
    return ref * er(min(v_at, V[-1])) / er(V[-1])


def slope_need(s, vin, vout, L, fs):
    """(VSL + K*RSL) / RSN must be >= this [A] for Q <= q_target (AN-1286 eq. 11)."""
    D = duty(s, vin, vout)
    need = D - 0.5 + 1 / (math.pi * s.q_target)
    return (vin / L) * need / ((1 - D) * fs) if need > 0 else 0.0


def losses(s, v, c, rf2):
    """Estimated losses [W] at one operating point (a stage() dict). Coss / Cj use the energy-equivalent
    capacitance 0 -> this Vout (from the model's C(V) curve when there is one); switching loss = DS (29) with
    the turn-on current at the valley (0 in DCM). L core: the inductor model's parallel resistance Rp, with
    the winding voltage Vin (on) and Vout + Vf - Vin (diode conducting); its parallel capacitance Cp is
    charged with the switch node, so it adds to the Coss term."""
    vin, vout, iout, fs = c["vin"], c["vout"], c["iout"], v["fs"]
    coss = c_er(v["coss_curve"], vout, v["coss"]) if v.get("coss_curve") else v["coss"]
    cj = c_er(v["cj_curve"], vout, v["cj"]) if v.get("cj_curve") else v["cj"]
    vl2 = fs * (vin ** 2 * c["ton"] + (vout + s.vd - vin) ** 2 * c["toff"])     # mean square winding voltage
    i_on = c["il"] - c["dil"] / 2 if c["mode"] == "CCM" else 0.0
    D = c["D"] if c["mode"] == "CCM" else min(c["D"], 0.95)
    out = {
        "L DCR": c["il_rms"] ** 2 * v["dcr"],
        "L core": vl2 / v["rp"] if v.get("rp") else 0.0,
        "Q cond": 1.3 * c["sw_rms"] ** 2 * v["rds"],                       # DS (28), +30 % hot
        "Q switch": 0.5 * vout * (i_on + c["ipk"]) * v["tsw"] * fs,           # DS (29)
        "Q Coss": 0.5 * (coss + (v.get("cp") or 0.0)) * vout ** 2 * fs,
        "Q gate": v["qg"] * ic_vin(s, vin) * fs,     # gate charge drawn from the IC supply (through its 7.2 V LDO)
        "D cond": s.vd * iout,
        "D Cj": 0.5 * cj * vout ** 2 * fs,
        "RSN": c["sw_rms"] ** 2 * v["rsn"],
        "Cout ESR": iout ** 2 * D / (1 - D) * v["esr"],                      # DS (32)
        "FB divider": vout ** 2 / (s.rf1 + rf2),
        "LM3478": ic_vin(s, vin) * I_SUPPLY,
    }
    pout = vout * iout
    pin = pout + sum(out.values())
    return dict(out, Pout=pout, Pin=pin, eff=pout / pin, Vin=vin, Vout=vout, Iout=iout)


def text_table(head, rows, left=1):
    """Monospace table: the first `left` columns left-aligned, the rest right-aligned; widths fit the content."""
    cols = list(zip(*([head] + rows))) if rows else [[h] for h in head]
    w = [max(len(str(x)) for x in c) for c in cols]
    fmt = lambda r: "  ".join(str(x).ljust(w[i]) if i < left else str(x).rjust(w[i]) for i, x in enumerate(r))
    return [fmt(head), "  ".join("-" * x for x in w)] + [fmt(r) for r in rows]


def point_losses(d, points):
    """Estimated losses at arbitrary (Vin, Vout, Iout) points with the design's parts. The feedback divider of
    the nearest Vout setting is used; points with Vout <= Vin or Iout <= 0 are skipped."""
    s, v = d.spec, d.values
    out = []
    for vin, vout, iout in points:
        if vout <= vin or iout <= 0:
            continue
        rf2 = min(v["rf2"], key=lambda t: abs(t[0] - vout))[1]
        c = stage(replace(s, vin=vin), vin, vout, iout, v["L"], v["fs"])
        out.append((f"{vin:g} V → {vout:g} V, {si(iout, 'A')}", losses(s, v, c, rf2)))
    return out


def loss_table(rows):
    head = ["Vin V", "Vout V", "Iout mA", "Pout", "Pin", "η %"] + LOSS_COLS
    body = [[f"{l['Vin']:g}", f"{l['Vout']:g}", f"{l['Iout']*1e3:g}", f"{l['Pout']*1e3:.0f}", f"{l['Pin']*1e3:.0f}",
             f"{l['eff']*100:.1f}"] + [f"{l[k]*1e3:.1f}" for k in LOSS_COLS] for lab, l in rows]
    out = text_table(head, body, left=0)
    out.append("Q gate is dissipated in the LM3478 driver and the gate resistance. L core: the inductor model's "
               "parallel R (0 without a model); its parallel C is in Q Coss.")
    return out


def q_factor(s, vin, vout, L, fs, rsn, vsl_tot):
    D = duty(s, vin, vout)
    x = (1 - D) * (vsl_tot * fs / rsn) / (vin / L) + 0.5 - D     # AN-1286 eq. (11)
    return 1 / (math.pi * x) if x != 0 else math.inf


# ---------------------------------------------------------------- compensation (AN-1286)

def loop_eval(d, comp, vin, vout, iout):
    _, _, info = loop_tf(d.params(vin, vout, iout), comp)
    return info


def autotune(d, ccm_pts, dcm_pts):
    """AN-1286 sec. 5 procedure: RC1 sets fc <= fz2/10 (and fs/10) at the binding corner,
    CC1 puts the zero a factor 3 below the lowest crossover, CC2 puts a pole ~10x above the highest."""
    s, fs = d.spec, d.values["fs"]
    if not ccm_pts:                                    # all DCM: no RHP zero, keep fc <= fs/20
        ccm_pts = [p for p in dcm_pts if p[2] == max(q[2] for q in dcm_pts)]

    def limit(p):
        i = loop_eval(d, {"rc1": 10e3, "cc1": 10e-9, "cc2": 1e-12}, *p)
        lim = min(i.get("fz2", np.inf) / 10, fs / 10) if i["mode"] == "CCM" else fs / 20
        return 0.95 * lim

    lims = [limit(p) for p in ccm_pts]

    def worst_ratio(rc1):
        comp = {"rc1": rc1, "cc1": 1 / (2 * np.pi * 300 * rc1), "cc2": 1e-12}
        r = []
        for p, lim in zip(ccm_pts, lims):
            fc = loop_eval(d, comp, *p)["fc"]
            r.append(fc / lim if np.isfinite(fc) else 0)
        return max(r)

    lo, hi = 200.0, 500e3                               # bisection on log RC1 for worst_ratio = 1
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if worst_ratio(mid) < 1 else (lo, mid)
    rc1 = e_round(lo, E24, "down")

    fcs = [loop_eval(d, {"rc1": rc1, "cc1": 1 / (2 * np.pi * 300 * rc1), "cc2": 1e-12}, *p)["fc"] for p in ccm_pts]
    fc_min, fc_max = np.nanmin(fcs), np.nanmax(fcs)
    cc1 = min(e_round(1 / (2 * np.pi * (fc_min / 3) * rc1), E12, "nearest"), CC1_MAX)
    fp3 = min(10 * fc_max, fs / 3)
    cc2 = e_round(1 / (2 * np.pi * fp3 * rc1), E12, "nearest")
    comp = {"rc1": rc1, "cc1": cc1, "cc2": cc2}

    # raise CC1 (lower the zero) while light-load margin is short, up to the slew-rate limit
    def worst_pm(c):
        ccm = min(loop_eval(d, c, *p)["pm"] for p in ccm_pts)
        dcm = min([loop_eval(d, c, *p)["pm"] for p in dcm_pts] or [180])
        return ccm, dcm
    while True:
        ccm_pm, dcm_pm = worst_pm(comp)
        nxt = e_round(comp["cc1"] * 1.01, E12, "up")
        if dcm_pm >= s.pm_target_dcm or nxt > CC1_MAX:
            break
        comp = dict(comp, cc1=nxt)
    return comp, dict(fc_min=fc_min, fc_max=fc_max, lims=lims)


# ---------------------------------------------------------------- the design

def design(s: Spec, choice=None):
    """Design from the spec. When the loss estimate says the efficiency estimate is optimistic,
    the currents (current limit, Isat, ratings) are sized with the lower, estimated efficiency."""
    d = _design(s, choice)
    est = d.values["eff_est"]
    if est < s.eff - 0.02:
        d = _design(replace(s, eff=round(est, 3)), choice)
        d.spec = s
        d.notes.insert(0, f"Currents are sized with the estimated efficiency {est:.0%}, not the {s.eff:.0%} "
                          f"entered (loss estimate below).")
    return d


def _design(s: Spec, choice=None):
    choice = choice or {}
    d = Design(spec=s)
    v = d.values
    vouts = sorted(s.vouts)
    vins = [s.vin]
    vmax, vmin_out = vouts[-1], vouts[0]

    # -- frequency set resistor, DS (7)
    rfa = e_round(FA_K * s.fs ** FA_EXP, E96)
    fs = (rfa / FA_K) ** (1 / FA_EXP)
    v.update(rfa=rfa, fs=fs)

    # -- inductor: ripple ratio at full load, worst over the corners (DS (13), (14))
    def l_need(vin, vout):
        D = duty(s, vin, vout)
        return vin * D / (fs * s.ripple_ratio * s.iout_max / ((1 - D) * s.eff))
    L_req = max(l_need(vi, vo) for vi in vins for vo in vouts)
    L_auto = e_round(L_req, E12, "up")
    L = choice.get("L") or L_auto
    v.update(L=L, L_req=L_req, L_auto=L_auto)

    full = [stage(s, vi, vo, s.iout_max, L, fs) for vo in vouts for vi in vins]
    worst = max(full, key=lambda c: c["ipk"])

    # -- RSN and RSL together: current limit (DS (17)-(25)) and Q <= q_target (AN-1286 eq. 11)
    A = max(slope_need(s, vi, vo, L, fs) for vi in vins for vo in vouts)
    vsl_cl = VSENSE * VSL_RATIO
    cands = sorted({e_round(x * 1e-3, E24) for x in np.logspace(0, 3, 400)}, reverse=True)
    rsl_fixed = choice.get("rsl")                                # user's RSL: the slope ramp is given
    x_of = lambda r: K_SL * rsl_fixed if rsl_fixed else max(0.0, A * r - VSL)
    slope_ok = lambda r: VSL + x_of(r) >= A * r * (1 - 1e-9)     # Q <= q_target at every corner
    cl_ok = lambda r: all(VSENSE - c["D"] * (vsl_cl + x_of(r)) >= r * s.cl_margin * c["ipk"] for c in full)
    rsn = choice.get("rsn")                                      # user's RSN
    for r in ([] if rsn else cands):                             # largest RSN that satisfies both
        if slope_ok(r) and cl_ok(r):
            rsn = r
            break
    if rsn is None:
        rsn = cands[-1]
        d.warnings.append("No RSN meets both the current limit and the slope requirement - "
                          "increase L or fs, or relax q_target / cl_margin" + (" or change RSL." if rsl_fixed else "."))
    x = x_of(rsn)
    rsl = rsl_fixed or (e_round(x / K_SL, E24, "up") if x > 0 else 100.0)
    x = K_SL * rsl
    ilim = lambda D: (VSENSE - D * (vsl_cl + x)) / rsn           # DS (25)
    Ds = [c["D"] for c in full]
    ilim_max, ilim_min = ilim(min(Ds)), ilim(max(Ds))
    if choice.get("rsn") or rsl_fixed:
        short = [c for c in full if ilim(c["D"]) < s.cl_margin * c["ipk"]]
        if short:
            c = min(short, key=lambda c: ilim(c["D"]) / c["ipk"])
            d.warnings.append(f"RSN {si(rsn, 'Ω')} / RSL {si(rsl, 'Ω')}: current limit {ilim(c['D']):.2f} A is only "
                              f"{ilim(c['D']) / c['ipk']:.2f} × the {c['ipk']:.2f} A peak at {c['vout']:g} V "
                              f"(target {s.cl_margin:g} ×) - risk of locking up in current limit.")
        qs = [q_factor(s, vi, vo, L, fs, rsn, VSL + x) for vi in vins for vo in vouts]
        bad = [q for q in qs if q < 0 or q > s.q_target]
        if bad:
            worst_q = min(bad) if min(bad) < 0 else max(bad)
            d.warnings.append(f"RSN {si(rsn, 'Ω')} / RSL {si(rsl, 'Ω')}: Q = {worst_q:.2f} at the worst corner "
                              f"(target ≤ {s.q_target:g}" + ("; negative = subharmonic oscillation)" if worst_q < 0 else ")"))
    rsn_ds21 = min(2 * VSL * fs * L / (vo - 2 * vi) for vi in vins for vo in vouts if vo > 2 * vi)
    v.update(rsn=rsn, rsl=rsl, ilim_max=ilim_max, ilim_min=ilim_min, isc=VSC / rsn)

    # -- output capacitor
    dv_c, dv_esr = 0.5 * s.vout_ripple, 0.5 * s.vout_ripple
    cout_req = max(c["iout"] * c["D"] / (fs * dv_c) for c in full)
    esr_max = dv_esr / worst["ipk"]
    cout_irms = max(c["iout"] * math.sqrt(c["D"] / (1 - c["D"])) for c in full)   # DS (32)
    cout = choice.get("cout") or e_round(cout_req, E6, "up")
    esr = choice.get("esr") or min(esr_max / 2, 0.010)
    v.update(cout=cout, esr=esr, dcr=choice.get("dcr"))

    # -- inductor requirements
    il_rms = max(c["il_rms"] for c in full)
    pout_max = vmax * s.iout_max
    dcr_max = s.loss_budget * pout_max / il_rms**2
    if v["dcr"] is None:
        v["dcr"] = dcr_max / 2
    volt_sec = max(c["vin"] * c["D"] / fs for c in full)
    light = [stage(s, s.vin, vo, s.iout_min, L, fs) for vo in vouts]
    iout_crit = [(vo, (stage(s, s.vin, vo, 1.0, L, fs)["dil"] / 2) * (1 - duty(s, s.vin, vo)) * s.eff)
                 for vo in vouts]
    iskip = [(vo, (s.vin * TMIN) ** 2 * fs * s.eff / (2 * L * (vo + s.vd - s.vin))) for vo in vouts]

    # -- feedback divider, DS (15)
    rf2 = [(vo, e_round(VFB * s.rf1 / (vo - VFB), E96)) for vo in vouts]
    v["rf2"] = rf2
    i_div = max(vo / (s.rf1 + r2) for vo, r2 in rf2)
    if i_div > 0.1 * s.iout_min or i_div > 1e-3:
        d.warnings.append(f"Feedback divider draws {si(i_div, 'A')} at {vmax:g} V "
                          f"({si(i_div * vmax, 'W')}): raise RF1 (now {si(s.rf1, 'Ω')}).")

    # -- MOSFET / diode / Cin requirements
    vdrv = min(ic_vin(s, s.vin), VDRV_MAX)
    sw_rms = max(c["sw_rms"] for c in full)
    rds_max = s.loss_budget * pout_max / (1.3 * sw_rms**2)                        # DS (28), +30 % hot
    ts = 1 / fs
    qg_max = 0.5 * I_DRV * 0.03 * ts                                              # edges < 3 % of Ts at ~0.5 A
    coss_max = 2 * s.loss_budget * pout_max / (vmax**2 * fs)
    for k, default in (("rds", rds_max), ("qg", qg_max), ("coss", coss_max), ("tsw", 20e-9)):
        v[k] = choice.get(k) or default             # no value entered: a part that just meets the target
    psw = 0.5 * vmax * (2 * worst["il"]) * v["tsw"] * fs                          # DS (29)
    dil_max = max(c["dil"] for c in full)
    cin_req = max(10e-6, dil_max / (8 * fs * s.vin_ripple * s.vin))
    cin_irms = dil_max / math.sqrt(12)                                            # DS (31)
    cj_max = 2 * s.loss_budget * pout_max / (vmax**2 * fs)
    v["cj"] = choice.get("cj") or cj_max
    for k in ("rp", "cp", "coss_curve", "cj_curve"):          # from the inductor / MOSFET / diode models
        v[k] = choice.get(k)

    # -- operating-point table
    for c in full + light:
        q = q_factor(s, c["vin"], c["vout"], L, fs, rsn, VSL + x) if c["mode"] == "CCM" else math.nan
        isen = rsn * c["ipk"] + x * c["D"]
        c["text"] = (f"Vin {c['vin']:5.1f} Vout {c['vout']:5.1f} Iout {si(c['iout'], 'A'):>7} {c['mode']} "
                     f"D {c['D']:.3f}  IL {c['il']:.3f} A  dIL {c['dil']:.3f} A  Ipk {c['ipk']:.3f} A  "
                     f"ISEN {isen*1e3:4.0f} mV  Q {q:5.2f}")
        d.corners.append(c)

    # -- compensation (AN-1286); without a user Cout, Cout grows until RC1 >= RC1_MIN
    ccm_pts = [(vi, vo, s.iout_max) for vo in vouts for vi in vins
               if stage(s, vi, vo, s.iout_max, L, fs)["mode"] == "CCM"]
    # light-load checks for the CC1 search: Iout min (any mode) and the DCM points between min and max;
    # pulse-skipping points (on-time below 325 ns) have no small-signal model and are left out
    dcm_pts = [(s.vin, vo, io) for vo in vouts for io in (s.iout_min, math.sqrt(s.iout_min * s.iout_max))
               if (io == s.iout_min or stage(s, s.vin, vo, io, L, fs)["mode"] == "DCM")
               and stage(s, s.vin, vo, io, L, fs)["ton"] >= TMIN]
    cout_ripple = v["cout"]
    while True:
        if choice.get("comp"):
            comp = dict(choice["comp"]); break
        comp, tune = autotune(d, ccm_pts, dcm_pts)
        if choice.get("cout") or comp["rc1"] >= RC1_MIN or v["cout"] > 1e-3:
            break
        v["cout"] = e_round(v["cout"] * 1.01, E6, "up")
    if v["cout"] > cout_ripple:
        d.notes.append(f"Cout raised from {si(cout_ripple, 'F')} (ripple) to {si(v['cout'], 'F')} so the loop "
                       f"gets RC1 ≥ {si(RC1_MIN, 'Ω')} (enough error-amp gain at crossover).")
    cout = v["cout"]
    v["comp"] = comp
    for vi, vo, io in [(s.vin, vo, io) for vo in vouts for io in (s.iout_min, s.iout_max)]:   # plotted cases
        info = loop_eval(d, comp, vi, vo, io)
        d.loops.append((f"{vi:g} V → {vo:g} V, {si(io, 'A')}", d.params(vi, vo, io), info))
        if io == s.iout_max and vi == s.vin and np.isfinite(info["fc"]):
            di = s.iout_max - s.iout_min
            d.notes.append(f"{vo:g} V: load step {si(s.iout_min, 'A')} → {si(s.iout_max, 'A')} deviates about "
                           f"{di / (2 * np.pi * info['fc'] * cout) * 1e3:.0f} mV (ΔI / (2π·fc·Cout)).")
    rout = AEA / GM

    # -- requirement rows
    R = d.reqs.append
    R(("R5", "RFA (frequency)", f"{si(rfa, 'Ω')} (E96) → fs = {si(fs, 'Hz')}", "DS (7)"))
    R(("L1", "Inductor", f"{si(L, 'H')}" + ("" if L == L_auto else f"  (design value {si(L_auto, 'H')})"),
       f"ripple {s.ripple_ratio:.0%} at full load needs ≥ {si(L_req, 'H')}; ±20 % tolerance"))
    R(("", "  Isat", f"≥ {ilim_max:.2f} A", f"= max current limit, so the limit protects the core; "
       f"peak in operation {worst['ipk']:.2f} A"))
    R(("", "  Irms (heating)", f"≥ {il_rms:.2f} A", "worst corner, incl. ripple"))
    R(("", "  DCR", f"≤ {si(dcr_max, 'Ω')}", f"{s.loss_budget:.0%} of Pout = {pout_max * s.loss_budget:.2f} W"))
    R(("", "  Ripple dIL (p-p)", f"{min(c['dil'] for c in full):.3f}–{dil_max:.3f} A", f"volt-seconds {volt_sec*1e6:.1f} V·µs (core loss)"))
    R(("", "  Rated voltage / SRF", f"≥ {vmax:.0f} V / ≥ {si(10 * fs, 'Hz')}", "shielded, soft saturation preferred"))
    R(("R1", "RSN (current sense)", f"{si(rsn, 'Ω')}",
       f"limit {ilim_min:.2f}–{ilim_max:.2f} A over D, short-circuit latch {VSC / rsn:.1f} A; "
       f"power {sw_rms**2 * rsn * 1e3:.0f} mW"))
    R(("R2", "RSL (slope comp)", f"{si(rsl, 'Ω')}",
       f"adds {x*1e3:.0f} mV ramp; Q ≤ {s.q_target:g} needs (VSL+K·RSL)/RSN ≥ {A:.2f} A; "
       f"DS (21) without RSL: RSN < {si(rsn_ds21, 'Ω')}"))
    R(("Q", "MOSFET", f"VDS ≥ {v_class(1.25 * (vmax + s.vd))} V class", f"off-state {vmax + s.vd:.0f} V + 25 %"))
    R(("", "  Current", f"Irms {sw_rms:.2f} A, peak ≥ {ilim_max:.2f} A", f"short-circuit {VSC / rsn:.1f} A for ≤ 325 ns"))
    R(("", "  RDS(on) at VGS = " + f"{vdrv:.1f} V", f"≤ {si(rds_max, 'Ω')}", "conduction loss budget, +30 % hot (DS (28))"))
    R(("", "  Qg / Coss(er)", f"≤ {si(qg_max, 'C')} / ≤ {si(coss_max, 'F')}",
       f"drive {vdrv:.1f} V ({'logic-level' if vdrv < 7.2 else 'standard or logic-level'}); "
       f"switching loss ≈ {psw*1e3:.0f} mW at {si(v['tsw'], 's')} edges (DS (29))"))
    R(("D", "Diode (Schottky)", f"VR ≥ {v_class(1.25 * vmax)} V class", f"reverse voltage {vmax:.0f} V + 25 %"))
    R(("", "  Current", f"IF(AV) ≥ {s.iout_max:.2f} A (2x margin: {2 * s.iout_max:.2f} A), IFRM ≥ {ilim_max:.2f} A", "DS (27)"))
    R(("", "  Loss / Cj", f"{s.vd * s.iout_max * 1e3:.0f} mW conduction, Cj ≤ {si(cj_max, 'F')} at {vmax:.0f} V",
       "capacitive loss ½·Cj·V²·fs within budget"))
    R(("Cout", "Output capacitor", f"{si(cout, 'F')} effective at bias" + ("" if choice.get("cout") else " (design)"),
       f"ripple {s.vout_ripple*1e3:.0f} mV pp needs ≥ {si(cout_req, 'F')}"))
    R(("", "  ESR / Irms / voltage", f"≤ {si(esr_max, 'Ω')} / ≥ {cout_irms:.2f} A / ≥ {v_class(1.25 * vmax)} V",
       f"using {si(esr, 'Ω')}; ceramics lose capacitance at DC bias - use the value at {vmax:.0f} V"))
    R(("Cin", "Input capacitor", f"≥ {si(cin_req, 'F')}, ≥ {v_class(1.5 * s.vin)} V, Irms ≥ {cin_irms:.2f} A",
       "DS 7.2.1.2.8: 10-20 µF good quality; + 0.1-1 µF at the VIN pin"
       + ("; add 20 Ω RIN filter (Vin < 8 V)" if ic_vin(s, s.vin) < 8 and not s.vcc else "")))
    if s.vcc:
        R(("", "  LM3478 VIN", f"{s.vcc:g} V (separate supply)",
           f"IC {si(s.vcc * I_SUPPLY, 'W')} + gate drive Qg·{s.vcc:g} V·fs; 0.1-1 µF at the pin"
           + ("; add 20 Ω RIN filter (< 8 V)" if s.vcc < 8 else "")))
    for vo, r2 in rf2:
        vact = VFB * (1 + s.rf1 / r2)
        R(("RF1/RF2", f"Feedback for {vo:g} V", f"{si(s.rf1, 'Ω')} / {si(r2, 'Ω')} (E96) → {vact:.2f} V", "DS (15)"))
    R(("R6", "RC1", si(comp["rc1"], "Ω"), "AN-1286 sec. 5: sets fc ≤ fz2/10"))
    R(("C4", "CC1", si(comp["cc1"], "F"),
       f"zero {si(1 / (2 * np.pi * comp['cc1'] * comp['rc1']), 'Hz')}, pole "
       f"{si(1 / (2 * np.pi * comp['cc1'] * (rout + comp['rc1'])), 'Hz')}; COMP slew {I_EA / comp['cc1'] / 1e3:.1f} V/ms"))
    R(("C3", "CC2", si(comp["cc2"], "F"), f"pole {si(1 / (2 * np.pi * comp['cc2'] * comp['rc1']), 'Hz')}"))

    # -- notes and checks
    # loss estimate per Vout x {Iout min, max}; the full-load worst case checks the efficiency estimate
    rf2_of = dict(rf2)
    for vo in vouts:
        for io in (s.iout_min, s.iout_max):
            d.losses.append((f"{s.vin:g} V → {vo:g} V, {si(io, 'A')}",
                             losses(s, v, stage(s, s.vin, vo, io, L, fs), rf2_of[vo])))
    eff_est = losses(s, v, stage(s, s.vin, vmax, s.iout_max, L, fs), rf2_of[vmax])["eff"]
    v["eff_est"] = eff_est
    d.notes.append(f"Estimated efficiency at {vmax:g} V / {si(s.iout_max, 'A')}: about {eff_est:.0%} "
                   f"(loss table in the report / Losses tab; MOSFET and diode values: from their models, typed, or the budget targets).")
    for vo, ic in iout_crit:
        d.notes.append(f"{vo:g} V: CCM above {si(ic, 'A')} at Vin {s.vin:g} V; pulse skipping below about "
                       f"{si(dict(iskip)[vo], 'A')} (325 ns minimum on-time).")
    d.notes.append("The loop model ignores inductor DCR and capacitor ESL; it uses the AN-1286 resistive load.")
    if s.vcc and not 2.97 <= s.vcc <= 40:
        d.warnings.append(f"LM3478 VIN {s.vcc:g} V is outside its 2.97-40 V operating range.")
    if vdrv < 4.5:
        d.warnings.append(f"Gate drive is only {vdrv:.1f} V (LM3478 VIN): check the MOSFET's RDS(on) at this VGS "
                          f"(Losses tab, RDS(on) vs VGS).")
    if max(Ds) > 0.9:
        d.warnings.append(f"Duty cycle reaches {max(Ds):.2f}: conduction losses and the RHP zero get severe.")
    ton_min = min(c["ton"] for c in full)
    if ton_min < TMIN:
        d.warnings.append(f"Full-load on-time {ton_min*1e9:.0f} ns < 325 ns minimum on-time.")
    if worst["ipk"] * rsn + worst["D"] * x > 0.110:
        d.warnings.append("ISEN peak exceeds 110 mV at full load: little margin to the 125 mV minimum threshold.")
    if L != L_auto and L < L_req:
        d.warnings.append(f"L = {si(L, 'H')} is below the {s.ripple_ratio:.0%}-ripple value; ripple and peak current rise.")
    if choice.get("cout") and cout < cout_req:
        d.warnings.append(f"Cout = {si(cout, 'F')} is below the {si(cout_req, 'F')} needed for the ripple target.")
    if choice.get("esr") and esr > esr_max:
        d.warnings.append(f"ESR = {si(esr, 'Ω')} exceeds {si(esr_max, 'Ω')} for the ripple target.")
    for lab, _, i in d.loops:
        target = s.pm_target if i["mode"] == "CCM" else s.pm_target_dcm
        if i["flag"]:
            d.warnings.append(f"{lab}: {i['flag']}")
        elif not np.isfinite(i["pm"]) or i["pm"] < target:
            d.warnings.append(f"{lab}: phase margin {i['pm']:.1f}° below the {target:g}° target")
    if comp["cc1"] > CC1_MAX:
        d.warnings.append("CC1 > 47 nF: COMP slews slowly; expect startup overshoot (check in simulation).")
    return d


if __name__ == "__main__":
    print(design(Spec()).report())
