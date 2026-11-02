"""Booster schematic drawn with matplotlib, annotated with the designed values.

Same layout as ../gen_schematic.py (the board netlist Non-iso-Booster-TI-ngspice.cir); designators and node
names match the netlist, so they can be used in sweep configs and v(...) / i(...) expressions.

    draw(ax, design, mosfet="BSZ520N15NS3_VD", diode="MBRS3200T3G_TT0")
"""
import numpy as np
from matplotlib.patches import Arc, Circle, Polygon, Rectangle

from booster_design import si

INK, NODE, PIN, GREY, BOX = "#222222", "#c0392b", "#1f4e8c", "#666666", "#f4f6fa"
FS = {"des": 9, "val": 8.5, "small": 7.5, "pin": 8, "node": 7.5, "meter": 9, "title": 12}


class Sheet:
    """SVG-like drawing primitives on a matplotlib axes (y grows downwards, 1 unit ~ 1 px at 1320 x 930)."""

    def __init__(self, ax):
        self.ax = ax

    def wire(self, *pts):
        x, y = zip(*pts)
        self.ax.plot(x, y, color=INK, lw=1.4, solid_capstyle="round")

    def line(self, x1, y1, x2, y2, lw=1.4):
        self.ax.plot([x1, x2], [y1, y2], color=INK, lw=lw)

    def dot(self, x, y):
        self.ax.add_patch(Circle((x, y), 3.5, color=INK, zorder=3))

    def text(self, x, y, s, cls="val", anchor="start"):
        ha = {"start": "left", "middle": "center", "end": "right"}[anchor]
        color = {"node": NODE, "pin": PIN, "small": GREY}.get(cls, INK)
        weight = "bold" if cls in ("des", "pin", "meter", "title") else "normal"
        style = "italic" if cls == "node" else "normal"
        self.ax.text(x, y, s, fontsize=FS.get(cls, 8.5), ha=ha, va="baseline", color=color,
                     fontweight=weight, fontstyle=style)

    def label(self, x, y, name, val, anchor="start", extra=None):
        self.text(x, y, name, "des", anchor)
        self.text(x, y + 15, val, "val", anchor)
        if extra:
            self.text(x, y + 29, extra, "small", anchor)

    def node(self, x, y, s, anchor="start"):
        self.text(x, y, s, "node", anchor)

    def gnd(self, x, y):
        self.wire((x, y), (x, y + 10))
        for i, w in enumerate((12, 8, 4)):
            self.line(x - w, y + 10 + i * 4, x + w, y + 10 + i * 4)

    def res_v(self, x, y1, y2, name, val, side="right", extra=None):
        ym = (y1 + y2) / 2
        self.wire((x, y1), (x, ym - 20)); self.wire((x, ym + 20), (x, y2))
        self.ax.add_patch(Rectangle((x - 7, ym - 20), 14, 40, fill=False, ec=INK, lw=1.4))
        if side == "right":
            self.label(x + 13, ym - 4, name, val, extra=extra)
        else:
            self.label(x - 13, ym - 4, name, val, "end", extra=extra)

    def res_h(self, x1, x2, y, name, val, extra=None):
        xm = (x1 + x2) / 2
        self.wire((x1, y), (xm - 20, y)); self.wire((xm + 20, y), (x2, y))
        self.ax.add_patch(Rectangle((xm - 20, y - 7), 40, 14, fill=False, ec=INK, lw=1.4))
        self.text(xm, y - 26, name, "des", "middle"); self.text(xm, y - 12, val, "val", "middle")
        if extra:
            self.text(xm, y + 24, extra, "small", "middle")

    def cap_v(self, x, y1, y2, name, val, side="right"):
        ym = (y1 + y2) / 2
        self.wire((x, y1), (x, ym - 4)); self.wire((x, ym + 4), (x, y2))
        for yy in (ym - 4, ym + 4):
            self.line(x - 12, yy, x + 12, yy, lw=2)
        if side == "right":
            self.label(x + 15, ym - 4, name, val)
        else:
            self.label(x - 15, ym - 4, name, val, "end")

    def ind_h(self, x1, x2, y, name, val, extra):
        xm = (x1 + x2) / 2
        self.wire((x1, y), (xm - 30, y)); self.wire((xm + 30, y), (x2, y))
        for k in range(4):                                   # four humps (arcs above the wire)
            self.ax.add_patch(Arc((xm - 22.5 + 15 * k, y), 15, 15, theta1=180, theta2=360, ec=INK, lw=1.4))
        self.text(xm, y - 26, name, "des", "middle"); self.text(xm, y - 12, val, "val", "middle")
        self.text(xm, y + 22, extra, "small", "middle")

    def diode_h(self, x1, x2, y, name, val):
        xm = (x1 + x2) / 2
        self.wire((x1, y), (xm - 12, y)); self.wire((xm + 12, y), (x2, y))
        self.ax.add_patch(Polygon([(xm - 12, y - 11), (xm - 12, y + 11), (xm + 10, y)], fill=False, ec=INK, lw=1.4))
        self.line(xm + 11, y - 11, xm + 11, y + 11, lw=2)
        self.text(xm, y - 26, name, "des", "middle"); self.text(xm, y + 24, val, "small", "middle")

    def ammeter(self, cx, cy):
        self.ax.add_patch(Circle((cx, cy), 11, fc="white", ec=INK, lw=1.4, zorder=2))
        self.ax.text(cx, cy, "A", fontsize=FS["meter"], ha="center", va="center", fontweight="bold", zorder=3)

    def vsrc(self, cx, cy):
        self.ax.add_patch(Circle((cx, cy), 20, fill=False, ec=INK, lw=1.4))
        self.ax.text(cx, cy - 7, "+", fontsize=10, ha="center", va="center", fontweight="bold")
        self.ax.text(cx, cy + 9, "−", fontsize=10, ha="center", va="center", fontweight="bold")

    def isrc(self, cx, cy):
        self.ax.add_patch(Circle((cx, cy), 20, fill=False, ec=INK, lw=1.4))
        self.line(cx, cy - 11, cx, cy + 9)
        self.ax.add_patch(Polygon([(cx - 5, cy + 4), (cx + 5, cy + 4), (cx, cy + 12)], color=INK))

    def nmos(self, x, gy, gate_from_x):
        gx, cx = x - 22, x - 14
        self.wire((gate_from_x, gy), (gx, gy))
        self.line(gx, gy - 16, gx, gy + 16, lw=2)
        for y0 in (gy - 18, gy - 6, gy + 6):
            self.line(cx, y0, cx, y0 + 12, lw=2.5)
        self.wire((cx, gy - 12), (x, gy - 12), (x, gy - 25))
        self.wire((cx, gy + 12), (x, gy + 12), (x, gy + 25))
        self.wire((cx, gy), (x, gy), (x, gy + 12))
        self.ax.add_patch(Polygon([(cx + 1, gy), (cx + 9, gy - 4), (cx + 9, gy + 4)], color=INK))


