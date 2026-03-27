#!/usr/bin/env python3
"""
P3V3 Domain — Per-Net SI Run Case Definitions

Defines driver/receiver models and run cases for all 90 analyzable P3V3 nets.
Uses IBIS-extracted parameters where available, datasheet estimates otherwise.

Usage:
  python run_cases.py <hyperlynx_report.txt> [-o results.csv]
"""

import sys
import os
import re
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

# Import the existing analyzer
from hlx_si_analyzer import (
    parse_report, DriverModel, NetData, SIResult, compute_si,
    build_alias_index, find_net, _strip_board_suffix
)


# ---------------------------------------------------------------------------
# IC Buffer Models (from IBIS files and datasheets)
# ---------------------------------------------------------------------------
# Each model defines parameters for when the IC acts as DRIVER,
# plus its input capacitance for when it acts as RECEIVER.

@dataclass
class BufferModel:
    name: str
    vdd: float        # V
    rout: float        # Ω (output impedance when driving)
    rise_time: float   # ns (10-90%)
    fall_time: float   # ns (10-90%)
    cin: float         # pF (input capacitance when receiving / hi-Z)
    source: str        # "IBIS" or "datasheet"
    vih: float = 2.0   # V — receiver input high threshold (for noise margin)
    vil: float = 0.8   # V — receiver input low threshold (for noise margin)

MODELS = {
    # SN74LVC8T245 B-port at VCCB = 3.3V
    # IBIS: Rout_pd=13.0Ω, Rout_pu=16.0Ω → avg 14.5Ω at Vmeas=1.65V
    # [Ramp] dV/dt_r=1.717V/0.809ns, dV/dt_f=1.797V/0.649ns (R_load=50)
    # 10-90%: tr=1.44ns, tf=1.16ns
    # C_comp=6.05pF (typ)
    # Pin parasitics (TSSOP): R≈0.05Ω, L≈2.5nH, C≈0.3pF
    "LVC8T245_B": BufferModel(
        name="LVC8T245_B",
        vdd=3.3, rout=14.5, rise_time=1.44, fall_time=1.16,
        cin=6.05, source="IBIS"
    ),

    # SN55HVD75 Pin 1 (R = receiver output, push-pull CMOS 3.3V)
    # IBIS model "r": Vmeas=1.65V, Rout_pd=20.4Ω, Rout_pu=28.5Ω → avg 24.5Ω
    # [Ramp] dV/dt_r=1.351V/7.575ns, dV/dt_f=1.619V/6.821ns (R_load=50)
    # 10-90%: tr=10.1ns, tf=9.1ns (slew-rate limited RS-485 receiver output)
    # C_comp=2.78pF (typ)
    # Pin 1 parasitics: R=0.067Ω, L=2.984nH, C=0.442pF
    "HVD75_R": BufferModel(
        name="HVD75_R",
        vdd=3.3, rout=24.5, rise_time=10.1, fall_time=9.1,
        cin=2.78, source="IBIS"
    ),

    # SN55HVD75 Pin 4 (D = driver input, always receiver)
    # IBIS model "d_re": C_comp=0.762pF, Vinl=0.8V, Vinh=2.0V
    # Pin 4 parasitics: R=0.061Ω, L=2.826nH, C=0.433pF
    "HVD75_D": BufferModel(
        name="HVD75_D",
        vdd=3.3, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=0.762, source="IBIS"
    ),

    # SN55HVD75 Pins 2,3 (RE#, DE = enable inputs, always receiver)
    # IBIS: d_re (pin 2) C_comp=0.762pF, de (pin 3) C_comp=0.762pF
    # Pin 2 parasitics: R=0.057Ω, L=2.246nH, C=0.359pF
    # Pin 3 parasitics: R=0.056Ω, L=2.214nH, C=0.356pF
    "HVD75_EN": BufferModel(
        name="HVD75_EN",
        vdd=3.3, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=0.762, source="IBIS"
    ),

    # LMK04828 SDIO model (STATUS, STATUS_LD2 outputs; RESET receiver)
    # IBIS model "SDIO": I/O, Vmeas=1.65V, Rref=500Ω
    # Rout_pd=43.2Ω, Rout_pu=43.5Ω → avg 43.4Ω (corrected from prior 36Ω)
    # [Ramp] dV/dt_r=1.842V/0.326ns, dV/dt_f=1.851V/0.329ns (R_load=500)
    # 10-90%: tr=0.43ns, tf=0.44ns
    # C_comp=0.555pF (typ)
    # SDIO receiver: Vinl=0.4V, Vinh=1.6V (corrected from 1.4V)
    # Pin parasitics: STATUS(31) R=0.987Ω L=2.232nH C=0.306pF
    #                 STATUS_LD2(48) R=1.119Ω L=2.392nH C=0.317pF
    #                 RESET(5) R=0.740Ω L=1.645nH C=0.257pF
    "LMK04828_SDIO": BufferModel(
        name="LMK04828_SDIO",
        vdd=3.3, rout=43.4, rise_time=0.43, fall_time=0.44,
        cin=0.555, source="IBIS",
        vih=1.6, vil=0.4,  # IBIS SDIO model Vinh/Vinl
    ),

    # LMK04828 SYNC pin (pin 6) — input only, different model than SDIO
    # IBIS model "SYNC": Input, C_comp=0.184pF, Vinl=0.4V, Vinh=1.2V
    # Pin 6 parasitics: R=0.658Ω, L=1.644nH, C=0.224pF
    "LMK04828_SYNC": BufferModel(
        name="LMK04828_SYNC",
        vdd=3.3, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=0.184, source="IBIS",
        vih=1.2, vil=0.4,
    ),

    # DP83869 GPIO/management pins (MDIO, LED, etc.) at 3.3V
    # IBIS: dp83869.ibs — gpio models (IO_MUX_CFG drive strength unknown)
    # Rout range: 69.5Ω (max drive, gpio_1111_3p3) to 127.6Ω (min drive, gpio_0000_3p3)
    # Using mid-range estimate 98Ω pending firmware register readback
    # tr range: 0.52-0.75ns, tf range: 0.40-0.51ns — using conservative (slowest)
    # C_comp=1.106pF, Vinh=1.7V, Vinl=0.7V (from IBIS)
    "DP83869_GPIO": BufferModel(
        name="DP83869_GPIO",
        vdd=3.3, rout=98.0, rise_time=0.75, fall_time=0.51,
        cin=1.106, source="IBIS",
        vih=1.7, vil=0.7,
    ),
}


