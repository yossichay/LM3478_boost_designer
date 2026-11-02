#!/usr/bin/env python3
"""LM3478 boost designer (GUI).

1. Enter the specification (Vin range, Vout settings, Iout range, fs, ...) and press "Design from spec":
   every part value is calculated (LM3478 datasheet sec. 7.2) and the compensation is tuned (AN-1286).
   The Parts tab lists values and, for L1 / MOSFET / diode / capacitors, the ratings to look for.
2. Put in the inductor and output capacitor you actually picked (value + DCR / ESR) and press
   "Recalculate": RSN, RSL and the compensation are redesigned around them and the loop plots update.
   Untick "Auto-tune" to type RC1 / CC1 / CC2 yourself.
3. "Run simulation" runs the board netlist with the designed values in ngspice (one case per line of
   Vin, Vout, Iout) and reports efficiency, output ripple and peak-current stability.

usage: python3 booster_designer.py [--snapshot DIR]   (--snapshot: save the plots + report, no window)
"""
import argparse
import datetime
import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import warnings
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

import numpy as np
import matplotlib
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

# clearing a figure with shared log axes makes matplotlib warn about a zero xlim; harmless
warnings.filterwarnings("ignore", message="Attempt to set non-positive xlim")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from booster_design import (Spec, design, si, loop_tf, F, CC1_MAX, LOSS_PARTS, LOSS_COLS, text_table,  # noqa: E402
                            point_losses, c_er, I_SUPPLY, VDRV_MAX)
from loop_explorer import parse_si                                         # noqa: E402
import booster_sim                                                         # noqa: E402
import schematic                                                           # noqa: E402
import characterize                                                        # noqa: E402

SPEC_FIELDS = [  # (attribute, label, unit)
    ("vin", "Vin", "V"), ("vcc", "LM3478 VIN", "V, empty = Vin"),
    ("vouts", "Vout settings", "V, comma list"), ("iout_min", "Iout min", "A"), ("iout_max", "Iout max", "A"),
    ("fs", "Switching frequency", "Hz"), ("eff", "Efficiency estimate", ""),
    ("ripple_ratio", "Inductor ripple dIL/IL", ""), ("vout_ripple", "Vout ripple target", "V pp"),
    ("cl_margin", "Current-limit margin", "× Ipk"), ("q_target", "Max Q (slope comp)", ""),
    ("pm_target", "PM target, CCM", "°"), ("pm_target_dcm", "PM target, light load", "°"),
    ("vd", "Diode Vf", "V"), ("rf1", "RF1 (top FB resistor)", "Ω"), ("loss_budget", "Loss budget per part", "× Pout"),
]
PART_FIELDS = [("L", "L", "H"), ("dcr", "L DCR", "Ω"), ("cout", "Cout (at bias)", "F"), ("esr", "Cout ESR", "Ω"),
               ("rsn", "RSN (R1)", "Ω"), ("rsl", "RSL (R2)", "Ω")]
AUTO_PARTS = ("rsn", "rsl")                 # designed until the user types a value (their "auto" box clears)
COMP_FIELDS = [("rc1", "RC1 (R6)", "Ω"), ("cc1", "CC1 (C4)", "F"), ("cc2", "CC2 (C3)", "F")]
CONFIG_DIR = Path(__file__).resolve().parent / "configs"
MODEL_HELP = {
    "mosfet": "Load a library file or paste SPICE text (it may hold many models), then pick the model. Usable as a "
              "MOSFET: a 3-pin .subckt (D G S), a 4-pin .subckt (D G S B, body tied to source), or a .model of type "
              "VDMOS / NMOS. The library is kept in designer/user_models/ and all its models are listed next time.",
    "inductor": "Load a library file or paste SPICE text (it may hold many models), then pick the model. Usable as "
                "an inductor: a 2-pin .subckt. L (at fs) and DCR are extracted from it, and the simulation uses the "
                "model itself. The library is kept in designer/user_models/.",
    "diode": "Load a library file or paste SPICE text (it may hold many models), then pick the model. Usable as a "
             "diode: a .model of type D, or a 2-pin .subckt (A K). The library is kept in designer/user_models/ "
             "and all its models are listed next time.",
}
SIG_DEFS = [  # simulation plot choices: (label, raw vector, unit)
    ("Vout", "v(out)", "V"), ("Inductor current", "i(vsense)", "A"), ("Peak IL per cycle", "pk", "A"),
    ("Switch node V(N002)", "v(n002)", "V"), ("COMP", "v(comp)", "V"), ("Gate V(N004)", "v(n004)", "V"),
    ("ISEN / RSN V(N008)", "v(n008)", "V"), ("Vin V(N001)", "v(n001)", "V"),
]
SIG_DEFAULT = ("v(out)", "i(vsense)", "pk")
SIM_COLS = [("n", "#", 30), ("vin", "Vin", 45), ("vout_set", "Vout set", 65), ("iout", "Iout", 65),
            ("vout", "Vout", 60), ("vpp_sw", "ripple sw mV", 85), ("vpp", "ripple mV", 70), ("eff", "η %", 50),
            ("ilavg", "IL avg", 55), ("ilmax", "IL pk", 55), ("pk_spread", "pk spread %", 80),
            ("skipped", "skips", 45), ("status", "status", 70)]


def fmt_val(x):
    return si(x).replace(" ", "").replace("µ", "u")


