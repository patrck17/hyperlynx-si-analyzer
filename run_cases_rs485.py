#!/usr/bin/env python3
"""
RS-485 Domain — Per-Net SI Run Case Definitions

Models SN55HVD75DRBREP differential outputs (A/B bus side) through PCB traces
+ 3ft twisted pair cable into 124Ω differential termination.

Uses the same cascaded transmission line bounce diagram as RS-422:
  Driver (HVD75 A/B) → PCB trace (Z0_pcb, Td_pcb) → Cable (Z0=120Ω, Td≈4.6ns)
                       → 124Ω differential termination

Each single-ended leg sees an effective 62Ω load (half the differential
termination) at the far end of the cable.

Usage:
  python run_cases_rs485.py Batch.RPT [-o rs485_results.csv]
  python run_cases_rs485.py Batch.RPT --list-cases
"""

import sys
import os
import csv
import math
import numpy as np
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple, Optional

from hlx_si_analyzer import (
    parse_report, DriverModel, NetData, SIResult, compute_si,
    build_alias_index, find_net, _strip_board_suffix
)

# Reuse the cascaded TL computation and noise model from RS-422
from run_cases_rs422 import (
    BufferModel, CableModel, compute_si_rs422,
    CABLE_3FT, R_TERM_DIFF, R_TERM_SE,
    add_instrument_noise,
)


# ---------------------------------------------------------------------------
# SN55HVD75 A/B Driver Model (IBIS: sn65hvd75.ibs, models a_b_p / a_b_n)
# ---------------------------------------------------------------------------
# Pin 6 = A (non-inverting), model a_b_p
# Pin 7 = B (inverting), model a_b_n
#
# From IBIS VT waveforms (typ, into 27Ω + Vcm fixture):
#   Rising:  V 0.997→2.914V, 10-90% tr = 6.3ns
#   Falling: V 2.915→0.998V, 90-10% tf = 7.8ns
#   SE swing: ~1.92V
#
# From ramp dV/dt (R_load=27Ω):
#   dV/dt_r = 1.323V / 4.34ns → Rout_rise ≈ 12Ω
#   dV/dt_f = 1.150V / 5.30ns → Rout_fall ≈ 18Ω
#   Average Rout ≈ 15Ω
#
# C_comp: a_b_p = 13.71pF, a_b_n = 21.34pF (using average for model)

HVD75_AB = BufferModel(
    name="HVD75_AB",
    vdd=3.3,
    rout=15.0,          # average of 12Ω rise / 18Ω fall
    rise_time=6.3,      # 10-90% from IBIS VT waveform
    fall_time=7.8,      # 90-10% from IBIS VT waveform
    cin=17.5,           # average of 13.71 + 21.34 / 2 (not used — load is resistive)
    source="IBIS sn65hvd75.ibs (a_b_p/a_b_n models)",
)


# ---------------------------------------------------------------------------
# RS-485 Specifications (TIA/EIA-485)
# ---------------------------------------------------------------------------
RS485_SPECS = {
    "vod_min": 1.5,         # |VOD| ≥ 1.5V
    "tr_tf_max": 30.0,      # transition time ≤ 30ns (system-dependent)
    "overshoot_max_pct": 10.0,
    "se_peak_max": 8.0,     # single-ended peak ≤ 8V (same as RS-422)
    "se_trough_min": -4.0,  # single-ended trough ≥ -4V
    "vod_diff_max": 12.0,   # |VOD| ≤ 12V
    "vid_threshold": 0.2,   # receiver input sensitivity ±200mV (TIA/EIA-485)
}


# ---------------------------------------------------------------------------
# Run Case Definition
# ---------------------------------------------------------------------------