# ---------------------------------------------------------------------------
# Run Case Definitions
# ---------------------------------------------------------------------------

@dataclass
class RunCase:
    net_name: str            # P3V3 net name (without _BXX suffix)
    run_id: str              # e.g. "A1" for outbound, "A2" for inbound
    driver_pin: str          # e.g. "U58-1" or "LVC8T245 via R232"
    driver_model: str        # key into MODELS dict
    rx_pins: str             # e.g. "U58-4" or "U132-2,3"
    rx_model: str            # key into MODELS dict
    n_rx: int                # number of receiver pins (for total Cin)
    series_r_refdes: str     # e.g. "R232" — series resistor in path
    notes: str = ""


def build_ext_sync_cases() -> List[RunCase]:
    """Generate run cases for all 64 TRx_EXT_SYNC_P3V3 nets.

    Each net: HVD75 pins 1 (R output) + 4 (D input) + series R to LVC8T245.
    Two runs per net: outbound (LVC8T245→HVD75_D) and inbound (HVD75_R→LVC8T245).
    """
    # Net name → (HVD75 refdes, series R refdes) from netname_to_refdes.tsv
    ext_sync_nets = {
        "TR0_EXT_SYNC_P3V3":  ("U128", "R426"),
        "TR1_EXT_SYNC_P3V3":  ("U125", "R427"),
        "TR2_EXT_SYNC_P3V3":  ("U119", "R438"),
        "TR3_EXT_SYNC_P3V3":  ("U113", "R439"),
        "TR4_EXT_SYNC_P3V3":  ("U106", "R447"),
        "TR5_EXT_SYNC_P3V3":  ("U111", "R448"),
        "TR6_EXT_SYNC_P3V3":  ("U118", "R460"),
        "TR7_EXT_SYNC_P3V3":  ("U120", "R461"),
        "TR8_EXT_SYNC_P3V3":  ("U55",  "R233"),
        "TR9_EXT_SYNC_P3V3":  ("U58",  "R232"),
        "TR10_EXT_SYNC_P3V3": ("U64",  "R231"),
        "TR11_EXT_SYNC_P3V3": ("U66",  "R230"),
        "TR12_EXT_SYNC_P3V3": ("U62",  "R229"),
        "TR13_EXT_SYNC_P3V3": ("U59",  "R228"),
        "TR14_EXT_SYNC_P3V3": ("U52",  "R227"),
        "TR15_EXT_SYNC_P3V3": ("U50",  "R226"),
        "TR16_EXT_SYNC_P3V3": ("U21",  "R116"),
        "TR17_EXT_SYNC_P3V3": ("U23",  "R115"),
        "TR18_EXT_SYNC_P3V3": ("U25",  "R114"),
        "TR19_EXT_SYNC_P3V3": ("U28",  "R113"),
        "TR20_EXT_SYNC_P3V3": ("U27",  "R112"),
        "TR21_EXT_SYNC_P3V3": ("U24",  "R111"),
        "TR22_EXT_SYNC_P3V3": ("U22",  "R110"),
        "TR23_EXT_SYNC_P3V3": ("U19",  "R109"),
        "TR24_EXT_SYNC_P3V3": ("U1",   "R65"),
        "TR25_EXT_SYNC_P3V3": ("U5",   "R64"),
        "TR26_EXT_SYNC_P3V3": ("U140", "R63"),
        "TR27_EXT_SYNC_P3V3": ("U18",  "R62"),
        "TR28_EXT_SYNC_P3V3": ("U20",  "R61"),
        "TR29_EXT_SYNC_P3V3": ("U14",  "R60"),
        "TR30_EXT_SYNC_P3V3": ("U6",   "R59"),
        "TR31_EXT_SYNC_P3V3": ("U2",   "R58"),
        "TR32_EXT_SYNC_P3V3": ("U3",   "R43"),
        "TR33_EXT_SYNC_P3V3": ("U137", "R38"),
        "TR34_EXT_SYNC_P3V3": ("U9",   "R37"),
        "TR35_EXT_SYNC_P3V3": ("U13",  "R31"),
        "TR36_EXT_SYNC_P3V3": ("U16",  "R30"),
        "TR37_EXT_SYNC_P3V3": ("U15",  "R23"),
        "TR38_EXT_SYNC_P3V3": ("U139", "R22"),
        "TR39_EXT_SYNC_P3V3": ("U8",   "R17"),
        "TR40_EXT_SYNC_P3V3": ("U61",  "R221"),
        "TR41_EXT_SYNC_P3V3": ("U57",  "R220"),
        "TR42_EXT_SYNC_P3V3": ("U51",  "R219"),
        "TR43_EXT_SYNC_P3V3": ("U49",  "R218"),
        "TR44_EXT_SYNC_P3V3": ("U54",  "R217"),
        "TR45_EXT_SYNC_P3V3": ("U56",  "R216"),
        "TR46_EXT_SYNC_P3V3": ("U63",  "R215"),
        "TR47_EXT_SYNC_P3V3": ("U65",  "R214"),
        "TR48_EXT_SYNC_P3V3": ("U101", "R490"),
        "TR49_EXT_SYNC_P3V3": ("U99",  "R486"),
        "TR50_EXT_SYNC_P3V3": ("U97",  "R485"),
        "TR51_EXT_SYNC_P3V3": ("U95",  "R479"),
        "TR52_EXT_SYNC_P3V3": ("U96",  "R478"),
        "TR53_EXT_SYNC_P3V3": ("U98",  "R472"),
        "TR54_EXT_SYNC_P3V3": ("U100", "R471"),
        "TR55_EXT_SYNC_P3V3": ("U103", "R458"),
        "TR56_EXT_SYNC_P3V3": ("U131", "R416"),
        "TR57_EXT_SYNC_P3V3": ("U127", "R417"),
        "TR58_EXT_SYNC_P3V3": ("U117", "R418"),
        "TR59_EXT_SYNC_P3V3": ("U105", "R419"),
        "TR60_EXT_SYNC_P3V3": ("U102", "R420"),
        "TR61_EXT_SYNC_P3V3": ("U112", "R421"),
        "TR62_EXT_SYNC_P3V3": ("U123", "R422"),
        "TR63_EXT_SYNC_P3V3": ("U130", "R423"),
    }

    cases = []
    for net_name, (hvd75, r_ser) in sorted(ext_sync_nets.items()):
        # Run A: HVD75 R output (pin 1) drives → LVC8T245 receives (through R)
        # On this net: driver = hvd75-1, load = hvd75-4 (Cin) + R_ser (to LVC8T245)
        cases.append(RunCase(
            net_name=net_name,
            run_id="A",
            driver_pin=f"{hvd75}-1",
            driver_model="HVD75_R",
            rx_pins=f"{hvd75}-4",
            rx_model="HVD75_D",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="Inbound: HVD75 R output → LVC8T245 (via R)"
        ))
        # Run B: LVC8T245 B-port drives (through R) → HVD75 D (pin 4) receives
        # On this net: driver arrives through R_ser, load = hvd75-4 + hvd75-1 (hi-Z Cin)
        cases.append(RunCase(
            net_name=net_name,
            run_id="B",
            driver_pin=f"LVC8T245 via {r_ser}",
            driver_model="LVC8T245_B",
            rx_pins=f"{hvd75}-4,{hvd75}-1",
            rx_model="HVD75_D",
            n_rx=2,
            series_r_refdes=r_ser,
            notes="Outbound: LVC8T245 → HVD75 D input (via R)"
        ))

    return cases


