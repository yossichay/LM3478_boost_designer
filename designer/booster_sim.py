"""Run the designed booster in ngspice (board netlist + designed values) and measure it.

Each operating point (Vin, Vout setting, Iout) becomes one case: the base netlist with R1/R2/R3/R4/R5/R6/C3/C4,
the inductor (ideal L + DCR) and Cout replaced (C10 = Cout - 4.5 µF of board ceramics, with the ESR), the load replaced, V(out) pre-charged to the setpoint,
and the load ramped in after soft-start (6-7 ms); measurements over the last 2 ms. Measurements over the last part of the run: efficiency, output ripple, inductor
current, input ripple, and the spread of the per-cycle peak current (subharmonic oscillation shows as a
large spread).

    from booster_sim import run_points
    results = run_points(design, [(12, 80.3, 0.1), (12, 41.7, 0.01)], on_result=print)
"""
import datetime
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SIM_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SIM_DIR))
import sweep  # noqa: E402
from booster_design import si  # noqa: E402

NETLIST = SIM_DIR / "Non-iso-Booster-TI-ngspice.cir"
MOSFET_DEFAULT = "BSZ520N15NS3_VD"           # pin-compatible choices: BSZ520N15NS3_L1 / _L0 (slower)
DIODE_DEFAULT = "MBRS3200T3G_TT0"            # or DRB068LAM150
USER_MODELS = Path(__file__).resolve().parent / "user_models"   # uploaded libraries, each may hold many models
# libraries the base netlist already includes; every usable model in them is offered
BASE_LIBS = ["BSZ520N15NS3_vdmos.lib", "OptiMOS3_150V_Spice.lib", "MBRS3200T3G.lib", "rb068lam150.lib"]
INDUCTOR_LIBS = ["WE-TI.lib", "WE-PD.lib"]                     # Würth inductor models (2-pin subcircuits)
IDEAL_L = "(ideal L + DCR)"


def parse_models(text):
    """Every top-level .subckt / .model in a SPICE text -> [{name, kind, pins | mtype}] (models defined
    inside a .subckt belong to it and are skipped; '+' continuation lines are joined). Names are upper case:
    SPICE ignores case, so "irlml0100" and "IRLML0100" are the same model."""
    lines = []
    for ln in text.replace("\r", "").splitlines():
        if ln.lstrip().startswith("+") and lines:
            lines[-1] += " " + ln.lstrip()[1:]
        else:
            lines.append(ln)
    out, depth = [], 0
    for ln in lines:
        toks = ln.split()
        if not toks:
            continue
        head = toks[0].lower()
        if head == ".subckt" and len(toks) >= 2:
            if depth == 0:
                pins = []
                for tok in toks[2:]:
                    if "=" in tok or tok.lower().rstrip(":") in ("params", "optional", "text"):
                        break
                    pins.append(tok)
                out.append(dict(name=toks[1].upper(), kind="subckt", pins=pins))
            depth += 1
        elif head == ".ends":
            depth = max(0, depth - 1)
        elif head == ".model" and len(toks) >= 3 and depth == 0:
            mtype = re.split(r"[\s(]", toks[2], 1)[0].lower()
            out.append(dict(name=toks[1].upper(), kind="model", mtype=mtype))
    return out


def parse_model(text):
    """First model of a text (kept for old configurations)."""
    m = parse_models(text)
    if not m:
        raise ValueError("no .subckt or .model line found")
    return m[0]