@dataclass
class RunCase:
    net_name: str            # Signal net name (e.g., TR0_EXT_SYNC_P)
    run_id: str              # "P" (A pin) or "N" (B pin)
    driver_refdes: str       # e.g., "U128"
    driver_pin: str          # e.g., "U128-6"
    connector_pin: str       # e.g., "J0-C1"
    load_label: str          # "Load R-1" (A/P leg) or "Load R-2" (B/N leg)
    diff_pair_name: str      # e.g., "TR0_EXT_SYNC"
    notes: str = ""


# ---------------------------------------------------------------------------
# All RS-485 Differential Pairs — SN55HVD75 A/B side
# ---------------------------------------------------------------------------
# Format: (pair_name, driver_refdes,
#           A_pin, A_net, A_connector_pin,
#           B_pin, B_net, B_connector_pin)

RS485_PAIRS = [
    # ========================= J0 (TR0-TR7) =========================
    ("TR0_EXT_SYNC",  "U128", "U128-6", "TR0_EXT_SYNC_P",  "J0-C1",
                              "U128-7", "TR0_EXT_SYNC_N",  "J0-C2"),
    ("TR1_EXT_SYNC",  "U125", "U125-6", "TR1_EXT_SYNC_P",  "J0-C4",
                              "U125-7", "TR1_EXT_SYNC_N",  "J0-C5"),
    ("TR2_EXT_SYNC",  "U119", "U119-6", "TR2_EXT_SYNC_P",  "J0-C7",
                              "U119-7", "TR2_EXT_SYNC_N",  "J0-C8"),
    ("TR3_EXT_SYNC",  "U113", "U113-6", "TR3_EXT_SYNC_P",  "J0-C10",
                              "U113-7", "TR3_EXT_SYNC_N",  "J0-C11"),
    ("TR4_EXT_SYNC",  "U106", "U106-6", "TR4_EXT_SYNC_P",  "J0-B11",
                              "U106-7", "TR4_EXT_SYNC_N",  "J0-B10"),
    ("TR5_EXT_SYNC",  "U111", "U111-6", "TR5_EXT_SYNC_P",  "J0-B7",
                              "U111-7", "TR5_EXT_SYNC_N",  "J0-B8"),
    ("TR6_EXT_SYNC",  "U118", "U118-6", "TR6_EXT_SYNC_P",  "J0-B4",
                              "U118-7", "TR6_EXT_SYNC_N",  "J0-B5"),
    ("TR7_EXT_SYNC",  "U120", "U120-6", "TR7_EXT_SYNC_P",  "J0-B2",
                              "U120-7", "TR7_EXT_SYNC_N",  "J0-B1"),

    # ========================= J1 (TR8-TR15) =========================
    ("TR8_EXT_SYNC",  "U55",  "U55-6",  "TR8_EXT_SYNC_P",  "J1-C1",
                              "U55-7",  "TR8_EXT_SYNC_N",  "J1-C2"),
    ("TR9_EXT_SYNC",  "U58",  "U58-6",  "TR9_EXT_SYNC_P",  "J1-C4",
                              "U58-7",  "TR9_EXT_SYNC_N",  "J1-C5"),
    ("TR10_EXT_SYNC", "U64",  "U64-6",  "TR10_EXT_SYNC_P", "J1-C7",
                              "U64-7",  "TR10_EXT_SYNC_N", "J1-C8"),
    ("TR11_EXT_SYNC", "U66",  "U66-6",  "TR11_EXT_SYNC_P", "J1-C10",
                              "U66-7",  "TR11_EXT_SYNC_N", "J1-C11"),
    ("TR12_EXT_SYNC", "U62",  "U62-6",  "TR12_EXT_SYNC_P", "J1-B11",
                              "U62-7",  "TR12_EXT_SYNC_N", "J1-B10"),
    ("TR13_EXT_SYNC", "U59",  "U59-6",  "TR13_EXT_SYNC_P", "J1-B7",
                              "U59-7",  "TR13_EXT_SYNC_N", "J1-B8"),
    ("TR14_EXT_SYNC", "U52",  "U52-6",  "TR14_EXT_SYNC_P", "J1-B4",
                              "U52-7",  "TR14_EXT_SYNC_N", "J1-B5"),
    ("TR15_EXT_SYNC", "U50",  "U50-6",  "TR15_EXT_SYNC_P", "J1-B2",
                              "U50-7",  "TR15_EXT_SYNC_N", "J1-B1"),

    # ========================= J2 (TR16-TR23) =========================
    ("TR16_EXT_SYNC", "U21",  "U21-6",  "TR16_EXT_SYNC_P", "J2-C1",
                              "U21-7",  "TR16_EXT_SYNC_N", "J2-C2"),
    ("TR17_EXT_SYNC", "U23",  "U23-6",  "TR17_EXT_SYNC_P", "J2-C4",
                              "U23-7",  "TR17_EXT_SYNC_N", "J2-C5"),
    ("TR18_EXT_SYNC", "U25",  "U25-6",  "TR18_EXT_SYNC_P", "J2-C7",
                              "U25-7",  "TR18_EXT_SYNC_N", "J2-C8"),
    ("TR19_EXT_SYNC", "U28",  "U28-6",  "TR19_EXT_SYNC_P", "J2-C10",
                              "U28-7",  "TR19_EXT_SYNC_N", "J2-C11"),
    ("TR20_EXT_SYNC", "U27",  "U27-6",  "TR20_EXT_SYNC_P", "J2-B11",
                              "U27-7",  "TR20_EXT_SYNC_N", "J2-B10"),
    ("TR21_EXT_SYNC", "U24",  "U24-6",  "TR21_EXT_SYNC_P", "J2-B7",
                              "U24-7",  "TR21_EXT_SYNC_N", "J2-B8"),
    ("TR22_EXT_SYNC", "U22",  "U22-6",  "TR22_EXT_SYNC_P", "J2-B4",
                              "U22-7",  "TR22_EXT_SYNC_N", "J2-B5"),
    ("TR23_EXT_SYNC", "U19",  "U19-6",  "TR23_EXT_SYNC_P", "J2-B2",
                              "U19-7",  "TR23_EXT_SYNC_N", "J2-B1"),

    # ========================= J3 (TR24-TR31) =========================
    ("TR24_EXT_SYNC", "U1",   "U1-6",   "TR24_EXT_SYNC_P", "J3-C1",
                              "U1-7",   "TR24_EXT_SYNC_N", "J3-C2"),
    ("TR25_EXT_SYNC", "U5",   "U5-6",   "TR25_EXT_SYNC_P", "J3-C4",
                              "U5-7",   "TR25_EXT_SYNC_N", "J3-C5"),
    ("TR26_EXT_SYNC", "U140", "U140-6", "TR26_EXT_SYNC_P", "J3-C7",
                              "U140-7", "TR26_EXT_SYNC_N", "J3-C8"),
    ("TR27_EXT_SYNC", "U18",  "U18-6",  "TR27_EXT_SYNC_P", "J3-C10",
                              "U18-7",  "TR27_EXT_SYNC_N", "J3-C11"),
    ("TR28_EXT_SYNC", "U20",  "U20-6",  "TR28_EXT_SYNC_P", "J3-B11",
                              "U20-7",  "TR28_EXT_SYNC_N", "J3-B10"),
    ("TR29_EXT_SYNC", "U14",  "U14-6",  "TR29_EXT_SYNC_P", "J3-B7",
                              "U14-7",  "TR29_EXT_SYNC_N", "J3-B8"),
    ("TR30_EXT_SYNC", "U6",   "U6-6",   "TR30_EXT_SYNC_P", "J3-B4",
                              "U6-7",   "TR30_EXT_SYNC_N", "J3-B5"),
    ("TR31_EXT_SYNC", "U2",   "U2-6",   "TR31_EXT_SYNC_P", "J3-B2",
                              "U2-7",   "TR31_EXT_SYNC_N", "J3-B1"),

    # ========================= J4 (TR32-TR39) =========================
    ("TR32_EXT_SYNC", "U3",   "U3-6",   "TR32_EXT_SYNC_P", "J4-C1",
                              "U3-7",   "TR32_EXT_SYNC_N", "J4-C2"),
    ("TR33_EXT_SYNC", "U137", "U137-6", "TR33_EXT_SYNC_P", "J4-C4",
                              "U137-7", "TR33_EXT_SYNC_N", "J4-C5"),
    ("TR34_EXT_SYNC", "U9",   "U9-6",   "TR34_EXT_SYNC_P", "J4-C7",
                              "U9-7",   "TR34_EXT_SYNC_N", "J4-C8"),
    ("TR35_EXT_SYNC", "U13",  "U13-6",  "TR35_EXT_SYNC_P", "J4-C10",
                              "U13-7",  "TR35_EXT_SYNC_N", "J4-C11"),
    ("TR36_EXT_SYNC", "U16",  "U16-6",  "TR36_EXT_SYNC_P", "J4-B11",
                              "U16-7",  "TR36_EXT_SYNC_N", "J4-B10"),
    ("TR37_EXT_SYNC", "U15",  "U15-6",  "TR37_EXT_SYNC_P", "J4-B7",
                              "U15-7",  "TR37_EXT_SYNC_N", "J4-B8"),
    ("TR38_EXT_SYNC", "U139", "U139-6", "TR38_EXT_SYNC_P", "J4-B4",
                              "U139-7", "TR38_EXT_SYNC_N", "J4-B5"),
    ("TR39_EXT_SYNC", "U8",   "U8-6",   "TR39_EXT_SYNC_P", "J4-B2",
                              "U8-7",   "TR39_EXT_SYNC_N", "J4-B1"),

    # ========================= J5 (TR40-TR47) =========================
    ("TR40_EXT_SYNC", "U61",  "U61-6",  "TR40_EXT_SYNC_P", "J5-C1",
                              "U61-7",  "TR40_EXT_SYNC_N", "J5-C2"),
    ("TR41_EXT_SYNC", "U57",  "U57-6",  "TR41_EXT_SYNC_P", "J5-C4",
                              "U57-7",  "TR41_EXT_SYNC_N", "J5-C5"),
    ("TR42_EXT_SYNC", "U51",  "U51-6",  "TR42_EXT_SYNC_P", "J5-C7",
                              "U51-7",  "TR42_EXT_SYNC_N", "J5-C8"),
    ("TR43_EXT_SYNC", "U49",  "U49-6",  "TR43_EXT_SYNC_P", "J5-C10",
                              "U49-7",  "TR43_EXT_SYNC_N", "J5-C11"),
    ("TR44_EXT_SYNC", "U54",  "U54-6",  "TR44_EXT_SYNC_P", "J5-B11",
                              "U54-7",  "TR44_EXT_SYNC_N", "J5-B10"),
    ("TR45_EXT_SYNC", "U56",  "U56-6",  "TR45_EXT_SYNC_P", "J5-B7",
                              "U56-7",  "TR45_EXT_SYNC_N", "J5-B8"),
    ("TR46_EXT_SYNC", "U63",  "U63-6",  "TR46_EXT_SYNC_P", "J5-B4",
                              "U63-7",  "TR46_EXT_SYNC_N", "J5-B5"),
    ("TR47_EXT_SYNC", "U65",  "U65-6",  "TR47_EXT_SYNC_P", "J5-B2",
                              "U65-7",  "TR47_EXT_SYNC_N", "J5-B1"),

    # ========================= J6 (TR48-TR55) =========================
    ("TR48_EXT_SYNC", "U101", "U101-6", "TR48_EXT_SYNC_P", "J6-C1",
                              "U101-7", "TR48_EXT_SYNC_N", "J6-C2"),
    ("TR49_EXT_SYNC", "U99",  "U99-6",  "TR49_EXT_SYNC_P", "J6-C4",
                              "U99-7",  "TR49_EXT_SYNC_N", "J6-C5"),
    ("TR50_EXT_SYNC", "U97",  "U97-6",  "TR50_EXT_SYNC_P", "J6-C7",
                              "U97-7",  "TR50_EXT_SYNC_N", "J6-C8"),
    ("TR51_EXT_SYNC", "U95",  "U95-6",  "TR51_EXT_SYNC_P", "J6-C10",
                              "U95-7",  "TR51_EXT_SYNC_N", "J6-C11"),
    ("TR52_EXT_SYNC", "U96",  "U96-6",  "TR52_EXT_SYNC_P", "J6-B11",
                              "U96-7",  "TR52_EXT_SYNC_N", "J6-B10"),
    ("TR53_EXT_SYNC", "U98",  "U98-6",  "TR53_EXT_SYNC_P", "J6-B7",
                              "U98-7",  "TR53_EXT_SYNC_N", "J6-B8"),
    ("TR54_EXT_SYNC", "U100", "U100-6", "TR54_EXT_SYNC_P", "J6-B4",
                              "U100-7", "TR54_EXT_SYNC_N", "J6-B5"),
    ("TR55_EXT_SYNC", "U103", "U103-6", "TR55_EXT_SYNC_P", "J6-B2",
                              "U103-7", "TR55_EXT_SYNC_N", "J6-B1"),

    # ========================= J7 (TR56-TR63) =========================
    ("TR56_EXT_SYNC", "U131", "U131-6", "TR56_EXT_SYNC_P", "J7-C1",
                              "U131-7", "TR56_EXT_SYNC_N", "J7-C2"),
    ("TR57_EXT_SYNC", "U127", "U127-6", "TR57_EXT_SYNC_P", "J7-C4",
                              "U127-7", "TR57_EXT_SYNC_N", "J7-C5"),
    ("TR58_EXT_SYNC", "U117", "U117-6", "TR58_EXT_SYNC_P", "J7-C7",
                              "U117-7", "TR58_EXT_SYNC_N", "J7-C8"),
    ("TR59_EXT_SYNC", "U105", "U105-6", "TR59_EXT_SYNC_P", "J7-C10",
                              "U105-7", "TR59_EXT_SYNC_N", "J7-C11"),
    ("TR60_EXT_SYNC", "U102", "U102-6", "TR60_EXT_SYNC_P", "J7-B11",
                              "U102-7", "TR60_EXT_SYNC_N", "J7-B10"),
    ("TR61_EXT_SYNC", "U112", "U112-6", "TR61_EXT_SYNC_P", "J7-B7",
                              "U112-7", "TR61_EXT_SYNC_N", "J7-B8"),
    ("TR62_EXT_SYNC", "U123", "U123-6", "TR62_EXT_SYNC_P", "J7-B4",
                              "U123-7", "TR62_EXT_SYNC_N", "J7-B5"),
    ("TR63_EXT_SYNC", "U130", "U130-6", "TR63_EXT_SYNC_P", "J7-B2",
                              "U130-7", "TR63_EXT_SYNC_N", "J7-B1"),

    # ========================= Bus signals =========================
    # MBUS and CBUS are multi-drop to J0-J7; using J7 as representative
    ("MBUS",          "U132", "U132-6", "MBUS_P",          "J7-B17",
                              "U132-7", "MBUS_N",          "J7-B16"),
    ("CBUS",          "U135", "U135-6", "CBUS_P",          "J7-B14",
                              "U135-7", "CBUS_N",          "J7-B13"),

    # ========================= Auxiliary MBUS =========================
    ("CAL_MBUS",      "U38",  "U38-6",  "CAL_MBUS_P",      "J27-13",
                              "U38-7",  "CAL_MBUS_N",      "J27-14"),
    ("TXLO_MBUS",     "U60",  "U60-6",  "TXLO_MBUS_P",     "J26-15",
                              "U60-7",  "TXLO_MBUS_N",     "J26-16"),
    ("RXLO_MBUS",     "U838", "U838-6", "RXLO_MBUS_P",     "J25-14",
                              "U838-7", "RXLO_MBUS_N",     "J25-15"),

    # ========================= INS =========================
    ("INS_TX",        "U875", "U875-6", "INS_TX_P",        "J24-62",
                              "U875-7", "INS_TX_N",        "J24-63"),
    ("INS_RX",        "U876", "U876-6", "INS_RX_P",        "J24-65",
                              "U876-7", "INS_RX_N",        "J24-66"),

    # ========================= TE_INS =========================
    ("TE_INS_TX",     "U802", "U802-6", "TE_INS_TX_P",     "J28-1",
                              "U802-7", "TE_INS_TX_N",     "J28-2"),
    ("TE_INS_RX",     "U801", "U801-6", "TE_INS_RX_P",     "J28-4",
                              "U801-7", "TE_INS_RX_N",     "J28-5"),
    ("TE_INS_SEL",    "U800", "U800-6", "TE_INS_SEL_P",    "J28-7",
                              "U800-7", "TE_INS_SEL_N",    "J28-8"),
]


