# HyperLynx SI Analysis — Claude Code Context

## Project Overview

Signal integrity analysis pipeline for PCB nets across voltage domains. Parses HyperLynx batch reports, applies per-IC IBIS-based driver/receiver models, and computes SI parameters (Z0, Td, length, C, VOH, VOL, tr, tf, overshoot, noise margin) using a bounce-diagram transmission line model.

## Key Files

| File | Purpose |
|---|---|
| `hlx_si_analyzer.py` | Core SI engine: parses HyperLynx reports, bounce-diagram model, `compute_si()` |
| `run_cases.py` | **P3V3 domain** — 155 run cases, 5 IC buffer models, 90 nets |
| `run_cases_p1v8.py` | **P1V8 domain** — 176 run cases, 7 IC buffer models, 108 nets |
| `Batch.RPT` | HyperLynx batch report (904 nets, VX.2.14) — input to both scripts |
| `temp.qcv` | Full netlist (7,396 nets) with component-pin connectivity |
| `netname_to_refdes.tsv` | Net-to-refdes mapping (1,564 lines) |
| `refdes_to_partnum.tsv` | Refdes-to-part-number mapping |
| `ic_partnum_pins.txt` | IC part numbers and their pins on analyzed nets |
| `p3v3_results.csv` / `p3v3_si_report.txt` | P3V3 computed results (155 cases) |
| `p1v8_results.csv` / `p1v8_si_report.txt` | P1V8 computed results (172 cases) |

## Usage

```bash
# P3V3 analysis
python3 run_cases.py Batch.RPT -o p3v3_results.csv
python3 run_cases.py Batch.RPT --list-cases

# P1V8 analysis
python3 run_cases_p1v8.py Batch.RPT -o p1v8_results.csv
python3 run_cases_p1v8.py Batch.RPT --list-cases

# Generic analyzer (single driver model)
python3 hlx_si_analyzer.py Batch.RPT --vdd 3.3 --rout 25 --tr 2.0 --tf 2.0 --cin 6.0
```

## IC Buffer Models