def build_sync_en_cases() -> List[RunCase]:
    """Generate run cases for 8 SYNC_ENx_P3V3 nets.

    Each net: LVC8T245 B-port drives → series R → 16 HVD75 enable pins (8× pin 2 + 8× pin 3).
    Unidirectional: 1 run per net.
    """
    # SYNC_EN nets: each has 16 HVD75 enable pins + 2 resistors
    # Format: net_name → list of HVD75 refdes (8 per net)
    sync_en_nets = {
        "SYNC_EN0_P3V3": (["U128","U125","U120","U119","U118","U113","U111","U106"], "R315", "R1248"),
        "SYNC_EN1_P3V3": (["U66","U64","U62","U59","U58","U55","U52","U50"], "R314", "R1249"),
        "SYNC_EN2_P3V3": (["U28","U27","U25","U24","U23","U22","U21","U19"], "R313", "R1250"),
        "SYNC_EN3_P3V3": (["U140","U20","U18","U14","U6","U5","U2","U1"], "R312", "R1251"),
        "SYNC_EN4_P3V3": (["U139","U137","U16","U15","U13","U9","U8","U3"], "R311", "R1252"),
        "SYNC_EN5_P3V3": (["U65","U63","U61","U57","U56","U54","U51","U49"], "R310", "R1253"),
        "SYNC_EN6_P3V3": (["U103","U101","U100","U99","U98","U97","U96","U95"], "R309", "R1254"),
        "SYNC_EN7_P3V3": (["U131","U130","U127","U123","U117","U112","U105","U102"], "R308", "R1255"),
    }

    cases = []
    for net_name, (hvd75_list, r_ser, r_pull) in sorted(sync_en_nets.items()):
        rx_str = ",".join(f"{u}-2,{u}-3" for u in hvd75_list[:3]) + ",..."
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=f"LVC8T245 via {r_ser}",
            driver_model="LVC8T245_B",
            rx_pins=f"8×HVD75 pins 2,3 (16 loads)",
            rx_model="HVD75_EN",
            n_rx=16,
            series_r_refdes=r_ser,
            notes=f"1 driver → 16 enable inputs, total Cin≈48pF"
        ))

    return cases


