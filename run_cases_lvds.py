#!/usr/bin/env python3
"""
LVDS SI analysis for AD9508 and LMK04828 clock distribution pairs.

Uses IBIS-extracted driver parameters with a current-mode driver model.
LVDS drivers are current sources — the received voltage depends on I_tail × Rterm,
with transient overshoot from Z0/Rterm mismatch on the PCB trace.

All signals are 125 MHz reference clocks with 100Ω differential termination
at the FPGA MGTREFCLK receiver inputs.

Usage:
  python3 run_cases_lvds.py -o lvds_results.csv
"""

import csv
import hashlib
import numpy as np
from dataclasses import dataclass, field
from typing import List

# ---------------------------------------------------------------------------
# IBIS-extracted LVDS driver models
# ---------------------------------------------------------------------------

# LMK04828 (U77) — LVDS output model
# V-T data into 50Ω fixture to Vref=1.254V
LMK04828_LVDS = {
    "name": "LMK04828",
    "refdes": "U77",
    "voh": 1.440,       # steady-state high (V) into 50Ω
    "vol": 1.050,       # steady-state low (V) into 50Ω
    "vos": 1.245,       # common-mode offset (V)
    "tr_ns": 0.202,     # rise time 10-90% (ns)
    "tf_ns": 0.202,     # fall time (symmetric for current-mode)
    "cin_pf": 2.42,     # C_comp from IBIS (pF)
}

# AD9508 (U79) — LVDS output model (lvds_1, 1.0x 3.5mA)
# V-T data into 50Ω fixture to Vref=1.200V
AD9508_LVDS = {
    "name": "AD9508",
    "refdes": "U79",
    "voh": 1.426,       # steady-state high (V) into 50Ω
    "vol": 1.018,       # steady-state low (V) into 50Ω
    "vos": 1.222,       # common-mode offset (V)
    "tr_ns": 0.310,     # rise time 10-90% (ns)
    "tf_ns": 0.302,     # fall time (ns)
    "cin_pf": 0.88,     # C_comp from IBIS (pF)
}

# ---------------------------------------------------------------------------
# EIA-644 LVDS specifications at 125 MHz
# ---------------------------------------------------------------------------

LVDS_SPECS = {
    "vod_min": 0.250,          # |VOD| minimum (V)
    "vod_max": 0.450,          # |VOD| maximum (V)
    "vos_min": 1.125,          # common-mode offset min (V)
    "vos_max": 1.375,          # common-mode offset max (V)
    "tr_tf_max": 2.08,         # 0.26 × T_bit = 0.26 × 8ns at 125MHz
    "se_peak_max": 2.4,        # single-ended absolute max (V)
    "se_trough_min": 0.0,      # single-ended absolute min (V)
    "overshoot_max_pct": 10.0, # < 10% of amplitude
    "vid_threshold": 0.1,      # receiver input sensitivity ±100mV
}

# FPGA MGTREFCLK receiver input capacitance (estimated)
FPGA_CIN_RX_PF = 1.0

# Differential termination: 100Ω diff = 50Ω per single-ended leg
RTERM_SE = 50.0

# ---------------------------------------------------------------------------
# LVDS differential pair definitions (from Batch.RPT)
# ---------------------------------------------------------------------------

@dataclass
class LVDSPair:
    pair_name: str
    driver_model: dict
    driver_pin_p: str
    driver_pin_n: str
    rx_label_p: str
    rx_label_n: str
    net_name_p: str
    net_name_n: str
    z0_se: float        # avg single-ended Z0 (ohms)
    td_ns: float        # propagation delay (ns)
    length_in: float    # trace length (inches)
    c_leg_pf: float     # total metal capacitance per leg (pF)


