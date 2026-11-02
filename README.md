# Non-Isolated Booster — ngspice simulation

Transient simulation of the LM3478 boost converter from the TI WEBENCH design:
**11–13 V in → 100 mA out**. This is a port of the original LTspice netlist
to ngspice.

> **Note:** the WEBENCH title says 70 V out, but the feedback divider in the
> netlist sets Vout = 1.26 V × (1 + R3 / (R4 ∥ R8)) ≈ **51.7 V**. The simulation
> regulates at about 50 V. For 70 V, R3 needs to be about 1.08 MΩ.

## Files

| File | What it is |
|---|---|
| `Non-iso-Booster-TI-ngspice.cir` | **The ngspice netlist — this is what you run** |
| `.spiceinit` | ngspice startup settings (PSpice compatibility mode). Read automatically from the current directory |
| `Non-iso-Booster-TI-LTspice.net` | Original LTspice netlist (reference only) |
| `LM3478_TRANS.lib` | TI LM3478 controller model (PSpice) |
| `OptiMOS3_150V_Spice.lib` | Infineon MOSFET models, includes BSZ520N15NS3 (`_L0`, `_L1`) |
| `BSZ520N15NS3_vdmos.lib` | LTspice's VDMOS model of BSZ520N15NS3, as subcircuit `BSZ520N15NS3_VD`. **Default**, because it is ~10× faster |
| `MBRS3200T3G.lib` | onsemi MBRS3200T3G Schottky diode. D2 uses `MBRS3200T3G_TT0`, the vendor model with `TT=0`; the vendor's `TT=45n` fakes reverse recovery that trips the current limit |
| `rb068lam150.lib` | ROHM RB068LAM150 Schottky diode model (the original D2; still available for sweeps) |
| `WE-TI.lib` | Würth WE-TI inductor models, includes 7447221101 (100 µH) |
| `schematic.svg` | **Schematic with every designator and node name.** Open it in a browser or in VS Code |
| `gen_schematic.py` | Generates `schematic.svg`. Component positions are set by hand in the script and are not read from the netlist, so update it when the circuit changes |
| `sweep.py` | Runs parameter/part sweeps, summarizes the measurements and plots waveforms |
| `sweep_example.toml` | Example sweep configuration. Copy and edit it for your own sweeps |
| `results/` | Output folders from `sweep.py`, one per run |
| `booster.raw` | Waveform output, created by each run |
| `ngspice_run.log` | Log from the last batch run, if you redirected output to it |

## Prerequisites

- ngspice 42 or newer (`ngspice --version`). On Ubuntu: `sudo apt install ngspice`.
- **Always run ngspice from this folder.** Two things depend on it:
  - `.spiceinit` is only read from the current directory. Without it the TI and Infineon models fail to parse, with errors like `Undefined parameter [if]`.
  - The model files are included by relative path.

## Running the simulation

### Batch mode (no GUI, prints the measurements)

```bash
cd "/home/yossi/Shade/DK/Non-Isolated Booster/sim"
ngspice -b Non-iso-Booster-TI-ngspice.cir | tee ngspice_run.log
```

A full 20 ms run takes several minutes. When it finishes it prints the
measurements and writes `booster.raw`. A quicker way to check that things
still work is to temporarily shorten the run: change `.tran 50n 20m 0 50n` to
`.tran 50n 3m 0 50n`.

### Interactive mode (to look at waveforms)

```bash
ngspice Non-iso-Booster-TI-ngspice.cir
```

The `.control` block runs the simulation and prints the measurements, then
leaves you at the `ngspice 1 ->` prompt, where you can plot (see below).
Type `quit` to exit.

## Viewing waveforms

### 1. Plot inside ngspice (quickest)

At the `ngspice ->` prompt, after the run:

