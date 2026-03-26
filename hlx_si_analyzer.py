#!/usr/bin/env python3
"""
HyperLynx Batch Report — Signal Integrity Analyzer

Parses HyperLynx batch mode reports, extracts per-net interconnect statistics,
and computes normalized SI parameters (VOH, VOL, rise/fall time, overshoot,
undershoot) using a common driver/receiver model.

Physics model:
  - Transmission line with average Z0, total propagation delay, and resistive loss
  - Driver: Thevenin source (VDD, Rout, slew rate)
  - Receiver: capacitive load (Cin per receiver pin)
  - Bounce diagram (lattice) with attenuation for overshoot/undershoot
  - Root-sum-square rise time model for capacitive + dispersive loading

Usage:
  python hlx_si_analyzer.py report.txt [-o results.csv] [--vdd 3.3] [--rout 1.0]
                                        [--tr 1.0] [--tf 1.0] [--cin 7.0]

  If --vdd/--rout/--tr/--tf/--cin are not specified, values are parsed from
  the report header (Default IC model section).
"""

import re
import csv
import sys
import math
import argparse
from dataclasses import dataclass, field, asdict
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class DriverModel:
    vdd: float = 3.3        # Supply voltage (V)
    rout: float = 1.0       # Output impedance (Ω)
    rise_time: float = 1.0  # 10-90% rise time (ns)
    fall_time: float = 1.0  # 10-90% fall time (ns)
    cin: float = 7.0        # Receiver input capacitance (pF)


@dataclass
class NetData:
    name: str = ""          # Full raw name line from report
    aliases: list = None    # All individual net names (split on comma)
    segments: int = 0
    ic_drivers: int = 0
    ic_receivers: int = 0
    resistors: int = 0
    capacitors: int = 0
    metal_delay_ps: float = 0.0
    z0_min: float = 0.0
    z0_max: float = 0.0
    z0_avg: float = 0.0
    cap_with_ics_pf: float = 0.0
    cap_metal_pf: float = 0.0
    inductance_nh: float = 0.0
    resistance_ohms: float = 0.0
    length_in: float = 0.0


@dataclass
class SIResult:
    net_name: str = ""
    z0_avg: float = 0.0
    z0_min: float = 0.0
    z0_max: float = 0.0
    z0_spread: float = 0.0        # max - min, impedance discontinuity indicator
    delay_ps: float = 0.0
    length_in: float = 0.0
    cap_pf: float = 0.0
    ind_nh: float = 0.0
    res_ohms: float = 0.0
    res_per_in: float = 0.0       # resistance per inch
    segments: int = 0
    ic_drivers: int = 0
    ic_receivers: int = 0
    resistors: int = 0
    capacitors: int = 0
    # Computed SI parameters
    voh: float = 0.0              # Steady-state output high (V)
    vol: float = 0.0              # Steady-state output low (V)
    rise_time_ns: float = 0.0     # 10-90% at receiver (ns)
    fall_time_ns: float = 0.0     # 10-90% at receiver (ns)
    overshoot_v: float = 0.0      # Peak above VDD (V)
    overshoot_pct: float = 0.0    # As % of VDD
    undershoot_v: float = 0.0     # Peak below GND (V), positive = below 0
    undershoot_pct: float = 0.0   # As % of VDD
    peak_v: float = 0.0           # Absolute max voltage at receiver
    trough_v: float = 0.0         # Absolute min voltage at receiver
    gamma_source: float = 0.0     # Source reflection coefficient
    settling_time_ns: float = 0.0 # Time to settle within 5% of final value
    bounce_count: int = 0         # Number of bounces before settling
    electrical_length_deg: float = 0.0  # At knee frequency


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_delay(value_str: str) -> float:
    """Parse a delay value, return picoseconds."""
    value_str = value_str.strip()
    if "ns" in value_str:
        return float(value_str.replace("ns", "").strip()) * 1000.0
    elif "ps" in value_str:
        return float(value_str.replace("ps", "").strip())
    else:
        # Assume ps
        return float(value_str)