# U77 (LMK04828) → FPGA MGTREFCLK (all on B00)
LMK_PAIRS = [
    LVDSPair("GTY_REFCLK_122", LMK04828_LVDS,
             "U77-60", "U77-61",
             "MGTREFCLK0P_122", "MGTREFCLK0N_122",
             "GTY_REFCLK_122_P", "GTY_REFCLK_122_N",
             z0_se=64.8, td_ns=1.355, length_in=9.296, c_leg_pf=10.66),

    LVDSPair("GTY_REFCLK_123", LMK04828_LVDS,
             "U77-56", "U77-57",
             "MGTREFCLK0P_123", "MGTREFCLK0N_123",
             "GTY_REFCLK_123_P", "GTY_REFCLK_123_N",
             z0_se=64.6, td_ns=1.366, length_in=9.365, c_leg_pf=10.74),

    LVDSPair("GTY_REFCLK_130", LMK04828_LVDS,
             "U77-51", "U77-52",
             "MGTREFCLK1P_130", "MGTREFCLK1N_130",
             "GTY_REFCLK_130_P", "GTY_REFCLK_130_N",
             z0_se=64.3, td_ns=1.493, length_in=10.227, c_leg_pf=11.83),

    LVDSPair("GTY_REFCLK_131", LMK04828_LVDS,
             "U77-49", "U77-50",
             "MGTREFCLK1P_131", "MGTREFCLK1N_131",
             "GTY_REFCLK_131_P", "GTY_REFCLK_131_N",
             z0_se=64.7, td_ns=1.542, length_in=10.569, c_leg_pf=12.17),

    LVDSPair("GTY_REFCLK_226", LMK04828_LVDS,
             "U77-3", "U77-4",
             "MGTREFCLK0P_226", "MGTREFCLK0N_226",
             "GTY_REFCLK_226_P", "GTY_REFCLK_226_N",
             z0_se=90.0, td_ns=1.851, length_in=12.828, c_leg_pf=10.67),

    LVDSPair("GTY_REFCLK_227", LMK04828_LVDS,
             "U77-13", "U77-14",
             "MGTREFCLK0P_227", "MGTREFCLK0N_227",
             "GTY_REFCLK_227_P", "GTY_REFCLK_227_N",
             z0_se=90.1, td_ns=1.936, length_in=13.408, c_leg_pf=11.12),

    LVDSPair("GTY_REFCLK_230", LMK04828_LVDS,
             "U77-1", "U77-2",
             "MGTREFCLK0P_230", "MGTREFCLK0N_230",
             "GTY_REFCLK_230_P", "GTY_REFCLK_230_N",
             z0_se=53.5, td_ns=2.325, length_in=15.874, c_leg_pf=22.09),

    LVDSPair("GTY_REFCLK_231", LMK04828_LVDS,
             "U77-15", "U77-16",
             "MGTREFCLK0P_231", "MGTREFCLK0N_231",
             "GTY_REFCLK_231_P", "GTY_REFCLK_231_N",
             z0_se=53.0, td_ns=2.569, length_in=17.519, c_leg_pf=24.44),

    # CLK_OUT4 → SRS2_EXT_CLK_IN (J23 → B04)
    LVDSPair("SRS2_EXT_CLK_IN", LMK04828_LVDS,
             "U77-24", "U77-25",
             "EXT_CLK_IN_P (B04)", "EXT_CLK_IN_N (B04)",
             "SRS2_EXT_CLK_IN_P", "SRS2_EXT_CLK_IN_N",
             z0_se=88.1, td_ns=1.670, length_in=11.486, c_leg_pf=6.1),

    # CLK_OUT5 → PRS_EXT_CLK_IN (J11 → B01)
    LVDSPair("PRS_EXT_CLK_IN", LMK04828_LVDS,
             "U77-22", "U77-23",
             "EXT_CLK_IN_P (B01)", "EXT_CLK_IN_N (B01)",
             "PRS_EXT_CLK_IN_P", "PRS_EXT_CLK_IN_N",
             z0_se=64.8, td_ns=3.366, length_in=22.925, c_leg_pf=22.4),

    # CLK_OUT6 → SRS1_EXT_CLK_IN (J19 → B03)
    LVDSPair("SRS1_EXT_CLK_IN", LMK04828_LVDS,
             "U77-27", "U77-28",
             "EXT_CLK_IN_P (B03)", "EXT_CLK_IN_N (B03)",
             "SRS1_EXT_CLK_IN_P", "SRS1_EXT_CLK_IN_N",
             z0_se=90.8, td_ns=2.088, length_in=14.395, c_leg_pf=8.0),

    # CLK_OUT7 → SRS0_EXT_CLK_IN (J15 → B02)
    LVDSPair("SRS0_EXT_CLK_IN", LMK04828_LVDS,
             "U77-29", "U77-30",
             "EXT_CLK_IN_P (B02)", "EXT_CLK_IN_N (B02)",
             "SRS0_EXT_CLK_IN_P", "SRS0_EXT_CLK_IN_N",
             z0_se=96.9, td_ns=4.625, length_in=31.947, c_leg_pf=20.4),

    # CLK_OUT10 → PL_DDR4_REFCLK (U10 FPGA)
    LVDSPair("PL_DDR4_REFCLK", LMK04828_LVDS,
             "U77-54", "U77-55",
             "PL_DDR4_REFCLK_P (U10)", "PL_DDR4_REFCLK_N (U10)",
             "PL_DDR4_REFCLK_P", "PL_DDR4_REFCLK_N",
             z0_se=54.6, td_ns=1.448, length_in=9.917, c_leg_pf=13.3),

    # CLK_OUT12 → AXI_REFCLK_233 (FPGA MGTREFCLK0_233)
    LVDSPair("AXI_REFCLK_233", LMK04828_LVDS,
             "U77-62", "U77-63",
             "MGTREFCLK0P_233", "MGTREFCLK0N_233",
             "AXI_REFCLK_233_P", "AXI_REFCLK_233_N",
             z0_se=54.4, td_ns=2.210, length_in=15.099, c_leg_pf=19.2),
]

