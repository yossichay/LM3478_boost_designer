"""AN-1286 (SNVA067D) compensation calculations for the LM3478 HV booster.

Every function is one AN-1286 equation, named by its number. Units: SI, rad/s for omegas.
usage: python3 an1286_calc.py            (prints the worked numbers for every corner)
"""
import numpy as np

# ---- LM3478 datasheet (SNVS085Y, typ)
VSL = 0.092          # internal slope-compensation ramp [V]
K_SL = 40e-6         # external slope comp factor [A] (datasheet eq. 5-6)
GM = 800e-6          # error amp transconductance [S]
AVOL = 38.0          # error amp voltage gain [V/V]
ROUT = AVOL / GM     # 47.5 kOhm
VFB = 1.26

# ---- power stage
L = 44.161e-6        # WE-PD 7447706470 (47 uH nominal, model value)
FS = 544e3 * (26.7e3 / 56e3) ** (1 / 1.26)   # R5 = 56k (datasheet RFA curve fit) -> ~302 kHz
RSN = 0.033         # design value (R1); 68 mOhm before this design
RSN_ORIG = 0.068
COUT = 22e-6 + 2.2e-6 + 2.2e-6 + 0.1e-6
RESR = 0.0108
R3, R8 = 792e3, 2000e3


def vout_from_r4(r4):
    r4p = 1 / (1 / r4 + 1 / R8)
    return VFB * (1 + R3 / r4p)

def eq21_D(vin, vout):               return (vout - vin) / vout
def eq22_RLOAD(vout, iout):          return vout / iout
def eq5_Se(vsl, rsn=RSN):            return vsl * FS / rsn          # [A/s]
def eq6_Sn(vin):                     return vin / L                 # [A/s]
def eq4_n(Se, Sn):                   return 1 + 2 * Se / Sn
def eq3_wcLe(Dp, n):                 return 2 * np.pi * FS * L / (Dp**3 * np.pi * n)
def par(*r):                         return 1 / sum(1 / x for x in r)
def eq2_ACM(Dp, wcLe, R, rsn=RSN):   return Dp * par(wcLe, R, R) / rsn
def eq7_Acm(Dp, R, rsn=RSN):         return Dp * R / (2 * rsn)
def eq8_wz1():                       return 1 / (COUT * RESR)
def eq9_wz2(R, vin, vout):           return R * (vin / vout) ** 2 / L
def eq10_wp1(R):                     return 2 / (COUT * R)
def eq11_Q(D, Dp, Se, Sn):           return 1 / (np.pi * (Dp * Se / Sn + 0.5 - D))
def eq13_AEA():                      return GM * ROUT
def eq14_wz3(Cc1, Rc1):              return 1 / (Cc1 * Rc1)
def eq15_wp2(Cc1):                   return 1 / (Cc1 * ROUT)
def eq16_AFB(vout):                  return VFB / vout
def eq18_ADC(Acm, AEA, AFB):         return Acm * AEA * AFB


def T_eq17(f, ADC, wz1, wz2, wp1, Q, wz3=None, wp2=None, Cc2=None, Rc1=None):
    """Loop gain, AN eq. (17). wz3/wp2 None -> uncompensated (Acomp = AEA, AN Fig. 5-1).
    Cc2 (optional, AN sec. 2 'additional pole') adds wp3 = 1/(Cc2*Rc1)."""
    s = 2j * np.pi * f
    ws2 = np.pi * FS
    T = ADC * (1 + s / wz1) * (1 - s / wz2) / ((1 + s / wp1) * (1 + s / (Q * ws2) + s**2 / ws2**2))
    if wz3 is not None:
        T = T * (1 + s / wz3) / (1 + s / wp2)
    if Cc2 is not None:
        T = T / (1 + s * Cc2 * Rc1)
    return T


def crossover(f, T):
    m = np.abs(T)
    k = np.where(m < 1)[0]
    if len(k) == 0:
        return np.nan, np.nan
    k = k[0]
    ph = np.degrees(np.unwrap(np.angle(T)))
    return f[k], 180 + ph[k]          # eq. (19), (20)


def corner(vin, vout, iout, vsl=VSL, rsn=RSN):
    D = eq21_D(vin, vout); Dp = 1 - D
    R = eq22_RLOAD(vout, iout)
    Se, Sn = eq5_Se(vsl, rsn), eq6_Sn(vin)
    n = eq4_n(Se, Sn)
    wcLe = eq3_wcLe(Dp, n)
    return dict(vin=vin, vout=vout, iout=iout, D=D, Dp=Dp, R=R, Se=Se, Sn=Sn, n=n, wcLe=wcLe,
                ACM=eq2_ACM(Dp, wcLe, R, rsn), Acm=eq7_Acm(Dp, R, rsn), wz1=eq8_wz1(), wz2=eq9_wz2(R, vin, vout),
                wp1=eq10_wp1(R), Q=eq11_Q(D, Dp, Se, Sn), AFB=eq16_AFB(vout))


if __name__ == "__main__":
    print(f"fs={FS/1e3:.1f}k ROUT={ROUT/1e3:.2f}k Cout={COUT*1e6:.2f}u")
    for r4 in (25e3, 17e3, 12.7e3):
        print(f"R4={r4/1e3:g}k Vout={vout_from_r4(r4):.2f}")
    for vsl, rsn, lab in ((VSL + K_SL * 100, RSN_ORIG, "as built: RSN 68m, RSL 100"),
                          (VSL + K_SL * 1.2e3, RSN, "design: RSN 33m, RSL 1.2k")):
        print("\n==", lab, f"VSL_tot={vsl*1e3:.0f} mV")
        for vout in (41.68, 60.46, 80.34):
            for vin in (11, 12, 13):
                c = corner(vin, vout, 0.1, vsl, rsn)
                print(f"Vin {vin} Vout {vout}: D {c['D']:.3f} R {c['R']:.0f} Se {c['Se']:.3g} Sn {c['Sn']:.3g} "
                      f"n {c['n']:.2f} Q {c['Q']:.2f} Acm7 {c['Acm']:.1f} ACM2 {c['ACM']:.1f} "
                      f"fz2 {c['wz2']/2/np.pi/1e3:.1f}k fp1 {c['wp1']/2/np.pi:.1f} fz1 {c['wz1']/2/np.pi/1e3:.0f}k "
                      f"ADC {20*np.log10(eq18_ADC(c['Acm'],eq13_AEA(),c['AFB'])):.1f}dB")
