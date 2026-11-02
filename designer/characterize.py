"""Characterize SPICE models with short ngspice runs, for the loss estimate and the part values.

MOSFET (at the board's conditions):
  RDS(on)  DC, VGS = drive voltage, ID = full-load inductor current
  Co(er)   energy-equivalent output capacitance 0 -> Vout (gate shorted to source): Co(er) = 2 E / V^2
  Qoss     charge 0 -> Vout
  Qg, edges  clamped inductive switching at Vout and the inductor current, driven like the board:
           R7 22 ohm + the LM3478 driver (16 ohm source / 4.5 ohm sink, averaged). Edges are 90 % <-> 10 % of Vout.
           Energies are V_DS * I_D at the terminals. At turn-off part of that charges Coss (stored, dissipated
           at the next turn-on - the Coss term), so E_off = terminal energy - E_oss(Vout). At turn-on the Coss
           discharge stays inside the part, so the terminal energy is the overlap loss only.
           t_eff = (E_on + E_off) / (Vout * I): the edge time that gives the same loss in DS (29)
           (P_sw = 1/2 * V * (I_on + I_off) * t * fs).
Diode:     Vf at Iout, Cj(er) 0 -> Vout, reverse current at Vout.
Inductor:  DCR (DC), L at fs and at 10 kHz, the self-resonance (impedance sweep), and what sits in parallel
           with DCR + L at fs: Rp (core loss) and Cp (winding capacitance).

Each result also carries "curves" (short lists, kept in the configuration) for the Losses-tab plots:
MOSFET RDS(on) vs VGS, Coss vs VDS, the switching edges and the gate charge; diode IF vs VF and Cj vs VR;
inductor L and |Z| vs frequency.

    from characterize import mosfet, diode, inductor
    mosfet("BSZ900N15NS3_L0", vdrv=7.2, vout=80, i_load=0.25, fs=1e6)
"""
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import booster_sim

DRV_RON, DRV_ROFF, R_GATE = 16.0, 4.5, 22.0      # LM3478 driver switch on-resistance (top / bottom), board R7
TAG = "@@"


def model_source(role, name):
    """(info, library path) of a built-in or uploaded model."""
    name = name.upper()
    for table in (booster_sim.BUILTIN, booster_sim.BUILTIN_L, booster_sim.user_models()):
        if name in table:
            path, info = table[name]
            return info, path
    raise ValueError(f"model {name} not found")


def instance(role, info, name, nodes, ref):
    """Element line for a model with the given nodes: MOSFET (d, g, s), diode (a, k), inductor (1, 2)."""
    if role == "mosfet":
        d, g, s = nodes
        if info["kind"] == "subckt":
            return f"X{ref} {d} {g} {s}{' ' + s if len(info['pins']) == 4 else ''} {name}"
        return f"M{ref} {d} {g} {s}{'' if info['mtype'] == 'vdmos' else ' ' + s} {name}"
    a, k = nodes
    if info["kind"] == "model":
        return f"D{ref} {a} {k} {name}"
    return f"X{ref} {a} {k} {name}"


def run(body, lib, timeout=120):
    """Run a netlist body (+ .control lines) in a temp dir; returns {name: value} of the '@@ name value' echoes,
    plus "_w" = {tag: array (scale, vectors...)} of every `wrdata w_<tag>.txt ...` (see wr())."""
    with tempfile.TemporaryDirectory(prefix="char_") as td:
        shutil.copy(booster_sim.SIM_DIR / ".spiceinit", Path(td) / ".spiceinit")
        cir = Path(td) / "t.cir"
        cir.write_text(f"* characterization\n.include \"{lib}\"\n{body}\n.end\n")
        p = subprocess.run(["ngspice", "-b", cir.name], cwd=td, capture_output=True, text=True, timeout=timeout)
        waves = {}
        for f in Path(td).glob("w_*.txt"):
            try:
                waves[f.stem[2:]] = np.loadtxt(f, skiprows=1, ndmin=2).T
            except ValueError:
                pass
    out = {}
    for ln in (p.stdout + p.stderr).splitlines():
        if ln.startswith(TAG):
            parts = ln.split()
            try:
                out[parts[1]] = float(parts[2])
            except (IndexError, ValueError):
                pass
    if not out:
        err = [ln for ln in (p.stdout + p.stderr).splitlines() if re.search(r"error|not found|unknown", ln, re.I)]
        raise RuntimeError("ngspice gave no results" + (": " + " | ".join(err[:3]) if err else ""))
    out["_w"] = waves
    return out


def echo(*names):
    return "\n".join(f"echo {TAG} {n} $&{n}" for n in names)


def wr(tag, *vectors):
    """Control lines that write the vectors (one shared scale column) to w_<tag>.txt."""
    return f"set wr_singlescale\nset wr_vecnames\nwrdata w_{tag}.txt {' '.join(vectors)}"