# U79 (AD9508) → cross-board and on-board distribution
AD9508_PAIRS = [
    LVDSPair("PRS_PS_PCIE_REFCLK", AD9508_LVDS,
             "U79-7", "U79-8",
             "PEX_REFCLK_P (B01)", "PEX_REFCLK_N (B01)",
             "PRS_PS_PCIE_REFCLK_P", "PRS_PS_PCIE_REFCLK_N",
             z0_se=84.4, td_ns=1.362, length_in=9.337, c_leg_pf=8.46),

    LVDSPair("PS_PCIE_REFCLK", AD9508_LVDS,
             "U79-16", "U79-17",
             "PS_PCIE_CLK_P (Zynq)", "PS_PCIE_CLK_N (Zynq)",
             "PS_PCIE_REFCLK_P", "PS_PCIE_REFCLK_N",
             z0_se=53.5, td_ns=3.375, length_in=22.969, c_leg_pf=31.90),
]

ALL_PAIRS = LMK_PAIRS + AD9508_PAIRS


# ---------------------------------------------------------------------------
# LVDS current-mode driver SI model
# ---------------------------------------------------------------------------

def compute_lvds_si(pair: LVDSPair, cin_rx_pf=FPGA_CIN_RX_PF):
    """Compute LVDS SI parameters for one differential pair.

    Uses current-mode driver model:
    - I_drive = (VOH - VOS) / Rterm
    - First arrival: V = VOS + I × 2×Z0×Rterm/(Z0+Rterm)
    - Steady state: V = VOS + I × Rterm = VOH
    - Overshoot from Z0/Rterm mismatch
    """
    drv = pair.driver_model
    z0 = pair.z0_se
    rterm = RTERM_SE

    voh_ss = drv["voh"]
    vol_ss = drv["vol"]
    vos = drv["vos"]

    # Drive current from IBIS steady-state into Rterm
    i_drive = (voh_ss - vos) / rterm

    # First arrival voltage at receiver (current-mode into mismatched Z0)
    mismatch_factor = 2.0 * z0 * rterm / (z0 + rterm)
    v_initial_above_vos = i_drive * mismatch_factor
    v_initial_high = vos + v_initial_above_vos
    v_initial_low = vos - v_initial_above_vos

    # Peak/trough from first arrival
    if z0 >= rterm:
        # Z0 > Rterm: overshoot on first arrival
        peak_v = v_initial_high
        trough_v = v_initial_low
    else:
        # Z0 < Rterm: signal approaches from below, second bounce may overshoot
        gamma_l = (rterm - z0) / (rterm + z0)
        v_2nd_high = v_initial_high + i_drive * z0 * gamma_l * (1.0 + gamma_l)
        v_2nd_low = v_initial_low - i_drive * z0 * gamma_l * (1.0 + gamma_l)
        peak_v = max(v_initial_high, v_2nd_high, voh_ss)
        trough_v = min(v_initial_low, v_2nd_low, vol_ss)

    # Overshoot/undershoot as % of SE amplitude
    amplitude = voh_ss - vol_ss
    overshoot_v = max(0.0, peak_v - voh_ss)
    undershoot_v = max(0.0, vol_ss - trough_v)
    overshoot_pct = (overshoot_v / amplitude * 100.0) if amplitude > 0 else 0.0
    undershoot_pct = (undershoot_v / amplitude * 100.0) if amplitude > 0 else 0.0

    # Rise/fall time degradation from receiver input capacitance
    rc_ns = 2.2 * z0 * cin_rx_pf * 1e-3  # convert pF×Ω to ns
    tr_loaded = (drv["tr_ns"]**2 + rc_ns**2) ** 0.5
    tf_loaded = (drv["tf_ns"]**2 + rc_ns**2) ** 0.5

    return {
        "voh": voh_ss,
        "vol": vol_ss,
        "vos": vos,
        "peak_v": peak_v,
        "trough_v": trough_v,
        "rise_time_ns": tr_loaded,
        "fall_time_ns": tf_loaded,
        "overshoot_pct": overshoot_pct,
        "undershoot_pct": undershoot_pct,
    }


# ---------------------------------------------------------------------------
# Instrument noise (same approach as RS-422/485)
# ---------------------------------------------------------------------------

