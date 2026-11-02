"""Generate schematic.svg for the ngspice booster netlist (hand-placed layout)."""
import sys

W, H = 1320, 930
out = []
INK = "#222"
NODE = "#c0392b"


def add(s):
    out.append(s)


def wire(*pts):
    d = " ".join(f"{x},{y}" for x, y in pts)
    add(f'<polyline points="{d}" fill="none" stroke="{INK}" stroke-width="2"/>')


def dot(x, y):
    add(f'<circle cx="{x}" cy="{y}" r="4" fill="{INK}"/>')


def text(x, y, s, cls="val", anchor="start"):
    add(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{s}</text>')


def label(x, y, name, val, anchor="start", extra=None):
    text(x, y, name, "des", anchor)
    text(x, y + 15, val, "val", anchor)
    if extra:
        text(x, y + 29, extra, "small", anchor)


def node(x, y, s, anchor="start"):
    text(x, y, s, "node", anchor)


def gnd(x, y):
    wire((x, y), (x, y + 10))
    for i, w in enumerate((12, 8, 4)):
        add(f'<line x1="{x - w}" y1="{y + 10 + i * 4}" x2="{x + w}" y2="{y + 10 + i * 4}" '
            f'stroke="{INK}" stroke-width="2"/>')


def res_v(x, y1, y2, name, val, side="right", extra=None):
    ym = (y1 + y2) / 2
    wire((x, y1), (x, ym - 20))
    wire((x, ym + 20), (x, y2))
    add(f'<rect x="{x - 7}" y="{ym - 20}" width="14" height="40" fill="none" stroke="{INK}" stroke-width="2"/>')
    if side == "right":
        label(x + 13, ym - 4, name, val, extra=extra)
    else:
        label(x - 13, ym - 4, name, val, "end", extra=extra)


def res_h(x1, x2, y, name, val, extra=None):
    xm = (x1 + x2) / 2
    wire((x1, y), (xm - 20, y))
    wire((xm + 20, y), (x2, y))
    add(f'<rect x="{xm - 20}" y="{y - 7}" width="40" height="14" fill="none" stroke="{INK}" stroke-width="2"/>')
    text(xm, y - 26, name, "des", "middle")
    text(xm, y - 12, val, "val", "middle")
    if extra:
        text(xm, y + 24, extra, "small", "middle")


def cap_v(x, y1, y2, name, val, side="right"):
    ym = (y1 + y2) / 2
    wire((x, y1), (x, ym - 4))
    wire((x, ym + 4), (x, y2))
    for yy in (ym - 4, ym + 4):
        add(f'<line x1="{x - 12}" y1="{yy}" x2="{x + 12}" y2="{yy}" stroke="{INK}" stroke-width="2.5"/>')
    if side == "right":
        label(x + 15, ym - 4, name, val)
    else:
        label(x - 15, ym - 4, name, val, "end")


def ind_h(x1, x2, y, name, val, extra):
    xm = (x1 + x2) / 2
    wire((x1, y), (xm - 30, y))
    wire((xm + 30, y), (x2, y))
    add(f'<path d="M{xm - 30},{y} a7.5,7.5 0 0 1 15,0 a7.5,7.5 0 0 1 15,0 a7.5,7.5 0 0 1 15,0 '
        f'a7.5,7.5 0 0 1 15,0" fill="none" stroke="{INK}" stroke-width="2"/>')
    text(xm, y - 26, name, "des", "middle")
    text(xm, y - 12, val, "val", "middle")
    text(xm, y + 22, extra, "small", "middle")


def diode_h(x1, x2, y, name, val):
    xm = (x1 + x2) / 2
    wire((x1, y), (xm - 12, y))
    wire((xm + 12, y), (x2, y))
    add(f'<polygon points="{xm - 12},{y - 11} {xm - 12},{y + 11} {xm + 10},{y}" fill="none" '
        f'stroke="{INK}" stroke-width="2"/>')
    add(f'<line x1="{xm + 11}" y1="{y - 11}" x2="{xm + 11}" y2="{y + 11}" stroke="{INK}" stroke-width="2.5"/>')
    text(xm, y - 26, name, "des", "middle")
    text(xm, y - 15, "", "val", "middle")
    text(xm, y + 24, val, "small", "middle")


def ammeter(cx, cy, horizontal):
    add(f'<circle cx="{cx}" cy="{cy}" r="11" fill="var(--bg)" stroke="{INK}" stroke-width="2"/>')
    text(cx, cy + 4, "A", "meter", "middle")


def vsrc(cx, cy):
    add(f'<circle cx="{cx}" cy="{cy}" r="20" fill="none" stroke="{INK}" stroke-width="2"/>')
    text(cx, cy - 4, "+", "meter", "middle")
    text(cx, cy + 14, "−", "meter", "middle")


def isrc(cx, cy):
    add(f'<circle cx="{cx}" cy="{cy}" r="20" fill="none" stroke="{INK}" stroke-width="2"/>')
    add(f'<line x1="{cx}" y1="{cy - 11}" x2="{cx}" y2="{cy + 9}" stroke="{INK}" stroke-width="2"/>')
    add(f'<polygon points="{cx - 5},{cy + 4} {cx + 5},{cy + 4} {cx},{cy + 12}" fill="{INK}"/>')


def nmos(x, gy, gate_from_x):
    # drain at (x, gy-25), source at (x, gy+25); gate lead from the left
    gx, cx = x - 22, x - 14
    wire((gate_from_x, gy), (gx, gy))
    add(f'<line x1="{gx}" y1="{gy - 16}" x2="{gx}" y2="{gy + 16}" stroke="{INK}" stroke-width="2.5"/>')
    for y0 in (gy - 18, gy - 6, gy + 6):
        add(f'<line x1="{cx}" y1="{y0}" x2="{cx}" y2="{y0 + 12}" stroke="{INK}" stroke-width="3"/>')
    wire((cx, gy - 12), (x, gy - 12), (x, gy - 25))
    wire((cx, gy + 12), (x, gy + 12), (x, gy + 25))
    wire((cx, gy), (x, gy), (x, gy + 12))
    add(f'<polygon points="{cx + 1},{gy} {cx + 9},{gy - 4} {cx + 9},{gy + 4}" fill="{INK}"/>')


# ------------------------------------------------------------ layout
RAIL = 120

# input source + RV1
vsrc(60, 220)
wire((60, RAIL), (60, 200))
wire((60, 240), (60, 270))
gnd(60, 270)
label(88, 214, "V1", "0→{vin}", extra="3 ms ramp")
node(62, RAIL - 8, "VS")
wire((60, RAIL), (80, RAIL))
res_h(80, 140, RAIL, "RV1", "10m")

# N001 rail + input caps
wire((140, RAIL), (409, RAIL))
dot(160, RAIL)
node(170, RAIL - 8, "N001 (VIN)")
for x, n, v in ((190, "C5", "10µ"), (245, "C7", "10µ"), (300, "C8*", "47µ"), (355, "C9", "0.1µ")):
    dot(x, RAIL)
    cap_v(x, RAIL, 230, n, v)
    gnd(x, 230)

# Vsense, L1
ammeter(420, RAIL, True)
text(420, RAIL - 18, "Vsense", "des", "middle")
text(420, RAIL + 28, "i(vsense) = I_L", "small", "middle")
wire((431, RAIL), (460, RAIL))
ind_h(460, 560, RAIL, "XL1", "100µH", "WE 7447221101")
wire((560, RAIL), (650, RAIL))
dot(620, RAIL)
node(613, RAIL + 44, "N002 (SW)", "end")

# MOSFET + gate resistor
wire((620, RAIL), (620, 205))
nmos(620, 230, 560)
label(636, 222, "XM2", "BSZ520N15NS3_VD")
res_h(490, 560, 230, "R7", "22")
node(566, 252, "N004")
wire((440, 450), (470, 450), (470, 230), (490, 230))
node(476, 345, "N003")

# current sense: R1, R2, C1
wire((620, 255), (620, 300))
dot(620, 300)
node(628, 296, "N008")
res_v(620, 300, 390, "R1", "50m", side="left", extra="Rsense")
gnd(620, 390)
wire((620, 300), (690, 300), (690, 500), (640, 500))
res_h(560, 640, 500, "R2", "100")
wire((440, 500), (560, 500))
dot(520, 500)
node(458, 492, "N007")
cap_v(520, 500, 570, "C1", "10p", side="left")
gnd(520, 570)

# diode + output rail + output caps
diode_h(650, 720, RAIL, "D2", "MBRS3200T3G_TT0")
wire((720, RAIL), (1150, RAIL))
node(735, RAIL - 8, "out (≈51.7 V)")
for x, n, v in ((770, "C10*", "22µ"), (830, "C11", "0.1µ"), (890, "C12", "2.2µ"), (950, "C2", "2.2µ")):
    dot(x, RAIL)
    cap_v(x, RAIL, 230, n, v)
    gnd(x, 230)

# load: Vload ammeter + I1
dot(1030, RAIL)
wire((1030, RAIL), (1030, 154))
ammeter(1030, 165, False)
label(1046, 162, "Vload", "i(vload)")
wire((1030, 176), (1030, 210))
node(1037, 202, "outl")
isrc(1030, 230)
wire((1030, 250), (1030, 270))
gnd(1030, 270)
label(1057, 236, "I1", "0→{iload}", extra="step @ 10 ms")

# feedback divider
res_v(1150, RAIL, 300, "R3", "792k")
dot(1150, 300)
wire((1150, 300), (1230, 300))
res_v(1150, 300, 400, "R4", "20k")
gnd(1150, 400)
res_v(1230, 300, 400, "R8", "2000k")
gnd(1230, 400)
wire((440, 610), (1090, 610), (1090, 300), (1150, 300))
node(1160, 292, "N006 (FB)")

# controller
add('<rect x="200" y="420" width="240" height="260" rx="6" fill="var(--box)" stroke="#222" stroke-width="2"/>')
text(320, 535, "XU1", "des", "middle")
text(320, 552, "LM3478", "val", "middle")
text(320, 567, "LM3478_TRANS, SS=0.0041", "small", "middle")
for y, p in ((450, "VIN"), (520, "FA/SD")):
    text(208, y + 4, p, "pin")
for y, p in ((450, "DR"), (500, "ISEN"), (610, "FB"), (650, "PGND")):
    text(432, y + 4, p, "pin", "end")
for x, p in ((260, "COMP"), (390, "AGND")):
    text(x, 670, p, "pin", "middle")

wire((200, 450), (160, 450), (160, RAIL))
wire((200, 520), (130, 520))
node(140, 512, "N005")
res_v(130, 520, 610, "R5", "26.7k", side="left", extra="sets fsw")
gnd(130, 610)
wire((440, 650), (460, 650))
gnd(460, 650)

wire((260, 680), (260, 720))
dot(260, 720)
node(268, 714, "COMP")
cap_v(260, 720, 800, "C3", "180p", side="left")
gnd(260, 800)
wire((260, 720), (330, 720))
res_v(330, 720, 800, "R6", "143k")
node(338, 812, "P001")
cap_v(330, 800, 870, "C4", "680p")
gnd(330, 870)
wire((390, 680), (390, 700))
gnd(390, 700)

# notes
notes = [
    ("des", "Notes"),
    ("val", "Designators and node names match Non-iso-Booster-TI-ngspice.cir:"),
    ("val", "use them in sweep configs, e.g. R1 = [\"50m\", \"68m\"], and in v(...) / i(...)."),
    ("val", "* C8  = 47µ + RC8 0.38 Ω (ESR) + LC8 200p (ESL); CC8p 10p in parallel"),
    ("val", "* C10 = 22µ + RC10 10.8 mΩ (ESR) + LC10 4.313n (ESL); CC10p 10p in parallel"),
    ("val", "Ammeters (0 V sources): i(vsense) inductor, i(vload) load, i(v1) input"),
    ("val", "Sweepable .params: vin, iload"),
    ("val", "Vout = 1.26 V × (1 + R3 / (R4 ∥ R8)) ≈ 51.7 V (R3 ≈ 1.08M for 70 V);  fsw set by R5;  compensation R6, C4, C3"),
    ("val", "Current sense: R1 → R2 / C1 filter → ISEN"),
]
add('<rect x="600" y="690" width="700" height="215" rx="6" fill="none" stroke="#bbb"/>')
for i, (cls, s) in enumerate(notes):
    text(616, 716 + i * 22, s, cls)
add(f'<text x="20" y="34" class="title">Non-isolated booster (LM3478): 11–13 V → ≈51.7 V @ 0.1 A (WEBENCH title says 70 V)</text>')
add(f'<text x="20" y="54" class="small">ngspice netlist: Non-iso-Booster-TI-ngspice.cir.  '
    f'Red italic = node names.  Bold = designators.</text>')

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}"
     font-family="DejaVu Sans, Arial, sans-serif">
<style>
  svg {{ --bg: #ffffff; --box: #f4f6fa; }}
  .title {{ font-size: 18px; font-weight: bold; fill: #111; }}
  .des   {{ font-size: 13px; font-weight: bold; fill: #111; }}
  .val   {{ font-size: 12px; fill: #333; }}
  .small {{ font-size: 10.5px; fill: #666; }}
  .pin   {{ font-size: 11px; fill: #1f4e8c; font-weight: bold; }}
  .node  {{ font-size: 11px; fill: {NODE}; font-style: italic; }}
  .meter {{ font-size: 13px; font-weight: bold; fill: #111; }}
</style>
<rect width="100%" height="100%" fill="#ffffff"/>
{chr(10).join(out)}
</svg>
'''
open(sys.argv[1], "w").write(svg)