def parse_resistance(value_str: str) -> float:
    """Parse a resistance value, return ohms."""
    value_str = value_str.strip()
    if "milliohms" in value_str:
        return float(value_str.replace("milliohms", "").strip()) / 1000.0
    elif "ohms" in value_str:
        return float(value_str.replace("ohms", "").strip())
    else:
        return float(value_str)


def parse_capacitance(value_str: str) -> float:
    """Parse capacitance, return pF."""
    value_str = value_str.strip()
    if "pF" in value_str:
        return float(value_str.replace("pF", "").strip())
    elif "nF" in value_str:
        return float(value_str.replace("nF", "").strip()) * 1000.0
    else:
        return float(value_str)


def parse_inductance(value_str: str) -> float:
    """Parse inductance, return nH."""
    value_str = value_str.strip()
    if "nH" in value_str:
        return float(value_str.replace("nH", "").strip())
    elif "uH" in value_str:
        return float(value_str.replace("uH", "").strip()) * 1000.0
    else:
        return float(value_str)


def parse_impedance(value_str: str) -> float:
    """Parse impedance, return ohms."""
    value_str = value_str.strip()
    return float(value_str.replace("ohms", "").strip())


def parse_length(value_str: str) -> float:
    """Parse length, return inches."""
    value_str = value_str.strip()
    return float(value_str.replace("in", "").strip())


def _strip_board_suffix(name: str) -> str:
    """Strip the _BXX board suffix from a net name.

    Examples: 'TR2_EXT_SYNC_P3V3_B00' -> 'TR2_EXT_SYNC_P3V3'
              '$28N9063_B00'           -> '$28N9063'
              'SOME_NET_B12'           -> 'SOME_NET'
    """
    return re.sub(r'_B\d{2}$', '', name)


def build_alias_index(nets: List[NetData]) -> dict:
    """Build a lookup dict: alias string -> NetData.

    Each net can have multiple aliases (comma-separated names in the NET line).
    All aliases point to the same NetData since they are the same electrical
    signal connected through series components.

    The index contains both the raw alias (with _BXX) and the stripped version
    (without _BXX) for flexible matching.  If the same stripped name appears
    on multiple boards, all boards are collected in a list.
    """
    index = {}          # raw alias -> NetData
    stripped = {}       # stripped alias -> [NetData, ...]
    for net in nets:
        if net.aliases:
            for alias in net.aliases:
                index[alias] = net
                key = _strip_board_suffix(alias)
                stripped.setdefault(key, []).append(net)
    return index, stripped


def find_net(index: tuple, signal_name: str) -> Optional[NetData]:
    """Find a net by signal name.

    Tries exact match (with _BXX) first, then stripped match (without _BXX).
    Returns the first match found.
    """
    raw_index, stripped_index = index
    # Exact match (includes _BXX)
    if signal_name in raw_index:
        return raw_index[signal_name]
    # Case-insensitive exact match
    for alias, net in raw_index.items():
        if alias.upper() == signal_name.upper():
            return net
    # Stripped match (signal_name without _BXX -> net with _BXX)
    if signal_name in stripped_index:
        return stripped_index[signal_name][0]
    for key, net_list in stripped_index.items():
        if key.upper() == signal_name.upper():
            return net_list[0]
    return None


def find_all_boards(index: tuple, signal_name: str) -> List[NetData]:
    """Find all board variants for a signal name (without _BXX suffix).

    Returns a list of NetData, one per board the signal appears on.
    """
    _, stripped_index = index
    if signal_name in stripped_index:
        return stripped_index[signal_name]
    for key, net_list in stripped_index.items():
        if key.upper() == signal_name.upper():
            return net_list
    return []


