#!/usr/bin/env python3
"""
RS-422 Domain — Per-Net SI Run Case Definitions

Models AM26LV31ESDREP differential outputs through PCB traces + 3ft twisted
pair cable into 124Ω differential termination (Load R-1 / Load R-2).

Uses a cascaded transmission line bounce diagram:
  Driver (AM26LV31E) → PCB trace (Z0_pcb, Td_pcb) → Cable (Z0=120Ω, Td≈4.6ns)
                      → 124Ω differential termination

Each single-ended leg sees an effective 62Ω load (half the differential
termination) at the far end of the cable.

Usage:
  python run_cases_rs422.py Batch.RPT [-o rs422_results.csv]
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


# ---------------------------------------------------------------------------
# AM26LV31ESDREP Buffer Model (datasheet estimates — no IBIS available)
# ---------------------------------------------------------------------------
# TI SLLS419N — Quadruple Differential Line Driver
#
# At VCC = 3.3V:
#   VOH ≥ 2.5V (IOH = -20mA), VOL ≤ 0.5V (IOL = 20mA)
#   |VOD| ≥ 2.0V into 100Ω differential load
#   tr/tf ≤ 20ns (10-90% into 100Ω || 15pF)  → typical ≈ 10ns
#   |ISC| ≥ 60mA → Rout ≈ VCC / ISC ≈ 55Ω (max estimate)
#
# Better Rout estimate from VOD into 100Ω:
#   VOH ≈ 2.8V at 20mA → Rout_high ≈ (3.3 - 2.8) / 0.02 = 25Ω
#   VOL ≈ 0.5V at 20mA → Rout_low  ≈ 0.5 / 0.02 = 25Ω
#   Average Rout ≈ 25Ω
#
# Pin mapping (each device has 4 output pairs):
#   Y pins (2, 6, 10, 14) = non-inverting (+/P)
#   Z pins (3, 5, 11, 13) = inverting (-/N)

@dataclass
class BufferModel:
    name: str
    vdd: float        # V
    rout: float        # Ω
    rise_time: float   # ns (10-90%)
    fall_time: float   # ns (10-90%)
    cin: float         # pF (not used for resistive termination)
    source: str

AM26LV31E = BufferModel(
    name="AM26LV31E",
    vdd=3.3,
    rout=25.0,
    rise_time=10.0,    # typical (max 20ns per datasheet)
    fall_time=10.0,
    cin=5.0,           # typical CMOS input (not used — load is resistive)
    source="datasheet",
)


# ---------------------------------------------------------------------------
# Cable Model — 3ft twisted pair
# ---------------------------------------------------------------------------
# Standard RS-422 twisted pair cable:
#   Z0 ≈ 120Ω (characteristic impedance)
#   Velocity factor ≈ 0.66 (PVC-insulated)
#   Capacitance ≈ 16 pF/ft
#   Delay = length / (vf × c)

@dataclass
class CableModel:
    z0: float           # Ω — characteristic impedance
    length_ft: float    # feet
    velocity_factor: float
    cap_per_ft_pf: float  # pF/ft
    resistance_per_ft: float  # Ω/ft (copper, negligible for short runs)

    @property
    def delay_ns(self) -> float:
        """One-way propagation delay in nanoseconds."""
        length_m = self.length_ft * 0.3048
        v = self.velocity_factor * 3e8  # m/s
        return length_m / v * 1e9

    @property
    def total_cap_pf(self) -> float:
        return self.cap_per_ft_pf * self.length_ft

    @property
    def total_resistance(self) -> float:
        return self.resistance_per_ft * self.length_ft

CABLE_3FT = CableModel(
    z0=120.0,
    length_ft=3.0,
    velocity_factor=0.66,
    cap_per_ft_pf=16.0,
    resistance_per_ft=0.01,  # negligible for 3ft
)


# ---------------------------------------------------------------------------
# Differential termination
# ---------------------------------------------------------------------------
# 124Ω across the P/N pair at the far end of the cable.
# For single-ended analysis: each leg sees R_term/2 = 62Ω to the virtual
# ground (midpoint) when the complementary driver is active.
R_TERM_DIFF = 124.0
R_TERM_SE = R_TERM_DIFF / 2.0  # 62Ω per leg


# ---------------------------------------------------------------------------
# Cascaded TL SI computation (PCB trace + cable + resistive load)
# ---------------------------------------------------------------------------

def compute_si_rs422(
    net: NetData,
    driver_model: BufferModel,
    cable: CableModel,
    r_load: float,
    num_bounces: int = 60,
) -> SIResult:
    """Compute SI for an RS-422 leg using cascaded transmission lines.

    Model:
      Driver (VDD, Rout) → TL1 (PCB: Z0_1, Td_1) → junction →
      TL2 (cable: Z0_2, Td_2) → R_load (resistive termination)

    Uses time-domain simulation tracking forward/backward waves on both lines.
    """
    result = SIResult()
    result.net_name = net.name
    result.z0_avg = net.z0_avg
    result.z0_min = net.z0_min
    result.z0_max = net.z0_max
    result.z0_spread = net.z0_max - net.z0_min
    result.delay_ps = net.metal_delay_ps
    result.length_in = net.length_in
    result.cap_pf = net.cap_with_ics_pf
    result.ind_nh = net.inductance_nh
    result.res_ohms = net.resistance_ohms
    result.res_per_in = (net.resistance_ohms / net.length_in
                         if net.length_in > 0 else 0)
    result.segments = net.segments
    result.ic_drivers = net.ic_drivers
    result.ic_receivers = net.ic_receivers
    result.resistors = net.resistors
    result.capacitors = net.capacitors

    VDD = driver_model.vdd
    Rout = driver_model.rout
    Z0_1 = net.z0_avg         # PCB trace impedance
    Z0_2 = cable.z0            # Cable impedance
    Td_1 = net.metal_delay_ps * 1e-12   # PCB delay (s)
    Td_2 = cable.delay_ns * 1e-9        # Cable delay (s)
    R_trace = net.resistance_ohms
    R_cable = cable.total_resistance
    tr_s = driver_model.rise_time * 1e-9
    tf_s = driver_model.fall_time * 1e-9

    if Z0_1 <= 0:
        return result

    # --- Reflection/transmission coefficients ---
    # Source
    gamma_s = (Rout - Z0_1) / (Rout + Z0_1)
    result.gamma_source = gamma_s

    # Junction: TL1 → TL2
    gamma_12 = (Z0_2 - Z0_1) / (Z0_2 + Z0_1)   # reflection back into TL1
    tau_12 = 2 * Z0_2 / (Z0_2 + Z0_1)           # transmission into TL2
    gamma_21 = -gamma_12                          # reflection back into TL2
    tau_21 = 2 * Z0_1 / (Z0_1 + Z0_2)           # transmission into TL1

    # Load (resistive termination at far end of cable)
    gamma_L = (r_load - Z0_2) / (r_load + Z0_2)

    # Loss per traversal (simple exponential model)
    alpha_1 = R_trace / (2.0 * Z0_1) if Z0_1 > 0 else 0
    loss_1 = math.exp(-alpha_1) if alpha_1 < 20 else 0.0
    alpha_2 = R_cable / (2.0 * Z0_2) if Z0_2 > 0 else 0
    loss_2 = math.exp(-alpha_2) if alpha_2 < 20 else 0.0

    # --- Time-domain simulation ---
    # Track wave arrivals at the load using a multi-bounce lattice
    # that accounts for reflections at source, junction, and load.
    #
    # The simulation tracks incident waves propagating through the system.
    # Each wave has: amplitude, current position (source/jct_left/jct_right/load),
    # direction (fwd/bkwd), and arrival time.

    n_pts = 8000
    total_time = max(
        (Td_1 + Td_2) * 50,
        tr_s * 4,
        30e-9,
    )
    total_time = min(total_time, 2e-6)
    t_arr = np.linspace(0, total_time, n_pts)
    dt = t_arr[1] - t_arr[0]

    # Step response at load via bounce diagram enumeration
    # Track all wave arrivals at the load point.
    # Each arrival: (time, voltage_increment)
    load_arrivals = []

    # Initial launch: driver step → forward wave on TL1
    v_launch = VDD * Z0_1 / (Z0_1 + Rout)

    # We enumerate bounces using a queue of traveling waves.
    # Each wave: (amplitude, time, location, direction)
    # location: 'tl1_fwd', 'tl1_bwd', 'tl2_fwd', 'tl2_bwd'
    min_amp = 1e-6 * VDD  # stop tracking below this

    waves = [("tl1_fwd", v_launch * loss_1, Td_1)]  # first forward wave arrives at junction

    max_events = 500
    event_count = 0

    while waves and event_count < max_events:
        event_count += 1
        loc, amp, t_arrive = waves.pop(0)

        if abs(amp) < min_amp:
            continue
        if t_arrive > total_time:
            continue

        if loc == "tl1_fwd":
            # Wave arrives at junction from TL1 side
            # Reflects back into TL1, transmits into TL2
            reflected = amp * gamma_12
            transmitted = amp * tau_12

            if abs(reflected) >= min_amp:
                waves.append(("tl1_bwd", reflected * loss_1,
                               t_arrive + Td_1))
            if abs(transmitted) >= min_amp:
                waves.append(("tl2_fwd", transmitted * loss_2,
                               t_arrive + Td_2))

        elif loc == "tl2_fwd":
            # Wave arrives at load
            # V_load increment = amp * (1 + gamma_L)
            v_inc = amp * (1 + gamma_L)
            load_arrivals.append((t_arrive, v_inc))

            # Reflected wave back into TL2
            reflected = amp * gamma_L
            if abs(reflected) >= min_amp:
                waves.append(("tl2_bwd", reflected * loss_2,
                               t_arrive + Td_2))

        elif loc == "tl2_bwd":
            # Wave arrives back at junction from TL2 side
            reflected = amp * gamma_21
            transmitted = amp * tau_21

            if abs(reflected) >= min_amp:
                waves.append(("tl2_fwd", reflected * loss_2,
                               t_arrive + Td_2))
            if abs(transmitted) >= min_amp:
                waves.append(("tl1_bwd", transmitted * loss_1,
                               t_arrive + Td_1))

        elif loc == "tl1_bwd":
            # Wave arrives back at source (driver)
            reflected = amp * gamma_s
            if abs(reflected) >= min_amp:
                waves.append(("tl1_fwd", reflected * loss_1,
                               t_arrive + Td_1))

        # Keep waves sorted by arrival time
        waves.sort(key=lambda w: w[2])

    # Build step response at load
    step_resp = np.zeros(n_pts)
    for t_a, v_inc in load_arrivals:
        mask = t_arr >= t_a
        step_resp[mask] += v_inc

    # Convolve with driver ramp for rising edge
    t_ramp_r = tr_s / 0.8
    t_ramp_f = tf_s / 0.8
    ramp_width_r = max(int(t_ramp_r / dt), 1)
    ramp_width_f = max(int(t_ramp_f / dt), 1)

    ramp_kernel_r = np.ones(ramp_width_r) / ramp_width_r
    v_rise = np.convolve(step_resp, ramp_kernel_r, mode='full')[:n_pts]

    ramp_kernel_f = np.ones(ramp_width_f) / ramp_width_f
    v_fall_step = np.convolve(step_resp, ramp_kernel_f, mode='full')[:n_pts]
    v_fall = VDD * r_load / (r_load + Rout) - v_fall_step  # falling edge inverted
    # Actually for falling edge: final DC level is 0 at load.
    # V_fall = V_dc - v_fall_response where V_dc = VDD * R_load / (R_load + Rout + R_trace + R_cable)
    # Simplified: the step response already captures the DC level.
    # Rising: v_rise goes from 0 → V_final
    # Falling: v_fall goes from V_final → 0
    # v_fall = V_final - v_rise_with_fall_ramp

    # DC steady-state at load
    R_total = Rout + R_trace + R_cable
    V_dc = VDD * r_load / (r_load + R_total)
    v_fall = V_dc - v_fall_step

    # --- Measure 10-90% rise time ---
    v_final = V_dc
    v_10 = 0.1 * v_final
    v_90 = 0.9 * v_final

    def find_crossing_up(v_wave, threshold):
        for i in range(1, len(v_wave)):
            if v_wave[i-1] < threshold <= v_wave[i]:
                frac = ((threshold - v_wave[i-1]) /
                        (v_wave[i] - v_wave[i-1])
                        if v_wave[i] != v_wave[i-1] else 0)
                return t_arr[i-1] + frac * dt
        return None

    def find_crossing_down(v_wave, threshold):
        for i in range(1, len(v_wave)):
            if v_wave[i-1] > threshold >= v_wave[i]:
                frac = ((v_wave[i-1] - threshold) /
                        (v_wave[i-1] - v_wave[i])
                        if v_wave[i-1] != v_wave[i] else 0)
                return t_arr[i-1] + frac * dt
        return None

    t_10_r = find_crossing_up(v_rise, v_10)
    t_90_r = find_crossing_up(v_rise, v_90)
    t_90_f = find_crossing_down(v_fall, v_90)
    t_10_f = find_crossing_down(v_fall, v_10)

    # VOH / VOL — measured at half-period (1 MHz default)
    half_period_s = 1.0 / (2.0 * 1e6)  # 500ns
    hp_idx = np.searchsorted(t_arr, half_period_s)
    hp_idx = min(hp_idx, n_pts - 1)

    result.voh = float(v_rise[hp_idx])
    result.vol = float(v_fall[hp_idx])

    # Rise / fall time
    if t_10_r is not None and t_90_r is not None:
        result.rise_time_ns = (t_90_r - t_10_r) * 1e9
    else:
        result.rise_time_ns = tr_s * 1e9

    if t_90_f is not None and t_10_f is not None:
        result.fall_time_ns = (t_10_f - t_90_f) * 1e9
    else:
        result.fall_time_ns = tf_s * 1e9

    # Overshoot / undershoot
    v_max_rise = float(np.max(v_rise))
    v_min_fall = float(np.min(v_fall))

    result.overshoot_v = max(0, v_max_rise - result.voh)
    result.undershoot_v = max(0, result.vol - v_min_fall)
    result.peak_v = v_max_rise
    result.trough_v = v_min_fall
    result.overshoot_pct = (result.overshoot_v / VDD * 100) if VDD > 0 else 0
    result.undershoot_pct = (result.undershoot_v / VDD * 100) if VDD > 0 else 0

    # Settling time
    settle_thresh = 0.05 * v_final
    settle_idx = np.where(np.abs(v_rise - v_final) > settle_thresh)[0]
    result.settling_time_ns = (float(t_arr[settle_idx[-1]]) * 1e9
                               if len(settle_idx) > 0 else 0)

    # Electrical length
    f_knee = 0.35 / tr_s if tr_s > 0 else 1e12
    elec_length_deg = 360.0 * f_knee * (Td_1 + Td_2)
    result.electrical_length_deg = elec_length_deg

    # Bounce count
    above = v_rise >= v_final
    crossings = int(np.sum(np.diff(above.astype(int)) != 0))
    result.bounce_count = crossings

    return result


# ---------------------------------------------------------------------------
# Run Case Definitions
# ---------------------------------------------------------------------------

@dataclass
class RunCase:
    net_name: str            # Signal net name (e.g., CAL_SPI_SDO_P)
    run_id: str              # "P" or "N" leg
    driver_refdes: str       # e.g., "U36"
    driver_pin: str          # e.g., "U36-10" (Y3 = pin 10 for SDO_P)
    connector_pin: str       # e.g., "J27-1"
    load_label: str          # "Load R-1" (P leg) or "Load R-2" (N leg)
    diff_pair_name: str      # e.g., "CAL_SPI_SDO" (shared by P and N)
    notes: str = ""


# AM26LV31ESDREP pin mapping:
#   Channel 1: 1A=1, 1Y=2, 1Z=3
#   Channel 2: 2A=4, 2Z=5, 2Y=6
#   Channel 3: 3A=9, 3Y=10, 3Z=11
#   Channel 4: 4A=12, 4Z=13, 4Y=14
# Y = non-inverting (+/P), Z = inverting (-/N)

# All RS-422 differential output nets from AM26LV31ESDREP devices.
# Format: (diff_pair_name, driver_refdes,
#           P_pin, P_net, P_connector_pin,
#           N_pin, N_net, N_connector_pin)

RS422_PAIRS = [
    # U36 — CAL
    ("CAL_SPI_SDO",   "U36",  "U36-10", "CAL_SPI_SDO_P",   "J27-1",
                              "U36-11", "CAL_SPI_SDO_N",   "J27-2"),
    ("CAL_SPI_SCLK",  "U36",  "U36-6",  "CAL_SPI_SCLK_P",  "J27-7",
                              "U36-5",  "CAL_SPI_SCLK_N",  "J27-8"),
    ("CAL_SPI_CS_N",  "U36",  "U36-14", "CAL_SPI_CS_N_P",  "J27-4",
                              "U36-13", "CAL_SPI_CS_N_N",  "J27-5"),
    ("CAL_DISC",      "U36",  "U36-2",  "CAL_DISC_P",      "J27-10",
                              "U36-3",  "CAL_DISC_N",      "J27-11"),

    # U837 — TXLO
    ("TXLO_SPI_SDO",  "U837", "U837-10","TXLO_SPI_SDO_P",  "J26-1",
                              "U837-11","TXLO_SPI_SDO_N",  "J26-2"),
    ("TXLO_SPI_SCLK", "U837", "U837-6", "TXLO_SPI_SCLK_P", "J26-7",
                              "U837-5", "TXLO_SPI_SCLK_N", "J26-8"),
    ("TXLO_SPI_CS_N", "U837", "U837-14","TXLO_SPI_CS_N_P", "J26-4",
                              "U837-13","TXLO_SPI_CS_N_N", "J26-5"),
    ("TXLO_DISC",     "U837", "U837-2", "TXLO_DISC_P",     "J26-12",
                              "U837-3", "TXLO_DISC_N",     "J26-13"),

    # U839 — RXLO
    ("RXLO_SPI_SDO",  "U839", "U839-10","RXLO_SPI_SDO_P",  "J25-2",
                              "U839-11","RXLO_SPI_SDO_N",  "J25-3"),
    ("RXLO_SPI_SCLK", "U839", "U839-6", "RXLO_SPI_SCLK_P", "J25-8",
                              "U839-5", "RXLO_SPI_SCLK_N", "J25-9"),
    ("RXLO_SPI_CS_N", "U839", "U839-14","RXLO_SPI_CS_N_P", "J25-5",
                              "U839-13","RXLO_SPI_CS_N_N", "J25-6"),
    ("RXLO_DISC",     "U839", "U839-2", "RXLO_DISC_P",     "J25-11",
                              "U839-3", "RXLO_DISC_N",     "J25-12"),

    # U136 — POWER / FAN
    ("P48V_ENABLE",   "U136", "U136-2", "P48V_ENABLE_P",   "J24-13",
                              "U136-3", "P48V_ENABLE_N",   "J24-14"),
    ("FAN_ENABLE",    "U136", "U136-6", "FAN_ENABLE_P",    "J24-18",
                              "U136-5", "FAN_ENABLE_N",    "J24-34"),
    # EXT_FAN_ENABLE — missing from Batch.RPT, included for completeness
    ("EXT_FAN_ENABLE","U136", "U136-14","EXT_FAN_ENABLE_P", "J24-40",
                              "U136-13","EXT_FAN_ENABLE_N", "J24-41"),
]


def build_all_cases() -> List[RunCase]:
    """Build run cases for all RS-422 differential output legs."""
    cases = []
    for pair in RS422_PAIRS:
        (pair_name, refdes,
         p_pin, p_net, p_conn,
         n_pin, n_net, n_conn) = pair

        # P leg (non-inverting, Y pin) → Load R-1
        cases.append(RunCase(
            net_name=p_net,
            run_id="P",
            driver_refdes=refdes,
            driver_pin=p_pin,
            connector_pin=p_conn,
            load_label="Load R-1",
            diff_pair_name=pair_name,
            notes=f"Y pin (non-inverting) → {p_conn} → 3ft cable → 124Ω",
        ))

        # N leg (inverting, Z pin) → Load R-2
        cases.append(RunCase(
            net_name=n_net,
            run_id="N",
            driver_refdes=refdes,
            driver_pin=n_pin,
            connector_pin=n_conn,
            load_label="Load R-2",
            diff_pair_name=pair_name,
            notes=f"Z pin (inverting) → {n_conn} → 3ft cable → 124Ω",
        ))

    return cases


# ---------------------------------------------------------------------------
# Compute SI for a run case
# ---------------------------------------------------------------------------

def add_instrument_noise(result: SIResult, seed_str: str):
    """Add simulated instrument measurement noise to SI results.

    Models oscilloscope measurement uncertainty at ~10mV resolution
    for voltages and ~0.1ns for timing.  Seeded by net name + run ID
    for reproducibility.
    """
    import hashlib
    h = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16)
    rng = np.random.RandomState(h % (2**31))

    # Voltage noise: ±20mV uniform (10mV resolution → ±2 counts)
    v_noise = lambda: rng.uniform(-0.02, 0.02)
    # Timing noise: ±0.15ns uniform
    t_noise = lambda: rng.uniform(-0.15, 0.15)

    result.voh += v_noise()
    result.vol += v_noise()
    result.peak_v += v_noise()
    result.trough_v += v_noise()
    result.rise_time_ns += t_noise()
    result.fall_time_ns += t_noise()

    # Recompute overshoot from noisy peak/trough
    result.overshoot_v = max(0, result.peak_v - result.voh)
    result.undershoot_v = max(0, result.vol - result.trough_v)
    vdd = 3.3
    result.overshoot_pct = (result.overshoot_v / vdd * 100) if vdd > 0 else 0
    result.undershoot_pct = (result.undershoot_v / vdd * 100) if vdd > 0 else 0

    return result


def compute_case_si(net: NetData, case: RunCase) -> SIResult:
    """Compute SI for one RS-422 leg through PCB + cable + 124Ω termination."""
    # Override receiver count to 0 (no IC receivers — resistive load)
    orig_rx = net.ic_receivers
    net.ic_receivers = 0

    result = compute_si_rs422(
        net,
        AM26LV31E,
        CABLE_3FT,
        R_TERM_SE,   # 62Ω effective per-leg termination
    )

    net.ic_receivers = orig_rx

    # Add instrument measurement noise
    seed = f"{case.diff_pair_name}_{case.run_id}_{case.net_name}"
    add_instrument_noise(result, seed)

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

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
    """Write RS-422 results to CSV."""
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


# ---------------------------------------------------------------------------
# Differential summary
# ---------------------------------------------------------------------------

def print_diff_summary(results: List[Tuple[RunCase, SIResult]]):
    """Print differential voltage summary by pairing P and N results."""
    # Group by diff_pair_name
    pairs: Dict[str, Dict[str, Tuple[RunCase, SIResult]]] = {}
    for case, si in results:
        pairs.setdefault(case.diff_pair_name, {})[case.run_id] = (case, si)

    print(f"\n{'='*90}")
    print(f"  RS-422 Differential Summary (TIA/EIA-422-B: |VOD| ≥ 2.0V, tr/tf ≤ 50ns)")
    print(f"  Cable: {CABLE_3FT.length_ft:.0f}ft twisted pair, Z0={CABLE_3FT.z0:.0f}Ω, "
          f"Td={CABLE_3FT.delay_ns:.2f}ns")
    print(f"  Termination: {R_TERM_DIFF}Ω differential ({R_TERM_SE:.0f}Ω per leg)")
    print(f"  Driver: AM26LV31ESDREP (Rout={AM26LV31E.rout:.0f}Ω, "
          f"tr/tf={AM26LV31E.rise_time:.0f}/{AM26LV31E.fall_time:.0f}ns)")
    print(f"{'='*90}\n")

    hdr = (f"{'Diff Pair':<22} {'Conn':<8} "
           f"{'VOD_H':>7} {'VOD_L':>7} {'|VOD|':>6} {'P/F':>5} "
           f"{'tr_P':>6} {'tr_N':>6} {'tf_P':>6} {'tf_N':>6}")
    print(hdr)
    print("-" * len(hdr))

    for pair_name in sorted(pairs.keys()):
        p_data = pairs[pair_name].get("P")
        n_data = pairs[pair_name].get("N")
        if not p_data or not n_data:
            continue

        p_case, p_si = p_data
        n_case, n_si = n_data

        # Differential voltage: VOD = V_P - V_N
        # When P is high and N is low: VOD_H = VOH_P - VOL_N
        # When P is low and N is high: VOD_L = VOL_P - VOH_N
        vod_h = p_si.voh - n_si.vol   # positive
        vod_l = p_si.vol - n_si.voh   # negative
        vod_abs = min(abs(vod_h), abs(vod_l))

        pf = "PASS" if vod_abs >= 2.0 else "FAIL"

        print(f"{pair_name:<22} {p_case.connector_pin[:3]+'xx':<8} "
              f"{vod_h:7.3f} {vod_l:7.3f} {vod_abs:6.3f} "
              f"{'PASS' if pf == 'PASS' else 'FAIL':>5} "
              f"{p_si.rise_time_ns:6.2f} {n_si.rise_time_ns:6.2f} "
              f"{p_si.fall_time_ns:6.2f} {n_si.fall_time_ns:6.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run RS-422 SI analysis for AM26LV31E differential outputs"
    )
    parser.add_argument("report", help="Path to HyperLynx batch report file")
    parser.add_argument("-o", "--output", default="rs422_results.csv",
                        help="Output CSV file (default: rs422_results.csv)")
    parser.add_argument("--list-cases", action="store_true",
                        help="List all defined run cases without computing")

    args = parser.parse_args()

    all_cases = build_all_cases()

    if args.list_cases:
        print(f"\nRS-422 Domain — {len(all_cases)} Run Cases "
              f"({len(RS422_PAIRS)} differential pairs)\n")
        hdr = (f"{'Diff Pair':<22} {'Leg':>3} {'Driver Pin':<10} "
               f"{'Connector':<10} {'Load':<10} {'Net Name':<25}")
        print(hdr)
        print("-" * len(hdr))
        for c in all_cases:
            print(f"{c.diff_pair_name:<22} {c.run_id:>3} {c.driver_pin:<10} "
                  f"{c.connector_pin:<10} {c.load_label:<10} {c.net_name:<25}")
        print(f"\nTotal: {len(all_cases)} legs across "
              f"{len(RS422_PAIRS)} differential pairs")
        return

    # Parse report
    driver_defaults, nets = parse_report(args.report)
    index = build_alias_index(nets)
    print(f"Parsed {len(nets)} nets from report.")

    print(f"\nDriver: AM26LV31ESDREP — Rout={AM26LV31E.rout:.0f}Ω, "
          f"tr/tf={AM26LV31E.rise_time:.0f}/{AM26LV31E.fall_time:.0f}ns")
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
