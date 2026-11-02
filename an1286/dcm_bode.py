"""Averaged CCM/DCM loop model (../bode_vc_vout.py) with the AN-1286 network, Vin = 11 V."""
import sys
sys.path.insert(0, "..")
import bode_vc_vout as b

b.Vg = 11.0
b.R6, b.C4, b.C3 = 8.2e3, 22e-9, 220e-12
b.R1 = 0.033
b.Vsl = (0.0646 + 38.5e-6 * 1.2e3) * 1.3      # LM3478 model ramp with R2 = RSL = 1.2k
b.main(sys.argv[1] if len(sys.argv) > 1 else "fig6-1_dcm.png")
