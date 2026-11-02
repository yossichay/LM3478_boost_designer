# LM3478 booster designer

```bash
python3 booster_designer.py                 # GUI
python3 booster_designer.py --snapshot out  # no window: write out/loop.png and out/report.txt
python3 booster_design.py                   # print the design for the default spec
```

1. **Specification** – Vin, Vout settings, Iout min/max, fs and design targets.
   **LM3478 VIN** (empty = Vin) supplies the IC and its gate driver from a separate rail. The gate drive
   becomes min(LM3478 VIN, 7.2 V), which sets the MOSFET test conditions and the RDS(on) requirement.
   The estimate charges the IC (2.7 mA) and the gate charge (Qg·fs) to that rail. The simulation feeds
   XU1's VIN pin from its own source (Vcc_ic).
   "Design from spec" calculates every part (LM3478 datasheet SNVS085Y sec. 7.2) and tunes the
   compensation (AN-1286).
2. **Selected parts** – type the L (+DCR) and Cout (+ESR, the capacitance at DC bias) you actually picked
   and press Enter / Recalculate. RSN, RSL and RC1/CC1/CC2 are redesigned around them. Untick
   "Auto-tune" to set the compensation by hand. Typing an RSN or RSL value fixes it (its "auto" box clears).
   The other one and the compensation are then designed around it.
   Every value typed by hand here (parts and compensation) is shown red. RSN / RSL turn red when their
   "auto" box is clear. L and DCR filled from an inductor model are not red. "Design from spec" / "Use
   design values" clear the red, and the configuration remembers which values were typed. The Parts tab warns when the
   current limit is below the margin or Q exceeds the target.
3. **Simulation** – one ngspice case per line (`Vin, Vout, Iout`) on the board netlist
   `../Non-iso-Booster-TI-ngspice.cir` with the designed values. Results land in the configuration's
   folder, `configs/<name>/sims/sim_<time>/` (netlists, raw files, logs, `design.txt`). The configuration
   must have a name first (you are asked once).
   - Every case has `case_NNN_notes.txt`: operating point, simulation parameters (analysis line, time
     step, run length, load profile, measured window, initial condition, models and their libraries),
     the BOM, the netlist elements set for the case, the full netlist, and the results once it has run.
   - When the list is empty, it is filled with each Vout setting at Iout max and at Iout min. A list that
     is already there is never changed by spec edits; clear it to regenerate.
   - The number of parallel jobs follows the number of points, up to the CPU count − 2.
   - **Model…** next to MOSFET / Diode opens the library dialog. Load a `.lib` file or paste SPICE text,
     which may hold many models. Every top-level `.subckt` / `.model` is listed (models nested in a
     subcircuit are skipped). Pick one and "Save library and use…". The whole library is kept in
     `user_models/`, and all its usable models appear in the dropdown. The dropdowns also list every usable
     model in the board libraries, e.g. all 3-pin OptiMOS 150 V parts.
     - MOSFET: a 3-pin `.subckt` (D G S), a 4-pin `.subckt` (D G S B, body tied to source), or a `.model`
       of type VDMOS / NMOS.
     - Diode: a `.model` of type D, or a 2-pin `.subckt` (A K).
   - **Stop** kills the running ngspice processes and skips the cases that have not started.

**Simulation tab**:
- Tick the plots to show: Vout, inductor current, peak IL per cycle, switch node, COMP, gate, ISEN, Vin.
- Ctrl / Shift-click table rows to overlay cases.
- The time axes zoom and pan together (toolbar). "Full run", "Measured window", "Last 20 µs" and
  "Fit Y" set the view.
- Plots use the full-resolution raw file. When it has been deleted, they fall back to the saved snapshot.

**Schematic tab**: the board circuit (same layout and designators as `../gen_schematic.py`), annotated with
the designed values and the chosen MOSFET / diode.

