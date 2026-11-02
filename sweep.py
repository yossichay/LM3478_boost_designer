#!/usr/bin/env python3
"""Run ngspice parameter/part sweeps, summarize measurements and plot waveforms.

Usage:
    python3 sweep.py sweep_example.toml            # run the sweep
    python3 sweep.py sweep_example.toml -j 8       # limit parallel ngspice jobs
    python3 sweep.py sweep_example.toml --dry-run  # only list cases / write netlists
    python3 sweep.py --replot results/<run_dir>    # redo plots from an existing run
    python3 sweep.py --list                        # list parts (designators) and nodes

See README.md ("Parameter and part sweeps") for the config file format.
"""

import argparse
import csv
import datetime
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

SPICE_SUFFIX = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3,
                "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
MEAS_FUNCS = {"avg", "min", "max", "pp", "rms", "integ"}
RESULT_TAG = "@@RESULT"


def spice_num(text):
    """'15m' -> 0.015, '2.2u' -> 2.2e-6, '1meg' -> 1e6. Trailing units are ignored."""
    m = re.fullmatch(r"\s*([-+]?[\d.]+(?:e[-+]?\d+)?)\s*(meg|[tgkmuµnpf])?[a-z]*\s*",
                     str(text), re.IGNORECASE)
    if not m:
        raise ValueError(f"not a SPICE number: {text!r}")
    return float(m.group(1)) * SPICE_SUFFIX.get((m.group(2) or "").lower(), 1.0)


# ---------------------------------------------------------------- cases

def build_cases(cfg):
    """Explicit [[case]] entries x the [grid] cartesian product."""
    base_cases = cfg.get("case") or [{}]
    grid = cfg.get("grid", {})
    axes = [("params", k, v) for k, v in grid.get("params", {}).items()]
    axes += [("elements", k, v) for k, v in grid.get("elements", {}).items()]

    cases = []
    for base in base_cases:
        combos = itertools.product(*[vals for _, _, vals in axes]) if axes else [()]
        for combo in combos:
            params = dict(base.get("params", {}))
            elements = dict(base.get("elements", {}))
            label_parts = [base["name"]] if "name" in base else []
            for (kind, key, _), val in zip(axes, combo):
                (params if kind == "params" else elements)[key] = val
                if len(grid.get(kind, {})[key]) > 1:  # label only what varies
                    label_parts.append(f"{key}={val}")
            cases.append({"label": ", ".join(label_parts) or "base",
                          "params": params, "elements": elements,
                          "load": base.get("load")})
    for i, c in enumerate(cases):
        c["id"] = f"case_{i:03d}"
    return cases


# ---------------------------------------------------------------- netlist

def strip_control(lines):
    out, inside = [], False
    for ln in lines:
        low = ln.strip().lower()
        if low.startswith(".control"):
            inside = True
        elif low.startswith(".endc"):
            inside = False
        elif not inside:
            out.append(ln)
    return out


def load_pwl(load):
    """[load] table -> (element name, 'PWL(...)'). Each step holds until the next one."""
    steps = load.get("steps", [])
    if not steps:
        raise ValueError("[load] needs steps = [[time, current], ...]")
    rise = spice_num(load.get("rise", "1u"))
    pts, prev_t, prev_i = [], None, None
    for t, i in steps:
        t, i = spice_num(t), spice_num(i)
        if prev_t is None:
            pts.append((t, i))
        else:
            if t <= prev_t + rise:
                raise ValueError(f"[load] step times must increase by more than rise ({t:g} s)")
            pts += [(t, prev_i), (t + rise, i)]
        prev_t, prev_i = t, i
    pwl = "PWL(" + " ".join(f"{t:.9g} {i:.9g}" for t, i in pts) + ")"
    return load.get("element", "I1").lower(), pwl