def draw(ax, d, mosfet, diode, inductor=None):
    """The booster with the designed values (d = booster_design.Design)."""
    v, s = d.values, d.spec
    S = Sheet(ax)
    ax.clear()
    ax.set_xlim(0, 1320); ax.set_ylim(930, 0); ax.set_aspect("equal"); ax.axis("off")
    c = v["comp"]
    f = lambda x, u: si(x, u).replace(" ", "")
    RAIL = 120
    cer = min(4.4e-6, max(v["cout"] - 0.1e-6, 0.0))
    bulk = v["cout"] - 0.1e-6 - cer

    S.vsrc(60, 220); S.wire((60, RAIL), (60, 200)); S.wire((60, 240), (60, 270)); S.gnd(60, 270)
    S.label(88, 214, "V1", f"{s.vin:g} V", extra="sim: 3 ms ramp")
    S.node(62, RAIL - 8, "VS")
    S.wire((60, RAIL), (80, RAIL)); S.res_h(80, 140, RAIL, "RV1", "10m")
    S.wire((140, RAIL), (409, RAIL)); S.dot(160, RAIL); S.node(170, RAIL - 8, "N001 (VIN)")
    for x, n, val in ((190, "C5", "10µ"), (245, "C7", "10µ"), (355, "C9", "0.1µ")):
        S.dot(x, RAIL); S.cap_v(x, RAIL, 230, n, val); S.gnd(x, 230)

    S.ammeter(420, RAIL)
    S.text(420, RAIL - 18, "Vsense", "des", "middle"); S.text(420, RAIL + 28, "i(vsense) = I_L", "small", "middle")
    S.wire((431, RAIL), (460, RAIL))
    lm = "" if not inductor or inductor.startswith("(") else f"{inductor}, "
    S.ind_h(460, 560, RAIL, "L1 (XL1)", f(v["L"], "H"), f"{lm}DCR {f(v['dcr'], 'Ω')}")
    S.wire((560, RAIL), (650, RAIL)); S.dot(620, RAIL); S.node(613, RAIL + 44, "N002 (SW)", "end")

    S.wire((620, RAIL), (620, 205)); S.nmos(620, 230, 560)
    S.label(636, 222, "XM2", mosfet)
    S.res_h(490, 560, 230, "R7", "22")
    S.node(566, 252, "N004")
    S.wire((440, 450), (470, 450), (470, 230), (490, 230)); S.node(476, 345, "N003")

    S.wire((620, 255), (620, 300)); S.dot(620, 300); S.node(628, 296, "N008")
    S.res_v(620, 300, 390, "R1", f(v["rsn"], "Ω"), side="left", extra="RSN")
    S.gnd(620, 390)
    S.wire((620, 300), (690, 300), (690, 500), (640, 500))
    S.res_h(560, 640, 500, "R2", f(v["rsl"], "Ω"), extra="RSL (slope comp)")
    S.wire((440, 500), (560, 500)); S.dot(520, 500); S.node(458, 492, "N007")
    S.cap_v(520, 500, 570, "C1", "10p", side="left"); S.gnd(520, 570)

    S.diode_h(650, 720, RAIL, "D2", diode)
    S.wire((720, RAIL), (1150, RAIL))
    vset = [1.26 * (1 + s.rf1 / r2) for _, r2 in v["rf2"]]
    S.node(735, RAIL - 8, "out (" + " / ".join(f"{x:.1f}" for x in vset) + " V)")
    for x, n, val in ((770, "C10", f(bulk, "F") if bulk > 1e-9 else "n.f."), (830, "C11", "0.1µ"),
                      (890, "C12", f(cer / 2, "F")), (950, "C2", f(cer / 2, "F"))):
        S.dot(x, RAIL); S.cap_v(x, RAIL, 230, n, val); S.gnd(x, 230)
    S.text(770, 286, f"Cout = {f(v['cout'], 'F')} at bias; C10 ESR {f(v['esr'], 'Ω')}", "small")

    S.dot(1030, RAIL); S.wire((1030, RAIL), (1030, 154)); S.ammeter(1030, 165)
    S.label(1046, 162, "Vload", "i(vload)")
    S.wire((1030, 176), (1030, 210)); S.node(1037, 202, "outl"); S.isrc(1030, 230)
    S.wire((1030, 250), (1030, 270)); S.gnd(1030, 270)
    S.label(1057, 236, "I1", f"{f(s.iout_min, 'A')}…{f(s.iout_max, 'A')}", extra="load")

    S.res_v(1150, RAIL, 300, "R3", f(s.rf1, "Ω"), extra="RF1")
    S.dot(1150, 300); S.wire((1150, 300), (1230, 300))
    S.res_v(1150, 300, 400, "R4", " / ".join(f(r2, "Ω") for _, r2 in v["rf2"]), side="left", extra="RF2 per Vout")
    S.gnd(1150, 400)
    S.res_v(1230, 300, 400, "R8", "n.f.", extra="not fitted")
    S.gnd(1230, 400)
    S.wire((440, 610), (1090, 610), (1090, 300), (1150, 300)); S.node(1160, 292, "N006 (FB)")

    ax.add_patch(Rectangle((200, 420), 240, 260, fc=BOX, ec=INK, lw=1.4))
    S.text(320, 535, "XU1", "des", "middle"); S.text(320, 552, "LM3478", "val", "middle")
    for y, p in ((450, "VIN"), (520, "FA/SD")):
        S.text(208, y + 4, p, "pin")
    for y, p in ((450, "DR"), (500, "ISEN"), (610, "FB"), (650, "PGND")):
        S.text(432, y + 4, p, "pin", "end")
    for x, p in ((260, "COMP"), (390, "AGND")):
        S.text(x, 670, p, "pin", "middle")
    if s.vcc:                                   # LM3478 VIN from its own supply
        S.wire((200, 450), (160, 450)); S.node(60, 444, f"VCC_IC {s.vcc:g} V")
    else:
        S.wire((200, 450), (160, 450), (160, RAIL))
    S.wire((200, 520), (130, 520)); S.node(140, 512, "N005")
    S.res_v(130, 520, 610, "R5", f(v["rfa"], "Ω"), side="left", extra=f"fs {f(v['fs'], 'Hz')}")
    S.gnd(130, 610)
    S.wire((440, 650), (460, 650)); S.gnd(460, 650)
    S.wire((260, 680), (260, 720)); S.dot(260, 720); S.node(268, 714, "COMP")
    S.cap_v(260, 720, 800, "C3", f(c["cc2"], "F"), side="left"); S.gnd(260, 800)
    S.wire((260, 720), (330, 720))
    S.res_v(330, 720, 800, "R6", f(c["rc1"], "Ω")); S.node(338, 812, "P001")
    S.cap_v(330, 800, 870, "C4", f(c["cc1"], "F")); S.gnd(330, 870)
    S.wire((390, 680), (390, 700)); S.gnd(390, 700)

    notes = [("des", "Notes"),
             ("val", "Designators / node names = Non-iso-Booster-TI-ngspice.cir (use them in v(...) / i(...))."),
             ("val", f"Compensation (AN-1286): RC1 = R6, CC1 = C4, CC2 = C3.  Current sense: R1 → R2 / C1 → ISEN."),
             ("val", f"Current limit {v['ilim_min']:.2f}…{v['ilim_max']:.2f} A over D; short-circuit latch {v['isc']:.1f} A."),
             ("val", "C10 = Cout − 4.5 µF (bulk, with ESR); C2 + C12 shrink when Cout < 4.6 µF.  C8 (47 µ) not fitted."),
             ("val", "Ammeters (0 V sources): i(vsense) inductor, i(vload) load, i(v1) input.")]
    ax.add_patch(Rectangle((600, 700), 700, 160, fill=False, ec="#bbbbbb", lw=1))
    for i, (cls, txt) in enumerate(notes):
        S.text(616, 726 + i * 22, txt, cls)
    S.text(20, 34, f"LM3478 boost: {s.vin:g} V → " + ", ".join(f"{x:g}" for x in s.vouts)
           + f" V, {f(s.iout_min, 'A')}…{f(s.iout_max, 'A')}, fs {f(v['fs'], 'Hz')}", "title")
    S.text(20, 54, "Designed values. Red italic = node names, bold = designators.", "small")