def parse_report(filepath: str):
    """Parse a HyperLynx batch report file.

    Returns (driver_defaults: DriverModel, nets: list[NetData])
    """
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # --- Parse header defaults ---
    driver = DriverModel()

    m = re.search(r"IC driver rise/fall time\s*\.+\s*([\d.]+)\s*ns", content)
    if m:
        driver.rise_time = float(m.group(1))
        driver.fall_time = float(m.group(1))

    m = re.search(r"IC driver switching voltage range\s*\.+\s*([\d.]+)\s*V", content)
    if m:
        driver.vdd = float(m.group(1))

    m = re.search(r"IC driver output impedance\s*\.+\s*([\d.]+)\s*ohms", content)
    if m:
        driver.rout = float(m.group(1))

    m = re.search(r"IC input capacitance\s*\.+\s*([\d.]+)\s*pF", content)
    if m:
        driver.cin = float(m.group(1))

    # --- Parse net blocks ---
    nets: List[NetData] = []

    # Split on NET = lines
    net_blocks = re.split(r"\n\s*NET\s*=\s*", content)

    for block in net_blocks[1:]:  # skip everything before first NET
        net = NetData()

        # Net name: first line up to newline
        # NET lines can have multiple comma-separated aliases — these are
        # the same electrical signal with series components between segments.
        # The _BXX suffix (e.g. _B00, _B01) identifies which board in a
        # multi-board project and must be preserved.
        name_line = block.split("\n")[0].strip()
        net.name = name_line
        net.aliases = [a.strip() for a in name_line.split(",") if a.strip()]

        # Counts
        m = re.search(r"segments\s*\.+\s*(\d+)", block)
        if m: net.segments = int(m.group(1))

        m = re.search(r"IC drivers\s*\.+\s*(\d+)", block)
        if m: net.ic_drivers = int(m.group(1))

        m = re.search(r"IC receivers\s*\.+\s*(\d+)", block)
        if m: net.ic_receivers = int(m.group(1))

        m = re.search(r"resistors\s*\.+\s*(\d+)", block)
        if m: net.resistors = int(m.group(1))

        m = re.search(r"capacitors\s*\.+\s*(\d+)", block)
        if m: net.capacitors = int(m.group(1))

        # Interconnect statistics
        m = re.search(r"total metal delay\s*\.+\s*([\d.]+\s*(?:ps|ns))", block)
        if m: net.metal_delay_ps = parse_delay(m.group(1))

        m = re.search(r"minimum metal Z0\s*\.+\s*([\d.]+\s*ohms)", block)
        if m: net.z0_min = parse_impedance(m.group(1))

        m = re.search(r"maximum metal Z0\s*\.+\s*([\d.]+\s*ohms)", block)
        if m: net.z0_max = parse_impedance(m.group(1))

        m = re.search(r"average metal Z0\s*\.+\s*([\d.]+\s*ohms)", block)
        if m: net.z0_avg = parse_impedance(m.group(1))

        m = re.search(r"total net capacitance \(with ICs\)\s*\.+\s*([\d.]+\s*pF)", block)
        if m: net.cap_with_ics_pf = parse_capacitance(m.group(1))

        m = re.search(r"total metal capacitance\s*\.+\s*([\d.]+\s*pF)", block)
        if m: net.cap_metal_pf = parse_capacitance(m.group(1))

        m = re.search(r"total metal inductance\s*\.+\s*([\d.]+\s*(?:nH|uH))", block)
        if m: net.inductance_nh = parse_inductance(m.group(1))

        m = re.search(r"total metal resistance\s*\.+\s*([\d.]+\s*(?:ohms|milliohms))", block)
        if m: net.resistance_ohms = parse_resistance(m.group(1))

        m = re.search(r"total metal length\s*\.+\s*([\d.]+\s*in)", block)
        if m: net.length_in = parse_length(m.group(1))

        nets.append(net)

    return driver, nets


# ---------------------------------------------------------------------------
# SI computation
# ---------------------------------------------------------------------------