**Losses tab** (also in the report):
- **Estimated**: an analytic loss table at each simulation point: L1 DCR and core loss; MOSFET conduction
  (+30 % hot), switching (DS (29)), Coss and gate; diode conduction and Cj; RSN; Cout ESR; FB divider; and
  the LM3478 supply.
  - Coss and Cj use the energy-equivalent capacitance 0 → that point's Vout, Co(er) = 2E/V², from the
    model's C(V) curve. It is larger than C at Vout because the capacitance is high at low voltage.
    A typed Co(er) / Cj scales the curve.
  - L core = (mean square winding voltage) / Rp, where Rp and Cp are what sits in parallel with DCR + L
    in the inductor model at fs (e.g. the Würth models' Rp / Cp). Cp is charged with the switch node,
    so it is added to the Coss term. Both are 0 with the ideal inductor.
- **Part values always come from the models.** The chosen MOSFET, diode and inductor models are tested
  in ngspice (`characterize.py`): at start-up, when a model is picked, and again when the design's test
  conditions (Vout, drive, full-load current, fs) move by more than 10 %. "Design from spec" keeps them.
  - A value typed by hand (RDS(on), Qg, Coss(er), edge, Cj, or the diode Vf on the left) is kept and
    shown **red**, and the line under the fields lists it next to the model's value.
  - "Reset to the model values" drops the typed values and re-tests the models.
  - The curves of each test are plotted under the fields (drag the divider to resize):
    - MOSFET: RDS(on) vs VGS, gate charge, Coss vs VDS, and the turn-on / turn-off edges.
    - Diode: IF vs VF, and Cj vs VR.
    - Inductor: L vs f, with fs and the self-resonance marked.
  - The tests measure:
    - MOSFET: RDS(on) at the real gate drive (min(Vin, 7.2 V)) and the full-load current; Co(er) and Qoss
      0 → Vout; Qg and the edge times from a clamped inductive switching test with R7 = 22 Ω and the LM3478
      driver.
    - Switching-edge field = t_eff = (E_on + E_off) / (Vout · I), the edge time that gives the measured
      switching energy in DS (29). E_off excludes the energy stored in Coss, which the Coss term already
      counts at turn-on.
    - Diode: Vf at Iout (written to the spec's Diode Vf) and Cj(er) at Vout.
    - Inductor: L at fs, DCR, self-resonance. These fill L and L DCR, and the simulation then uses the
      inductor model itself instead of ideal L + DCR.
    - The Würth WE-TI / WE-PD libraries are built in. Upload others with **Model…** next to "L model".
    - Typing L or DCR goes back to the ideal inductor.
- **Simulated**: measured in ngspice from V × I at each part, so it reflects the chosen MOSFET and diode
  models. The drain current is the RSN current minus the gate current through R7, because the gate charge
  returns through the source. The LM3478 model's driver and supply are ideal, so the IC current and the
  gate-drive power are not in the simulated Pin. "IC+gate (calc)" calculates them, and "η incl. IC" adds
  them to the simulated Pin, which is the efficiency to compare with the estimated table.

**File → Save / Load configuration** (Ctrl+S / Ctrl+O). Every configuration has its own folder:

```
configs/<name>/<name>.json     everything below
configs/<name>/report.txt      the Report tab, written on every save and after every simulation run
configs/<name>/sims/sim_<time>/  one folder per simulation run (netlists, raw, logs, case_NNN_notes.txt)
```

When a run finishes, the JSON and the report are saved automatically. Picking `<name>.json` anywhere under
`configs/` saves to `configs/<name>/`. An old single-file configuration moves into its folder on the next save.
If a folder is moved, the raw files are looked up again under its `sims/`. The JSON holds:
- the spec, the selected parts (with the RSN / compensation auto flags) and the compensation;
- the simulation points, run length and jobs;
- the MOSFET / diode choice, plus the text of any pasted model, which is re-created on another machine;
- the model test results and curves, and which loss values were typed by hand;
- the simulation results, each with a waveform snapshot (Vout envelope, last 20 µs of Vout and IL,
  peak IL per cycle). Reloaded plots work even if the raw files in `../results/` are deleted.

About 170 kB per simulated case.

| File | Contents |
| --- | --- |
| `booster_design.py` | Calculations: RFA, L and its ratings, RSN + RSL (current limit and Q ≤ target together), MOSFET / diode / Cin / Cout requirements, feedback divider, AN-1286 auto-compensation |
| `booster_sim.py` | Builds and runs the ngspice cases (uses `../sweep.py`), measures η, ripple, peak-current spread |
| `booster_designer.py` | Tk GUI |

## How values are chosen

- **L**: ripple `dIL/IL` at full load, worst corner, rounded up to E12. Isat target = the highest
  cycle-by-cycle current limit, so the limit protects the core.
- **RSN, RSL**: the largest RSN for which both hold at every corner:
  `VSENSE − D·(VSL·ratio + K·RSL) ≥ RSN · margin · Ipk` (DS (25)) and `Q ≤ q_target` (AN-1286 eq. 11).
  RSL is the external slope compensation (K = 40 µA); 100 Ω when none is needed.
  The margin defaults to 1.6, not the datasheet's 1.2. In simulation (60 V, 25 mA, 820 µH) a limit of
  1.15–1.36 × Ipk locked up in current limit after a load step: COMP railed, multi-cycle on-times,
  Vout hunting about 1 V. 1.6 × Ipk regulated cleanly. Ipk uses the lower of the entered efficiency
  and the tool's loss estimate.
- **Cout**: ripple target (half capacitive, half ESR); when you have not picked one, raised until
  RC1 ≥ 4.7 kΩ so the error amplifier keeps gain at crossover.
- **Compensation**: RC1 puts crossover at ≤ 0.95 · min(fz2/10, fs/10) at the binding CCM corner,
  CC1 puts the zero at fc_min/3 (raised for light-load margin, capped at 47 nF because COMP slews
  slowly with more), CC2 puts a pole at min(10·fc_max, fs/3).

## Simulation setup

- Cout is pre-charged to the setpoint, and the load ramps from 0 at 6–7 ms, after soft-start.
- Measurements cover the last 2 ms, plus the last 20 µs for switching ripple.
- C10 = Cout − 4.5 µF and carries the ESR. The board's ceramics (C2, C12, C11) stay as HF decoupling.
- R8 is removed, because RF2 is the whole bottom resistor.
- The time step is min(50 ns, Ts/50). The LM3478 model's clock is a SIN() source, and coarser
  steps miss clock edges: at 1 MHz a 50 ns step ran the converter at about 210 kHz. Expect about
  4–5 min per case at 1 MHz.
- The MOSFET and diode models are chosen in the GUI. Model names are shown in upper case (SPICE
  ignores case; older configurations with lower-case names load as before).
- A case whose Vout still moves over the measurement window is marked `not settled`; its η and
  ripple are not steady-state values. The plot title lists the part values that were simulated.
- `pk spread` is the spread of the per-cycle peak inductor current. A large spread, or skipped cycles,
  means subharmonic oscillation or instability.

The loop model is CCM AN-1286 eq. (17) with a DCM extension (see `../an1286/loop_explorer.py`).
It ignores inductor DCR and capacitor ESL.