def apply_overrides(lines, case, netlist_dir, tran, load=None):
    params = {k.lower(): v for k, v in case["params"].items()}
    elements = {k.lower(): str(v) for k, v in case["elements"].items()}
    load_el, pwl = load_pwl(load) if load else (None, None)
    if load and "iload" in params:
        raise ValueError("iload has no effect when [load] is set - sweep the load steps instead")
    seen_params, seen_elems = set(), set()
    out = []
    for ln in lines:
        toks = ln.split()
        head = toks[0].lower() if toks else ""

        if head in (".include", ".inc", ".lib") and len(toks) >= 2:
            # absolute paths so the case netlist works from the results folder
            p = Path(toks[1].strip("\"'"))
            if not p.is_absolute():
                p = (netlist_dir / p).resolve()
            ln = " ".join([toks[0], f'"{p}"'] + toks[2:])

        elif head == ".param":
            # rewrite name=value pairs that are being overridden
            def sub(m):
                name = m.group(1).lower()
                if name in params:
                    seen_params.add(name)
                    return f"{m.group(1)}={params[name]}"
                return m.group(0)
            ln = re.sub(r"(\w+)\s*=\s*(\{[^}]*\}|\S+)", sub, ln)

        elif head.startswith(".tran") and tran:
            ln = tran

        elif head == load_el:
            ln = " ".join(toks[:3] + [pwl])   # keep name + 2 nodes, new waveform
            seen_elems.add(head)

        elif head in elements:
            new = elements[head]
            seen_elems.add(head)
            if new.split()[0].lower() == head:
                ln = new                        # full replacement line
            else:
                ln = " ".join(toks[:-1] + [new])  # replace last token (value / model)
        out.append(ln)

    missing_e = (set(elements) | ({load_el} if load_el else set())) - seen_elems
    if missing_e:
        raise ValueError(f"element(s) not found in netlist: {sorted(missing_e)}")
    missing_p = set(params) - seen_params
    if missing_p:
        names = {ln.split()[0].lower() for ln in lines if ln.split()}
        hint = [p for p in missing_p if p in names]
        msg = f"parameter(s) not defined by a .param line in the netlist: {sorted(missing_p)}"
        if hint:
            msg += f"\n  {sorted(hint)} are element names - put them under [grid.elements]"
        raise ValueError(msg)
    return out


def measure_windows(cfg):
    """[(name, from, to)]; a single unnamed window when only from/to are given."""
    m = cfg.get("measure", {})
    if m.get("windows"):
        return [(w["name"], w["from"], w["to"]) for w in m["windows"]]
    return [("", m.get("from"), m.get("to"))]


def vec_name(base, window):
    """ngspice vector name of a measurement in a window: pin + '10mA' -> pin_10ma."""
    return f"{base}_{re.sub(r'[^a-z0-9]+', '_', window.lower())}" if window else base


def measure_names(cfg):
    """(base names in order, {(window, base): ngspice vector name})."""
    m = cfg.get("measure", {})
    items = m.get("meas", []) + m.get("derived", [])
    bases = [it["name"].lower() for it in items]
    full = {}
    for w, _, _ in measure_windows(cfg):
        for it in items:
            b = it["name"].lower()
            full[(w, b)] = b if "raw" in it else vec_name(b, w)
    return bases, full


def control_block(cfg, raw_path, signals):
    m = cfg.get("measure", {})
    lines = [".control", "run"]
    windowed = set()
    for item in m.get("meas", []):
        name = item["name"].lower()
        if "raw" in item:      # full meas spec, evaluated once (it sets its own range)
            lines.append(f"meas tran {name} {item['raw']}")
        else:
            if item["func"].lower() not in MEAS_FUNCS:
                raise ValueError(f"meas {name}: func must be one of {sorted(MEAS_FUNCS)}")
            # ngspice meas needs a plain vector, so evaluate the expression first
            lines.append(f"let mv_{name} = {item['expr']}")
            windowed.add(name)
    for w, t_from, t_to in measure_windows(cfg):
        rng = f" from={t_from} to={t_to}" if t_from and t_to else ""
        for item in m.get("meas", []):
            name = item["name"].lower()
            if name in windowed:
                lines.append(f"meas tran {vec_name(name, w)} {item['func'].lower()} mv_{name}{rng}")
        for item in m.get("derived", []):
            # point measurement names in the expression at this window's results
            expr = re.sub(r"\b[A-Za-z_]\w*\b",
                          lambda mm: vec_name(mm.group(0).lower(), w)
                          if mm.group(0).lower() in windowed else mm.group(0), item["expr"])
            name = item["name"].lower()
            lines.append(f"let {vec_name(name, w)} = {expr}")
            windowed.add(name)
    names = list(dict.fromkeys(measure_names(cfg)[1].values()))
    for name in names:
        lines.append(f"echo {RESULT_TAG} {name} $&{name}")
    if signals:
        # plain file name: ngspice keeps quotes literally, and cwd is the run folder
        lines.append(f"write {raw_path.name} " + " ".join(signals))
    lines.append(".endc")
    return lines, names