def compute_si(net: NetData, driver: DriverModel, num_bounces: int = 40) -> SIResult:
    """Compute signal integrity parameters for a net using bounce diagram analysis.

    Model:
      Driver (VDD, Rout) --> Transmission line (Z0, Td, R_loss) --> Receiver (Cin * N)

    Rising edge analysis (falling edge is symmetric for CMOS).
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
    result.res_per_in = net.resistance_ohms / net.length_in if net.length_in > 0 else 0
    result.segments = net.segments
    result.ic_drivers = net.ic_drivers
    result.ic_receivers = net.ic_receivers
    result.resistors = net.resistors
    result.capacitors = net.capacitors

    Z0 = net.z0_avg
    Rout = driver.rout
    VDD = driver.vdd
    Td_s = net.metal_delay_ps * 1e-12       # propagation delay in seconds
    R_trace = net.resistance_ohms             # total trace resistance
    N_rx = max(net.ic_receivers, 1)           # at least 1 receiver
    C_load_F = driver.cin * N_rx * 1e-12     # total load capacitance in F
    tr_driver_s = driver.rise_time * 1e-9     # driver rise time in seconds
    tf_driver_s = driver.fall_time * 1e-9

    if Z0 <= 0:
        return result

    # --- Reflection coefficients ---
    gamma_s = (Rout - Z0) / (Rout + Z0)
    result.gamma_source = gamma_s

    # Effective load reflection: capacitive load
    # At t=0 (high freq), capacitor is short: gamma_L = -1
    # At t=∞ (DC), capacitor is open: gamma_L = +1
    # Time constant: tau_load = Z0 * C_load
    tau_load_s = Z0 * C_load_F

    # Attenuation per one-way traversal (lossy line)
    # alpha ≈ R / (2 * Z0) for low-loss approximation
    if Z0 > 0:
        alpha = R_trace / (2.0 * Z0)
        loss_per_traversal = math.exp(-alpha) if alpha < 20 else 0.0
    else:
        loss_per_traversal = 1.0

    # --- Bounce diagram with capacitive load ---
    # We simulate using discrete time steps.
    # Time resolution: use Td/10 or tau_load/10, whichever is smaller
    dt = min(Td_s, tau_load_s, tr_driver_s) / 20.0
    if dt <= 0:
        dt = 1e-12  # 1ps minimum

    total_time = max(Td_s * num_bounces * 2, tr_driver_s * 10, tau_load_s * 10)
    total_time = min(total_time, 100e-9)  # cap at 100ns
    n_steps = int(total_time / dt) + 1
    n_steps = min(n_steps, 100000)  # cap computation
    dt = total_time / n_steps

    # Simplified approach: analytical bounce diagram
    # Track voltage waves arriving at the load at times Td, 3*Td, 5*Td, ...
    # and at the source at times 2*Td, 4*Td, ...

    # For the capacitive load, the voltage response to an arriving step of
    # amplitude V_inc is:
    #   v(t) = V_inc * (2 - exp(-t/tau))   for t >= 0
    # where tau = Z0 * C_load
    # This accounts for the initial short-circuit reflection transitioning to open.
    # The reflected wave amplitude evolves as:
    #   v_ref(t) = V_inc * (1 - 2*exp(-t/tau))  — starts at -V_inc, goes to +V_inc

    # We'll track load voltage as superposition of responses to each arriving wave.

    # Each arriving wave at the load: (arrival_time, amplitude)
    load_arrivals = []

    # Initial wave launched by driver (rising edge as a step)
    v_launched = VDD * Z0 / (Z0 + Rout)

    # Forward wave to load
    wave_fwd = v_launched * loss_per_traversal
    arrival_time = Td_s
    load_arrivals.append((arrival_time, wave_fwd))

    # Iterate bounces
    # After each load arrival, a reflection goes back to source.
    # The "average" reflected amplitude depends on how long it's been at the load.
    # For simplicity in the bounce model, we compute the reflected wave
    # using the steady-state gamma_L at each bounce (effective gamma).
    # We use gamma_L_eff that accounts for cap charging: after the first
    # bounce the cap is partially charged, so gamma_L moves toward +1.

    for bounce in range(num_bounces):
        # Time the wave has been "at" the load for this bounce
        # Approximate: use Td as dwell time (wave traverses, cap charges during Td)
        if tau_load_s > 0:
            gamma_l_eff = 1.0 - 2.0 * math.exp(-Td_s / tau_load_s)
        else:
            gamma_l_eff = 1.0  # no cap = open circuit

        # Reflected from load
        wave_back = wave_fwd * gamma_l_eff * loss_per_traversal

        # Reflected from source
        wave_fwd = wave_back * gamma_s * loss_per_traversal

        arrival_time += 2 * Td_s
        if abs(wave_fwd) < VDD * 1e-6:
            break
        load_arrivals.append((arrival_time, wave_fwd))

    # Compute load voltage over time as sum of step responses through cap
    # Each arriving wave V_i at time t_i contributes:
    #   For t >= t_i:  V_i * (2.0 - exp(-(t - t_i)/tau_load))   if tau_load > 0
    #   For t >= t_i:  V_i * 2.0                                  if tau_load ≈ 0

    # Sample voltage at key times
    sample_times = []
    for t_arr, _ in load_arrivals:
        sample_times.append(t_arr)
        # Also sample just after each arrival
        sample_times.append(t_arr + tau_load_s * 0.1)
        sample_times.append(t_arr + tau_load_s)
        sample_times.append(t_arr + tau_load_s * 3)

    # Add fine samples around first arrival
    for i in range(50):
        sample_times.append(Td_s + i * tau_load_s * 0.1)

    # Add coarse samples over full range
    for i in range(200):
        sample_times.append(i * total_time / 200.0)

    sample_times = sorted(set(t for t in sample_times if 0 <= t <= total_time))

    v_max = 0.0
    v_min = VDD  # for falling edge analysis we'd track differently
    v_waveform = []

    for t in sample_times:
        v = 0.0
        for t_arr, v_inc in load_arrivals:
            if t >= t_arr:
                elapsed = t - t_arr
                if tau_load_s > 1e-15:
                    # Capacitive load response to incident step
                    v += v_inc * (2.0 - math.exp(-elapsed / tau_load_s))
                else:
                    v += v_inc * 2.0
        v_waveform.append((t, v))
        if v > v_max:
            v_max = v
        if v < v_min:
            v_min = v

    # Also account for driver rise time smoothing the edges.
    # The actual peak is reduced if tr_driver >> Td (electrically short line).
    # Apply a smoothing correction: if the line is electrically short,
    # reflections are masked by the slow driver edge.
    # Knee frequency of driver: f_knee = 0.35 / tr
    f_knee = 0.35 / tr_driver_s if tr_driver_s > 0 else 1e12
    # Electrical length at knee frequency (one-way, in degrees)
    elec_length_deg = 360.0 * f_knee * Td_s
    result.electrical_length_deg = elec_length_deg

    # If electrically short (< 30°), reflections are negligible
    # Scale overshoot by a factor that goes from 0 at 0° to 1 at ~90°
    if elec_length_deg < 90:
        reflection_factor = math.sin(math.radians(elec_length_deg)) if elec_length_deg > 0 else 0
    else:
        reflection_factor = 1.0

    # --- Steady-state values ---
    # DC: CMOS receiver draws negligible current
    # VOH limited by resistive divider Rout + R_trace into high-Z load
    result.voh = VDD  # negligible DC drop for high-Z CMOS input
    result.vol = 0.0

    # --- Overshoot / Undershoot ---
    raw_overshoot = max(0, v_max - VDD)
    raw_undershoot = max(0, -v_min)  # how far below 0V

    result.overshoot_v = raw_overshoot * reflection_factor
    result.undershoot_v = raw_undershoot * reflection_factor
    result.peak_v = VDD + result.overshoot_v
    result.trough_v = -result.undershoot_v
    result.overshoot_pct = (result.overshoot_v / VDD * 100) if VDD > 0 else 0
    result.undershoot_pct = (result.undershoot_v / VDD * 100) if VDD > 0 else 0

    # --- Rise time at receiver ---
    # Root-sum-square model:
    # tr_total = sqrt(tr_driver² + tr_line² + tr_cap²)
    # tr_line: diffusion rise time of lossy line ≈ 2.2 * R_trace * C_metal / 2
    #          (distributed RC, factor of 1/2 for distributed vs lumped)
    # tr_cap:  capacitive loading at receiver = 2.2 * Z0 * C_load
    C_metal_F = net.cap_metal_pf * 1e-12
    tr_line_s = 1.1 * R_trace * C_metal_F  # distributed RC (half of lumped 2.2*RC)
    tr_cap_s = 2.2 * Z0 * C_load_F

    tr_total_s = math.sqrt(tr_driver_s**2 + tr_line_s**2 + tr_cap_s**2)
    tf_total_s = math.sqrt(tf_driver_s**2 + tr_line_s**2 + tr_cap_s**2)

    result.rise_time_ns = tr_total_s * 1e9
    result.fall_time_ns = tf_total_s * 1e9

    # --- Settling time ---
    # Find time when voltage stays within 5% of VDD
    settle_thresh = 0.05 * VDD
    settle_time = 0
    for t, v in reversed(v_waveform):
        if abs(v - VDD) > settle_thresh:
            settle_time = t
            break
    result.settling_time_ns = settle_time * 1e9

    # Bounce count: number of threshold crossings
    crossings = 0
    if len(v_waveform) > 1:
        for i in range(1, len(v_waveform)):
            v_prev = v_waveform[i-1][1]
            v_curr = v_waveform[i][1]
            # Count crossings of VDD (high rail)
            if (v_prev < VDD and v_curr >= VDD) or (v_prev >= VDD and v_curr < VDD):
                crossings += 1
    result.bounce_count = crossings

    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "net_name", "z0_avg", "z0_min", "z0_max", "z0_spread",
    "delay_ps", "length_in", "cap_pf", "ind_nh",
    "res_ohms", "res_per_in", "segments",
    "ic_drivers", "ic_receivers", "resistors", "capacitors",
    "voh", "vol", "rise_time_ns", "fall_time_ns",
    "overshoot_v", "overshoot_pct", "undershoot_v", "undershoot_pct",
    "peak_v", "trough_v", "gamma_source",
    "settling_time_ns", "bounce_count", "electrical_length_deg",
]


def write_csv(results: List[SIResult], filepath: str):
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in results:
            row = asdict(r)
            # Round floats for readability
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 4)
            writer.writerow({k: row[k] for k in CSV_FIELDS})


def print_summary(results: List[SIResult], driver: DriverModel):
    """Print a summary table to stdout."""
    print(f"\n{'='*100}")
    print(f"  HyperLynx SI Analysis — {len(results)} nets")
    print(f"  Driver: VDD={driver.vdd}V  Rout={driver.rout}Ω  "
          f"tr/tf={driver.rise_time}/{driver.fall_time}ns  Cin={driver.cin}pF")
    print(f"{'='*100}\n")

    # Header
    hdr = (f"{'Net Name':<45} {'Z0':>5} {'Td':>7} {'Len':>6} "
           f"{'VOH':>5} {'VOL':>5} {'tr':>6} {'tf':>6} "
           f"{'OS%':>6} {'US%':>6} {'Peak':>6} {'Settle':>7}")
    print(hdr)
    print("-" * len(hdr))

    for r in results:
        name = r.net_name[:44]
        print(f"{name:<45} {r.z0_avg:5.1f} {r.delay_ps:7.1f} {r.length_in:6.3f} "
              f"{r.voh:5.2f} {r.vol:5.2f} {r.rise_time_ns:6.3f} {r.fall_time_ns:6.3f} "
              f"{r.overshoot_pct:6.1f} {r.undershoot_pct:6.1f} {r.peak_v:6.2f} "
              f"{r.settling_time_ns:7.2f}")

    # Summary statistics
    if results:
        print(f"\n{'— Summary —':^100}")
        os_vals = [r.overshoot_pct for r in results]
        us_vals = [r.undershoot_pct for r in results]
        tr_vals = [r.rise_time_ns for r in results]
        settle_vals = [r.settling_time_ns for r in results]
        z0_spreads = [r.z0_spread for r in results]

        print(f"  Overshoot:   min={min(os_vals):.1f}%  max={max(os_vals):.1f}%  "
              f"avg={sum(os_vals)/len(os_vals):.1f}%")
        print(f"  Undershoot:  min={min(us_vals):.1f}%  max={max(us_vals):.1f}%  "
              f"avg={sum(us_vals)/len(us_vals):.1f}%")
        print(f"  Rise time:   min={min(tr_vals):.3f}ns  max={max(tr_vals):.3f}ns  "
              f"avg={sum(tr_vals)/len(tr_vals):.3f}ns")
        print(f"  Settling:    min={min(settle_vals):.2f}ns  max={max(settle_vals):.2f}ns  "
              f"avg={sum(settle_vals)/len(settle_vals):.2f}ns")
        print(f"  Z0 spread:   min={min(z0_spreads):.1f}Ω  max={max(z0_spreads):.1f}Ω  "
              f"avg={sum(z0_spreads)/len(z0_spreads):.1f}Ω")

        # Flag worst offenders
        print(f"\n{'— Nets with highest overshoot (top 10) —':^100}")
        for r in sorted(results, key=lambda x: x.overshoot_pct, reverse=True)[:10]:
            print(f"  {r.net_name[:60]:<60} OS={r.overshoot_pct:.1f}%  "
                  f"Z0={r.z0_avg:.1f}Ω  Td={r.delay_ps:.0f}ps")

        print(f"\n{'— Nets with highest Z0 spread (impedance discontinuity, top 10) —':^100}")
        for r in sorted(results, key=lambda x: x.z0_spread, reverse=True)[:10]:
            print(f"  {r.net_name[:60]:<60} ΔZ0={r.z0_spread:.1f}Ω  "
                  f"({r.z0_min:.1f}–{r.z0_max:.1f}Ω)")

        print(f"\n{'— Slowest rise times (top 10) —':^100}")
        for r in sorted(results, key=lambda x: x.rise_time_ns, reverse=True)[:10]:
            print(f"  {r.net_name[:60]:<60} tr={r.rise_time_ns:.3f}ns  "
                  f"C={r.cap_pf:.1f}pF  L={r.length_in:.3f}in")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze HyperLynx batch reports for signal integrity"
    )
    parser.add_argument("report", help="Path to HyperLynx batch report file")
    parser.add_argument("-o", "--output", default=None,
                        help="Output CSV file (default: <report>_si_results.csv)")
    parser.add_argument("--vdd", type=float, default=None,
                        help="Override driver VDD (V)")
    parser.add_argument("--rout", type=float, default=None,
                        help="Override driver output impedance (Ω)")
    parser.add_argument("--tr", type=float, default=None,
                        help="Override driver rise time (ns)")
    parser.add_argument("--tf", type=float, default=None,
                        help="Override driver fall time (ns)")
    parser.add_argument("--cin", type=float, default=None,
                        help="Override receiver input capacitance (pF)")
    parser.add_argument("--bounces", type=int, default=40,
                        help="Number of bounce iterations (default: 40)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress summary output, only write CSV")

    args = parser.parse_args()

    # Parse report
    driver, nets = parse_report(args.report)
    print(f"Parsed {len(nets)} nets from report.")
    print(f"Header defaults: VDD={driver.vdd}V  Rout={driver.rout}Ω  "
          f"tr/tf={driver.rise_time}/{driver.fall_time}ns  Cin={driver.cin}pF")

    # Apply overrides
    if args.vdd is not None: driver.vdd = args.vdd
    if args.rout is not None: driver.rout = args.rout
    if args.tr is not None: driver.rise_time = args.tr
    if args.tf is not None: driver.fall_time = args.tf
    if args.cin is not None: driver.cin = args.cin

    if any([args.vdd, args.rout, args.tr, args.tf, args.cin]):
        print(f"Using overrides: VDD={driver.vdd}V  Rout={driver.rout}Ω  "
              f"tr/tf={driver.rise_time}/{driver.fall_time}ns  Cin={driver.cin}pF")

    # Compute SI for each net
    results = []
    for net in nets:
        result = compute_si(net, driver, num_bounces=args.bounces)
        results.append(result)

    # Output
    if not args.quiet:
        print_summary(results, driver)

    out_path = args.output or args.report.rsplit(".", 1)[0] + "_si_results.csv"
    write_csv(results, out_path)
    print(f"\nResults written to: {out_path}")


if __name__ == "__main__":
    main()