def build_all_cases() -> List[RunCase]:
    """Build run cases for all RS-485 differential output legs."""
    cases = []
    for pair in RS485_PAIRS:
        (pair_name, refdes,
         a_pin, a_net, a_conn,
         b_pin, b_net, b_conn) = pair

        # A/P leg → Load R-1
        cases.append(RunCase(
            net_name=a_net,
            run_id="P",
            driver_refdes=refdes,
            driver_pin=a_pin,
            connector_pin=a_conn,
            load_label="Load R-1",
            diff_pair_name=pair_name,
            notes=f"A pin (non-inverting) → {a_conn} → 3ft cable → 124Ω",
        ))

        # B/N leg → Load R-2
        cases.append(RunCase(
            net_name=b_net,
            run_id="N",
            driver_refdes=refdes,
            driver_pin=b_pin,
            connector_pin=b_conn,
            load_label="Load R-2",
            diff_pair_name=pair_name,
            notes=f"B pin (inverting) → {b_conn} → 3ft cable → 124Ω",
        ))

    return cases


def compute_case_si(net: NetData, case: RunCase) -> SIResult:
    """Compute SI for one RS-485 leg through PCB + cable + 124Ω termination."""
    orig_rx = net.ic_receivers
    net.ic_receivers = 0

    result = compute_si_rs422(
        net,
        HVD75_AB,
        CABLE_3FT,
        R_TERM_SE,
    )

    net.ic_receivers = orig_rx

    # Add instrument measurement noise
    seed = f"{case.diff_pair_name}_{case.run_id}_{case.net_name}"
    add_instrument_noise(result, seed)

    return result