### P3V3 Domain (run_cases.py)
| Model | IC | VDD | Rout(Ω) | tr/tf(ns) | Cin(pF) | Source |
|---|---|---|---|---|---|---|
| LVC8T245_B | SN74LVC8T245 B-port | 3.3V | 25 | 2.1/2.0 | 6.0 | datasheet |
| HVD75_R | SN55HVD75 pin 1 (R output) | 3.3V | 42 | 3.0/3.0 | 3.0 | datasheet |
| HVD75_D | SN55HVD75 pin 4 (D input) | 3.3V | — | — | 3.0 | receiver only |
| HVD75_EN | SN55HVD75 pins 2,3 (RE#/DE) | 3.3V | — | — | 3.0 | receiver only |
| LMK04828_SDIO | LMK04828 STATUS/RESET/SYNC | 3.3V | 36 | 0.43/0.44 | 0.555 | IBIS |

### P1V8 Domain (run_cases_p1v8.py)
| Model | IC | VDD | Rout(Ω) | tr/tf(ns) | Cin(pF) | Source |
|---|---|---|---|---|---|---|
| FPGA_LVCMOS18 | XQVU13P HP_LVCMOS18_S_2 | 1.8V | 122 | 3.0/3.0 | 2.694 | IBIS (Rout), est (tr/tf) |
| ZYNQ_LVCMOS18 | XQZU3EG (same model) | 1.8V | 122 | 3.0/3.0 | 2.694 | IBIS |
| LVC8T245_18 | SN74LVC8T245 at 1.8V | 1.8V | 50 | 1.5/1.5 | 6.0 | scaled from 3.3V IBIS |
| TXS0104_A18 | TXS0104 A-side 1.8V | 1.8V | — | — | 4.18 | IBIS (passive, rx only) |
| DP83869_MDIO | DP83869 MDIO/MDC | 1.8V | 50 | 2.0/2.0 | 5.0 | datasheet est |
| LM239A | Quad comparator | 1.8V | — | — | 5.0 | receiver only (open-collector) |

## Output Columns

Reports include: Signal, Run, Driver Pin, RX Pin, Z0, Td(ps), Len, C(pF), **VOH**, **VOL**, tr(ns), tf(ns), OS%, **NMH**, **NML**

- **VOH/VOL**: Steady-state output voltage at receiver (high/low), reflects driver Rout drop + trace loss
- **NMH**: VOH − VIH (noise margin high)
- **NML**: VIL − VOL (noise margin low)
- VIH/VIL per receiver model: LVCMOS33 = 2.0V/0.8V, LVCMOS18 = 1.17V/0.63V, LMK04828 = 1.4V/0.4V

## Key Results Summary

### P3V3 (155 cases)
- **EXT_SYNC (128 cases)**: All pass, <5% OS in both directions
- **SYNC_EN (8 cases)**: 11-15% OS on longest multi-drop fanout nets (48pF load)
- **MBUS/CBUS TX/EN (10 cases)**: 25-28% OS on 10"+ traces (LVC8T245 Rout=25Ω vs Z0≈45Ω)
- **LMK STATUS (2 cases)**: ~11% OS (fast LMK04828 driver, tr=0.43ns)
- **LMK RST/SYNC (2 cases)**: 27-28% OS (LVC8T245 driving 10"+ to LMK04828)
- NMH: 1.22-1.30V, NML: 0.72-0.80V (all positive)

### P1V8 (172 cases)
- **Zero overshoot on all nets** — FPGA Rout=122Ω >> Z0≈46Ω, gamma_source positive
- Rise times range from 1.5ns (LVC8T245-driven short traces) to 23ns (FPGA-driven long SRS0 UCD nets)
- NMH/NML: 0.40-0.63V (all positive, tightest on long SRS0/MOSI nets)
- 4 FAN_PS nets not in batch report

## IBIS Extraction Notes

### HP_LVCMOS18_S_2 (XQVU13P)
- Rout_pd = 121.7Ω (pulldown, from V/I at 0.9V)
- Rout_pu = 176.7Ω (pullup, from V/I at VDD-0.9V)
- C_comp = 2.694pF (typ)
- No [Ramp] or Waveform data in IBIS file — tr/tf estimated at 3.0ns (SLOW slew, drive strength 2)
- Vinl = 0.63V, Vinh = 1.17V, Vmeas = 0.9V

### TXS0104 (LVC1T65_IO_A_18)
- Model_type = Input (passive open-drain translator, no push-pull driver)
- C_comp = 4.18pF (A-side at 1.8V)
- Internal ~10kΩ pullup — not suitable for bounce-diagram SI analysis as driver

## Architecture

```
Batch.RPT (HyperLynx) → hlx_si_analyzer.py (parse_report) → NetData objects
                                                                    ↓
run_cases.py / run_cases_p1v8.py → RunCase definitions + BufferModel per IC
                                                                    ↓
                                                        compute_case_si()
                                                                    ↓
                                              SIResult (VOH, VOL, tr, tf, OS%, NM)
                                                                    ↓
                                              print_results_table() + CSV output
```

## Session State

**Last session:** 2026-03-26

### What we did
- Extracted HP_LVCMOS18_S_2 IBIS parameters (Rout, C_comp) for XQVU13P FPGA
- Extracted TXS0104 IBIS parameters (C_comp=4.18pF, passive/receiver-only)
- Built `run_cases_p1v8.py` — 176 run cases across 108 P1V8 nets with 7 IC models
- Added VOH, VOL, NMH, NML columns to both P3V3 and P1V8 output formats
- Added VIH/VIL to BufferModel dataclass for per-receiver noise margin calculation
- Ran both P3V3 (155 cases) and P1V8 (172 cases) against Batch.RPT
- Ingested new files: Batch.RPT (batch report), temp.qcv (full 7,396-net netlist)

### Next up
- Investigate P1V8 nets with tight noise margins (SRS0_UCD, PRS_SPI0_MOSI)
- Consider series termination for P3V3 nets with >25% overshoot (MBUS/CBUS TX, LMK RST/SYNC)
- 4 FAN_PS P1V8 nets missing from batch report — may need re-export
- Potential use of temp.qcv netlist for expanded net coverage or cross-checking

### Context & decisions
- FPGA (XQVU13P) and Zynq (XQZU3EG) both use HP_LVCMOS18_S_2 model per user direction
- TXS0104 is passive (open-drain with internal pullup) — modeled as receiver only
- IBIS file for XQVU13P was truncated (no Ramp data) — tr/tf estimated
- LVC8T245 at 1.8V Rout scaled from 3.3V IBIS (25Ω → 50Ω)
- Batch.RPT contains 904 nets covering both P3V3 and P1V8 domains