def device_line(role, info, name):
    """Netlist line for the MOSFET (D=N002 G=N004 S=N008) or the diode (A=N002 K=out)."""
    if role == "mosfet":
        if info["kind"] == "subckt" and len(info["pins"]) == 3:
            return f"XM2 N002 N004 N008 {name}"
        if info["kind"] == "subckt" and len(info["pins"]) == 4:          # D G S B: body to source
            return f"XM2 N002 N004 N008 N008 {name}"
        if info["kind"] == "model" and info["mtype"] == "vdmos":
            return f"MM2 N002 N004 N008 {name}"
        if info["kind"] == "model" and info["mtype"] == "nmos":
            return f"MM2 N002 N004 N008 N008 {name}"
        raise ValueError(f"MOSFET {name}: need a 3-pin (D G S) or 4-pin (D G S B) .subckt, "
                         f"or a .model of type VDMOS / NMOS (got {info.get('pins') or info.get('mtype')})")
    if role == "inductor":
        if info["kind"] == "subckt" and len(info["pins"]) == 2:
            return f"XL1 N001a N002 {name}"
        raise ValueError(f"Inductor {name}: need a 2-pin .subckt (got {info.get('pins') or info.get('mtype')})")
    if info["kind"] == "model" and info["mtype"] == "d":
        return f"D2 N002 out {name}"
    if info["kind"] == "subckt" and len(info["pins"]) == 2:              # A K
        return f"XD2 N002 out {name}"
    raise ValueError(f"Diode {name}: need a .model of type D or a 2-pin (A K) .subckt "
                     f"(got {info.get('pins') or info.get('mtype')})")


def usable(role, info):
    try:
        device_line(role, info, info["name"]); return True
    except ValueError:
        return False


def _scan(paths):
    out = {}
    for p in paths:
        try:
            for info in parse_models(Path(p).read_text(errors="replace")):
                out.setdefault(info["name"], (Path(p), info))
        except OSError:
            pass
    return out


def find_lib(name):
    """A library named by the board netlist: in sim/, or moved to designer/user_models/."""
    for d in (SIM_DIR, USER_MODELS):
        if (d / name).exists():
            return d / name
    return SIM_DIR / name


BUILTIN = _scan([find_lib(n) for n in BASE_LIBS])                 # name -> (lib, info)
BUILTIN_L = _scan([find_lib(n) for n in INDUCTOR_LIBS])
MOSFETS = sorted(k for k, (_, i) in BUILTIN.items() if usable("mosfet", i))
DIODES = sorted(k for k, (_, i) in BUILTIN.items() if usable("diode", i))
INDUCTORS = sorted(k for k, (_, i) in BUILTIN_L.items() if usable("inductor", i))
# libraries the base netlist includes itself (their models need no extra .include)
NETLIST_INCLUDES = {Path(ln.split()[1].strip('"')).name for ln in (SIM_DIR / "Non-iso-Booster-TI-ngspice.cir")
                    .read_text().splitlines() if ln.lower().startswith(".include")}


def user_models():
    """name -> (library path, info) for every model in the uploaded libraries (built-in names excluded)."""
    return {k: v for k, v in _scan(sorted(USER_MODELS.glob("*.lib"))).items()
            if k not in BUILTIN and k not in BUILTIN_L}


def model_choices(role):
    """Built-in + uploaded model names usable for the role."""
    builtin = {"mosfet": MOSFETS, "diode": DIODES, "inductor": [IDEAL_L] + INDUCTORS}[role]
    return builtin, sorted(k for k, (_, i) in user_models().items() if usable(role, i))


def save_user_library(text, libname):
    """Store an uploaded / pasted library (all its models) as user_models/<libname>.lib. Returns its models."""
    models = parse_models(text)
    if not models:
        raise ValueError("no .subckt or .model definitions found")
    USER_MODELS.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", Path(libname).stem) or models[0]["name"]
    path = USER_MODELS / f"{safe}.lib"
    path.write_text(text.replace("\r", "").strip() + "\n")
    return path, models


def save_user_model(text, role):
    """One pasted model (old API): stored as its own library. Returns its name."""
    info = parse_model(text)
    device_line(role, info, info["name"])
    save_user_library(text, info["name"])
    return info["name"]