```
plot v(out)                          output voltage
plot i(vsense)                       inductor current
plot v(n002)                         switch node (MOSFET drain)
plot v(n004)                         MOSFET gate
plot v(comp)                         error-amp output / compensation node
plot v(out) v(n001)                  several traces on one plot
plot i(vsense) xlimit 15m 15.05m     zoom into a few switching cycles
plot v(out) xlimit 15m 20m ylimit 69 71
```

In the plot window, drag with the left mouse button to zoom into a region, and
click to show the coordinates. `display` lists every vector you can plot.

### 2. Open a saved result without re-simulating

`booster.raw` contains `v(out)`, `v(n001)`, `v(n002)`, `v(n004)`, `v(comp)`,
`i(vsense)` and `i(v1)`. To open it:

```bash
ngspice
ngspice 1 -> load booster.raw
ngspice 2 -> plot v(out)
```

To save more signals, add them to the `write` line in the `.control` block.
For example, `write booster.raw all` saves every node; the file is much
larger.

### 3. Export to CSV (for Python, Excel, etc.)

Add this line to the `.control` block after `run`, or type it at the prompt:

```
wrdata booster.csv v(out) i(vsense) v(n002)
```

Each signal gets its own time column, followed by its values. A simple way to
read the file in Python:

```python
import numpy as np, matplotlib.pyplot as plt
d = np.loadtxt("booster.csv")
t, vout, il = d[:, 0], d[:, 1], d[:, 3]
plt.plot(t * 1e3, vout); plt.xlabel("ms"); plt.ylabel("Vout [V]"); plt.show()
```

### 4. gnuplot (good-looking plots, PNG/SVG export)

If gnuplot is installed (`sudo apt install gnuplot`), run this at the ngspice
prompt:

```
gnuplot vout_plot v(out) xlimit 10m 20m
```

### Node names