def write_case_netlist(cfg, case, run_dir, base_lines, netlist_dir, signals):
    lines = apply_overrides(strip_control(base_lines), case, netlist_dir, cfg.get("tran"),
                            case.get("load") or cfg.get("load"))
    for inc in cfg.get("includes", []):
        p = Path(inc)
        p = p if p.is_absolute() else (netlist_dir / p).resolve()
        lines.insert(1, f'.include "{p}"')
    raw_path = run_dir / f"{case['id']}.raw"
    ctrl, names = control_block(cfg, raw_path, signals)
    end = next((i for i in range(len(lines) - 1, -1, -1)
                if lines[i].strip().lower() == ".end"), len(lines))
    lines = lines[:end] + ctrl + [".end"]
    cir = run_dir / f"{case['id']}.cir"
    cir.write_text("\n".join(lines) + "\n")
    return cir, names


# ---------------------------------------------------------------- running

def run_case(case, cir, names, timeout):
    t0 = time.time()
    log_path = cir.with_suffix(".log")
    # stream output to the log while running, so progress can be read from it.
    # cwd = run folder, which holds a copy of the netlist's .spiceinit
    with open(log_path, "w") as log:
        proc = subprocess.Popen(["ngspice", "-b", cir.name], cwd=cir.parent,
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log.write(f"\n*** TIMEOUT after {timeout}s\n")
    out = log_path.read_text(errors="replace")

    results = {n: math.nan for n in names}
    for ln in out.splitlines():
        if ln.startswith(RESULT_TAG):
            parts = ln.split()
            if len(parts) >= 3 and parts[1] in results:
                try:
                    results[parts[1]] = float(parts[2])
                except ValueError:
                    pass
    if "TIMEOUT after" in out:
        status = "timeout"
    elif "Timestep too small" in out or "simulation(s) aborted" in out:
        m = re.search(r"Timestep too small; time = ([0-9.eE+-]+)", out)
        status = f"failed@{m.group(1)}s" if m else "failed"
    elif all(math.isnan(v) for v in results.values()) and names:
        status = "no-results"
    else:
        status = "ok"
    return {**case, "status": status, "runtime_s": round(time.time() - t0, 1),
            "results": results}


def sim_time(log_path):
    """Latest simulated time ngspice reported in a (still growing) log, or None."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode(errors="replace")
    except OSError:
        return None
    hits = re.findall(r"Reference value\s*:\s*([0-9.eE+-]+)", tail)
    return float(hits[-1]) if hits else None


def fmt_dur(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def report_progress(running, tstop, t0):
    """One status line: simulated time / % per running case, plus an ETA."""
    parts, etas = [], []
    for cid, (log_path, started) in sorted(running.items()):
        t = sim_time(log_path)
        if t is None or not tstop:
            parts.append(f"{cid[5:]}: starting")
            continue
        frac = min(t / tstop, 1.0)
        parts.append(f"{cid[5:]}: {t * 1e3:.2f}ms {frac:4.0%}")
        if frac > 0.02:
            etas.append((time.time() - started) * (1 - frac) / frac)
    eta = f"  ETA ~{fmt_dur(max(etas))}" if etas else ""
    print(f"  [{fmt_dur(time.time() - t0)}] " + " | ".join(parts) + eta, flush=True)


# ---------------------------------------------------------------- raw files

def read_raw(path):
    """Minimal reader for ngspice binary raw files (single real plot)."""
    data = Path(path).read_bytes()
    if b"Binary:\n" not in data:
        raise ValueError(f"{path}: only binary raw files are supported")
    hdr_end = data.index(b"Binary:\n")
    header = data[:hdr_end].decode(errors="replace").splitlines()
    nvars = npts = 0
    names = []
    in_vars = False
    for ln in header:
        if ln.startswith("No. Variables:"):
            nvars = int(ln.split(":")[1])
        elif ln.startswith("No. Points:"):
            npts = int(ln.split(":")[1])
        elif ln.startswith("Flags:") and "complex" in ln:
            raise ValueError("complex raw files are not supported")
        elif ln.startswith("Variables:"):
            in_vars = True
        elif in_vars and ln.strip():
            names.append(ln.split()[1].lower())
    body = data[hdr_end + len(b"Binary:\n"):]
    arr = np.frombuffer(body, dtype="<f8", count=nvars * npts).reshape(npts, nvars)
    return {n: arr[:, i] for i, n in enumerate(names)}


def find_signal(vecs, sig):
    s = sig.lower().replace(" ", "")
    cands = [s]
    m = re.fullmatch(r"v\((\w+)\)", s)
    if m:
        cands.append(m.group(1))
    m = re.fullmatch(r"i\((\w+)\)", s)
    if m:
        cands.append(f"{m.group(1)}#branch")
    for c in cands:
        if c in vecs:
            return vecs[c]
    return None


# ---------------------------------------------------------------- outputs

def write_summary(run_dir, results, cfg):
    """One row per case and measurement window."""
    bases, full = measure_names(cfg)
    windows = measure_windows(cfg)
    var_keys = []
    for r in results:
        for k in list(r["params"]) + list(r["elements"]):
            if k not in var_keys:
                var_keys.append(k)
    rows = []
    for r in results:
        for w, _, _ in windows:
            row = {"case": r["id"], "window": w, "label": r["label"], "status": r["status"],
                   "runtime_s": r["runtime_s"]}
            for k in var_keys:
                row[k] = r["params"].get(k, r["elements"].get(k, ""))
            for b in bases:
                row[b] = r["results"].get(full[(w, b)], math.nan)
            rows.append(row)
    win_col = ["window"] if any(w for w, _, _ in windows) else []
    cols = ["case"] + win_col + var_keys + bases + ["status", "runtime_s"]
    with open(run_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    def fmt(v):
        if isinstance(v, float):
            return "nan" if math.isnan(v) else f"{v:.4g}"
        return str(v)
    table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    table += ["| " + " | ".join(fmt(row.get(c, "")) for c in cols) + " |" for row in rows]
    (run_dir / "summary.md").write_text("\n".join(table) + "\n")
    return "\n".join(table)


def make_plots(run_dir, cfg, results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pcfg = cfg.get("plot", {})
    signals = pcfg.get("signals", [])
    windows = pcfg.get("windows", [None]) or [None]
    made = []

    for sig in signals:
        fig, axes = plt.subplots(1, len(windows), figsize=(6 * len(windows), 4.2),
                                 squeeze=False)
        for ax, win in zip(axes[0], windows):
            for r in results:
                raw = run_dir / f"{r['id']}.raw"
                if not raw.exists():
                    continue
                vecs = read_raw(raw)
                t, y = vecs.get("time"), find_signal(vecs, sig)
                if t is None or y is None:
                    continue
                if win:
                    lo, hi = spice_num(win[0]), spice_num(win[1])
                    sel = (t >= lo) & (t <= hi)
                    t, y = t[sel], y[sel]
                ax.plot(t * 1e3, y, lw=0.8, label=r["label"])
            ax.set_xlabel("time [ms]")
            ax.set_ylabel(sig)
            ax.set_title(f"{sig}" + (f"  ({win[0]}–{win[1]})" if win else ""))
            ax.grid(alpha=0.3)
        if axes[0][0].get_legend_handles_labels()[0]:
            axes[0][0].legend(fontsize=7, loc="best")
        fig.tight_layout()
        fname = run_dir / ("wave_" + re.sub(r"[^\w]+", "_", sig).strip("_") + ".png")
        fig.savefig(fname, dpi=130)
        plt.close(fig)
        made.append(fname.name)

    # one bar chart per measurement: cases side by side, one bar colour per window
    bases, full = measure_names(cfg)
    windows = measure_windows(cfg)
    if bases and results:
        n = len(bases)
        ncol = min(3, n)
        nrow = math.ceil(n / ncol)
        fig, axes = plt.subplots(nrow, ncol, figsize=(5 * ncol, 3.2 * nrow + 1),
                                 squeeze=False)
        labels = [r["label"] for r in results]
        width = 0.8 / len(windows)
        for k, b in enumerate(bases):
            ax = axes[k // ncol][k % ncol]
            for j, (w, _, _) in enumerate(windows):
                vals = [r["results"].get(full[(w, b)], math.nan) for r in results]
                xs = [x - 0.4 + width * (j + 0.5) for x in range(len(vals))]
                ax.bar(xs, vals, width, label=w or None)
            ax.set_title(b)
            ax.set_xticks(range(len(results)))
            ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
            ax.grid(axis="y", alpha=0.3)
        if len(windows) > 1:
            axes[0][0].legend(fontsize=7, title="window", title_fontsize=7)
        for k in range(n, nrow * ncol):
            axes[k // ncol][k % ncol].axis("off")
        fig.tight_layout()
        fig.savefig(run_dir / "summary.png", dpi=130)
        plt.close(fig)
        made.append("summary.png")
    return made


# ---------------------------------------------------------------- part list

def subckt_ports(netlist):
    """{subckt name: [port names]} from the files the netlist includes."""
    ports = {}
    for ln in netlist.read_text(errors="replace").splitlines():
        toks = ln.split()
        if len(toks) >= 2 and toks[0].lower() in (".include", ".inc", ".lib"):
            p = Path(toks[1].strip("\"'"))
            p = p if p.is_absolute() else netlist.parent / p
            if not p.exists():
                continue
            for sl in p.read_text(errors="replace").splitlines():
                st = sl.split()
                if len(st) >= 2 and st[0].lower() == ".subckt":
                    pins = []
                    for t in st[2:]:
                        if t.lower().startswith("params:") or "=" in t:
                            break
                        pins.append(t)
                    ports[st[1].lower()] = pins
    return ports


def split_element(toks):
    """-> (nodes, value) for one element line."""
    kind = toks[0][0].lower()
    if kind == "x":
        end = len(toks)
        for i, t in enumerate(toks):
            if t.lower().startswith("params:") or ("=" in t and i > 1):
                end = i
                break
        return toks[1:end - 1], " ".join(toks[end - 1:])
    n = {"m": 4, "q": 3, "e": 4, "g": 4, "s": 4}.get(kind, 2)
    return toks[1:1 + n], " ".join(toks[1 + n:])


def list_parts(netlist):
    ports = subckt_ports(netlist)
    section, inside = "", False
    groups, nodes = {}, {}
    for ln in netlist.read_text(errors="replace").splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith(".control"):
            inside = True
        elif low.startswith(".endc"):
            inside = False
        if inside or not s:
            continue
        m = re.match(r"\*\s*-{2,}\s*(.*)", s)
        if m:
            section = m.group(1)
            continue
        if s[0] in "*.+":
            continue
        toks = s.split()
        el_nodes, value = split_element(toks)
        groups.setdefault(section, []).append((toks[0], el_nodes, value))
        pins = ports.get(value.split()[0].lower(), []) if toks[0][0].lower() == "x" else []
        for i, n in enumerate(el_nodes):
            pin = f"{toks[0]}.{pins[i]}" if i < len(pins) else toks[0]
            nodes.setdefault(n.lower(), []).append(pin)

    for section, items in groups.items():
        print(f"\n== {section or '(no section)'}")
        for name, el_nodes, value in items:
            print(f"  {name:<8} {' '.join(el_nodes):<34} {value}")
    print("\n== Nodes (what connects where)")
    for n in sorted(nodes, key=lambda k: (k != "0", k)):
        print(f"  {n:<8} {', '.join(nodes[n])}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", help="sweep config (.toml)")
    ap.add_argument("-j", "--jobs", type=int, help="parallel ngspice processes")
    ap.add_argument("-o", "--out", help="results folder (default results/<name>_<time>)")
    ap.add_argument("--dry-run", action="store_true", help="write netlists, don't simulate")
    ap.add_argument("--progress", type=int, default=30, metavar="SEC",
                    help="seconds between progress lines (default 30)")
    ap.add_argument("--replot", metavar="RUN_DIR", help="re-plot an existing run")
    ap.add_argument("--list", nargs="?", const="Non-iso-Booster-TI-ngspice.cir",
                    metavar="NETLIST", help="list parts and node connections of a netlist")
    args = ap.parse_args()

    if args.list:
        list_parts(Path(args.list).resolve())
        return

    if args.replot:
        run_dir = Path(args.replot)
        saved = json.loads((run_dir / "run.json").read_text())
        made = make_plots(run_dir, saved["config"], saved["results"])
        print("plots:", ", ".join(made))
        return
    if not args.config:
        ap.error("config file required")

    cfg_path = Path(args.config).resolve()
    try:
        cfg = tomllib.loads(cfg_path.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"error in {cfg_path.name}: {e}\n"
                 "hint: SPICE values with suffixes must be quoted, e.g. \"20k\" not 20k")
    netlist = (cfg_path.parent / cfg.get("netlist", "Non-iso-Booster-TI-ngspice.cir")).resolve()
    netlist_dir = netlist.parent
    base_lines = netlist.read_text(errors="replace").splitlines()

    name = cfg.get("name", cfg_path.stem)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.out) if args.out else netlist_dir / "results" / f"{name}_{stamp}"
    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    spiceinit = netlist_dir / ".spiceinit"
    if spiceinit.exists():
        shutil.copy(spiceinit, run_dir / ".spiceinit")
    else:
        print(f"warning: no .spiceinit next to {netlist.name}; PSpice models may not parse",
              file=sys.stderr)

    cases = build_cases(cfg)
    signals = list(dict.fromkeys(cfg.get("plot", {}).get("signals", [])
                                 + cfg.get("save", [])))
    prepared = []
    for c in cases:
        try:
            cir, names = write_case_netlist(cfg, c, run_dir, base_lines, netlist_dir, signals)
        except ValueError as e:
            sys.exit(f"error in {cfg_path.name} ({c['id']}: {c['label']}): {e}")
        prepared.append((c, cir))
    jobs = args.jobs or cfg.get("jobs") or min(len(cases), max(1, (os.cpu_count() or 2) - 2))

    print(f"{len(cases)} case(s), {jobs} parallel job(s) -> {run_dir}")
    for c, _ in prepared:
        print(f"  {c['id']}: {c['label']}")
    if args.dry_run:
        return

    timeout = cfg.get("timeout_s")
    tran = cfg.get("tran") or next((ln for ln in base_lines
                                    if ln.strip().lower().startswith(".tran")), "")
    try:
        tstop = spice_num(tran.split()[2])
    except (IndexError, ValueError):
        tstop = None
    print(f"progress every {args.progress}s (case number: simulated time, % of {tran.split()[2] if tstop else '?'})")
    t0 = time.time()
    results = []
    running = {}  # case id -> (log path, start time)

    def job(c, cir):
        running[c["id"]] = (cir.with_suffix(".log"), time.time())
        try:
            return run_case(c, cir, names, timeout)
        finally:
            running.pop(c["id"], None)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        pending = {ex.submit(job, c, cir) for c, cir in prepared}
        while pending:
            done, pending = wait(pending, timeout=args.progress, return_when=FIRST_COMPLETED)
            for fut in done:
                r = fut.result()
                results.append(r)
                print(f"[{len(results)}/{len(cases)}] {r['id']} {r['status']:<8} "
                      f"{fmt_dur(r['runtime_s']):>7}  {r['label']}", flush=True)
            if not done and running:
                report_progress(dict(running), tstop, t0)
    results.sort(key=lambda r: r["id"])
    print(f"done in {fmt_dur(time.time() - t0)}\n")

    print(write_summary(run_dir, results, cfg))
    (run_dir / "run.json").write_text(json.dumps(
        {"config": cfg, "names": names, "results": results}, indent=1, default=str))
    made = make_plots(run_dir, cfg, results)
    print(f"\nsummary: {run_dir / 'summary.csv'}")
    print(f"plots:   {', '.join(made)}")
    if any(r["status"] != "ok" for r in results):
        print("some cases did not finish cleanly - see the case_XXX.log files", file=sys.stderr)


if __name__ == "__main__":
    main()