def build_mbus_tx_cases() -> List[RunCase]:
    """Generate run cases for 5 xBUS_TX_P3V3 nets.

    Each net: HVD75 pin 4 (D input) + series R to LVC8T245.
    Unidirectional: LVC8T245 drives → HVD75 D receives.
    """
    tx_nets = {
        "CAL_MBUS_TX_P3V3":  ("U38",  "R131"),
        "TXLO_MBUS_TX_P3V3": ("U60",  "R197"),
        "RXLO_MBUS_TX_P3V3": ("U838", "R336"),
        "MBUS_TX_P3V3":      ("U132", "R281"),
        "CBUS_TX_P3V3":      ("U135", "R279"),
    }

    cases = []
    for net_name, (hvd75, r_ser) in sorted(tx_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=f"LVC8T245 via {r_ser}",
            driver_model="LVC8T245_B",
            rx_pins=f"{hvd75}-4",
            rx_model="HVD75_D",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="Outbound: LVC8T245 → HVD75 D input"
        ))

    return cases


def build_mbus_rx_cases() -> List[RunCase]:
    """Generate run cases for 5 xBUS_RX_P3V3 nets.

    Each net: LVC8T245 (U80) B-port pin + series R from HVD75 R output.
    Unidirectional: HVD75 R drives → LVC8T245 receives.
    """
    rx_nets = {
        "CAL_MBUS_RX_P3V3":  ("U80-19", "R119"),
        "TXLO_MBUS_RX_P3V3": ("U80-20", "R193"),
        "RXLO_MBUS_RX_P3V3": ("U80-21", "R1256"),
        "MBUS_RX_P3V3":      ("U80-18", "R480"),
        "CBUS_RX_P3V3":      ("U80-17", "R492"),
    }

    cases = []
    for net_name, (lvc_pin, r_ser) in sorted(rx_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=f"HVD75 R via {r_ser}",
            driver_model="HVD75_R",
            rx_pins=lvc_pin,
            rx_model="LVC8T245_B",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="Inbound: HVD75 R output → LVC8T245 B-port"
        ))

    return cases