def resolve(role, name):
    """(netlist line, include path or None) for a model name."""
    name = name.upper()
    for table in (BUILTIN, BUILTIN_L):
        if name in table:                                               # board libraries
            path, info = table[name]
            return device_line(role, info, name), (None if path.name in NETLIST_INCLUDES else path)
    um = user_models()
    if name in um:
        path, info = um[name]
        return device_line(role, info, name), path
    raise ValueError(f"{role} model {name} not found (built-in libraries or user_models/)")


SIGNALS = ["v(out)", "i(vsense)", "v(n002)", "v(comp)", "v(n001)", "v(n008)", "v(n001a)", "v(n003)", "v(n004)"]
R_GATE = 22.0                                # R7, DR (N003) -> gate (N004) in the board netlist
LOAD_ON = 6e-3                               # soft-start (internal ~4 ms) is over by then


def sim_signals():
    return list(SIGNALS)


def case_setup(design, vin, vout, iout, mosfet, diode, tran_end, window, maxstep=None):
    """sweep.py case + cfg for one operating point."""
    v, s = design.values, design.spec
    # the LM3478 model's clock is a SIN() source: steps > ~Ts/50 miss clock edges (50 ns works up to ~400 kHz)
    maxstep = maxstep or min(50e-9, 1 / (50 * v["fs"]))
    rf2 = min(v["rf2"], key=lambda t: abs(t[0] - vout))[1]
    vset = 1.26 * (1 + s.rf1 / rf2)
    comp = v["comp"]
    # Cout = C11 (0.1 µF) + C2 + C12 (board ceramics, up to 2 x 2.2 µF) + C10 (the rest, bulk with the ESR).
    # Below 4.6 µF the ceramics shrink so the total stays the chosen Cout.
    cer = min(4.4e-6, max(v["cout"] - 0.1e-6, 0.0))
    bulk = v["cout"] - 0.1e-6 - cer
    elements = {
        "R1": f"{v['rsn']:.6g}", "R2": f"{v['rsl']:.6g}", "R5": f"{v['rfa']:.6g}",
        "R3": f"{s.rf1:.6g}", "R4": f"{rf2:.6g}", "R8": "1e12",           # RF2 is the whole bottom resistor
        "R6": f"{comp['rc1']:.6g}", "C4": f"{comp['cc1']:.6g}", "C3": f"{comp['cc2']:.6g}",
        "XL1": "XL1 N001a N002 LDESIGN",
        "C12": f"{max(cer / 2, 1e-12):.6g}", "C2": f"{max(cer / 2, 1e-12):.6g}",
        "C10": f"{max(bulk, 1e-12):.6g}", "RC10": f"{v['esr']:.6g}",
        # no load during soft-start (Cout is pre-charged), then a 1 ms ramp to the full load
        "I1": f"I1 outl 0 PWL(0 0 {LOAD_ON:.6g} 0 {LOAD_ON + 1e-3:.6g} {iout:.6g})",
    }
    cfg = {
        "tran": f".tran {maxstep:.3g} {tran_end:.6g} 0 {maxstep:.3g}",
        "measure": {
            "windows": [{"name": "ss", "from": f"{window[0]:.6g}", "to": f"{window[1]:.6g}"},
                        {"name": "sw", "from": f"{tran_end - 20e-6:.6g}", "to": f"{tran_end:.6g}"}],
            "meas": [
                {"name": "pin", "func": "avg", "expr": "-v(n001)*i(v1)"},
                {"name": "pout", "func": "avg", "expr": "v(out)*i(vload)"},
                {"name": "vout", "func": "avg", "expr": "v(out)"},
                {"name": "vpp", "func": "pp", "expr": "v(out)"},
                {"name": "ilavg", "func": "avg", "expr": "i(vsense)"},
                {"name": "ilmax", "func": "max", "expr": "i(vsense)"},
                {"name": "vinpp", "func": "pp", "expr": "v(n001)"},
                {"name": "comp", "func": "avg", "expr": "v(comp)"},
            ],
            "derived": [{"name": "eff", "expr": "100*pout/pin"}],
        },
    }
    return cfg, {"params": {"vin": f"{vin:.6g}"}, "elements": elements}, vset