class App:
    def __init__(self, root):
        self.root = root
        self.d = None
        self.pts_key = None                     # (vin, vouts, iout_min, iout_max) the points were made for
        self.config_path = None                 # configs/<name>/<name>.json; the folder also holds report + sims
        self.char_results, self.overrides = {}, set()
        self.part_typed = set()                 # selected-part / compensation fields typed by hand (shown red)
        self.char_busy, self.char_pending = False, set()
        menu = tk.Menu(root)
        fm = tk.Menu(menu, tearoff=0)
        fm.add_command(label="Load configuration…", accelerator="Ctrl+O", command=self.load_config)
        fm.add_command(label="Save configuration", accelerator="Ctrl+S", command=self.save_config)
        fm.add_command(label="Save configuration as…", command=lambda: self.save_config(ask=True))
        fm.add_separator()
        fm.add_command(label="Quit", command=root.destroy)
        menu.add_cascade(label="File", menu=fm)
        root.config(menu=menu)
        root.bind_all("<Control-s>", self.save_config)
        root.bind_all("<Control-o>", self.load_config)
        self.sim_q = queue.Queue()
        self.sim_rows = {}
        root.title("LM3478 Booster Designer")
        root.geometry("1500x900")
        self.mono = tkfont.Font(family=tkfont.nametofont("TkFixedFont").actual("family"), size=9)
        self.raw_cache = {}
        pw = ttk.PanedWindow(root, orient="horizontal")
        pw.pack(fill="both", expand=True)
        outer = ttk.Frame(pw)                                    # scrollable input panel
        cv = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        left = ttk.Frame(cv, padding=6)
        cv.create_window((0, 0), window=left, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        left.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all"), width=left.winfo_reqwidth()))
        sb.pack(side="right", fill="y"); cv.pack(side="left", fill="both", expand=True)

        def wheel(e):
            cv.yview_scroll(-1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1, "units")
        outer.bind("<Enter>", lambda e: [root.bind_all(s, wheel) for s in ("<Button-4>", "<Button-5>", "<MouseWheel>")])
        outer.bind("<Leave>", lambda e: [root.unbind_all(s) for s in ("<Button-4>", "<Button-5>", "<MouseWheel>")])
        right = ttk.Notebook(pw)
        pw.add(outer, weight=0)
        pw.add(right, weight=1)
        self.build_inputs(left)
        self.build_tabs(right)
        self.recalc(from_spec=True)
        root.after(300, self.poll_sim)
        root.after(500, self.characterize_models)            # loss parameters come from the models

    # ------------------------------------------------------------ inputs
    def entry_rows(self, parent, fields, store, values):
        for r, (key, label, unit) in enumerate(fields):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w")
            var = tk.StringVar(value=values.get(key, ""))
            e = ttk.Entry(parent, textvariable=var, width=16)
            e.grid(row=r, column=1, sticky="ew", padx=3, pady=1)
            e.bind("<Return>", lambda _e: self.recalc())
            ttk.Label(parent, text=unit, foreground="grey").grid(row=r, column=2, sticky="w")
            store[key] = (var, e)

    def build_inputs(self, f):
        s = Spec()
        sv = {k: (", ".join(f"{x:g}" for x in getattr(s, k)) if k == "vouts" else fmt_val(getattr(s, k))
                  if getattr(s, k) >= 1000 or getattr(s, k) < 0.1 else f"{getattr(s, k):g}") for k, _, _ in SPEC_FIELDS}
        sv["vcc"] = ""
        lf = ttk.LabelFrame(f, text="Specification", padding=4)
        lf.pack(fill="x")
        self.spec_vars = {}
        self.entry_rows(lf, SPEC_FIELDS, self.spec_vars, sv)
        ttk.Button(lf, text="Design from spec", command=lambda: self.recalc(from_spec=True)).grid(
            row=len(SPEC_FIELDS), column=0, columnspan=3, sticky="ew", pady=4)

        pf = ttk.LabelFrame(f, text="Selected parts (Enter or Recalculate; red = typed by hand)", padding=4)
        pf.pack(fill="x", pady=6)
        self.part_vars = {}
        self.entry_rows(pf, PART_FIELDS, self.part_vars, {})
        n = len(PART_FIELDS)
        # RSN / RSL: designed (auto) until the user types a value
        self.auto = {}
        for k in AUTO_PARTS:
            var = self.auto[k] = tk.BooleanVar(value=True)
            ttk.Checkbutton(pf, text="auto", variable=var, command=self.mark_parts).grid(row=[f[0] for f in PART_FIELDS].index(k),
                                                                 column=3, sticky="w")
            self.part_vars[k][1].bind("<Key>", lambda e, var=var: var.set(False) if e.char or e.keysym in
                                      ("BackSpace", "Delete") else None)
        self.auto_rsn = self.auto["rsn"]
        ttk.Label(pf, text="L model").grid(row=n, column=0, sticky="w")
        ttk.Button(pf, text="Model…", width=7, command=lambda: self.edit_model("inductor")).grid(
            row=n, column=1, sticky="e", padx=3)
        n += 1
        self.lmodel = ttk.Combobox(pf, width=34, height=30)
        self.lmodel.set(booster_sim.IDEAL_L); self.lmodel.grid(row=n, column=0, columnspan=4, sticky="ew", padx=3)
        self.lmodel.bind("<<ComboboxSelected>>", lambda e: self.model_selected("inductor"))
        for k in ("L", "dcr"):                    # typing L or DCR goes back to the ideal inductor
            self.part_vars[k][1].bind("<Key>", lambda e: self.lmodel.set(booster_sim.IDEAL_L)
                                      if e.char or e.keysym in ("BackSpace", "Delete") else None)
        n += 1
        self.auto_comp = tk.BooleanVar(value=True)
        ttk.Checkbutton(pf, text="Auto-tune compensation (AN-1286)", variable=self.auto_comp,
                        command=self.toggle_comp).grid(row=n, column=0, columnspan=3, sticky="w", pady=(6, 0))
        cf = ttk.Frame(pf)
        cf.grid(row=n + 1, column=0, columnspan=3, sticky="ew")
        self.comp_vars = {}
        self.entry_rows(cf, COMP_FIELDS, self.comp_vars, {})
        bf = ttk.Frame(pf)
        bf.grid(row=n + 2, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Button(bf, text="Recalculate", command=self.recalc).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="Use design values", command=lambda: self.recalc(from_spec=True)).pack(
            side="left", expand=True, fill="x")
        self.toggle_comp()

        sf = ttk.LabelFrame(f, text="Simulation (ngspice, board netlist)", padding=4)
        sf.pack(fill="x")
        ttk.Label(sf, text="Points: Vin, Vout, Iout per line").grid(row=0, column=0, columnspan=3, sticky="w")
        self.points = tk.Text(sf, height=4, width=30, font=self.mono)
        self.points.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.points.bind("<<Modified>>", self.points_edited)
        ttk.Label(sf, text="MOSFET").grid(row=2, column=0, sticky="w")
        self.mosfet = ttk.Combobox(sf, width=24, height=30)
        self.mosfet.set(booster_sim.MOSFET_DEFAULT); self.mosfet.grid(row=2, column=1, sticky="ew")
        ttk.Button(sf, text="Model…", width=7, command=lambda: self.edit_model("mosfet")).grid(row=2, column=2)
        ttk.Label(sf, text="Diode").grid(row=3, column=0, sticky="w")
        self.diode = ttk.Combobox(sf, width=24, height=30)
        self.diode.set(booster_sim.DIODE_DEFAULT); self.diode.grid(row=3, column=1, sticky="ew")
        ttk.Button(sf, text="Model…", width=7, command=lambda: self.edit_model("diode")).grid(row=3, column=2)
        self.refresh_models()
        for box in (self.mosfet, self.diode, self.lmodel):
            self.wide_dropdown(box)
        self.mosfet.bind("<<ComboboxSelected>>", lambda e: self.model_selected("mosfet"))
        self.diode.bind("<<ComboboxSelected>>", lambda e: self.model_selected("diode"))
        ttk.Label(sf, text="Run length / jobs").grid(row=4, column=0, sticky="w")
        self.tran = tk.StringVar(value="16m"); self.jobs = tk.StringVar(value="4")
        ttk.Entry(sf, textvariable=self.tran, width=7).grid(row=4, column=1, sticky="w")
        ttk.Entry(sf, textvariable=self.jobs, width=4).grid(row=4, column=2, sticky="w")
        rb = ttk.Frame(sf)
        rb.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        self.run_btn = ttk.Button(rb, text="Run simulation", command=self.run_sim)
        self.run_btn.pack(side="left", expand=True, fill="x")
        self.stop_btn = ttk.Button(rb, text="Stop", command=self.stop_sim, state="disabled")
        self.stop_btn.pack(side="left")
        self.stop_event = threading.Event()
        self.sim_status = ttk.Label(sf, text="", foreground="grey", wraplength=300)
        self.sim_status.grid(row=6, column=0, columnspan=3, sticky="w")
        for k, (_, e) in list(self.part_vars.items()) + list(self.comp_vars.items()):
            e.bind("<Key>", lambda ev, k=k: (self.part_typed.add(k), self.root.after_idle(self.mark_parts))
                   if ev.char or ev.keysym in ("BackSpace", "Delete") else None, add="+")

    def mark_parts(self):
        """Red: a selected part or compensation value typed by hand (RSN / RSL: their "auto" box is clear)."""
        red = "#c00000"
        for k, (_, e) in self.part_vars.items():
            manual = not self.auto[k].get() if k in self.auto else k in self.part_typed
            e.configure(foreground=red if manual else "")
        for k, (_, e) in self.comp_vars.items():
            e.configure(foreground=red if not self.auto_comp.get() and k in self.part_typed else "")

    def refresh_models(self):
        for role, box in (("mosfet", self.mosfet), ("diode", self.diode), ("inductor", self.lmodel)):
            builtin, mine = booster_sim.model_choices(role)
            box.configure(values=builtin + mine)

    def edit_model(self, role):
        """Model libraries: load a file or paste text, then pick one of the models it contains."""
        box = {"mosfet": self.mosfet, "diode": self.diode, "inductor": self.lmodel}[role]
        um = booster_sim.user_models()
        w = tk.Toplevel(self.root); w.title(f"{role.capitalize()} SPICE model library"); w.geometry("1100x650")
        ttk.Label(w, text=MODEL_HELP[role], wraplength=1060, foreground="grey").pack(anchor="w", padx=6, pady=4)
        top = ttk.Frame(w); top.pack(fill="x", padx=6)
        libname = tk.StringVar()
        ttk.Button(top, text="Load file…", command=lambda: load()).pack(side="left")
        ttk.Label(top, text="   Library name").pack(side="left")
        ttk.Entry(top, textvariable=libname, width=30).pack(side="left", padx=4)
        ttk.Button(top, text="List models in the text", command=lambda: parse()).pack(side="left", padx=8)
        pw = ttk.PanedWindow(w, orient="horizontal"); pw.pack(fill="both", expand=True, padx=6, pady=4)
        tf = ttk.Frame(pw); pw.add(tf, weight=3)
        txt = tk.Text(tf, wrap="none", font=self.mono, undo=True)
        ys = ttk.Scrollbar(tf, orient="vertical", command=txt.yview); txt.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y"); txt.pack(fill="both", expand=True)
        lf = ttk.Frame(pw); pw.add(lf, weight=2)
        ttk.Label(lf, text="Models in the text (double-click = use)").pack(anchor="w")
        lb = tk.Listbox(lf, font=self.mono, activestyle="dotbox", exportselection=False)
        ls = ttk.Scrollbar(lf, orient="vertical", command=lb.yview); lb.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y"); lb.pack(fill="both", expand=True)
        found = []

        def parse():
            found.clear(); lb.delete(0, "end")
            for m in booster_sim.parse_models(txt.get("1.0", "end")):
                ok = booster_sim.usable(role, m)
                found.append((m, ok))
                kind = f"subckt, {len(m['pins'])} pins" if m["kind"] == "subckt" else f"model {m['mtype'].upper()}"
                lb.insert("end", f"{m['name']:<28} {kind}{'' if ok else '   (not a ' + role + ')'}")
                if not ok:
                    lb.itemconfigure("end", foreground="grey")
            usable = [i for i, (_, ok) in enumerate(found) if ok]
            if usable:
                sel = next((i for i in usable if found[i][0]["name"] == box.get()), usable[0])
                lb.selection_set(sel); lb.see(sel)

        def load():
            p = filedialog.askopenfilename(parent=w, title="SPICE library", filetypes=[
                ("SPICE models", "*.lib *.LIB *.mod *.model *.cir *.sub *.txt *.spi *.sp"), ("All files", "*")])
            if p:
                txt.delete("1.0", "end"); txt.insert("1.0", Path(p).read_text(errors="replace"))
                libname.set(Path(p).stem); parse()

        def use(_=None):
            if not found:
                parse()
            sel = lb.curselection()
            if not sel or not found[sel[0]][1]:
                messagebox.showerror("Model", f"Select a model usable as a {role}.", parent=w); return
            name = found[sel[0]][0]["name"]
            if name in booster_sim.BUILTIN or name in booster_sim.BUILTIN_L:
                messagebox.showinfo("Model", f"{name} is in the board libraries already; using that one.", parent=w)
            else:
                try:
                    booster_sim.save_user_library(txt.get("1.0", "end"), libname.get() or name)
                except ValueError as e:
                    messagebox.showerror("Model", str(e), parent=w); return
            self.refresh_models(); box.set(name); w.destroy(); self.model_selected(role)
            self.sim_status.configure(text=f"{role}: {name} selected (library in designer/user_models/)")
        lb.bind("<Double-Button-1>", use)
        if box.get() in um:                                       # show the library of the current choice
            path = um[box.get()][0]
            txt.insert("1.0", path.read_text(errors="replace")); libname.set(path.stem); parse()
        bf = ttk.Frame(w); bf.pack(fill="x", padx=6, pady=6)
        ttk.Button(bf, text="Save library and use the selected model", command=use).pack(side="right")
        ttk.Button(bf, text="Cancel", command=w.destroy).pack(side="right")

    def wide_dropdown(self, box):
        """Make the drop-down list as wide as its longest entry (the entry field keeps its width)."""
        style = f"W{id(box)}.TCombobox"
        ttk.Style().layout(style, ttk.Style().layout("TCombobox"))

        def widen():
            font = tkfont.nametofont("TkDefaultFont")
            longest = max((font.measure(str(v)) for v in box.cget("values")), default=0)
            extra = longest + 60 - box.winfo_width()          # + scrollbar and padding
            ttk.Style().configure(style, postoffset=(0, 0, max(0, extra), 0))
        box.configure(style=style, postcommand=widen)

    def model_selected(self, role):
        self.draw_schematic()
        if role == "inductor" and self.lmodel.get() == booster_sim.IDEAL_L:
            self.draw_char_curves(); return
        self.characterize_models(roles=(role,))

    def char_conditions(self):
        """Test conditions from the current design: drive voltage, Vout, full-load inductor current, fs, Iout."""
        s, v = self.d.spec, self.d.values
        full = [c for c in self.d.corners if c["iout"] == s.iout_max] or self.d.corners
        return dict(vdrv=min(s.vcc or s.vin, VDRV_MAX), vout=max(s.vouts), i_load=max(c["il"] for c in full), fs=v["fs"],
                    iout=s.iout_max)

    def characterize_models(self, roles=("mosfet", "diode", "inductor")):
        """Run the model tests in the background; results come back through the polling queue."""
        if self.d is None:
            return
        if self.char_busy:                                   # one test at a time; run the rest afterwards
            self.char_pending |= set(roles); return
        cond = self.char_conditions()
        jobs = {"mosfet": self.mosfet.get(), "diode": self.diode.get(), "inductor": self.lmodel.get()}
        jobs = {r: n for r, n in jobs.items() if r in roles and n and n != booster_sim.IDEAL_L}
        if not jobs:
            return
        self.char_busy = True
        self.char_text.set("Testing " + ", ".join(jobs.values()) + " in ngspice…")
        self.root.config(cursor="watch")

        def work():
            for role, name in jobs.items():
                try:
                    if role == "mosfet":
                        r = characterize.mosfet(name, cond["vdrv"], cond["vout"], cond["i_load"], cond["fs"])
                    elif role == "diode":
                        r = characterize.diode(name, cond["vout"], cond["iout"])
                    else:
                        r = characterize.inductor(name, cond["fs"])
                    self.sim_q.put(("char", (role, r)))
                except Exception as e:
                    self.sim_q.put(("char_error", f"{name}: {e}"))
            self.sim_q.put(("char_done", None))
        threading.Thread(target=work, daemon=True).start()

    def char_finished(self):
        self.char_busy = False
        self.root.config(cursor="")
        if self.char_pending:
            roles, self.char_pending = tuple(self.char_pending), set()
            self.characterize_models(roles)

    def stale_models(self):
        """Roles whose model test is missing (or has no curves), for another model, or at conditions > 10 % away
        from the design's."""
        if self.d is None:
            return ()
        cond, c = self.char_conditions(), self.char_results
        near = lambda a, b: a is not None and abs(a - b) <= 0.1 * abs(b)
        c = {role: r for role, r in c.items() if r.get("curves")   # tested before the curves / Rp existed: again
             and (role != "inductor" or "rp" in r)}
        out = []
        m = c.get("mosfet")
        if not (m and m["model"] == self.mosfet.get() and near(m["vdrv"], cond["vdrv"]) and near(m["vout"], cond["vout"])
                and near(m["i_load"], cond["i_load"])):
            out.append("mosfet")
        dd = c.get("diode")
        if not (dd and dd["model"] == self.diode.get() and near(dd["vout"], cond["vout"]) and near(dd["iout"], cond["iout"])):
            out.append("diode")
        li = c.get("inductor")
        if self.lmodel.get() != booster_sim.IDEAL_L and not (li and li["model"] == self.lmodel.get()
                                                             and near(li["fs"], cond["fs"])):
            out.append("inductor")
        return tuple(out)

    def model_values(self):
        """Loss-estimate parameters measured on the current MOSFET / diode models: field key -> value."""
        c, out = self.char_results, {}
        if c.get("mosfet", {}).get("model") == self.mosfet.get():
            out.update({k: c["mosfet"][k] for k in ("rds", "qg", "coss", "tsw")})
        if c.get("diode", {}).get("model") == self.diode.get():
            out.update(cj=c["diode"]["cj"], vd=c["diode"]["vf"])
        return out

    def param_entry(self, k):
        return (self.spec_vars if k == "vd" else self.loss_vars)[k]

    def mark_overrides(self):
        """Values typed by hand that differ from the model's are red; the line under the fields lists them."""
        mv = self.model_values()
        typed = []
        for k in list(LOSS_PARTS) + ["vd"]:
            var, e = self.param_entry(k)
            try:
                differs = k in mv and abs(parse_si(var.get()) - mv[k]) > 0.005 * abs(mv[k])
            except ValueError:
                differs = True
            if k in self.overrides and not differs:
                self.overrides.discard(k)
            red = k in self.overrides and k in mv
            e.configure(foreground="#c00000" if red else "")
            if red:
                label = "Diode Vf" if k == "vd" else LOSS_PARTS[k][0]
                typed.append(f"{label} {var.get()} (model {fmt_val(mv[k])})")
        self.override_text.set("Typed by hand (red): " + "; ".join(typed) + ".  \"Reset to the model values\" "
                               "undoes them." if typed else "")

    def apply_characterization(self, role, r):
        """Put the measured model values into the fields (except values typed by hand) and redesign."""
        self.char_results[role] = r
        if role == "mosfet":
            for k in ("rds", "qg", "coss", "tsw"):
                if k not in self.overrides:
                    self.loss_vars[k][0].set(fmt_val(r[k]))
        elif role == "diode":
            if "cj" not in self.overrides:
                self.loss_vars["cj"][0].set(fmt_val(r["cj"]))
            if "vd" not in self.overrides:
                self.spec_vars["vd"][0].set(f"{r['vf']:.3g}")
        else:
            self.part_vars["L"][0].set(fmt_val(r["L"])); self.part_vars["dcr"][0].set(fmt_val(r["dcr"]))
            self.part_typed -= {"L", "dcr"}                  # from the inductor model, not typed
        self.show_char_text()
        self.recalc()
        self.draw_char_curves()

    def show_char_text(self):
        lines = []
        c = self.char_results
        if "mosfet" in c:
            m = c["mosfet"]
            lines.append(f"{m['model']} at VGS {m['vdrv']:g} V, {m['vout']:g} V, {m['i_load']:.3g} A: "
                         f"RDS(on) {si(m['rds'], 'Ω')}, Co(er) {si(m['coss'], 'F')} (Qoss {si(m['qoss'], 'C')}), "
                         f"Qg {si(m['qg'], 'C')}, edges on {si(m['t_on'], 's')} / off {si(m['t_off'], 's')}, "
                         f"E_on {si(m['e_on'], 'J')} + E_off {si(m['e_off'], 'J')} (turn-off minus the {si(m.get('e_oss', 0), 'J')} "
                         f"stored in Coss) → t_eff {si(m['tsw'], 's')}")
        if "diode" in c:
            dd = c["diode"]
            lines.append(f"{dd['model']}: Vf {dd['vf']:.3g} V at {si(dd['iout'], 'A')} (→ Diode Vf), "
                         f"Cj(er) {si(dd['cj'], 'F')} at {dd['vout']:g} V, reverse current {si(dd['ir'], 'A')}")
        if "inductor" in c:
            li = c["inductor"]
            lines.append(f"{li['model']}: L {si(li['L'], 'H')} at {si(li['fs'], 'Hz')} ({si(li['L_10k'], 'H')} at "
                         f"10 kHz), DCR {si(li['dcr'], 'Ω')}, self-resonance {si(li['srf'], 'Hz')}, in parallel at fs: "
                         f"Rp {si(li.get('rp') or np.nan, 'Ω')} (→ L core loss), Cp {si(li.get('cp') or np.nan, 'F')} (→ Q Coss) "
                         f"(→ L, L DCR; the simulation uses the model)")
            if li["srf"] < 5 * li["fs"]:
                lines[-1] += f" - WARNING: SRF < 5 × fs"
        self.char_text.set("From the models: " + "\n".join(lines) if lines else "")

    def reset_to_models(self):
        """Drop the values typed by hand and re-test the models."""
        self.overrides.clear()
        mv = self.model_values()
        for k, val in mv.items():
            self.param_entry(k)[0].set(f"{val:.3g}" if k == "vd" else fmt_val(val))
        self.recalc()
        self.characterize_models()

    def draw_char_curves(self):
        """Datasheet-style curves of the model tests, under the parameter fields."""
        fig, c = self.char_fig, self.char_results
        fig.clear()
        m = c.get("mosfet") if c.get("mosfet", {}).get("model") == self.mosfet.get() else None
        dd = c.get("diode") if c.get("diode", {}).get("model") == self.diode.get() else None
        li = c.get("inductor") if c.get("inductor", {}).get("model") == self.lmodel.get() else None
        cv = lambda r, k: (r or {}).get("curves", {}).get(k)
        panels = [p for p in ("rds_vgs", "qg_vgs", "coss_vds", "sw_on", "sw_off") if cv(m, p)] + \
                 [p for p in ("if_vf", "cj_vr") if cv(dd, p)] + (["l_f"] if cv(li, "l_f") else [])
        if not panels:
            fig.text(0.5, 0.5, "Model curves appear here after the MOSFET / diode / inductor tests", ha="center",
                     color="grey"); self.char_canvas.draw_idle(); return
        ncol = min(4, len(panels))
        axes = fig.subplots((len(panels) + ncol - 1) // ncol, ncol, squeeze=False).ravel()
        for ax in axes[len(panels):]:
            ax.set_visible(False)
        blue, orange, green = "#1f77b4", "#ff7f0e", "#2ca02c"
        for ax, p in zip(axes, panels):
            ax.grid(True, which="both", alpha=0.3); ax.tick_params(labelsize=7)
            if p == "rds_vgs":
                x, y = cv(m, p)
                ax.semilogy(x, np.array(y) * 1e3, color=blue, lw=1.5)
                ax.axvline(m["vdrv"], color="grey", ls=":", lw=1)
                ax.plot(m["vdrv"], m["rds"] * 1e3, "o", color=blue, mec="white")
                ax.set_ylim(top=min(max(y) * 1e3, 50 * m["rds"] * 1e3))
                ax.set_xlabel("VGS (V)", fontsize=7); ax.set_ylabel("RDS(on) (mΩ)", fontsize=7)
                ax.set_title(f"{m['model']}: RDS(on) vs VGS (VDS 50 mV)\n● {si(m['rds'], 'Ω')} at {m['vdrv']:g} V, "
                             f"{m['i_load']:.3g} A", fontsize=7.5, loc="left")
            elif p == "qg_vgs":
                x, y = cv(m, p)
                ax.plot(np.array(x) * 1e9, y, color=blue, lw=1.5)
                ax.set_xlabel("Qg (nC)", fontsize=7); ax.set_ylabel("VGS (V)", fontsize=7)
                ax.set_title(f"Gate charge at {m['vout']:g} V, {m['i_load']:.3g} A\nQg {si(m['qg'], 'C')} to "
                             f"{m['vdrv']:g} V", fontsize=7.5, loc="left")
            elif p == "coss_vds":
                x, y = cv(m, p)
                er = [c_er(cv(m, p), xx, m["coss"]) * 1e12 for xx in x]
                ax.semilogy(x, np.array(y) * 1e12, color=blue, lw=1.5, label="C(V)")
                ax.semilogy(x, er, color=blue, ls="--", lw=1.2, label="Co(er) 0→V")
                ax.plot(m["vout"], m["coss"] * 1e12, "o", color=blue, mec="white")
                ax.legend(fontsize=6.5, loc="upper right")
                ax.set_xlabel("VDS (V)", fontsize=7); ax.set_ylabel("Coss (pF)", fontsize=7)
                ax.set_title(f"Coss vs VDS. Co(er) = 2E/V² (stored energy)\n● {si(m['coss'], 'F')} at {m['vout']:g} V, "
                             f"C({m['vout']:g} V) {si(y[-1], 'F')}", fontsize=7.5, loc="left")
            elif p in ("sw_on", "sw_off"):
                t, vds, idr, vgs = cv(m, p)
                ax.plot(t, 100 * np.array(vds) / m["vout"], color=blue, lw=1.2, label=f"VDS / {m['vout']:g} V")
                ax.plot(t, 100 * np.array(idr) / m["i_load"], color=orange, lw=1.2, label=f"ID / {m['i_load']:.3g} A")
                ax.plot(t, 100 * np.array(vgs) / m["vdrv"], color=green, lw=1.2, label=f"VGS / {m['vdrv']:g} V")
                ax.set_xlabel("time from the drive edge (ns)", fontsize=7); ax.set_ylabel("% of final", fontsize=7)
                on = p == "sw_on"
                ax.set_title(f"Turn-{'on' if on else 'off'} (clamped inductive, R7 + driver)\n90→10 % "
                             f"{si(m['t_on' if on else 't_off'], 's')}, E_{'on' if on else 'off'} "
                             f"{si(m['e_on' if on else 'e_off'], 'J')}", fontsize=7.5, loc="left")
                ax.set_ylim(-10, 115)
                if on:
                    ax.legend(fontsize=6.5, loc="center right")
            elif p == "if_vf":
                x, y = cv(dd, p)
                ax.semilogy(x, np.array(y) * 1e3, color=orange, lw=1.5)
                ax.plot(dd["vf"], dd["iout"] * 1e3, "o", color=orange, mec="white")
                ax.set_xlabel("VF (V)", fontsize=7); ax.set_ylabel("IF (mA)", fontsize=7)
                ax.set_title(f"{dd['model']}: IF vs VF\n● Vf {dd['vf']:.3g} V at {si(dd['iout'], 'A')}",
                             fontsize=7.5, loc="left")
            elif p == "cj_vr":
                x, y = cv(dd, p)
                er = [c_er(cv(dd, p), xx, dd["cj"]) * 1e12 for xx in x]
                ax.semilogy(x, np.array(y) * 1e12, color=orange, lw=1.5, label="C(V)")
                ax.semilogy(x, er, color=orange, ls="--", lw=1.2, label="Cj(er) 0→V")
                ax.plot(dd["vout"], dd["cj"] * 1e12, "o", color=orange, mec="white")
                ax.legend(fontsize=6.5, loc="upper right")
                ax.set_xlabel("VR (V)", fontsize=7); ax.set_ylabel("Cj (pF)", fontsize=7)
                ax.set_title(f"Cj vs VR. Cj(er) = 2E/V²; IR {si(dd['ir'], 'A')}\n● {si(dd['cj'], 'F')} at "
                             f"{dd['vout']:g} V, C({dd['vout']:g} V) {si(y[-1], 'F')}", fontsize=7.5, loc="left")
            elif p == "l_f":
                f, lf, zm = (np.array(a) for a in cv(li, p))
                ok = lf > 0
                ax.loglog(f[ok], lf[ok] * 1e6, color=green, lw=1.5, label="L")
                ax.set_ylim(0.5 * li["L_10k"] * 1e6, 4 * li["L_10k"] * 1e6)
                ax.axvline(li["fs"], color="grey", ls=":", lw=1)
                if np.isfinite(li["srf"]):
                    ax.axvline(li["srf"], color="#b3261e", ls="--", lw=1)
                ax.set_xlabel("frequency (Hz)", fontsize=7); ax.set_ylabel("L (µH)", fontsize=7)
                ax.set_title(f"{li['model']}: L vs f, SRF {si(li['srf'], 'Hz')} (dashed)\nL {si(li['L'], 'H')} at fs "
                             f"(dotted), DCR {si(li['dcr'], 'Ω')}, Rp {si(li.get('rp') or np.nan, 'Ω')}, "
                             f"Cp {si(li.get('cp') or np.nan, 'F')}", fontsize=7.5, loc="left")
        for ax in axes[:len(panels)]:                  # plain numbers on log axes, 1-2-5 steps
            for a in (ax.xaxis, ax.yaxis):
                if a.get_scale() == "log":
                    lo, hi = (ax.get_xlim() if a is ax.xaxis else ax.get_ylim())
                    a.set_major_locator(LogLocator(subs=(1, 2, 5) if hi / lo < 300 else (1,)))
                    a.set_major_formatter(FuncFormatter(lambda x, _: fmt_val(x) if x >= 1e3 else f"{x:g}"))
                    a.set_minor_formatter(NullFormatter())
        fig.tight_layout(pad=0.6)
        self.char_canvas.draw_idle()

    def toggle_comp(self):
        state = "disabled" if self.auto_comp.get() else "normal"
        for var, e in self.comp_vars.values():
            e.configure(state=state)
        self.mark_parts()

    # ------------------------------------------------------------ tabs
    def build_tabs(self, nb):
        t1 = ttk.Frame(nb); nb.add(t1, text="Parts")
        cols = ("ref", "part", "value", "notes")
        self.tree = ttk.Treeview(t1, columns=cols, show="headings")
        for c, w in zip(cols, (70, 170, 330, 700)):
            self.tree.heading(c, text=c.capitalize()); self.tree.column(c, width=w, anchor="w", stretch=c == "notes")
        self.tree.pack(fill="both", expand=True)
        self.warn = tk.Text(t1, height=10, wrap="word", font=("TkDefaultFont", 10))
        self.warn.tag_configure("w", foreground="#b3261e"); self.warn.tag_configure("n", foreground="#333")
        self.warn.pack(fill="x")

        t2 = ttk.Frame(nb); nb.add(t2, text="Loop gain / phase margin")
        self.loop_fig = Figure(figsize=(11, 7.5))
        self.loop_canvas, _ = self.canvas(t2, self.loop_fig)

        t3 = ttk.Frame(nb); nb.add(t3, text="Simulation")
        st = ttk.Frame(t3); st.pack(fill="x")
        self.sim_tree = ttk.Treeview(st, columns=[c for c, _, _ in SIM_COLS], show="headings", height=7)
        for c, h, w in SIM_COLS:
            self.sim_tree.heading(c, text=h); self.sim_tree.column(c, width=w, anchor="e")
        self.sim_tree.pack(fill="x")
        self.sim_tree.bind("<<TreeviewSelect>>", self.show_case)
        sc = ttk.Frame(t3, padding=(0, 4)); sc.pack(fill="x")
        ttk.Label(sc, text="Plots:").pack(side="left")
        self.sig_vars = {}
        for label, key, _ in SIG_DEFS:
            var = self.sig_vars[key] = tk.BooleanVar(value=key in SIG_DEFAULT)
            ttk.Checkbutton(sc, text=label, variable=var, command=self.show_case).pack(side="left", padx=3)
        vc = ttk.Frame(t3); vc.pack(fill="x")
        ttk.Label(vc, text="View:").pack(side="left")
        for label, which in (("Full run", "full"), ("Measured window", "window"), ("Last 20 µs", "end"),
                             ("Fit Y to view", "fit")):
            ttk.Button(vc, text=label, command=lambda w=which: self.sim_view(w)).pack(side="left", padx=2)
        ttk.Label(vc, text="   Ctrl / Shift-click rows to overlay cases. Zoom / pan with the toolbar (time axes "
                  "move together).", foreground="grey").pack(side="left")
        self.sim_fig = Figure(figsize=(11, 6))
        self.sim_canvas, _ = self.canvas(t3, self.sim_fig)

        t6 = ttk.Frame(nb); nb.add(t6, text="Schematic")
        self.sch_fig = Figure(figsize=(13.2, 9.3))
        self.sch_fig.subplots_adjust(0, 0, 1, 1)
        self.sch_ax = self.sch_fig.add_subplot(111)
        self.sch_canvas, _ = self.canvas(t6, self.sch_fig)

        t5 = ttk.Frame(nb, padding=4); nb.add(t5, text="Losses")
        lf = ttk.LabelFrame(t5, text="Part values for the loss estimate: measured on the MOSFET / diode models "
                            "(red = typed by hand; diode Vf and L DCR are on the left)", padding=4)
        lf.pack(fill="x")
        self.loss_vars = {}
        for i, (k, (label, unit)) in enumerate(LOSS_PARTS.items()):
            ttk.Label(lf, text=label).grid(row=i // 3, column=(i % 3) * 3, sticky="w", padx=(8, 2))
            var = tk.StringVar()
            e = ttk.Entry(lf, textvariable=var, width=10)
            e.grid(row=i // 3, column=(i % 3) * 3 + 1, sticky="w")
            e.bind("<Return>", lambda _e: self.recalc())
            ttk.Label(lf, text=unit, foreground="grey").grid(row=i // 3, column=(i % 3) * 3 + 2, sticky="w")
            self.loss_vars[k] = (var, e)
        for k in list(LOSS_PARTS) + ["vd"]:                  # typing a value overrides the model's
            self.param_entry(k)[1].bind("<Key>", lambda e, k=k: self.overrides.add(k) if e.char or e.keysym in
                                        ("BackSpace", "Delete") else None)
            self.param_entry(k)[1].bind("<FocusOut>", lambda e: self.mark_overrides(), add="+")
        ttk.Button(lf, text="Recalculate", command=self.recalc).grid(row=1, column=8, sticky="e", padx=8)
        ttk.Button(lf, text="Reset to the model values", command=self.reset_to_models).grid(
            row=0, column=8, sticky="e", padx=8)
        self.override_text = tk.StringVar()
        ttk.Label(t5, textvariable=self.override_text, foreground="#c00000", wraplength=1500).pack(anchor="w")
        self.char_text = tk.StringVar(value="The MOSFET / diode / inductor models are tested in ngspice at the "
                                      "design's conditions; the values above come from those tests.")
        ttk.Label(t5, textvariable=self.char_text, foreground="#1f4e8c", wraplength=1500, justify="left").pack(
            anchor="w", pady=(4, 0))
        lp = ttk.PanedWindow(t5, orient="vertical")                # model curves | loss tables (drag to resize)
        lp.pack(fill="both", expand=True, pady=(4, 0))
        cf = ttk.Frame(lp); lp.add(cf, weight=3)
        self.char_fig = Figure(figsize=(13, 4.4))
        self.char_canvas, _ = self.canvas(cf, self.char_fig)
        t5 = ttk.Frame(lp); lp.add(t5, weight=2)
        ttk.Label(t5, text="Estimated losses, mW (analytic, at the simulation points listed on the left)").pack(
            anchor="w", pady=(4, 0))
        ecols = ["Vin (V)", "Vout (V)", "Iout (mA)", "Pout", "Pin", "η %"] + LOSS_COLS
        self.est_tree = ttk.Treeview(t5, columns=ecols, show="headings", height=6)
        for c in ecols:
            self.est_tree.heading(c, text=c); self.est_tree.column(c, width=70, anchor="e")
        self.sortable(self.est_tree)
        self.est_tree.pack(fill="x")
        ttk.Label(t5, text="Simulated losses, mW (ngspice: V × I at each part; MOSFET includes its gate path; other = "
                  "Pin − Pout − the parts: FB divider, capacitor ESR, R7). The LM3478 model's driver and supply are "
                  "ideal, so the IC current and gate-drive power (estimated table: LM3478, Q gate) are not in the "
                  "simulated Pin: 'IC+gate' calculates them and 'η incl. IC' adds them, to compare with the estimate. "
                  "'other' can be slightly negative.", wraplength=1400).pack(anchor="w", pady=(8, 0))
        scols = ["Vin (V)", "Vout (V)", "Iout (mA)", "Pout", "Pin", "η %", "IC+gate (calc)", "η incl. IC %",
                 "L1 (incl. DCR)", "MOSFET", "Diode", "RSN", "other", "simulated with"]
        self.simloss_tree = ttk.Treeview(t5, columns=scols, show="headings", height=6)
        for c in scols:
            self.simloss_tree.heading(c, text=c)
            self.simloss_tree.column(c, width={"simulated with": 520}.get(c, 80 if "(" in c else 90),
                                     anchor="w" if c == "simulated with" else "e")
        self.sortable(self.simloss_tree)
        self.simloss_tree.pack(fill="both", expand=True)

        t4 = ttk.Frame(nb); nb.add(t4, text="Report")
        self.report_where = tk.StringVar(value="Written to the configuration folder (report.txt) on every save.")
        ttk.Label(t4, textvariable=self.report_where, foreground="grey").pack(anchor="w")
        self.report = tk.Text(t4, wrap="none", font=self.mono)
        self.report.pack(fill="both", expand=True)

    def canvas(self, parent, fig):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        c = FigureCanvasTkAgg(fig, master=parent)
        tb = NavigationToolbar2Tk(c, parent, pack_toolbar=False)
        tb.pack(side="bottom", fill="x")
        c.get_tk_widget().pack(fill="both", expand=True)
        return c, tb

    # ------------------------------------------------------------ design
    def read_spec(self):
        kw = {}
        for k, (var, _) in self.spec_vars.items():
            txt = var.get().strip()
            if k == "vcc" and not txt:                         # LM3478 supplied from the regulator input
                kw[k] = 0.0; continue
            kw[k] = tuple(parse_si(x) for x in txt.split(",") if x.strip()) if k == "vouts" else parse_si(txt)
        return Spec(**kw)

    def read_choice(self):
        ch = {}
        for k, (var, _) in self.part_vars.items():
            if var.get().strip() and not (k in self.auto and self.auto[k].get()):
                ch[k] = parse_si(var.get())
        for k, (var, _) in self.loss_vars.items():
            if var.get().strip():
                ch[k] = parse_si(var.get())
        if not self.auto_comp.get():
            ch["comp"] = {k: parse_si(var.get()) for k, (var, _) in self.comp_vars.items()}
        ch.update(self.model_extras())
        return ch

    def model_extras(self):
        """What the loss estimate takes from the model tests beyond the fields: C(V) curves of the MOSFET and
        the diode (Co(er) / Cj(er) at each Vout) and the inductor model's parallel Rp / Cp."""
        c, out = self.char_results, {}
        cv = lambda role, k: c.get(role, {}).get("curves", {}).get(k)
        if c.get("mosfet", {}).get("model") == self.mosfet.get() and cv("mosfet", "coss_vds"):
            out["coss_curve"] = cv("mosfet", "coss_vds")
        if c.get("diode", {}).get("model") == self.diode.get() and cv("diode", "cj_vr"):
            out["cj_curve"] = cv("diode", "cj_vr")
        li = c.get("inductor", {})
        if li.get("model") == self.lmodel.get() != booster_sim.IDEAL_L and li.get("rp") is not None:
            out.update(rp=li["rp"] if np.isfinite(li["rp"]) else None, cp=li.get("cp") or 0.0)
        return out

    def recalc(self, from_spec=False):
        if from_spec:
            for var in self.auto.values():
                var.set(True)
            self.part_typed.clear()
        try:
            spec = self.read_spec()
            choice = {"comp": self.read_choice()["comp"]} if from_spec and not self.auto_comp.get() else \
                ({} if from_spec else self.read_choice())
            if from_spec:                     # the loss parameters stay those of the models (or typed by hand)
                mv = self.model_values()
                choice.update({k: parse_si(self.loss_vars[k][0].get()) for k in LOSS_PARTS
                               if k in mv or k in self.overrides})
                choice.update(self.model_extras())
            self.d = design(spec, choice)
        except Exception as e:
            messagebox.showerror("Design", str(e)); return
        v = self.d.values
        for k, (var, _) in self.part_vars.items():
            var.set(fmt_val(v[k]))
        for k, (var, e) in self.comp_vars.items():
            e.configure(state="normal"); var.set(fmt_val(v["comp"][k]))
        for k, (var, _) in self.loss_vars.items():
            var.set(fmt_val(v[k]))
        self.toggle_comp()
        key = (spec.vin, tuple(spec.vouts), spec.iout_min, spec.iout_max)
        if not self.points.get("1.0", "end").strip():
            # empty list: one point per Vout at Iout max and Iout min (a list already there is kept)
            self.pts_key = key
            pts = [(spec.vin, vo, io) for vo in spec.vouts for io in (spec.iout_max, spec.iout_min)]
            self.points.delete("1.0", "end")
            self.points.insert("1.0", "\n".join(f"{a:g}, {b:g}, {c:g}" for a, b, c in pts))
        self.fill_parts(); self.plot_loop(); self.draw_schematic()
        self.refresh_estimates()
        if hasattr(self, "override_text"):
            self.mark_overrides()
        stale = self.stale_models()           # design moved (Vout, current, fs) or new model: test again
        if stale and self.char_results and not self.char_busy:
            self.characterize_models(stale)

    def points_edited(self, _=None):
        """Jobs = one per simulation point, up to the CPU count - 2 (like sweep.py)."""
        if not self.points.edit_modified():
            return
        n = sum(1 for ln in self.points.get("1.0", "end").splitlines() if ln.strip())
        self.jobs.set(str(max(1, min(n, (os.cpu_count() or 4) - 2))))
        self.points.edit_modified(False)
        self.refresh_estimates()

    def read_points(self):
        pts = [tuple(parse_si(x) for x in ln.split(",")) for ln in self.points.get("1.0", "end").splitlines()
               if ln.strip()]
        if not all(len(p) == 3 for p in pts):
            raise ValueError("each line needs Vin, Vout, Iout")
        return pts

    def refresh_estimates(self):
        """Estimated losses at the simulation points (the design's own corners while the list is unreadable)."""
        if self.d is None:
            return
        try:
            self.d.losses = point_losses(self.d, self.read_points()) or self.d.losses
        except ValueError:
            pass                                      # half-typed line: keep the last table
        self.est_tree.delete(*self.est_tree.get_children())
        for lab, l in self.d.losses:
            self.est_tree.insert("", "end", values=[f"{l['Vin']:g}", f"{l['Vout']:g}", f"{l['Iout']*1e3:g}",
                                                    f"{l['Pout']*1e3:.0f}", f"{l['Pin']*1e3:.0f}", f"{l['eff']*100:.1f}"]
                                 + [f"{l[k]*1e3:.1f}" for k in LOSS_COLS])
        self.resort(self.est_tree)
        self.refresh_report()

    def fill_parts(self):
        self.tree.delete(*self.tree.get_children())
        for row in self.d.reqs:
            self.tree.insert("", "end", values=row)
        self.warn.delete("1.0", "end")
        for w in self.d.warnings:
            self.warn.insert("end", "⚠ " + w + "\n", "w")
        for n in self.d.notes:
            self.warn.insert("end", "• " + n + "\n", "n")

    def plot_loop(self):
        fig, d = self.loop_fig, self.d
        fig.clear()
        gs = fig.add_gridspec(2, 2, width_ratios=[2.3, 1], hspace=0.08, wspace=0.25,
                              left=0.07, right=0.98, top=0.94, bottom=0.08)
        am = fig.add_subplot(gs[0, 0]); ap = fig.add_subplot(gs[1, 0], sharex=am)
        ab = fig.add_subplot(gs[:, 1])
        comp, fs = d.values["comp"], d.values["fs"]
        n = len(d.loops)
        cols = matplotlib.colormaps["tab10" if n <= 10 else "tab20"](np.arange(n) % 20)
        pms, labs, bar_c = [], [], []
        for (lab, p, info), c in zip(d.loops, cols):
            mag, ph, i = loop_tf(p, comp)
            ls = "-" if i["mode"] == "CCM" else "--"
            am.semilogx(F, mag, ls, color=c, lw=1.5, label=f"{lab} ({i['mode']})")
            ap.semilogx(F, 180 + ph, ls, color=c, lw=1.5)
            if np.isfinite(i["fc"]):
                am.plot(i["fc"], 0, "o", color=c, mec="white"); ap.plot(i["fc"], i["pm"], "o", color=c, mec="white")
            pms.append(i["pm"]); labs.append(lab); bar_c.append(c)
        for a in (am, ap):
            a.axvline(fs / 2, color="grey", ls="--", lw=0.8); a.grid(True, which="both", alpha=0.3)
        am.axhline(0, color="grey", lw=1); am.set_ylim(-60, 90); am.set_ylabel("Loop gain |T| (dB)")
        am.set_xlim(F[0], F[-1]); am.legend(fontsize=7, loc="lower left")
        am.tick_params(labelbottom=False)
        s = d.spec
        ap.axhline(s.pm_target, color="grey", ls=":", lw=1); ap.axhline(0, color="grey", lw=1)
        ap.set_ylim(-180, 200); ap.set_ylabel("Phase margin 180° + ∠T (°)"); ap.set_xlabel("Frequency (Hz)")
        y = np.arange(n)
        ab.barh(y, np.nan_to_num(pms), color=bar_c)
        for yi, pm, (lab, _, i) in zip(y, pms, d.loops):
            ab.text(max(np.nan_to_num(pm), 0) + 2, yi, f"{pm:.0f}°  fc {si(i['fc'], 'Hz')}", va="center", fontsize=7)
        ab.axvline(s.pm_target, color="grey", ls=":", lw=1); ab.axvline(s.pm_target_dcm, color="grey", ls=":", lw=0.6)
        ab.set_yticks(y, labs, fontsize=7); ab.invert_yaxis(); ab.set_xlim(0, 130)
        ab.set_xlabel("Phase margin at crossover (°)")
        rout = 38 / 800e-6
        fig.suptitle(f"RC1 {si(comp['rc1'], 'Ω')}, CC1 {si(comp['cc1'], 'F')}, CC2 {si(comp['cc2'], 'F')}  |  "
                     f"zero {si(1 / (2 * np.pi * comp['cc1'] * comp['rc1']), 'Hz')}, "
                     f"pole {si(1 / (2 * np.pi * comp['cc1'] * (rout + comp['rc1'])), 'Hz')}, "
                     f"HF pole {si(1 / (2 * np.pi * comp['cc2'] * comp['rc1']), 'Hz')}   "
                     f"(solid CCM, dashed DCM; dotted PM targets)", fontsize=9, x=0.07, ha="left")
        self.loop_canvas.draw_idle()

    def sortable(self, tree):
        """Click a column heading to sort by it; click again to reverse (▲ / ▼ in the heading)."""
        tree._sort = None                                      # (column, descending)
        for c in tree["columns"]:
            tree.heading(c, command=lambda c=c: self.sort_by(tree, c))

    def sort_by(self, tree, col):
        desc = tree._sort is not None and tree._sort[0] == col and not tree._sort[1]
        tree._sort = (col, desc)
        self.resort(tree)

    def resort(self, tree):
        if not getattr(tree, "_sort", None):
            return
        col, desc = tree._sort

        def key(iid):
            s = str(tree.set(iid, col))
            try:
                return (0, float(s.replace("–", "nan")), "")
            except ValueError:
                return (1, 0.0, s)
        for i, iid in enumerate(sorted(tree.get_children(""), key=key, reverse=desc)):
            tree.move(iid, "", i)
        for c in tree["columns"]:                              # arrow on the sorted column only
            tree.heading(c, text=c + ((" ▼" if desc else " ▲") if c == col else ""))

    def draw_schematic(self):
        if self.d is None or not hasattr(self, "sch_ax"):
            return
        schematic.draw(self.sch_ax, self.d, self.mosfet.get(), self.diode.get(), self.lmodel.get())
        self.sch_canvas.draw_idle()

    def full_report(self):
        """Design report + the simulated losses of the current results."""
        out = [self.d.report()]
        rows = [r for r in self.sim_rows.values() if r.get("p_l") is not None]
        if rows:
            out += ["", "Simulated losses [mW] (ngspice, V × I at each part; MOSFET incl. its gate path. The LM3478 "
                    "model's supply and gate drive are ideal: not in Pin)", "-" * 60]
            body = [self.simloss_row(r) for r in rows]
            out += text_table(["Vin V", "Vout V", "Iout mA", "Pout", "Pin", "η %", "IC+gate", "η incl. IC %", "L1",
                               "MOSFET", "Diode", "RSN", "other"], [b[:-1] for b in body], left=0)
            out.append("IC+gate (calculated, not simulated): LM3478 VIN × (2.7 mA + Qg × fs); "
                       "'η incl. IC' adds it to the simulated Pin, like the estimated table.")
            out += ["", "simulated with:"] + [f"  {b[0]} V → {b[1]} V, {b[2]} mA: {b[-1]}" for b in body]
        return "\n".join(out)

    def refresh_report(self):
        self.report.delete("1.0", "end"); self.report.insert("1.0", self.full_report())

    def simloss_row(self, r):
        mw = lambda k: (r.get(k) or np.nan) * 1e3
        v = self.d.values                    # IC supply + gate drive: ideal in the LM3478 model, so calculated
        vcc = r.get("vcc") or r["vin"]                     # the LM3478 VIN the case was simulated with
        ic = (vcc * (I_SUPPLY + v["qg"] * v["fs"])) * 1e3
        eff_ic = 100 * mw("pout") / (mw("pin") + ic)
        vals = [mw("pout"), mw("pin"), (r.get("eff") or np.nan), mw("p_l"), mw("p_q"), mw("p_d"), mw("p_rsn"), mw("p_other")]
        return ([f"{r['vin']:g}", f"{r['vout_set']:.1f}", f"{r['iout'] * 1e3:g}", f"{vals[0]:.0f}", f"{vals[1]:.0f}",
                 f"{vals[2]:.1f}", f"{ic:.1f}", f"{eff_ic:.1f}"] + [f"{x:.1f}" for x in vals[3:]] + [r.get("parts", "")])

    # ------------------------------------------------------------ simulation
    def run_sim(self):
        if self.config_path is None:          # results go into the configuration's folder
            messagebox.showinfo("Simulation", "Name the configuration first: its folder holds the simulation "
                                "results, their notes, the report and the JSON file.")
            if not self.save_config():
                return
        try:
            pts = self.read_points()
            tend = parse_si(self.tran.get()); jobs = int(self.jobs.get())
            assert tend >= booster_sim.LOAD_ON + 8e-3, "run length must be ≥ 14 ms (the load ramps in at 6-7 ms)"
        except Exception as e:
            messagebox.showerror("Simulation", str(e)); return
        self.sim_tree.delete(*self.sim_tree.get_children()); self.sim_rows.clear()
        self.simloss_tree.delete(*self.simloss_tree.get_children())
        self.run_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.stop_event.clear()
        stop = self.stop_event
        self.sim_status.configure(text=f"running {len(pts)} case(s), ~1-3 min each…")
        d, mos, dio, ind = self.d, self.mosfet.get(), self.diode.get(), self.lmodel.get()
        run_root = self.config_path.parent / "sims"

        def worker():
            try:
                booster_sim.run_points(d, pts, mosfet=mos, diode=dio, inductor=ind, tran_end=tend,
                                       window=(tend - 2e-3, tend), jobs=jobs,
                                       on_result=lambda r: self.sim_q.put(("result", r)),
                                       on_start=lambda n, p: self.sim_q.put(("start", (n, p))), stop=stop,
                                       run_root=run_root)
                self.sim_q.put(("done", None))
            except Exception as e:
                self.sim_q.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def stop_sim(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.sim_status.configure(text="stopping: killing ngspice, skipping cases not started…")

    def poll_sim(self):
        try:
            while True:
                kind, data = self.sim_q.get_nowait()
                if kind == "start":
                    n, (vin, vout, iout) = data
                    self.sim_status.configure(text=f"case {n}: Vin {vin:g} V, Vout {vout:g} V, Iout {si(iout, 'A')} running…")
                elif kind == "result":
                    self.add_sim_row(data)
                elif kind == "done":
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    run = next(iter(self.sim_rows.values()), {}).get("run_dir", "")
                    word = "stopped" if self.stop_event.is_set() else "done"
                    self.write_config_files()             # JSON (with the results) + report into the folder
                    self.sim_status.configure(text=f"{word}. Saved {self.config_path.name} + report; "
                                              f"simulation files: {run}")
                elif kind == "char":
                    self.apply_characterization(*data)
                elif kind == "char_error":
                    self.char_text.set(f"Model test failed: {data}")
                elif kind == "char_done":
                    self.char_finished()
                elif kind == "error":
                    self.run_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.sim_status.configure(text=f"error: {data}")
        except queue.Empty:
            pass
        except Exception as e:                  # never let one bad result stop the polling
            self.sim_status.configure(text=f"display error: {e}")
        finally:
            self.root.after(500, self.poll_sim)

    def add_sim_row(self, r):
        def f(k):
            x = r.get(k)
            if k == "status":
                return x
            if x is None or (isinstance(x, float) and not np.isfinite(x)):
                return "–"
            if k in ("vpp", "vpp_sw"):
                return f"{x * 1e3:.0f}"
            if k == "status":
                return x
            if isinstance(x, (int, np.integer)):
                return str(x)
            return f"{x:.3g}" if x is not None and np.isfinite(x) else "–"
        iid = self.sim_tree.insert("", "end", values=[f(c) for c, _, _ in SIM_COLS])
        self.sim_rows[iid] = r
        if (r.get("pk_spread") or 0) > 20 or (r.get("skipped") or 0) > 0 or r["status"] != "ok":
            self.sim_tree.item(iid, tags=("bad",))
        self.sim_tree.tag_configure("bad", foreground="#b3261e")
        if r["status"] not in ("stopped", "netlist error") and not self.sim_tree.selection():
            self.sim_tree.selection_set(iid)
        if r.get("p_l") is not None:
            self.simloss_tree.insert("", "end", values=self.simloss_row(r))
            self.resort(self.simloss_tree)
            self.refresh_report()

    def case_vectors(self, r):
        """Full-resolution vectors of a result (raw file, cached), or the saved snapshot when the raw is gone."""
        raw = r.get("raw")
        if raw and not Path(raw).exists() and self.config_path:     # folder moved: look in this config's sims/
            moved = self.config_path.parent / "sims" / Path(raw).parent.name / Path(raw).name
            raw = str(moved) if moved.exists() else raw
        if raw and raw in self.raw_cache:
            return self.raw_cache[raw]
        fs = self.d.values["fs"]
        try:
            vec = booster_sim.sweep.read_raw(raw)
            w0, w1 = (r.get("wave") or {}).get("window", (vec["time"][-1] - 2e-3, vec["time"][-1]))
            il = vec["i(vsense)"] if "i(vsense)" in vec else vec["vsense#branch"]
            pk = booster_sim.cycle_peaks(vec["time"], il, w0, w1, fs)
            out = dict(vec, pk=pk, pk_t=w0 + (np.arange(len(pk)) + 0.5) / fs, full=True, window=(w0, w1),
                       load_on=booster_sim.LOAD_ON)
            out["i(vsense)"] = il
            if len(self.raw_cache) > 6:
                self.raw_cache.pop(next(iter(self.raw_cache)))
            self.raw_cache[raw] = out
            return out
        except Exception:
            w = r.get("wave")
            if not w:
                return None
            t_stop = w["t_stop"]
            pk = np.array(w["pk"])
            return {"snapshot": True, "t_env": np.array(w["t"]), "vlo": np.array(w["vlo"]), "vhi": np.array(w["vhi"]),
                    "t_end": t_stop + np.array(w["t_end"]) * 1e-6, "v_end": np.array(w["v_end"]),
                    "il_end": np.array(w["il_end"]), "pk": pk,
                    "pk_t": w["window"][0] + (np.arange(len(pk)) + 0.5) / fs, "window": tuple(w["window"]),
                    "load_on": w["load_on"], "full": False}

    def show_case(self, _=None):
        rows = [self.sim_rows[i] for i in self.sim_tree.selection() if i in self.sim_rows]
        sigs = [(lab, key, unit) for lab, key, unit in SIG_DEFS if self.sig_vars[key].get()]
        fig = self.sim_fig; fig.clear()
        if not rows or not sigs:
            self.sim_canvas.draw_idle(); return
        data = [(r, self.case_vectors(r)) for r in rows]
        data = [(r, v) for r, v in data if v is not None]
        if not data:
            self.sim_status.configure(text="no waveform data for the selected case(s)"); self.sim_canvas.draw_idle(); return
        axes = fig.subplots(len(sigs), 1, sharex=True, squeeze=False)[:, 0]
        cols = matplotlib.colormaps["tab10"](np.arange(len(data)) % 10)
        missing = set()
        for ax, (lab, key, unit) in zip(axes, sigs):
            for (r, v), c in zip(data, cols):
                name = f"{r['vin']:g}→{r['vout_set']:.1f} V, {si(r['iout'], 'A')}"
                if key == "pk":
                    ax.plot(np.asarray(v["pk_t"]) * 1e3, v["pk"], ".", ms=2.5, color=c, label=name)
                elif not v["full"] and key == "v(out)":
                    ax.fill_between(v["t_env"] * 1e3, v["vlo"], v["vhi"], color=c, alpha=0.6, lw=0, label=name)
                    ax.plot(v["t_end"] * 1e3, v["v_end"], color=c, lw=0.8)
                elif not v["full"] and key == "i(vsense)":
                    ax.plot(v["t_end"] * 1e3, v["il_end"], color=c, lw=0.8, label=name)
                elif v["full"] and key in v:
                    ax.plot(v["time"] * 1e3, v[key], color=c, lw=0.6, label=name)
                else:
                    missing.add(lab)
            w0, w1 = data[0][1]["window"]; lo = data[0][1]["load_on"]
            ax.axvspan(lo * 1e3, lo * 1e3 + 1, color="#f0efec", zorder=0)
            ax.axvspan(w0 * 1e3, w1 * 1e3, color="#e7f1fb", zorder=0)
            ax.set_ylabel(f"{lab} ({unit})", fontsize=8); ax.grid(alpha=0.3)
            ax.ticklabel_format(axis="x", useOffset=False)
        axes[-1].set_xlabel("time (ms)   - grey: load ramp, blue: measured window")
        if len(data) > 1:
            axes[0].legend(fontsize=7, loc="upper right")
        r = data[0][0]
        eff, vpp = r.get("eff"), r.get("vpp_sw")
        title = (f"Vin {r['vin']:g} V, Vout {r['vout_set']:.1f} V set, Iout {si(r['iout'], 'A')}: "
                 f"η {eff if eff is not None else np.nan:.1f} %, ripple {(vpp if vpp is not None else np.nan) * 1e3:.0f} mV"
                 f" (switching), peak spread {r.get('pk_spread') or np.nan:.1f} %, skipped {r.get('skipped')}"
                 f"{'  - NOT SETTLED' if r['status'] == 'not settled' else ''}\nsimulated with: {r.get('parts', '')}")
        if len(data) > 1:
            title = f"{len(data)} cases overlaid (first: " + title.split(':', 1)[0] + ")"
        axes[0].set_title(title, fontsize=8.5, loc="left")
        if missing:
            self.sim_status.configure(text="raw file gone, snapshot only: no data for " + ", ".join(sorted(missing)))
        fig.tight_layout()
        self.sim_view("full" if any(v["full"] for _, v in data) else "window")

    def sim_view(self, which):
        axes = self.sim_fig.axes
        if not axes:
            return
        sel = [self.sim_rows[i] for i in self.sim_tree.selection() if i in self.sim_rows]
        v = self.case_vectors(sel[0]) if sel else None
        if v is None:
            return
        t_end = (v["time"][-1] if v["full"] else v["t_end"][-1])
        w0, w1 = v["window"]
        if which != "fit":
            x0, x1 = {"full": (0, t_end), "window": (w0, w1), "end": (t_end - 20e-6, t_end)}[which]
            axes[0].set_xlim(x0 * 1e3, x1 * 1e3)
        x0, x1 = axes[0].get_xlim()
        for ax in axes:                                         # y to the data inside the x range
            lo, hi = np.inf, -np.inf
            for ln in ax.get_lines():
                x, y = ln.get_xdata(), ln.get_ydata()
                m = (np.asarray(x) >= x0) & (np.asarray(x) <= x1)
                if m.any():
                    lo, hi = min(lo, np.nanmin(np.asarray(y)[m])), max(hi, np.nanmax(np.asarray(y)[m]))
            for coll in ax.collections:                         # snapshot envelopes
                for path in coll.get_paths():
                    vx, vy = path.vertices[:, 0], path.vertices[:, 1]
                    m = (vx >= x0) & (vx <= x1)
                    if m.any():
                        lo, hi = min(lo, vy[m].min()), max(hi, vy[m].max())
            if np.isfinite(lo) and np.isfinite(hi):
                pad = 0.05 * (hi - lo) or 0.05 * abs(hi) or 0.1
                ax.set_ylim(lo - pad, hi + pad)
        self.sim_canvas.draw_idle()

    # ------------------------------------------------------------ configurations
    def get_config(self):
        """Everything needed to restore the session: inputs, model texts and simulation results."""
        um = booster_sim.user_models()
        models = {role: {"name": box.get(), "lib": um[box.get()][0].stem,
                         "text": um[box.get()][0].read_text(errors="replace")}
                  for role, box in (("mosfet", self.mosfet), ("diode", self.diode), ("inductor", self.lmodel))
                  if box.get() in um}

        def clean(x):                                       # numpy / NaN -> plain JSON
            if isinstance(x, dict):
                return {k: clean(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [clean(v) for v in x]
            if isinstance(x, (np.integer,)):
                return int(x)
            if isinstance(x, (float, np.floating)):
                return float(x) if np.isfinite(x) else None
            return x
        return {
            "app": "LM3478 Booster Designer", "version": 1, "saved": datetime.datetime.now().isoformat(timespec="seconds"),
            "spec": {k: var.get() for k, (var, _) in self.spec_vars.items()},
            "parts": {k: var.get() for k, (var, _) in self.part_vars.items()},
            "auto_rsn": self.auto["rsn"].get(), "auto_rsl": self.auto["rsl"].get(), "auto_comp": self.auto_comp.get(),
            "comp": {k: var.get() for k, (var, _) in self.comp_vars.items()},
            "loss_parts": {k: var.get() for k, (var, _) in self.loss_vars.items()},
            "sim": {"points": self.points.get("1.0", "end").strip(), "mosfet": self.mosfet.get(),
                    "diode": self.diode.get(), "inductor": self.lmodel.get(),
                    "run_length": self.tran.get(), "jobs": self.jobs.get()},
            "characterization": clean(self.char_results),
            "loss_overrides": sorted(self.overrides),
            "part_overrides": sorted(self.part_typed),
            "models": models,
            "results": [clean(r) for r in self.sim_rows.values()],
        }

    @staticmethod
    def config_file(p):
        """configs/<name>/<name>.json for a chosen path (a file picked inside its own folder stays there)."""
        p = Path(p)
        stem = p.stem
        folder = p.parent if p.parent.name == stem else p.parent / stem
        return folder / f"{stem}.json"

    def save_config(self, _=None, ask=False):
        """Write <name>.json and report.txt into the configuration's folder (asks for a name the first time)."""
        if ask or self.config_path is None:
            p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", initialdir=str(CONFIG_DIR),
                                             initialfile=self.config_path.name if self.config_path else "booster.json",
                                             title="Configuration name (it gets its own folder)",
                                             filetypes=[("Designer configuration", "*.json")])
            if not p:
                return False
            self.set_config_path(self.config_file(p))
        self.write_config_files()
        self.sim_status.configure(text=f"saved {self.config_path}")
        return True

    def set_config_path(self, path):
        self.config_path = Path(path)
        self.root.title(f"LM3478 Booster Designer - {self.config_path.stem}  ({self.config_path.parent})")
        self.report_where.set(f"Written to {self.config_path.parent / 'report.txt'} on every save and after "
                              f"every simulation run.")

    def write_config_files(self):
        folder = self.config_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.get_config(), indent=1, allow_nan=False))
        (folder / "report.txt").write_text(self.full_report() + "\n")

    def load_config(self, _=None, path=None):
        p = path or filedialog.askopenfilename(parent=self.root, initialdir=str(CONFIG_DIR),
                                               filetypes=[("Designer configuration", "*.json")])
        if not p:
            return
        try:
            cfg = json.loads(Path(p).read_text())
            self.set_config_path(self.config_file(p))       # an old single-file config moves to its folder on save
            self.apply_config(cfg)
        except Exception as e:
            messagebox.showerror("Load configuration", f"{p}:\n{e}"); return
        self.sim_status.configure(text=f"loaded {p} ({len(cfg.get('results', []))} simulation results)")

    def apply_config(self, cfg):
        for k, (var, _) in self.spec_vars.items():
            if k in cfg["spec"]:
                var.set(cfg["spec"][k])
        for k, (var, _) in self.part_vars.items():
            var.set(cfg["parts"].get(k, ""))
        for k, (var, e) in self.comp_vars.items():
            e.configure(state="normal"); var.set(cfg["comp"].get(k, ""))
        for k, (var, _) in self.loss_vars.items():
            var.set(cfg.get("loss_parts", {}).get(k, ""))
        for k in AUTO_PARTS:
            self.auto[k].set(cfg.get(f"auto_{k}", True))
        self.auto_comp.set(cfg.get("auto_comp", True))
        self.part_typed = set(cfg.get("part_overrides", []))
        if "part_overrides" not in cfg and not self.auto_comp.get():    # older file: hand-set compensation
            self.part_typed |= set(self.comp_vars)
        self.toggle_comp()
        up = lambda n: n if n == booster_sim.IDEAL_L else n.upper()      # model names are upper case
        um = booster_sim.user_models()                      # restore pasted models that are not on this machine
        for role, m in cfg.get("models", {}).items():
            name = up(m["name"])
            if name not in um and name not in booster_sim.BUILTIN and name not in booster_sim.BUILTIN_L:
                booster_sim.save_user_library(m["text"], m.get("lib") or name)
        self.refresh_models()
        sim = cfg["sim"]
        self.mosfet.set(up(sim["mosfet"])); self.diode.set(up(sim["diode"])); self.tran.set(sim["run_length"])
        self.lmodel.set(up(sim.get("inductor", booster_sim.IDEAL_L)))
        self.char_results = {role: dict(r, model=up(r["model"])) for role, r in cfg.get("characterization", {}).items()}
        if "loss_overrides" in cfg:
            self.overrides = set(cfg["loss_overrides"])
        else:                                               # older file: whatever differs from the model was typed
            mv = self.model_values()
            vals = dict(cfg.get("loss_parts", {}), vd=cfg["spec"].get("vd", ""))
            self.overrides = set()
            for k, val in mv.items():
                try:
                    if abs(parse_si(vals.get(k, "")) - val) > 0.005 * abs(val):
                        self.overrides.add(k)
                except ValueError:
                    pass
        spec = self.read_spec()
        self.pts_key = (spec.vin, tuple(spec.vouts), spec.iout_min, spec.iout_max)   # keep the saved points
        self.recalc()
        self.points.delete("1.0", "end"); self.points.insert("1.0", sim["points"])
        self.points.edit_modified(False)                    # no auto job count: use the saved one
        self.jobs.set(sim["jobs"])
        self.refresh_estimates()
        self.sim_tree.delete(*self.sim_tree.get_children()); self.sim_rows.clear()
        self.simloss_tree.delete(*self.simloss_tree.get_children())
        for r in cfg.get("results", []):
            self.add_sim_row(r)
        self.draw_schematic()
        self.show_char_text(); self.draw_char_curves()
        self.show_case()


def snapshot(out_dir):
    """Headless check: write the report and the loop plot."""
    matplotlib.use("Agg")
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    root = tk.Tk(); root.withdraw()
    app = App(root)
    app.loop_fig.savefig(out / "loop.png", dpi=90)
    (out / "report.txt").write_text(app.d.report())
    root.destroy()
    print("wrote", out / "loop.png", out / "report.txt")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", metavar="DIR", help="save the loop plot and report without opening a window")
    a = ap.parse_args()
    if a.snapshot:
        snapshot(a.snapshot)
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()