def thin(*cols, n=300):
    """Columns cut to about n points, as plain lists (for the configuration file)."""
    step = max(1, len(cols[0]) // n)
    return [[float(f"{x:.5g}") for x in np.asarray(c)[::step]] for c in cols]


def mosfet(name, vdrv, vout, i_load, fs):
    info, lib = model_source("mosfet", name)
    if not booster_sim.usable("mosfet", info):
        raise ValueError(f"{name} is not a usable MOSFET model")
    res, w = {}, {}

    def go(body):
        r = run(body, lib)
        w.update(r.pop("_w"))
        res.update(r)
    # RDS(on) at the drive voltage and the load current, plus RDS(on) vs VGS (VDS 50 mV, linear region)
    go(f"""Vg g 0 {vdrv}
Id 0 d DC {i_load}
{instance("mosfet", info, name, ("d", "g", "0"), "Q")}
.control
op
let rds = v(d) / {i_load}
{echo("rds")}
.endc""")
    go(f"""Vg g 0 {vdrv}
Vd d 0 50m
{instance("mosfet", info, name, ("d", "g", "0"), "Q")}
.options gmin=1e-12
.control
dc Vg 1 10 0.05
let rv = 50m / (-i(vd) + 1e-12)
{wr("rds", "rv")}
echo {TAG} done 1
.endc""")
    # Co(er), Qoss: drain ramped 0 -> Vout in 10 us, gate on source
    t_r = 10e-6
    go(f"""Vd d 0 PWL(0 0 {t_r} {vout})
{instance("mosfet", info, name, ("d", "0", "0"), "Q")}
.control
tran 5n {t_r} 0 5n
let ic = -i(vd)
let p = ic * v(d)
meas tran eoss integ p from=0 to={t_r}
meas tran qoss integ ic from=0 to={t_r}
{echo("eoss", "qoss")}
let vd = v(d)
{wr("coss", "vd", "ic")}
.endc""")
    # clamped inductive switching, board-like gate drive
    t1, ton = 200e-9, 600e-9
    go(f"""Vdd vdd 0 {vout}
Il 0 d DC {i_load}
Dclamp d vdd DCLAMP
.model DCLAMP D(IS=1e-9 N=1 RS=10m CJO=1p TT=0)
Vdrv drv 0 PULSE(0 {vdrv} {t1} 2n 2n {ton} 10u)
Rg drv gm {R_GATE + (DRV_RON + DRV_ROFF) / 2}
Vgm gm g 0
Vdm d dq 0
{instance("mosfet", info, name, ("dq", "g", "0"), "Q")}
.options method=gear abstol=1e-6 vntol=1e-4 reltol=0.01 gmin=1e-12
.control
tran 0.2n {t1 + ton + 600e-9} 0 0.5n
let pq = i(vdm) * v(d)
let ig = i(vgm)
meas tran ton1 when v(d)={0.9 * vout} fall=1
meas tran ton2 when v(d)={0.1 * vout} fall=1
meas tran toff1 when v(d)={0.1 * vout} rise=1
meas tran toff2 when v(d)={0.9 * vout} rise=1
meas tran eon integ pq from={t1} to={t1 + ton / 2}
meas tran eoff integ pq from={t1 + ton} to={t1 + ton + 600e-9}
meas tran qg integ ig from={t1} to={t1 + ton}
{echo("ton1", "ton2", "toff1", "toff2", "eon", "eoff", "qg")}
let vds = v(d)
let vgs = v(g)
let idr = i(vdm)
{wr("sw", "vds", "vgs", "idr", "ig")}
.endc""")
    eon = max(res.get("eon", 0), 0)
    curves = {}
    if "rds" in w:
        vg_, rv = w["rds"]
        m = (rv > 0) & (rv < 1e3)
        curves["rds_vgs"] = thin(vg_[m], rv[m])
    if "coss" in w:
        t, vd_, ic = w["coss"]
        m = vd_ > 0.02 * vout
        curves["coss_vds"] = thin(vd_[m], ic[m] / (vout / t_r))          # C = I / (dV/dt), dV/dt constant
    if "sw" in w:
        t, vds, vgs, idr, ig = w["sw"]
        q = np.concatenate([[0], np.cumsum(0.5 * (ig[1:] + ig[:-1]) * np.diff(t))])
        on = (t >= t1 - 20e-9) & (t <= t1 + 150e-9)                   # turn-on edge
        off = (t >= t1 + ton - 20e-9) & (t <= t1 + ton + 150e-9)       # turn-off edge
        curves["sw_on"] = thin((t[on] - t1) * 1e9, vds[on], idr[on], vgs[on])
        curves["sw_off"] = thin((t[off] - t1 - ton) * 1e9, vds[off], idr[off], vgs[off])
        g = (t >= t1) & (t <= t1 + ton)
        curves["qg_vgs"] = thin(q[g] - q[g][0], vgs[g])
    eoff = max(res.get("eoff", 0) - res["eoss"], 0)          # minus the energy stored in Coss
    return dict(
        model=name, vdrv=vdrv, vout=vout, i_load=i_load,
        rds=res["rds"], coss=2 * res["eoss"] / vout ** 2, qoss=res["qoss"], qg=res.get("qg", math.nan),
        t_on=res.get("ton2", math.nan) - res.get("ton1", math.nan),
        t_off=res.get("toff2", math.nan) - res.get("toff1", math.nan),
        e_on=eon, e_off=eoff, e_off_terminal=res.get("eoff", math.nan), e_oss=res["eoss"],
        tsw=(eon + eoff) / (vout * i_load),
        p_sw=(eon + eoff) * fs, p_coss=0.5 * 2 * res["eoss"] / vout ** 2 * vout ** 2 * fs, curves=curves)


def diode(name, vout, iout):
    info, lib = model_source("diode", name)
    if not booster_sim.usable("diode", info):
        raise ValueError(f"{name} is not a usable diode model")
    res = run(f"""If 0 a DC {iout}
{instance("diode", info, name, ("a", "0"), "D")}
.control
op
let vf = v(a)
{echo("vf")}
.endc""", lib)
    res.pop("_w")
    w = run(f"""Va a 0 0
{instance("diode", info, name, ("a", "0"), "D")}
.control
dc Va 0 1.5 0.005
let ia = -i(va)
{wr("ivf", "ia")}
echo {TAG} done 1
.endc""", lib)["_w"]
    t_r = 10e-6
    res.update(run(f"""Vk k 0 PWL(0 0 {t_r} {vout} {2 * t_r} {vout})
{instance("diode", info, name, ("0", "k"), "D")}
.control
tran 5n {2 * t_r} 0 5n
let ic = -i(vk)
let p = ic * v(k)
meas tran ej integ p from=0 to={t_r}
meas tran ir find ic at={2 * t_r}
{echo("ej", "ir")}
let vr = v(k)
{wr("cj", "vr", "ic")}
.endc""", lib))
    w.update(res.pop("_w"))
    curves = {}
    if "ivf" in w:
        va, ia = w["ivf"]
        m = (ia > 1e-6) & (ia < 20 * iout)
        curves["if_vf"] = thin(va[m], ia[m])
    if "cj" in w:
        t, vr, ic = w["cj"]
        m = (t < t_r) & (vr > 0.02 * vout)
        curves["cj_vr"] = thin(vr[m], ic[m] / (vout / t_r))
    return dict(model=name, vout=vout, iout=iout, vf=res["vf"], cj=2 * res["ej"] / vout ** 2, ir=res.get("ir", math.nan),
                curves=curves)


def inductor(name, fs):
    info, lib = model_source("inductor", name)
    if not (info["kind"] == "subckt" and len(info["pins"]) == 2):
        raise ValueError(f"{name} is not a 2-pin inductor subcircuit")
    res = run(f"""I1 0 n DC 1 AC 1
{instance("inductor", info, name, ("n", "0"), "L")}
.control
op
let dcr = v(n)
{echo("dcr")}
ac dec 100 1k 1G
let zi = imag(v(n))
let lf = zi / (2 * pi * frequency)
meas ac l_fs find lf at={fs}
meas ac l_10k find lf at=10e3
meas ac srf when zi=0 fall=1
let zre = real(v(n))
meas ac zr_fs find zre at={fs}
meas ac zi_fs find zi at={fs}
{echo("l_fs", "l_10k", "srf", "zr_fs", "zi_fs")}
let zm = mag(v(n))
{wr("z", "lf", "zm")}
.endc""", lib)
    curves = {}
    if "z" in res["_w"]:
        z = res["_w"]["z"]                                   # freq, L, (imag 0), |Z|
        f, lf, zm = z[0], z[1], z[-1]
        curves["l_f"] = thin(f, lf, zm)
    # what is in parallel with DCR + L at fs: Y_par = 1/Z - 1/(DCR + jwL), L from 10 kHz -> Rp (core loss), Cp
    rp = cp = math.nan
    if "zr_fs" in res and "l_10k" in res:
        w = 2 * math.pi * fs
        yp = 1 / complex(res["zr_fs"], res["zi_fs"]) - 1 / complex(res["dcr"], w * res["l_10k"])
        rp = 1 / yp.real if yp.real > 0 else math.inf
        cp = max(yp.imag / w, 0.0)
    return dict(model=name, fs=fs, dcr=res["dcr"], L=res.get("l_fs", math.nan), L_10k=res.get("l_10k", math.nan),
                srf=res.get("srf", math.nan), rp=rp, cp=cp, curves=curves)


def vth(name, i_th=250e-6):
    """Gate threshold: VGS = VDS (gate tied to drain) at ID = 250 uA, the usual datasheet definition."""
    info, lib = model_source("mosfet", name)
    res = run(f"""Vg g 0 0
{instance("mosfet", info, name, ("g", "g", "0"), "Q")}
.options gmin=1e-12
.control
dc Vg 0 8 0.005
let id = -i(vg)
meas dc vth when id={i_th} rise=1
{echo("vth")}
.endc""", lib)
    return res.get("vth", math.nan)