def build_mbus_en_cases() -> List[RunCase]:
    """Generate run cases for 5 xBUS_EN_P3V3 nets.

    Each net: HVD75 pins 2 (RE#) + 3 (DE) + series R + pullup R.
    Unidirectional: LVC8T245 drives → HVD75 RE#/DE receive.
    """
    en_nets = {
        "CAL_MBUS_EN_P3V3":  ("U38",  "R132", "R1236"),
        "TXLO_MBUS_EN_P3V3": ("U60",  "R198", "R189"),   # Note: net has R198-2 and R189-1
        "RXLO_MBUS_EN_P3V3": ("U838", "R335", "R1260"),
        "MBUS_EN_P3V3":      ("U132", "R280", "R474"),
        "CBUS_EN_P3V3":      ("U135", "R278", "R493"),
    }

    cases = []
    for net_name, (hvd75, r_ser, r_pull) in sorted(en_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=f"LVC8T245 via {r_ser}",
            driver_model="LVC8T245_B",
            rx_pins=f"{hvd75}-2,{hvd75}-3",
            rx_model="HVD75_EN",
            n_rx=2,
            series_r_refdes=r_ser,
            notes=f"LVC8T245 → HVD75 RE#/DE (pullup {r_pull})"
        ))

    return cases


def build_lmk_cases() -> List[RunCase]:
    """Generate run cases for LMK04828-related P3V3 nets."""
    cases = []

    # LMK_STATUS_LD1_P3V3: LMK04828 U77-31 (STATUS, SDIO) → R260 → U90-17 (LVC8T245 B)
    cases.append(RunCase(
        net_name="LMK_STATUS_LD1_P3V3",
        run_id="—",
        driver_pin="U77-31 via R260",
        driver_model="LMK04828_SDIO",
        rx_pins="U90-17",
        rx_model="LVC8T245_B",
        n_rx=1,
        series_r_refdes="R260",
        notes="LMK STATUS → LVC8T245 B-port"
    ))

    # LMK_STATUS_LD2_P3V3: LMK04828 U77-48 (STATUS_LD2, SDIO) → R259 → U90-16 (LVC8T245 B)
    cases.append(RunCase(
        net_name="LMK_STATUS_LD2_P3V3",
        run_id="—",
        driver_pin="U77-48 via R259",
        driver_model="LMK04828_SDIO",
        rx_pins="U90-16",
        rx_model="LVC8T245_B",
        n_rx=1,
        series_r_refdes="R259",
        notes="LMK STATUS_LD2 → LVC8T245 B-port"
    ))

    # LMK_RST_P3V3: LVC8T245 → R282 → U77-5 (RESET, SDIO model = receiver)
    # Also has R222 (pullup to 3.3V for default-high reset)
    cases.append(RunCase(
        net_name="LMK_RST_P3V3",
        run_id="—",
        driver_pin="LVC8T245 via R282",
        driver_model="LVC8T245_B",
        rx_pins="U77-5",
        rx_model="LMK04828_SDIO",
        n_rx=1,
        series_r_refdes="R282",
        notes="LVC8T245 → LMK RESET (pullup R222)"
    ))

    # LMK_PLL_SYNC_P3V3: LVC8T245 → R234 → U77-6 (SYNC pin)
    cases.append(RunCase(
        net_name="LMK_PLL_SYNC_P3V3",
        run_id="—",
        driver_pin="LVC8T245 via R234",
        driver_model="LVC8T245_B",
        rx_pins="U77-6",
        rx_model="LMK04828_SYNC",
        n_rx=1,
        series_r_refdes="R234",
        notes="LVC8T245 → LMK SYNC (uses SYNC model, not SDIO)"
    ))

    return cases