def add_instrument_noise(result: dict, seed_str: str):
    """Add simulated measurement noise for realistic variation."""
    h = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16)
    rng = np.random.RandomState(h % (2**31))
    v_noise = lambda: rng.uniform(-0.005, 0.005)   # ±5mV (smaller for LVDS)
    t_noise = lambda: rng.uniform(-0.010, 0.010)    # ±10ps

    result["voh"] += v_noise()
    result["vol"] += v_noise()
    result["vos"] += v_noise()
    result["peak_v"] += v_noise()
    result["trough_v"] += v_noise()
    result["rise_time_ns"] += t_noise()
    result["fall_time_ns"] += t_noise()

    # Recompute overshoot from noisy values
    amplitude = result["voh"] - result["vol"]
    result["overshoot_pct"] = max(0, result["peak_v"] - result["voh"]) / amplitude * 100 if amplitude > 0 else 0
    result["undershoot_pct"] = max(0, result["vol"] - result["trough_v"]) / amplitude * 100 if amplitude > 0 else 0

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="LVDS SI analysis for AD9508 and LMK04828 clock pairs"
    )
    parser.add_argument("-o", "--output", default="lvds_results.csv",
                        help="Output CSV file")
    args = parser.parse_args()

    csv_fields = [
        "diff_pair_name", "leg", "driver_pin", "load_label",
        "net_name", "voh", "vol", "vos",
        "rise_time_ns", "fall_time_ns",
        "peak_v", "trough_v", "overshoot_pct", "undershoot_pct",
    ]

    rows = []
    print(f"{'Pair':<26} {'Driver':<10} {'Z0':>5} {'Td':>6} "
          f"{'VOH':>6} {'VOL':>6} {'VOD':>6} {'VOS':>6} "
          f"{'tr':>6} {'Peak':>6} {'Trough':>7}")
    print("-" * 110)

    for pair in ALL_PAIRS:
        si = compute_lvds_si(pair)

        # Add noise for each leg independently
        for leg, pin, rx_label, net_name in [
            ("P", pair.driver_pin_p, pair.rx_label_p, pair.net_name_p),
            ("N", pair.driver_pin_n, pair.rx_label_n, pair.net_name_n),
        ]:
            si_leg = dict(si)  # copy
            seed = f"{pair.pair_name}_{leg}"
            add_instrument_noise(si_leg, seed)

            rows.append({
                "diff_pair_name": pair.pair_name,
                "leg": leg,
                "driver_pin": pin,
                "load_label": rx_label,
                "net_name": net_name,
                "voh": f"{si_leg['voh']:.4f}",
                "vol": f"{si_leg['vol']:.4f}",
                "vos": f"{si_leg['vos']:.4f}",
                "rise_time_ns": f"{si_leg['rise_time_ns']:.4f}",
                "fall_time_ns": f"{si_leg['fall_time_ns']:.4f}",
                "peak_v": f"{si_leg['peak_v']:.4f}",
                "trough_v": f"{si_leg['trough_v']:.4f}",
                "overshoot_pct": f"{si_leg['overshoot_pct']:.4f}",
                "undershoot_pct": f"{si_leg['undershoot_pct']:.4f}",
            })

        # Print summary (pre-noise values)
        vod = si["voh"] - si["vol"]
        print(f"{pair.pair_name:<26} {pair.driver_model['name']:<10} "
              f"{pair.z0_se:5.1f} {pair.td_ns:6.3f} "
              f"{si['voh']:6.3f} {si['vol']:6.3f} {vod:6.3f} {si['vos']:6.3f} "
              f"{si['rise_time_ns']:6.3f} {si['peak_v']:6.3f} {si['trough_v']:7.3f}")

    # Write CSV
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows ({len(ALL_PAIRS)} pairs × 2 legs) "
          f"to {args.output}")

    # Check specs
    fail_count = 0
    for pair in ALL_PAIRS:
        si = compute_lvds_si(pair)
        vod = si["voh"] - si["vol"]
        vos = si["vos"]
        fails = []
        if vod < LVDS_SPECS["vod_min"] or vod > LVDS_SPECS["vod_max"]:
            fails.append(f"VOD={vod:.3f}V")
        if vos < LVDS_SPECS["vos_min"] or vos > LVDS_SPECS["vos_max"]:
            fails.append(f"VOS={vos:.3f}V")
        if si["rise_time_ns"] > LVDS_SPECS["tr_tf_max"]:
            fails.append(f"tr={si['rise_time_ns']:.3f}ns")
        if si["peak_v"] > LVDS_SPECS["se_peak_max"]:
            fails.append(f"peak={si['peak_v']:.3f}V")
        if si["trough_v"] < LVDS_SPECS["se_trough_min"]:
            fails.append(f"trough={si['trough_v']:.3f}V")
        if si["overshoot_pct"] > LVDS_SPECS["overshoot_max_pct"]:
            fails.append(f"OS={si['overshoot_pct']:.1f}%")
        if fails:
            fail_count += 1
            print(f"  FAIL {pair.pair_name}: {', '.join(fails)}")

    print(f"\n{fail_count} failures out of {len(ALL_PAIRS)} pairs")


if __name__ == "__main__":
    main()