def cycle_peaks(t, il, t0, t1, fs):
    T = 1 / fs
    n = int((t1 - t0) / T)
    edges = t0 + np.arange(n + 1) * T
    idx = np.searchsorted(t, edges)
    return np.array([il[a:b].max() for a, b in zip(idx[:-1], idx[1:]) if b > a])


def make_wave(vec, fs, window, bins=1500, end_span=20e-6):
    """Compact waveform snapshot of a run (kept with the results, so plots work without the raw file):
    Vout min/max envelope over the whole run, Vout and IL over the last 20 µs, peak IL per cycle."""
    t = vec["time"]; vo = vec["v(out)"]
    il = vec["i(vsense)"] if "i(vsense)" in vec else vec["vsense#branch"]
    idx = np.searchsorted(t, np.linspace(t[0], t[-1], bins + 1))
    seg = [(a, b) for a, b in zip(idx[:-1], idx[1:]) if b > a]
    m = t > t[-1] - end_span
    step = max(1, int(m.sum() // 1000))
    r = lambda a: [float(f"{x:.7g}") for x in a]
    return dict(t=r([t[a:b].mean() for a, b in seg]), vlo=r([vo[a:b].min() for a, b in seg]),
                vhi=r([vo[a:b].max() for a, b in seg]),
                t_end=r((t[m][::step] - t[-1]) * 1e6), v_end=r(vo[m][::step]), il_end=r(il[m][::step]),
                pk=r(cycle_peaks(t, il, *window, fs)), t_stop=float(t[-1]), window=list(window), load_on=LOAD_ON)


def part_losses(vec, window, rsn):
    """Average power [W] in L1 (incl. DCR), the MOSFET, the diode and RSN over the window, from the
    terminal voltages and currents. The RSN current also carries the gate charge current (it returns through
    the source), so drain current = V(RSN)/RSN - I(R7); diode current = IL - drain current. The MOSFET
    power includes its gate path (V_GS * I_G); the loss in R7 and in the LM3478 driver is in "other"."""
    t = vec["time"]
    m = (t >= window[0]) & (t <= window[1])
    tt = t[m]
    avg = lambda y: float(np.trapezoid(y[m], tt) / (tt[-1] - tt[0]))
    il = vec["i(vsense)"] if "i(vsense)" in vec else vec["vsense#branch"]
    vsw, v8, vo, v1a = vec["v(n002)"], vec["v(n008)"], vec["v(out)"], vec["v(n001a)"]
    vdr, vg = vec["v(n003)"], vec["v(n004)"]
    ig = (vdr - vg) / R_GATE
    i_rsn = v8 / rsn
    idrain = i_rsn - ig
    return {"p_l": avg((v1a - vsw) * il), "p_q": avg((vsw - v8) * idrain + (vg - v8) * ig),
            "p_d": avg((vsw - vo) * (il - idrain)), "p_rsn": avg(v8 * i_rsn)}


def run_ngspice(cir, names, timeout, stop):
    """sweep.run_case, but stoppable: kills ngspice when `stop` (threading.Event) is set."""
    t0 = time.time()
    log_path = cir.with_suffix(".log")
    status = None
    with open(log_path, "w") as log:
        proc = subprocess.Popen(["ngspice", "-b", cir.name], cwd=cir.parent, stdout=log, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            if stop is not None and stop.is_set():
                status = "stopped"
            elif time.time() - t0 > timeout:
                status = "timeout"
            if status:
                proc.kill(); proc.wait(); break
            time.sleep(0.2)
    out = log_path.read_text(errors="replace")
    results = {n: math.nan for n in names}
    for ln in out.splitlines():
        if ln.startswith(sweep.RESULT_TAG):
            parts = ln.split()
            if len(parts) >= 3 and parts[1] in results:
                try:
                    results[parts[1]] = float(parts[2])
                except ValueError:
                    pass
    if status is None:
        if "Timestep too small" in out or "simulation(s) aborted" in out:
            status = "failed"
        elif re.search(r"^Error|could not find|unknown (model|subckt)", out, re.I | re.M) and \
                all(math.isnan(x) for x in results.values()):
            status = "netlist error"
        elif all(math.isnan(x) for x in results.values()) and names:
            status = "no-results"
        else:
            status = "ok"
    return dict(status=status, runtime_s=round(time.time() - t0, 1), results=results)


def fix_include(ln):
    """.include of a board library that is no longer in sim/: point it at where it is now."""
    toks = ln.split(None, 1)
    if len(toks) == 2 and toks[0].lower() == ".include":
        path = Path(toks[1].strip().strip('"'))
        if not path.exists():
            return f'.include "{find_lib(path.name)}"'
    return ln


def case_notes(design, case_id, pt, vset, elements, cfg, models, cir_text, tran_end, window, maxstep):
    """Text of <case>_notes.txt: operating point, simulation parameters, BOM and the netlist."""
    vin, vout, iout = pt
    v = design.values
    out = [f"{case_id}: Vin {vin:g} V, Vout {vout:g} V (set {vset:.2f} V), Iout {si(iout, 'A')}",
           f"written {datetime.datetime.now().isoformat(timespec='seconds')}", "",
           "Simulation parameters", "-" * 60,
           f"netlist base      {NETLIST.name} (elements below replaced)",
           f"analysis          {cfg['tran']}",
           f"max time step     {si(maxstep, 's')} (Ts/50 or 50 ns, whichever is smaller)",
           f"run length        {si(tran_end, 's')}",
           f"load              0 until {si(LOAD_ON, 's')}, ramp to {si(iout, 'A')} over 1 ms",
           f"measured window   {si(window[0], 's')} .. {si(window[1], 's')} (switching ripple: last 20 µs)",
           f"initial condition V(out) = {0.97 * vset:.4g} V (97 % of the setpoint)",
           f"LM3478 VIN        {design.spec.vcc:g} V (Vcc_ic, separate supply)" if design.spec.vcc
           else "LM3478 VIN        the regulator input (N001)",
           "models            " + ", ".join(f"{role} {name}" + (f" ({lib})" if lib else "")
                                          for role, (name, lib) in models.items()),
           "", "BOM (designed values)", "-" * 60]
    w = max(len(r[0]) for r in design.reqs); wp = max(len(r[1]) for r in design.reqs)
    out += [f"{ref:<{w}}  {part:<{wp}}  {val}" for ref, part, val, _ in design.reqs]
    out += ["", f"fs {si(v['fs'], 'Hz')}, L {si(v['L'], 'H')} (DCR {si(v['dcr'], 'Ω')}), Cout {si(v['cout'], 'F')} "
            f"(ESR {si(v['esr'], 'Ω')})",
            "", "Netlist elements set for this case", "-" * 60]
    out += [f"{k:<5} {val}" for k, val in elements.items()]
    out += ["", "Netlist", "-" * 60, cir_text.rstrip()]
    return "\n".join(out) + "\n"


def result_notes(out):
    """Results appended to the notes file once the case has run."""
    f = lambda k, scale=1, fmt="{:.4g}": "–" if out.get(k) is None or not np.isfinite(out.get(k)) \
        else fmt.format(out[k] * scale)
    lines = ["", "Results", "-" * 60, f"status {out['status']}, runtime {out.get('runtime', 0):.0f} s",
             f"Vout {f('vout')} V, ripple {f('vpp', 1e3, '{:.0f}')} mV (window), {f('vpp_sw', 1e3, '{:.0f}')} mV "
             f"(switching), η {f('eff', 1, '{:.2f}')} %",
             f"IL avg {f('ilavg')} A, IL max {f('ilmax')} A, peak spread {f('pk_spread', 1, '{:.1f}')} %, "
             f"skipped cycles {out.get('skipped')}",
             f"Pin {f('pin', 1e3, '{:.1f}')} mW, Pout {f('pout', 1e3, '{:.1f}')} mW, losses: L1 {f('p_l', 1e3, '{:.1f}')}, "
             f"MOSFET {f('p_q', 1e3, '{:.1f}')}, diode {f('p_d', 1e3, '{:.1f}')}, RSN {f('p_rsn', 1e3, '{:.1f}')}, "
             f"other {f('p_other', 1e3, '{:.1f}')} mW"]
    return "\n".join(lines) + "\n"


def run_points(design, points, mosfet=MOSFET_DEFAULT, diode=DIODE_DEFAULT, inductor=IDEAL_L, tran_end=16e-3,
               window=(14e-3, 16e-3), jobs=4, timeout=3600, on_result=None, on_start=None, maxstep=None,
               stop=None, run_root=None):
    """Simulate each (vin, vout, iout) point. Returns a list of result dicts (also passed to on_result).
    mosfet / diode: a built-in name or a model saved in user_models/. stop: threading.Event to abort.
    run_root: folder for the run directory (default ../results); each case gets <case>_notes.txt."""
    mos_line, mos_lib = resolve("mosfet", mosfet)
    dio_line, dio_lib = resolve("diode", diode)
    ind_line, ind_lib = (None, None) if inductor in (None, "", IDEAL_L) else resolve("inductor", inductor)
    stamp = f"{'sim' if run_root else 'designer'}_{datetime.datetime.now():%Y%m%d_%H%M%S}"
    root = Path(run_root) if run_root else SIM_DIR / "results"
    run_dir = root / stamp
    n = 1
    while True:                                   # unique even when two runs start in the same second
        try:
            run_dir.mkdir(parents=True); break
        except FileExistsError:
            n += 1; run_dir = root / f"{stamp}_{n}"
    shutil.copy(SIM_DIR / ".spiceinit", run_dir / ".spiceinit")
    v = design.values
    (run_dir / "designer_parts.lib").write_text(
        f"* designed inductor: ideal L + DCR\n.subckt LDESIGN 1 2\nRdcr 1 3 {max(v['dcr'], 1e-6):.6g}\n"
        f"L1 3 2 {v['L']:.6g}\n.ends LDESIGN\n")
    (run_dir / "design.txt").write_text(design.report())
    base = NETLIST.read_text().splitlines()
    maxstep_used = maxstep or min(50e-9, 1 / (50 * v["fs"]))
    models = {"MOSFET": (mosfet, mos_lib and Path(mos_lib).name), "diode": (diode, dio_lib and Path(dio_lib).name),
              "L1": (inductor or IDEAL_L, ind_lib and Path(ind_lib).name)}

    def job(n, pt):
        vin, vout, iout = pt
        cfg, case, vset = case_setup(design, vin, vout, iout, mosfet, diode, tran_end, window, maxstep)
        cfg["includes"] = [str(run_dir / "designer_parts.lib")] + [str(p) for p in (mos_lib, dio_lib, ind_lib) if p]
        case["id"] = f"case_{n:03d}"
        cir, names = sweep.write_case_netlist(cfg, case, run_dir, base, NETLIST.parent, SIGNALS)
        # start with Cout charged near the setpoint (the base netlist uses 50 V)
        txt = cir.read_text().splitlines()
        txt = [f".ic V(out)={0.97 * vset:.4g}" if ln.lower().startswith(".ic") else ln for ln in txt]
        # MOSFET and diode lines (the element letter changes for .model / .subckt devices)
        swap = {"xm2": mos_line, "d2": dio_line}
        if ind_line:                                    # the inductor's own model instead of ideal L + DCR
            swap["xl1"] = ind_line
        txt = [swap.get(ln.split()[0].lower(), ln) if ln.split() else ln for ln in txt]
        txt = [fix_include(ln) for ln in txt]
        if design.spec.vcc:                    # LM3478 VIN pin (8th node of XU1) from its own supply
            for i, ln in enumerate(txt):
                tk = ln.split()
                if tk and tk[0].upper() == "XU1":
                    tk[8] = "VCC_IC"
                    txt[i] = " ".join(tk)
                    txt.insert(i, f"Vcc_ic VCC_IC 0 {design.spec.vcc:.6g}")
                    break
        cir.write_text("\n".join(txt) + "\n")
        notes = run_dir / f"{case['id']}_notes.txt"
        notes.write_text(case_notes(design, case["id"], pt, vset, case["elements"], cfg, models, cir.read_text(),
                                    tran_end, window, maxstep_used))
        if stop is not None and stop.is_set():
            r = dict(status="stopped", runtime_s=0, results={})
        else:
            if on_start:
                on_start(n, pt)
            r = run_ngspice(cir, names, timeout, stop)
        res = {k.removesuffix("_ss"): val for k, val in r["results"].items() if k.endswith("_ss")}
        res["vpp_sw"] = r["results"].get("vpp_sw", math.nan)              # switching ripple, last 20 µs
        out = dict(n=n, vin=vin, vcc=design.spec.vcc or vin, vout_set=vset, iout=iout, status=r["status"],
                   runtime=r["runtime_s"],
                   run_dir=str(run_dir), raw=str(run_dir / f"{case['id']}.raw"), **res)
        c = v["comp"]
        lname = "" if ind_line is None else f" ({inductor})"
        out["parts"] = (f"{mosfet}, {diode}, L {si(v['L'], 'H')}{lname}, Cout {si(v['cout'], 'F')}, RSN {si(v['rsn'], 'Ω')}, "
                        f"RSL {si(v['rsl'], 'Ω')}, RC1 {si(c['rc1'], 'Ω')}, CC1 {si(c['cc1'], 'F')}, CC2 {si(c['cc2'], 'F')}")
        # Vout still moving over the measurement window: η and ripple are not steady-state values
        if out["status"] == "ok" and out.get("vpp", 0) > max(5 * out.get("vpp_sw", 0), 0.01 * vout):
            out["status"] = "not settled"
        try:
            vec = sweep.read_raw(out["raw"])
            t = vec["time"]; il = vec["i(vsense)"] if "i(vsense)" in vec else vec["vsense#branch"]
            pk = cycle_peaks(t, il, *window, v["fs"])
            out["wave"] = make_wave(vec, v["fs"], window)
            out.update(part_losses(vec, window, v["rsn"]))
            # the rest: LM3478 supply + gate drive, feedback divider, capacitor ESR, PCB resistances
            out["p_other"] = out["pin"] - out["pout"] - sum(out[k] for k in ("p_l", "p_q", "p_d", "p_rsn"))
            med = np.median(pk)
            out.update(pk_min=pk.min(), pk_max=pk.max(), pk_spread=100 * (pk.max() - pk.min()) / med,
                       skipped=int(np.sum(pk < 0.5 * med)))
        except Exception as e:                       # raw missing (failed / stopped run)
            out.update(pk_min=math.nan, pk_max=math.nan, pk_spread=math.nan, skipped=-1, error=str(e))
        with open(notes, "a") as fh:
            fh.write(result_notes(out))
        if on_result:
            on_result(out)
        return out

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(lambda a: job(*a), enumerate(points)))
    return results


if __name__ == "__main__":
    from booster_design import Spec, design
    d = design(Spec(fs=302e3), {"L": 44.16e-6, "dcr": 0.0612, "cout": 26.5e-6, "esr": 0.0108})
    pts = [(12, 80.3, 0.1)]
    for r in run_points(d, pts, on_result=print):
        pass