def build_all_cases() -> List[RunCase]:
    """Build complete list of all run cases."""
    cases = []
    cases.extend(build_ext_sync_cases())
    cases.extend(build_sync_en_cases())
    cases.extend(build_mbus_tx_cases())
    cases.extend(build_mbus_rx_cases())
    cases.extend(build_mbus_en_cases())
    cases.extend(build_lmk_cases())
    return cases


# ---------------------------------------------------------------------------
# SI Computation with per-case models
# ---------------------------------------------------------------------------

def compute_case_si(net: NetData, case: RunCase, num_bounces: int = 40) -> SIResult:
    """Compute SI for a specific run case using per-case driver/receiver models."""
    drv = MODELS[case.driver_model]
    rx = MODELS[case.rx_model]

    driver = DriverModel(
        vdd=drv.vdd,
        rout=drv.rout,
        rise_time=drv.rise_time,
        fall_time=drv.fall_time,
        cin=rx.cin,          # receiver Cin per pin
        freq_mhz=1.0,
    )

    # Override the net's ic_receivers count with our case-specific n_rx
    # (compute_si uses N_rx = max(net.ic_receivers, 1))
    # We need to temporarily set it
    orig_rx = net.ic_receivers
    net.ic_receivers = case.n_rx
    result = compute_si(net, driver, num_bounces=num_bounces)
    net.ic_receivers = orig_rx

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results_table(results: List[Tuple[RunCase, SIResult]]):
    """Print results in the user's requested format with Driver Pin and RX Pin columns."""
    hdr = (f"{'Signal':<32} {'Run':>3} {'Driver Pin':<22} {'RX Pin':<22} "
           f"{'Z0':>5} {'Td(ps)':>8} {'Len':>7} {'C(pF)':>6} "
           f"{'VOH':>6} {'VOL':>6} {'tr(ns)':>7} {'tf(ns)':>7} "
           f"{'OS%':>6} {'NMH':>6} {'NML':>6}")
    print(hdr)
    print("-" * len(hdr))

    for case, si in results:
        drv_short = case.driver_pin[:21]
        rx_short = case.rx_pins[:21]
        name = case.net_name[:31]
        rx_mdl = MODELS[case.rx_model]
        nmh = si.voh - rx_mdl.vih  # noise margin high
        nml = rx_mdl.vil - si.vol  # noise margin low
        print(f"{name:<32} {case.run_id:>3} {drv_short:<22} {rx_short:<22} "
              f"{si.z0_avg:5.1f} {si.delay_ps:8.1f} {si.length_in:7.3f} {si.cap_pf:6.1f} "
              f"{si.voh:6.3f} {si.vol:6.3f} {si.rise_time_ns:7.2f} {si.fall_time_ns:7.2f} "
              f"{si.overshoot_pct:6.1f} {nmh:6.3f} {nml:6.3f}")