| Node / vector | Meaning |
|---|---|
| `n001` | Input rail (after the source's 10 mΩ series resistance) |
| `n002` | Switch node (inductor → MOSFET drain → diode anode) |
| `n004` | MOSFET gate (after R7 = 22 Ω) |
| `n003` | LM3478 DR pin (driver output) |
| `n008` | MOSFET source / current-sense resistor R1 |
| `n006` | Feedback (FB) pin |
| `comp` | Compensation node (COMP pin) |
| `out` | Output, ≈51.7 V as set by R3/R4/R8 (see the note at the top) |
| `i(vsense)` | Inductor current (from a 0 V ammeter in series with L1) |
| `i(v1)` | Input source current (negative = current flowing out of the source) |

## Measurements

The `.control` block reproduces the LTspice `.meas` lines. All averages, mins
and maxes are taken over 15–20 ms, after the load step at 10 ms:

| Name | Meaning |
|---|---|
| `pin`, `pout` | Average input and output power |
| `efficiency` | `100 * pout / pin` in % |
| `vout_max`, `vout_min`, `vout_ripple` | Output voltage extremes and peak-to-peak ripple |
| `il_max`, `il_min`, `il_ripple` | Inductor current extremes and peak-to-peak ripple |

`pout` is measured as `v(out) * i(vload)`. `Vload` is a 0 V ammeter in series
with the load, so the result is correct for any load current.

## Changing the circuit

- **Input voltage and load** are `.param`s near the top of the netlist: `.param vin=12 iload=0.1`.
  - `V1 … PULSE(0 {vin} 0 3m)` ramps the input up to `vin` over 3 ms.
  - `I1 … PULSE(0 {iload} 10m 1u)` steps the load from 0 to `iload` at 10 ms.
- **Run length / resolution:** `.tran <step> <stop> 0 <maxstep>`. **Keep `maxstep` ≤ 50 ns.** With 200 ns, 500 ns, 1 µs or no limit, the converter never started with the `_L1` or `_VD` MOSFET. The likely cause is that the LM3478 model makes its clock with `SIN(2π·f·TIME)` in a behavioural source, and large steps skip the clock edges.
- **MOSFET model:** all of these are pin-compatible in `XM2`:

  | Model | 20 ms run time | Notes |
  |---|---|---|
  | `BSZ520N15NS3_VD` (default) | ~4 min | LTspice's VDMOS. Efficiency, ripple and inductor current within ~5% of `_L1` |
  | `BSZ520N15NS3_L0` | ~9 min | Infineon's simple model. Efficiency matches, but ripple is far off (inductor ripple 0.21 A vs 0.86 A). Not recommended |
  | `BSZ520N15NS3_L1` | ~35 min | Infineon's detailed model: temperature effects, package inductances. Use it for final checks |

  `BSZ520N15NS3` (5 pins) adds thermal nodes (`Tj`, `Tcase`).

## Parameter and part sweeps (`sweep.py`)

`sweep.py` runs many variants of the netlist in parallel. For each sweep it
collects the measurements into one table and plots chosen waveforms from all
cases on top of each other. It needs Python 3.11+ with numpy and matplotlib.

```bash
python3 sweep.py sweep_example.toml               # run it
python3 sweep.py sweep_example.toml --dry-run     # just list the cases and write the netlists
python3 sweep.py sweep_example.toml -j 4          # limit to 4 parallel ngspice processes
python3 sweep.py --replot results/<run_folder>    # redo the plots after editing [plot] in run.json
```

While it runs, it prints a progress line every 30 s (change this with `--progress SEC`):
`[12m30s] 000: 6.10ms  31% | 001: 5.95ms  30% ...  ETA ~27m`. The ETA is
optimistic early on, because the simulation slows down once the converter
starts switching and again after the 10 ms load step. Each case's
`case_NNN.log` also grows live, so you can `tail -f` it.

A 20 ms case takes about 4 minutes with the default VDMOS MOSFET, or about 35 minutes with `BSZ520N15NS3_L1`, and cases run in parallel (by
default, number of CPUs − 2 at a time). For quick experiments, shorten `tran`
in the config and move the `from`/`to` window to match.

### Finding designators

To find the name of a part you want to sweep, use `schematic.svg`, or list the
netlist from the terminal:

```bash
python3 sweep.py --list               # every part grouped by function, then each node and what connects to it
python3 sweep.py --list | grep -i n006   # e.g. what is on the FB node
```

### Config file

Copy `sweep_example.toml` and edit it. Quote SPICE values that have a suffix
(`"20k"`, `"2.2u"`). Bare numbers like `12` or `0.05` don't need quotes. The
sections are:

- **`[grid.params]`** — values for netlist `.param`s (`vin`, `iload`, or any you add). Every combination is run. Only names defined by a `.param` line are accepted; parts like `R4` go under `[grid.elements]`.
- **`[grid.elements]`** — alternative values or parts for named elements:
  - A plain value replaces the **last token** of that line: the value of an R/C/L, the model of a D, or the subcircuit of an X.
    `R1 = ["50m", "68m"]`, `XM2 = ["BSZ520N15NS3_L1", "BSC520N15NS3_L1"]`, `D2 = ["DRB068LAM150", "MYDIODE"]`.
  - A string starting with the element name replaces the **whole line**, e.g. when the pin count changes:
    `XM2 = ["XM2 N002 N004 N008 BSZ900N15NS3_L1"]`.
- **`[[case]]`** (optional, repeatable) — named one-off variants with their own `params` / `elements`.
  Each case is combined with the whole grid.
- **`includes`** — extra model files, for parts that aren't in the four libraries already included.
- **`tran`** — replaces the netlist's `.tran` line.
- **`[load]`** (optional) — a stepped load-current profile that replaces `I1`:
  `steps = [["0", "0.1m"], ["15m", "10m"], ["18m", "40m"], ["20m", "100m"]]`, `rise = "1u"`.
  Each current holds until the next step. A `[[case]]` can set its own `load = { steps = [...] }`.
  While `[load]` is set, the `iload` parameter is not used, and sweeping it is rejected.
- **`[measure]`** — the measurement windows and the measurements:
  - `windows = [{ name = "10mA", from = "17m", to = "17.9m" }, ...]`: every measurement and derived value is computed in each window. In ngspice the results are named `<meas>_<window>`, e.g. `efficiency_10ma`. For a single window, `from`/`to` can be used instead.
  - `func` is one of `avg|min|max|pp|rms|integ`, applied to any ngspice expression `expr`.
  - `raw` passes a full ngspice `meas` spec, e.g. `raw = "when v(out)=69 rise=1"`.
  - `derived` entries are computed from the other measurements, e.g. efficiency.
- **`[plot]`** — `signals` to plot and time `windows`. You get one subplot per window, with all cases overlaid. `save` lists extra signals to keep in the `.raw` files without plotting them.

The script's own measurements replace the netlist's `.control` block, so the
netlist file itself is never modified.

### Output (`results/<name>_<timestamp>/`)

| File | Contents |
|---|---|
| `summary.csv` / `summary.md` | One row per case and measurement window: swept values, every measurement, status, runtime |
| `summary.png` | A bar chart per measurement, comparing all cases, with one bar colour per window |
| `wave_<signal>.png` | Overlaid waveforms for each plotted signal |
| `case_NNN.cir` | The exact netlist that was run. You can open it in ngspice by hand |
| `case_NNN.log` | ngspice output. Check here when `status` is not `ok` |
| `case_NNN.raw` | Waveforms. Open with `ngspice` → `load case_NNN.raw` → `plot …` |
| `run.json` | Config and results, used by `--replot` |

`status` is `ok`, `failed@<time>` (timestep too small at that time),
`timeout` or `no-results`.

## Differences from the LTspice netlist

ngspice doesn't support some LTspice features, so the port does the following:

| LTspice | ngspice port |
|---|---|
| `Rser=` / `Lser=` / `Cpar=` on V, L and C | Written out as separate R, L and C elements |
| `L1` with Rser/Rpar/Cpar | Würth `TI_1016_7447221101_100u` subcircuit from `WE-TI.lib` |
| `M2 … BSZ520N15NS3` (LTspice built-in VDMOS) | `XM2 … BSZ520N15NS3_VD`, the same VDMOS model wrapped as a subcircuit (or the Infineon `_L0` / `_L1`) |
| `D2 … RB068LAM150` | `DRB068LAM150` (the model name in the ROHM file) |
| `.lib file` (no section name) | `.include file` |
| `.tran 20m` | `.tran 50n 20m 0 50n` (ngspice needs an explicit step) |
| `.meas … AVG <expression>` | `let` + `meas` in a `.control` block (ngspice `.meas` only takes plain vectors) |
| `I(L1)` | `i(vsense)` (0 V ammeter, because L1 is now a subcircuit) |
| `µ` unit prefix | `u` |
| — | `.options method=gear` (see Troubleshooting) |

## Troubleshooting

- **`Undefined parameter [if]` / `[temp]`, or `model type mismatch`**:
  `.spiceinit` wasn't loaded. Run ngspice from this folder, or put
  `set ngbehavior=psa` in `~/.spiceinit`.
- **`Timestep too small … trouble with node "xm2.g"`**: the gate-drive
  network rings with the default trapezoidal integration once switching starts
  (around 0.7 ms). `method=gear` on the `.options` line fixes this. Keep it.
- **`unrecognized parameter (tikf) - ignored`**: harmless. It is a ROHM
  temperature coefficient that ngspice's diode model doesn't support.
- **`plot` does nothing in batch mode**: expected. `-b` has no graphics, so
  use interactive mode or open `booster.raw` (see above).
- **The run is slow**: the LM3478 model contains a lot of behavioural logic,
  and the step is limited to 50 ns. Shorten the stop time for quick checks.