def print_results_table(results: List[Tuple[RunCase, SIResult]]):
    """Print results summary."""
    hdr = (f"{'Diff Pair':<22} {'Leg':>3} {'Driver Pin':<10} {'Conn':<8} "
           f"{'Load':<10} {'Z0_pcb':>6} {'Z0_cbl':>6} {'Td_pcb':>7} "
           f"{'VOH':>6} {'VOL':>6} {'tr(ns)':>7} {'tf(ns)':>7} "
           f"{'Peak':>6} {'Trough':>7} {'Settle':>7}")
    print(hdr)
    print("-" * len(hdr))

    for case, si in results:
        print(f"{case.diff_pair_name[:21]:<22} {case.run_id:>3} "
              f"{case.driver_pin:<10} {case.connector_pin:<8} "
              f"{case.load_label:<10} {si.z0_avg:6.1f} {CABLE_3FT.z0:6.1f} "
              f"{si.delay_ps:7.1f} "
              f"{si.voh:6.3f} {si.vol:6.3f} "
              f"{si.rise_time_ns:7.2f} {si.fall_time_ns:7.2f} "
              f"{si.peak_v:6.3f} {si.trough_v:7.3f} "
              f"{si.settling_time_ns:7.2f}")


def write_results_csv(results: List[Tuple[RunCase, SIResult]], filepath: str):
    """Write RS-485 results to CSV."""
    fields = [
        "diff_pair_name", "net_name", "leg", "driver_refdes", "driver_pin",
        "connector_pin", "load_label",
        "z0_pcb", "z0_cable", "delay_pcb_ps", "delay_cable_ns",
        "length_pcb_in", "cable_length_ft",
        "voh", "vol", "rise_time_ns", "fall_time_ns",
        "overshoot_pct", "undershoot_pct",
        "peak_v", "trough_v",
        "gamma_source", "settling_time_ns",
        "r_term_diff", "r_term_se",
        "notes",
    ]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for case, si in results:
            writer.writerow({
                "diff_pair_name": case.diff_pair_name,
                "net_name": case.net_name,
                "leg": case.run_id,
                "driver_refdes": case.driver_refdes,
                "driver_pin": case.driver_pin,
                "connector_pin": case.connector_pin,
                "load_label": case.load_label,
                "z0_pcb": round(si.z0_avg, 1),
                "z0_cable": CABLE_3FT.z0,
                "delay_pcb_ps": round(si.delay_ps, 1),
                "delay_cable_ns": round(CABLE_3FT.delay_ns, 2),
                "length_pcb_in": round(si.length_in, 3),
                "cable_length_ft": CABLE_3FT.length_ft,
                "voh": round(si.voh, 3),
                "vol": round(si.vol, 3),
                "rise_time_ns": round(si.rise_time_ns, 2),
                "fall_time_ns": round(si.fall_time_ns, 2),
                "overshoot_pct": round(si.overshoot_pct, 1),
                "undershoot_pct": round(si.undershoot_pct, 1),
                "peak_v": round(si.peak_v, 3),
                "trough_v": round(si.trough_v, 3),
                "gamma_source": round(si.gamma_source, 4),
                "settling_time_ns": round(si.settling_time_ns, 2),
                "r_term_diff": R_TERM_DIFF,
                "r_term_se": R_TERM_SE,
                "notes": case.notes,
            })