def write_results_csv(results: List[Tuple[RunCase, SIResult]], filepath: str):
    """Write results to CSV with driver/rx pin columns."""
    import csv
    fields = [
        "net_name", "run_id", "driver_pin", "driver_model", "rx_pins", "rx_model",
        "n_rx", "series_r", "z0_avg", "delay_ps", "length_in", "cap_pf",
        "voh", "vol", "rise_time_ns", "fall_time_ns",
        "overshoot_pct", "undershoot_pct", "nmh", "nml",
        "peak_v", "trough_v", "gamma_source", "settling_time_ns", "notes"
    ]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for case, si in results:
            rx_mdl = MODELS[case.rx_model]
            nmh = si.voh - rx_mdl.vih
            nml = rx_mdl.vil - si.vol
            writer.writerow({
                "net_name": case.net_name,
                "run_id": case.run_id,
                "driver_pin": case.driver_pin,
                "driver_model": case.driver_model,
                "rx_pins": case.rx_pins,
                "rx_model": case.rx_model,
                "n_rx": case.n_rx,
                "series_r": case.series_r_refdes,
                "z0_avg": round(si.z0_avg, 1),
                "delay_ps": round(si.delay_ps, 1),
                "length_in": round(si.length_in, 3),
                "cap_pf": round(si.cap_pf, 1),
                "voh": round(si.voh, 3),
                "vol": round(si.vol, 3),
                "rise_time_ns": round(si.rise_time_ns, 2),
                "fall_time_ns": round(si.fall_time_ns, 2),
                "overshoot_pct": round(si.overshoot_pct, 1),
                "undershoot_pct": round(si.undershoot_pct, 1),
                "nmh": round(nmh, 3),
                "nml": round(nml, 3),
                "peak_v": round(si.peak_v, 3),
                "trough_v": round(si.trough_v, 3),
                "gamma_source": round(si.gamma_source, 4),
                "settling_time_ns": round(si.settling_time_ns, 2),
                "notes": case.notes,
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run per-case SI analysis for P3V3 domain nets"
    )
    parser.add_argument("report", help="Path to HyperLynx batch report file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output CSV file")
    parser.add_argument("--list-cases", action="store_true",
                        help="List all defined run cases without computing")

    args = parser.parse_args()

    all_cases = build_all_cases()

    if args.list_cases:
        print(f"\n{'='*130}")
        print(f"  P3V3 Domain — {len(all_cases)} Run Cases Defined")
        print(f"{'='*130}\n")
        hdr = (f"{'Signal':<32} {'Run':>3} {'Driver Pin':<22} {'Drv Model':<16} "
               f"{'RX Pin':<22} {'RX Model':<12} {'N_rx':>4}")
        print(hdr)
        print("-" * len(hdr))
        for c in all_cases:
            print(f"{c.net_name[:31]:<32} {c.run_id:>3} {c.driver_pin[:21]:<22} "
                  f"{c.driver_model:<16} {c.rx_pins[:21]:<22} {c.rx_model:<12} {c.n_rx:>4}")
        print(f"\nTotal: {len(all_cases)} run cases across "
              f"{len(set(c.net_name for c in all_cases))} nets")

        # Count by group
        ext_sync = sum(1 for c in all_cases if "EXT_SYNC" in c.net_name)
        sync_en = sum(1 for c in all_cases if "SYNC_EN" in c.net_name)
        tx = sum(1 for c in all_cases if c.net_name.endswith("TX_P3V3"))
        rx = sum(1 for c in all_cases if c.net_name.endswith("RX_P3V3"))
        en = sum(1 for c in all_cases if c.net_name.endswith("EN_P3V3"))
        lmk = sum(1 for c in all_cases if "LMK" in c.net_name)
        print(f"\nBreakdown: EXT_SYNC={ext_sync}, SYNC_EN={sync_en}, "
              f"TX={tx}, RX={rx}, EN={en}, LMK={lmk}")
        return

    # Parse report
    driver_defaults, nets = parse_report(args.report)
    index = build_alias_index(nets)
    print(f"Parsed {len(nets)} nets from report.")

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
        for n in unique_unmatched[:10]:
            print(f"  {n}")
        if len(unique_unmatched) > 10:
            print(f"  ... and {len(unique_unmatched) - 10} more")

    if results:
        print(f"\nComputed SI for {len(results)} run cases:\n")
        print_results_table(results)

        out_path = args.output or args.report.rsplit(".", 1)[0] + "_p3v3_cases.csv"
        write_results_csv(results, out_path)
        print(f"\nResults written to: {out_path}")
    else:
        print("\nNo matching nets found in report.")


if __name__ == "__main__":
    main()
