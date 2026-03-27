#!/usr/bin/env python3
"""
P1V8 Domain — Per-Net SI Run Case Definitions

Defines driver/receiver models and run cases for all analyzable P1V8 nets.
Uses IBIS-extracted parameters where available, datasheet estimates otherwise.

Models:
  - FPGA (XQVU13P) HP_LVCMOS18_S_2: IBIS-extracted Rout, estimated tr/tf
  - Zynq (XQZU3EG) HP_LVCMOS18_S_2: same model per user direction
  - SN74LVC8T245 at 1.8V: scaled from 3.3V IBIS (Rout, tr/tf)
  - TXS0104 A-side 1.8V: IBIS Cin only (passive, receiver-only)
  - DP83869 MDIO: datasheet estimate
  - LM239A: open-collector comparator (receiver-only for SI)

Usage:
  python3 run_cases_p1v8.py <hyperlynx_report.txt> [-o results.csv] [--list-cases]
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

@dataclass
class BufferModel:
    name: str
    vdd: float        # V
    rout: float        # Ω (output impedance when driving)
    rise_time: float   # ns (10-90%)
    fall_time: float   # ns (10-90%)
    cin: float         # pF (input capacitance when receiving / hi-Z)
    source: str        # "IBIS" or "datasheet"
    vih: float = 0.0   # V — receiver input high threshold (for noise margin)
    vil: float = 0.0   # V — receiver input low threshold (for noise margin)

MODELS = {
    # XQVU13P / XQZU3EG — HP_LVCMOS18_S_2 (SLOW slew, drive strength 2)
    # IBIS: Rout_pd=121.7Ω (V/I @ 0.9V), Rout_pu=112.1Ω (V/I @ 0.9V)
    # Average Rout = 117Ω
    # C_comp=2.694pF (typ), Voltage Range=1.8V
    # [Ramp] dV/dt_r=0.3329/0.1684ns, dV/dt_f=0.3128/0.1778ns (R_load=50)
    # 10-90% from waveform data: tr≈0.28ns, tf≈0.24ns
    "FPGA_LVCMOS18": BufferModel(
        name="FPGA_LVCMOS18",
        vdd=1.8, rout=117.0, rise_time=0.28, fall_time=0.24,
        cin=2.694, source="IBIS",
        vih=1.17, vil=0.63,  # LVCMOS18: 0.65*VDD / 0.35*VDD
    ),

    # Zynq uses same HP_LVCMOS18_S_2 model per user direction
    "ZYNQ_LVCMOS18": BufferModel(
        name="ZYNQ_LVCMOS18",
        vdd=1.8, rout=117.0, rise_time=0.28, fall_time=0.24,
        cin=2.694, source="IBIS",
        vih=1.17, vil=0.63,
    ),

    # SN74LVC8T245 port at 1.8V (A-side or B-side, whichever is at 1.8V)
    # Scaled from 3.3V IBIS (Rout=25Ω at 3.3V → ~50Ω at 1.8V)
    # Datasheet: VOH ≥ VCC-0.45V @ -2mA, VOL ≤ 0.45V @ 2mA at 1.8V
    # Cin per pin ≈ 6 pF (datasheet Table 6.6, independent of VCC)
    # IBIS: sn74lvc8t245.ibs — 1.8V model extracted
    # Rout_pd=21.5Ω, Rout_pu=23.9Ω → avg 22.7Ω
    # [Ramp] dV/dt_r=0.7697/0.5776ns → 20-80% × 1.333 = 2.99ns (10-90%)
    # [Ramp] dV/dt_f=0.8072/0.4659ns → 20-80% × 1.333 = 2.41ns (10-90%)
    # C_comp=6.34pF, VIH=0.75*1.69=1.2675V, VIL=0.25*2.31=0.5775V
    "LVC8T245_18": BufferModel(
        name="LVC8T245_18",
        vdd=1.8, rout=22.7, rise_time=2.99, fall_time=2.41,
        cin=6.34, source="IBIS",
        vih=1.2675, vil=0.5775,
    ),

    # TXS0104 A-side at 1.8V — passive open-drain level translator
    # IBIS: LVC1T65_IO_A_18, Model_type=Input, C_comp=4.18pF
    # No active output driver — always acts as receiver
    "TXS0104_A18": BufferModel(
        name="TXS0104_A18",
        vdd=1.8, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=4.18, source="IBIS",
        vih=1.17, vil=0.63,
    ),

    # TXS0104 OE (enable) pin at 1.8V
    # IBIS: LVC1T65_IN_18, C_comp=3.03pF
    "TXS0104_OE": BufferModel(
        name="TXS0104_OE",
        vdd=1.8, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=3.03, source="IBIS",
        vih=1.17, vil=0.63,
    ),

    # DP83869 MDIO/MDC at 1.8V LVCMOS I/O
    # IBIS: dp83869.ibs — MDIO model (IO_MUX_CFG drive strength unknown)
    # Rout range: 36.5Ω (max drive, 0x1F) to 69.5Ω (min drive, 0x00)
    # Using mid-range estimate 50Ω pending firmware register readback
    # tr/tf from IBIS [Ramp]: 0.14-0.32ns rise, 0.13-0.33ns fall (drive-dependent)
    # Using conservative (slowest) values: tr=0.32ns, tf=0.33ns
    # C_comp=1.311pF, VIH=0.669*1.8=1.204V, VIL=0.191*1.8=0.344V
    "DP83869_MDIO": BufferModel(
        name="DP83869_MDIO",
        vdd=1.8, rout=50.0, rise_time=0.32, fall_time=0.33,
        cin=1.311, source="IBIS",
        vih=1.204, vil=0.344,
    ),

    # LM239A — quad comparator, open-collector output
    # Not a CMOS push-pull driver; treated as receiver for SI
    "LM239A": BufferModel(
        name="LM239A",
        vdd=1.8, rout=999.0, rise_time=99.0, fall_time=99.0,
        cin=5.0, source="datasheet-est",
        vih=1.17, vil=0.63,
    ),
}


# ---------------------------------------------------------------------------
# Run Case Definitions
# ---------------------------------------------------------------------------

@dataclass
class RunCase:
    net_name: str            # P1V8 net name (without board suffix)
    run_id: str              # e.g. "A" for outbound, "B" for inbound
    driver_pin: str          # e.g. "U10-P15" or "LVC8T245 via R268"
    driver_model: str        # key into MODELS dict
    rx_pins: str             # e.g. "U10-B29" or "8×U83 pins"
    rx_model: str            # key into MODELS dict
    n_rx: int                # number of receiver pins (for total Cin)
    series_r_refdes: str     # e.g. "R268" — series resistor in path
    notes: str = ""


def build_ext_sync_p1v8_cases() -> List[RunCase]:
    """Generate run cases for all 64 TRx_EXT_SYNC_P1V8 nets.

    Each net: FPGA (U10) pin → series R → [LVC8T245 A-side at 1.8V on other net].
    Two runs per net: outbound (FPGA drives) and inbound (LVC8T245 drives).
    """
    # Net name → (FPGA pin, series R refdes) from netname_to_refdes.tsv
    ext_sync_nets = {
        "TR0_EXT_SYNC_P1V8":  ("U10-H14",  "R428"),
        "TR1_EXT_SYNC_P1V8":  ("U10-H13",  "R429"),
        "TR2_EXT_SYNC_P1V8":  ("U10-F13",  "R440"),
        "TR3_EXT_SYNC_P1V8":  ("U10-E13",  "R441"),
        "TR4_EXT_SYNC_P1V8":  ("U10-G14",  "R449"),
        "TR5_EXT_SYNC_P1V8":  ("U10-G13",  "R450"),
        "TR6_EXT_SYNC_P1V8":  ("U10-E16",  "R462"),
        "TR7_EXT_SYNC_P1V8":  ("U10-E15",  "R463"),
        "TR8_EXT_SYNC_P1V8":  ("U10-R16",  "R269"),
        "TR9_EXT_SYNC_P1V8":  ("U10-P15",  "R268"),
        "TR10_EXT_SYNC_P1V8": ("U10-N14",  "R267"),
        "TR11_EXT_SYNC_P1V8": ("U10-N13",  "R266"),
        "TR12_EXT_SYNC_P1V8": ("U10-N16",  "R265"),
        "TR13_EXT_SYNC_P1V8": ("U10-M16",  "R264"),
        "TR14_EXT_SYNC_P1V8": ("U10-L15",  "R263"),
        "TR15_EXT_SYNC_P1V8": ("U10-K15",  "R262"),
        "TR16_EXT_SYNC_P1V8": ("U10-E36",  "R141"),
        "TR17_EXT_SYNC_P1V8": ("U10-E37",  "R140"),
        "TR18_EXT_SYNC_P1V8": ("U10-G38",  "R139"),
        "TR19_EXT_SYNC_P1V8": ("U10-F38",  "R138"),
        "TR20_EXT_SYNC_P1V8": ("U10-E39",  "R137"),
        "TR21_EXT_SYNC_P1V8": ("U10-E40",  "R136"),
        "TR22_EXT_SYNC_P1V8": ("U10-F35",  "R135"),
        "TR23_EXT_SYNC_P1V8": ("U10-E35",  "R134"),
        "TR24_EXT_SYNC_P1V8": ("U10-L35",  "R84"),
        "TR25_EXT_SYNC_P1V8": ("U10-L36",  "R83"),
        "TR26_EXT_SYNC_P1V8": ("U10-L33",  "R82"),
        "TR27_EXT_SYNC_P1V8": ("U10-K33",  "R81"),
        "TR28_EXT_SYNC_P1V8": ("U10-K35",  "R80"),
        "TR29_EXT_SYNC_P1V8": ("U10-J35",  "R79"),
        "TR30_EXT_SYNC_P1V8": ("U10-H33",  "R78"),
        "TR31_EXT_SYNC_P1V8": ("U10-H34",  "R77"),
        "TR32_EXT_SYNC_P1V8": ("U10-AW35", "R42"),
        "TR33_EXT_SYNC_P1V8": ("U10-AW36", "R36"),
        "TR34_EXT_SYNC_P1V8": ("U10-AV36", "R35"),
        "TR35_EXT_SYNC_P1V8": ("U10-AV37", "R29"),
        "TR36_EXT_SYNC_P1V8": ("U10-AU34", "R28"),
        "TR37_EXT_SYNC_P1V8": ("U10-AU35", "R21"),
        "TR38_EXT_SYNC_P1V8": ("U10-AV38", "R20"),
        "TR39_EXT_SYNC_P1V8": ("U10-AW38", "R16"),
        "TR40_EXT_SYNC_P1V8": ("U10-BE38", "R251"),
        "TR41_EXT_SYNC_P1V8": ("U10-BF38", "R250"),
        "TR42_EXT_SYNC_P1V8": ("U10-BF42", "R249"),
        "TR43_EXT_SYNC_P1V8": ("U10-BF43", "R248"),
        "TR44_EXT_SYNC_P1V8": ("U10-BD39", "R247"),
        "TR45_EXT_SYNC_P1V8": ("U10-BD40", "R246"),
        "TR46_EXT_SYNC_P1V8": ("U10-BC37", "R245"),
        "TR47_EXT_SYNC_P1V8": ("U10-BC38", "R244"),
        "TR48_EXT_SYNC_P1V8": ("U10-AY11", "R489"),
        "TR49_EXT_SYNC_P1V8": ("U10-BA11", "R484"),
        "TR50_EXT_SYNC_P1V8": ("U10-AU14", "R483"),
        "TR51_EXT_SYNC_P1V8": ("U10-AV14", "R477"),
        "TR52_EXT_SYNC_P1V8": ("U10-AW15", "R476"),
        "TR53_EXT_SYNC_P1V8": ("U10-AY15", "R470"),
        "TR54_EXT_SYNC_P1V8": ("U10-AU13", "R469"),
        "TR55_EXT_SYNC_P1V8": ("U10-AV13", "R457"),
        "TR56_EXT_SYNC_P1V8": ("U10-BF10", "R383"),
        "TR57_EXT_SYNC_P1V8": ("U10-BF9",  "R384"),
        "TR58_EXT_SYNC_P1V8": ("U10-BC7",  "R385"),
        "TR59_EXT_SYNC_P1V8": ("U10-BD7",  "R386"),
        "TR60_EXT_SYNC_P1V8": ("U10-BD9",  "R387"),
        "TR61_EXT_SYNC_P1V8": ("U10-BD8",  "R388"),
        "TR62_EXT_SYNC_P1V8": ("U10-BA10", "R389"),
        "TR63_EXT_SYNC_P1V8": ("U10-BA9",  "R390"),
    }

    cases = []
    for net_name, (fpga_pin, r_ser) in sorted(ext_sync_nets.items()):
        # Run A: FPGA drives outbound → LVC8T245 receives (through series R)
        cases.append(RunCase(
            net_name=net_name,
            run_id="A",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=f"LVC8T245 via {r_ser}",
            rx_model="LVC8T245_18",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="Outbound: FPGA → LVC8T245 A-port (via R)"
        ))
        # Run B: LVC8T245 drives inbound → FPGA receives (through series R)
        cases.append(RunCase(
            net_name=net_name,
            run_id="B",
            driver_pin=f"LVC8T245 via {r_ser}",
            driver_model="LVC8T245_18",
            rx_pins=fpga_pin,
            rx_model="FPGA_LVCMOS18",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="Inbound: LVC8T245 A-port → FPGA (via R)"
        ))

    return cases


def build_te_ext_sync_case() -> List[RunCase]:
    """TE_EXT_SYNC_P1V8: U72-19 (LVC8T245 B1 at 1.8V) ↔ U10-B29 + R525.

    Bidirectional: 2 runs.
    """
    cases = []
    # Run A: LVC8T245 drives → FPGA receives
    cases.append(RunCase(
        net_name="TE_EXT_SYNC_P1V8",
        run_id="A",
        driver_pin="U72-19",
        driver_model="LVC8T245_18",
        rx_pins="U10-B29",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R525",
        notes="LVC8T245 B1 → FPGA (via R525)"
    ))
    # Run B: FPGA drives → LVC8T245 receives
    cases.append(RunCase(
        net_name="TE_EXT_SYNC_P1V8",
        run_id="B",
        driver_pin="U10-B29",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U72-19",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R525",
        notes="FPGA → LVC8T245 B1 (via R525)"
    ))
    return cases


def build_sync_en_p1v8_case() -> List[RunCase]:
    """SYNC_EN_P1V8: U10-F15 (FPGA) drives → 8 U83 pins (LVC8T245).

    Multi-drop: 1 driver → 8 receivers (pins 14-19 are B-port data + pin 20 DIR).
    Unidirectional: 1 run.
    """
    return [RunCase(
        net_name="SYNC_EN_P1V8",
        run_id="—",
        driver_pin="U10-F15",
        driver_model="FPGA_LVCMOS18",
        rx_pins="8×U83 pins (14-21)",
        rx_model="LVC8T245_18",
        n_rx=8,
        series_r_refdes="—",
        notes="FPGA → 8 LVC8T245 pins, multi-drop ~48pF"
    )]


def build_dir_ab_p1v8_case() -> List[RunCase]:
    """DIR_AB_P1V8: U10-M14 (FPGA) drives → 8 U91 pins (LVC8T245).

    Multi-drop direction control: 1 driver → 8 receivers.
    """
    return [RunCase(
        net_name="DIR_AB_P1V8",
        run_id="—",
        driver_pin="U10-M14",
        driver_model="FPGA_LVCMOS18",
        rx_pins="8×U91 pins (14-21)",
        rx_model="LVC8T245_18",
        n_rx=8,
        series_r_refdes="—",
        notes="FPGA → 8 LVC8T245 DIR pins, multi-drop ~48pF"
    )]


def build_srs_ucd_cases() -> List[RunCase]:
    """SRSx_UCD_DATA/CLK/ALERT/CNTRL P1V8 nets (12 data/clk/alert + 3 cntrl = 15 nets).

    DATA/CLK/ALERT: FPGA drives → series R → TXS0104 (receiver only).
    CNTRL: TXS0104 (passive, B-side drives through) → FPGA receives.
      CNTRL is excluded from active-driver SI since TXS0104 has no push-pull driver.
    """
    # FPGA-driven UCD nets (through series R)
    fpga_driven = {
        "SRS0_UCD_DATA_P1V8":  ("U10-AW33", "R52"),
        "SRS0_UCD_CLK_P1V8":   ("U10-AV31", "R48"),
        "SRS0_UCD_ALERT_P1V8": ("U10-AV33", "R55"),
        "SRS1_UCD_DATA_P1V8":  ("U10-D35",  "R143"),
        "SRS1_UCD_CLK_P1V8":   ("U10-J29",  "R144"),
        "SRS1_UCD_ALERT_P1V8": ("U10-D34",  "R142"),
        "SRS2_UCD_DATA_P1V8":  ("U10-AU30", "R397"),
        "SRS2_UCD_CLK_P1V8":   ("U10-AU27", "R398"),
        "SRS2_UCD_ALERT_P1V8": ("U10-AU29", "R392"),
    }

    # TXS0104-to-FPGA CNTRL nets (TXS0104 is passive — open-drain with ~10kΩ pullup)
    cntrl_nets = {
        "SRS0_UCD_CNTRL_P1V8": ("U17-2",  "U10-AY31"),
        "SRS1_UCD_CNTRL_P1V8": ("U43-2",  "U10-K31"),
        "SRS2_UCD_CNTRL_P1V8": ("U110-2", "U10-AV29"),
    }

    cases = []
    for net_name, (fpga_pin, r_ser) in sorted(fpga_driven.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=f"TXS0104 via {r_ser}",
            rx_model="TXS0104_A18",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="FPGA → TXS0104 (receiver only, passive translator)"
        ))

    # CNTRL nets: TXS0104 has no active driver; model FPGA as driver for
    # worst-case analysis (FPGA pulling against TXS0104 pullup)
    for net_name, (txs_pin, fpga_pin) in sorted(cntrl_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=txs_pin,
            rx_model="TXS0104_A18",
            n_rx=1,
            series_r_refdes="—",
            notes="Bidirectional: FPGA drives, TXS0104 is passive load"
        ))

    return cases


def build_srs_pll_sync_cases() -> List[RunCase]:
    """SRSx_PLL_SYNC_P1V8 nets (3 nets).

    TXS0104 (U67) → FPGA. TXS0104 is passive, so FPGA is modeled as driver
    for worst-case analysis.
    """
    pll_sync_nets = {
        "SRS0_PLL_SYNC_P1V8": ("U67-3", "U10-P28"),
        "SRS1_PLL_SYNC_P1V8": ("U67-4", "U10-A29"),
        "SRS2_PLL_SYNC_P1V8": ("U67-5", "U10-A30"),
    }

    cases = []
    for net_name, (txs_pin, fpga_pin) in sorted(pll_sync_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=txs_pin,
            rx_model="TXS0104_A18",
            n_rx=1,
            series_r_refdes="—",
            notes="FPGA drives, TXS0104 passive receiver"
        ))

    return cases


def build_prs_ucd_cases() -> List[RunCase]:
    """PRS_UCD_DATA/CLK/ALERT/CNTRL P1V8 nets (4 nets).

    Same pattern as SRS UCD nets.
    """
    cases = []

    # FPGA-driven
    fpga_driven = {
        "PRS_UCD_DATA_P1V8":  ("U10-F18", "R323"),
        "PRS_UCD_CLK_P1V8":   ("U10-F17", "R322"),
        "PRS_UCD_ALERT_P1V8": ("U10-D19", "R324"),
    }
    for net_name, (fpga_pin, r_ser) in sorted(fpga_driven.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=f"TXS0104 via {r_ser}",
            rx_model="TXS0104_A18",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="FPGA → TXS0104 (passive receiver)"
        ))

    # CNTRL: TXS0104 U86-2 → FPGA U10-D20
    cases.append(RunCase(
        net_name="PRS_UCD_CNTRL_P1V8",
        run_id="—",
        driver_pin="U10-D20",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U86-2",
        rx_model="TXS0104_A18",
        n_rx=1,
        series_r_refdes="—",
        notes="Bidirectional: FPGA drives, TXS0104 passive"
    ))

    return cases


def build_prs_spi_cases() -> List[RunCase]:
    """PRS_SPI0 P1V8 nets — FPGA ↔ Zynq SPI interface (6 nets).

    Both endpoints use HP_LVCMOS18_S_2. Direction depends on signal function.
    """
    cases = []

    # CS_N: master (FPGA or Zynq) drives chip select
    # VX_CS_N: FPGA U10-C18 → R354 (to Zynq via level translator)
    cases.append(RunCase(
        net_name="PRS_SPI0_VX_CS_N_P1V8",
        run_id="—",
        driver_pin="U10-C18",
        driver_model="FPGA_LVCMOS18",
        rx_pins="Zynq via R354",
        rx_model="ZYNQ_LVCMOS18",
        n_rx=1,
        series_r_refdes="R354",
        notes="FPGA CS_N → Zynq"
    ))

    # PS_CS_N: Zynq U4-AC21 → R353
    cases.append(RunCase(
        net_name="PRS_SPI0_PS_CS_N_P1V8",
        run_id="—",
        driver_pin="U4-AC21",
        driver_model="ZYNQ_LVCMOS18",
        rx_pins="FPGA via R353",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R353",
        notes="Zynq CS_N → FPGA"
    ))

    # SCLK_VX: FPGA U10-C21 → R355
    cases.append(RunCase(
        net_name="PRS_SPI0_SCLK_P1V8_VX",
        run_id="—",
        driver_pin="U10-C21",
        driver_model="FPGA_LVCMOS18",
        rx_pins="Zynq via R355",
        rx_model="ZYNQ_LVCMOS18",
        n_rx=1,
        series_r_refdes="R355",
        notes="FPGA SCLK → Zynq"
    ))

    # SCLK_PS: Zynq U4-AB20 → R526
    cases.append(RunCase(
        net_name="PRS_SPI0_SCLK_P1V8_PS",
        run_id="—",
        driver_pin="U4-AB20",
        driver_model="ZYNQ_LVCMOS18",
        rx_pins="FPGA via R526",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R526",
        notes="Zynq SCLK → FPGA"
    ))

    # MOSI: FPGA U10-C19 + Zynq U4-AB18 + R356
    # MOSI = Master Out Slave In — master drives
    # Both FPGA and Zynq on net; whichever is master drives
    cases.append(RunCase(
        net_name="PRS_SPI0_MOSI_P1V8",
        run_id="A",
        driver_pin="U10-C19",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U4-AB18",
        rx_model="ZYNQ_LVCMOS18",
        n_rx=1,
        series_r_refdes="R356",
        notes="FPGA MOSI → Zynq"
    ))
    cases.append(RunCase(
        net_name="PRS_SPI0_MOSI_P1V8",
        run_id="B",
        driver_pin="U4-AB18",
        driver_model="ZYNQ_LVCMOS18",
        rx_pins="U10-C19",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R356",
        notes="Zynq MOSI → FPGA"
    ))

    # MISO: U81-16 (LVC8T245 B4) + FPGA U10-B21 + R1027
    cases.append(RunCase(
        net_name="PRS_SPI0_MISO_P1V8",
        run_id="A",
        driver_pin="U81-16",
        driver_model="LVC8T245_18",
        rx_pins="U10-B21",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R1027",
        notes="LVC8T245 → FPGA MISO"
    ))
    cases.append(RunCase(
        net_name="PRS_SPI0_MISO_P1V8",
        run_id="B",
        driver_pin="U10-B21",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U81-16",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R1027",
        notes="FPGA → LVC8T245 MISO"
    ))

    return cases


def build_prs_misc_cases() -> List[RunCase]:
    """PRS_PS_PCIE_RESET_N and PRS_PLL_SYNC P1V8 nets."""
    cases = []

    # PRS_PS_PCIE_RESET_N_P1V8: Zynq U4-G16 → R306
    cases.append(RunCase(
        net_name="PRS_PS_PCIE_RESET_N_P1V8",
        run_id="—",
        driver_pin="U4-G16",
        driver_model="ZYNQ_LVCMOS18",
        rx_pins="FPGA via R306",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R306",
        notes="Zynq PCIE_RESET_N → FPGA"
    ))

    # PRS_PLL_SYNC_P1V8: U67-2 (TXS0104) → U10-R28
    # TXS0104 passive, FPGA modeled as driver for analysis
    cases.append(RunCase(
        net_name="PRS_PLL_SYNC_P1V8",
        run_id="—",
        driver_pin="U10-R28",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U67-2",
        rx_model="TXS0104_A18",
        n_rx=1,
        series_r_refdes="—",
        notes="FPGA drives, TXS0104 passive receiver"
    ))

    return cases


def build_lmk_p1v8_cases() -> List[RunCase]:
    """LMK status/control P1V8 nets (4 nets)."""
    cases = []

    # LMK_STATUS_LD1_P1V8: U10-D28 → R349 (FPGA → LMK04828 via level translator)
    cases.append(RunCase(
        net_name="LMK_STATUS_LD1_P1V8",
        run_id="—",
        driver_pin="U10-D28",
        driver_model="FPGA_LVCMOS18",
        rx_pins="LMK via R349",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R349",
        notes="FPGA → LMK STATUS (via level translator)"
    ))

    # LMK_STATUS_LD2_P1V8: U10-E28 → R350
    cases.append(RunCase(
        net_name="LMK_STATUS_LD2_P1V8",
        run_id="—",
        driver_pin="U10-E28",
        driver_model="FPGA_LVCMOS18",
        rx_pins="LMK via R350",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R350",
        notes="FPGA → LMK STATUS_LD2 (via level translator)"
    ))

    # LMK_RST_P1V8: U81-21 (LVC8T245 VCCB/pin) → U10-G22
    # Pin 21 on LVC8T245 is VCCB; on this net it's the level translator output
    cases.append(RunCase(
        net_name="LMK_RST_P1V8",
        run_id="—",
        driver_pin="U81-21",
        driver_model="LVC8T245_18",
        rx_pins="U10-G22",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="—",
        notes="LVC8T245 → FPGA LMK_RST"
    ))

    # LMK_PLL_SYNC_P1V8: U72-14 (LVC8T245 B6) → U10-C27
    cases.append(RunCase(
        net_name="LMK_PLL_SYNC_P1V8",
        run_id="—",
        driver_pin="U72-14",
        driver_model="LVC8T245_18",
        rx_pins="U10-C27",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="—",
        notes="LVC8T245 → FPGA LMK_PLL_SYNC"
    ))

    return cases


def build_ge1_cases() -> List[RunCase]:
    """GE1_MDIO/MDC P1V8 nets — DP83869 Ethernet PHY management (2 nets)."""
    cases = []

    # GE1_MDIO_P1V8: U874-41 (DP83869) + U78-5 (TXS0104) + R1713
    # MDIO is bidirectional — DP83869 can drive or receive
    cases.append(RunCase(
        net_name="GE1_MDIO_P1V8",
        run_id="A",
        driver_pin="U874-41",
        driver_model="DP83869_MDIO",
        rx_pins="U78-5",
        rx_model="TXS0104_A18",
        n_rx=1,
        series_r_refdes="R1713",
        notes="DP83869 MDIO → TXS0104 (via R)"
    ))
    cases.append(RunCase(
        net_name="GE1_MDIO_P1V8",
        run_id="B",
        driver_pin="TXS0104 via R1713",
        driver_model="DP83869_MDIO",
        rx_pins="U874-41",
        rx_model="DP83869_MDIO",
        n_rx=1,
        series_r_refdes="R1713",
        notes="External → DP83869 MDIO (via TXS0104 + R)"
    ))

    # GE1_MDC_P1V8: U874-42 (DP83869 MDC output) → R307
    cases.append(RunCase(
        net_name="GE1_MDC_P1V8",
        run_id="—",
        driver_pin="U874-42",
        driver_model="DP83869_MDIO",
        rx_pins="FPGA via R307",
        rx_model="FPGA_LVCMOS18",
        n_rx=1,
        series_r_refdes="R307",
        notes="DP83869 MDC clock → FPGA"
    ))

    return cases


def build_fan_cases() -> List[RunCase]:
    """FAN control P1V8 nets (5 nets).

    FAN0/1/2_PS: LM239A comparator (U30) + FPGA + series R.
    FAN_EN, EXT_FAN_EN: LVC8T245 (U72) → FPGA + series R.
    """
    cases = []

    # FAN0/1/2_PS_P1V8: LM239A U30 (open-collector) + FPGA
    # LM239A has open-collector output — FPGA is the active driver
    fan_ps_nets = {
        "FAN0_PS_P1V8": ("U30-1",  "U10-A27", "R340"),
        "FAN1_PS_P1V8": ("U30-2",  "U10-K25", "R341"),
        "FAN2_PS_P1V8": ("U30-14", "U10-K26", "R527"),
    }
    for net_name, (lm239_pin, fpga_pin, r_ser) in sorted(fan_ps_nets.items()):
        cases.append(RunCase(
            net_name=net_name,
            run_id="—",
            driver_pin=fpga_pin,
            driver_model="FPGA_LVCMOS18",
            rx_pins=lm239_pin,
            rx_model="LM239A",
            n_rx=1,
            series_r_refdes=r_ser,
            notes="FPGA drives, LM239A is passive (open-collector)"
        ))

    # FAN_EN_P1V8: U72-20 (LVC8T245 DIR pin) → U10-C28 + R524
    cases.append(RunCase(
        net_name="FAN_EN_P1V8",
        run_id="—",
        driver_pin="U10-C28",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U72-20",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R524",
        notes="FPGA → LVC8T245 DIR (via R)"
    ))

    # EXT_FAN_EN_P1V8: U72-18 (LVC8T245 B2) → U10-A28 + R534
    cases.append(RunCase(
        net_name="EXT_FAN_EN_P1V8",
        run_id="—",
        driver_pin="U10-A28",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U72-18",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R534",
        notes="FPGA → LVC8T245 B2 (via R)"
    ))

    return cases


def build_power_ctrl_cases() -> List[RunCase]:
    """Power control P1V8 nets (3 nets).

    RUN_P1V8: LTC7151 + TPS3808 — power sequencing, slow signals.
    P48V_EN_P1V8: LVC8T245 + FPGA.
    P3V3_P1V8_MGTV_RUN: multi-drop power enable to 4 LTC7151 + TPS3808.
    """
    cases = []

    # RUN_P1V8: U75-2 (LTC7151 RUN) + U73-1 (TPS3808) + R253
    # Power sequencing — FPGA not on net; TPS3808 drives
    # TPS3808 is a voltage supervisor with push-pull CMOS output
    cases.append(RunCase(
        net_name="RUN_P1V8",
        run_id="—",
        driver_pin="U73-1",
        driver_model="FPGA_LVCMOS18",  # approximate: TPS3808 CMOS output
        rx_pins="U75-2",
        rx_model="FPGA_LVCMOS18",  # approximate: LTC7151 RUN input
        n_rx=1,
        series_r_refdes="R253",
        notes="TPS3808 → LTC7151 RUN (power sequencing, slow)"
    ))

    # P48V_EN_P1V8: U72-21 (LVC8T245 VCCB) + U10-C29 + R523
    cases.append(RunCase(
        net_name="P48V_EN_P1V8",
        run_id="—",
        driver_pin="U10-C29",
        driver_model="FPGA_LVCMOS18",
        rx_pins="U72-21",
        rx_model="LVC8T245_18",
        n_rx=1,
        series_r_refdes="R523",
        notes="FPGA → LVC8T245 (P48V enable via R)"
    ))

    # P3V3_P1V8_MGTV_RUN: multi-drop to U104-2, U84-2, U69-2, U46-1, R204
    # 4× LTC7151 RUN + 1× TPS3808 — all receiver
    cases.append(RunCase(
        net_name="P3V3_P1V8_MGTV_RUN",
        run_id="—",
        driver_pin="TPS3808/ext",
        driver_model="FPGA_LVCMOS18",  # approximate
        rx_pins="4×LTC7151+TPS3808",
        rx_model="FPGA_LVCMOS18",  # approximate
        n_rx=5,
        series_r_refdes="R204",
        notes="Power enable → 5 receivers (multi-drop)"
    ))

    return cases


def build_all_cases() -> List[RunCase]:
    """Build complete list of all P1V8 run cases."""
    cases = []
    cases.extend(build_ext_sync_p1v8_cases())
    cases.extend(build_te_ext_sync_case())
    cases.extend(build_sync_en_p1v8_case())
    cases.extend(build_dir_ab_p1v8_case())
    cases.extend(build_srs_ucd_cases())
    cases.extend(build_srs_pll_sync_cases())
    cases.extend(build_prs_ucd_cases())
    cases.extend(build_prs_spi_cases())
    cases.extend(build_prs_misc_cases())
    cases.extend(build_lmk_p1v8_cases())
    cases.extend(build_ge1_cases())
    cases.extend(build_fan_cases())
    cases.extend(build_power_ctrl_cases())
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

    orig_rx = net.ic_receivers
    net.ic_receivers = case.n_rx
    result = compute_si(net, driver, num_bounces=num_bounces)
    net.ic_receivers = orig_rx

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results_table(results: List[Tuple[RunCase, SIResult]]):
    """Print results in tabular format with Driver Pin and RX Pin columns."""
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
        nmh = si.voh - rx_mdl.vih
        nml = rx_mdl.vil - si.vol
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
        description="Run per-case SI analysis for P1V8 domain nets"
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
        print(f"  P1V8 Domain — {len(all_cases)} Run Cases Defined")
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
        ext_sync = sum(1 for c in all_cases if "EXT_SYNC" in c.net_name and "TE_" not in c.net_name)
        te_sync = sum(1 for c in all_cases if "TE_EXT_SYNC" in c.net_name)
        sync_en = sum(1 for c in all_cases if c.net_name == "SYNC_EN_P1V8")
        dir_ab = sum(1 for c in all_cases if c.net_name == "DIR_AB_P1V8")
        srs = sum(1 for c in all_cases if "SRS" in c.net_name)
        prs = sum(1 for c in all_cases if "PRS" in c.net_name)
        lmk = sum(1 for c in all_cases if "LMK" in c.net_name)
        ge1 = sum(1 for c in all_cases if "GE1" in c.net_name)
        fan = sum(1 for c in all_cases if "FAN" in c.net_name)
        pwr = sum(1 for c in all_cases if c.net_name in ("RUN_P1V8", "P48V_EN_P1V8", "P3V3_P1V8_MGTV_RUN"))
        print(f"\nBreakdown: EXT_SYNC={ext_sync}, TE_SYNC={te_sync}, SYNC_EN={sync_en}, "
              f"DIR_AB={dir_ab}, SRS={srs}, PRS={prs}, LMK={lmk}, GE1={ge1}, FAN={fan}, PWR={pwr}")
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

        out_path = args.output or args.report.rsplit(".", 1)[0] + "_p1v8_cases.csv"
        write_results_csv(results, out_path)
        print(f"\nResults written to: {out_path}")
    else:
        print("\nNo matching nets found in report.")


if __name__ == "__main__":
    main()