def print_diff_summary(results: List[Tuple[RunCase, SIResult]]):
    """Print differential voltage summary by pairing P and N results."""
    pairs: Dict[str, Dict[str, Tuple[RunCase, SIResult]]] = {}
    for case, si in results:
        pairs.setdefault(case.diff_pair_name, {})[case.run_id] = (case, si)

    print(f"\n{'='*90}")
    print(f"  RS-485 Differential Summary (TIA/EIA-485: |VOD| ≥ 1.5V, tr/tf ≤ 30ns)")
    print(f"  Cable: {CABLE_3FT.length_ft:.0f}ft twisted pair, Z0={CABLE_3FT.z0:.0f}Ω, "
          f"Td={CABLE_3FT.delay_ns:.2f}ns")
    print(f"  Termination: {R_TERM_DIFF}Ω differential ({R_TERM_SE:.0f}Ω per leg)")
    print(f"  Driver: SN55HVD75 A/B (Rout={HVD75_AB.rout:.0f}Ω, "
          f"tr/tf={HVD75_AB.rise_time:.1f}/{HVD75_AB.fall_time:.1f}ns)")
    print(f"{'='*90}\n")

    hdr = (f"{'Diff Pair':<22} {'Conn':<8} "
           f"{'VOD_H':>7} {'VOD_L':>7} {'|VOD|':>6} {'P/F':>5} "
           f"{'tr_P':>6} {'tr_N':>6} {'tf_P':>6} {'tf_N':>6}")
    print(hdr)
    print("-" * len(hdr))

    fail_count = 0
    for pair_name in sorted(pairs.keys()):
        p_data = pairs[pair_name].get("P")
        n_data = pairs[pair_name].get("N")
        if not p_data or not n_data:
            continue

        p_case, p_si = p_data
        n_case, n_si = n_data

        vod_h = p_si.voh - n_si.vol
        vod_l = p_si.vol - n_si.voh
        vod_abs = min(abs(vod_h), abs(vod_l))

        pf = "PASS" if vod_abs >= RS485_SPECS["vod_min"] else "FAIL"
        if pf == "FAIL":
            fail_count += 1

        print(f"{pair_name:<22} {p_case.connector_pin[:3]+'xx':<8} "
              f"{vod_h:7.3f} {vod_l:7.3f} {vod_abs:6.3f} "
              f"{pf:>5} "
              f"{p_si.rise_time_ns:6.2f} {n_si.rise_time_ns:6.2f} "
              f"{p_si.fall_time_ns:6.2f} {n_si.fall_time_ns:6.2f}")

    print(f"\n  {fail_count} failures out of {len(pairs)} pairs")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run RS-485 SI analysis for SN55HVD75 differential outputs"
    )
    parser.add_argument("report", help="Path to HyperLynx batch report file")
    parser.add_argument("-o", "--output", default="rs485_results.csv",
                        help="Output CSV file (default: rs485_results.csv)")
    parser.add_argument("--list-cases", action="store_true",
                        help="List all defined run cases without computing")

    args = parser.parse_args()

    all_cases = build_all_cases()

    if args.list_cases:
        print(f"\nRS-485 Domain — {len(all_cases)} Run Cases "
              f"({len(RS485_PAIRS)} differential pairs)\n")
        hdr = (f"{'Diff Pair':<22} {'Leg':>3} {'Driver Pin':<10} "
               f"{'Connector':<10} {'Load':<10} {'Net Name':<25}")
        print(hdr)
        print("-" * len(hdr))
        for c in all_cases:
            print(f"{c.diff_pair_name:<22} {c.run_id:>3} {c.driver_pin:<10} "
                  f"{c.connector_pin:<10} {c.load_label:<10} {c.net_name:<25}")
        print(f"\nTotal: {len(all_cases)} legs across "
              f"{len(RS485_PAIRS)} differential pairs")
        return

    # Parse report
    driver_defaults, nets = parse_report(args.report)
    index = build_alias_index(nets)
    print(f"Parsed {len(nets)} nets from report.")

    print(f"\nDriver: SN55HVD75 A/B — Rout={HVD75_AB.rout:.0f}Ω, "
          f"tr/tf={HVD75_AB.rise_time:.1f}/{HVD75_AB.fall_time:.1f}ns (IBIS)")
    print(f"Cable:  {CABLE_3FT.length_ft:.0f}ft twisted pair, "
          f"Z0={CABLE_3FT.z0:.0f}Ω, Td={CABLE_3FT.delay_ns:.2f}ns")
    print(f"Load:   {R_TERM_DIFF}Ω differential termination "
          f"({R_TERM_SE:.0f}Ω per leg)")

    # Match cases to nets and compute
    results = []
    unmatched = []
    for case in all_cases:
        net = find_net(index, case.net_name)
        if net is None:
            unmatched.append(case.net_name)
            continue
        si = compute_case_si(net, case)
        results.append((case, si))

    if unmatched:
        unique_unmatched = sorted(set(unmatched))
        print(f"\nWarning: {len(unique_unmatched)} nets not found in report:")
        for n in unique_unmatched:
            print(f"  {n}")

    if results:
        print(f"\nComputed SI for {len(results)} legs:\n")
        print_results_table(results)
        print_diff_summary(results)

        write_results_csv(results, args.output)
        print(f"\nResults written to: {args.output}")
    else:
        print("\nNo matching nets found in report.")


if __name__ == "__main__":
    main()
