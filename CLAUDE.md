# HyperLynx SI Analysis — Claude Code Context

## Project Overview

Signal integrity analysis pipeline for PCB nets across voltage domains. Parses HyperLynx batch reports, applies per-IC IBIS-based driver/receiver models, and computes SI parameters (Z0, Td, length, C, VOH, VOL, tr, tf, overshoot, noise margin) using a bounce-diagram transmission line model.

## Key Files

| File | Purpose |
|---|---|
| `hlx_si_analyzer.py` | Core SI engine: parses HyperLynx reports, bounce-diagram model, `compute_si()` |
| `run_cases.py` | **P3V3 domain** — 155 run cases, 7 IC buffer models (incl. LMK04828_SYNC), 91 signals |
| `run_cases_p1v8.py` | **P1V8 domain** — 176 run cases, 7 IC buffer models, 104 signals |
| `update_docx.py` | Generates Word reports from CSV results with JEDEC-spec pass/fail |
| `Batch.RPT` | HyperLynx batch report (904 nets, VX.2.14) — input to both scripts |
| `temp.qcv` | Full netlist (7,396 nets) with component-pin connectivity |
| `delta_batch_nets.txt` | Nets missing from Batch.RPT or missing run cases — pending delta export |
| `p3v3_results.csv` | P3V3 computed results (155 cases) |
| `p1v8_results.csv` | P1V8 computed results (172 cases) |
| `problem_nets.csv` | Failing nets only, with failure reasons (JEDEC specs) |
| `HyperLynx_SI_Analysis.docx` | P3V3 Word report — per-signal subsections (§3.7.x) with JEDEC specs |
| `HyperLynx_P1V8_SI_Analysis.docx` | P1V8 Word report — per-signal subsections (§3.8.x) with JEDEC specs |
| `netname_to_refdes.tsv` | Net-to-refdes mapping (1,564 lines) |
| `refdes_to_partnum.tsv` | Refdes-to-part-number mapping |

## Usage

```bash
# P3V3 analysis
python3 run_cases.py Batch.RPT -o p3v3_results.csv

# P1V8 analysis
python3 run_cases_p1v8.py Batch.RPT -o p1v8_results.csv

# Rebuild Word reports from CSV results (JEDEC specs, per-signal tables)
python3 update_docx.py

# Generic analyzer (single driver model)
python3 hlx_si_analyzer.py Batch.RPT --vdd 3.3 --rout 25 --tr 2.0 --tf 2.0 --cin 6.0
```

## IC Buffer Models (IBIS-corrected 2026-03-27)

### P3V3 Domain (run_cases.py)
| Model | IC | VDD | Rout(Ω) | tr/tf(ns) | Cin(pF) | VIH/VIL(V) | Source |
|---|---|---|---|---|---|---|---|
| LVC8T245_B | SN74LVC8T245 B-port | 3.3V | 14.5 | 1.44/1.16 | 6.05 | —/— | IBIS |
| HVD75_R | SN55HVD75 pin 1 (R output) | 3.3V | 24.5 | 10.1/9.1 | 2.78 | —/— | IBIS |
| HVD75_D | SN55HVD75 pin 4 (D input) | 3.3V | — | — | 0.762 | —/— | IBIS (rx only) |
| HVD75_EN | SN55HVD75 pins 2,3 (RE#/DE) | 3.3V | — | — | 0.762 | —/— | IBIS (rx only) |
| LMK04828_SDIO | LMK04828 STATUS/RESET | 3.3V | 43.4 | 0.43/0.44 | 0.555 | 1.6/0.4 | IBIS |
| LMK04828_SYNC | LMK04828 pin 6 (SYNC) | 3.3V | — | — | 0.184 | 1.2/0.4 | IBIS (rx only) |
| DP83869_GPIO | DP83869 GPIO at 3.3V | 3.3V | 98.0 | 0.75/0.51 | 1.106 | 1.7/0.7 | IBIS (mid-range drive) |

### P1V8 Domain (run_cases_p1v8.py)
| Model | IC | VDD | Rout(Ω) | tr/tf(ns) | Cin(pF) | VIH/VIL(V) | Source |
|---|---|---|---|---|---|---|---|
| FPGA_LVCMOS18 | XQVU13P HP_LVCMOS18_S_2 | 1.8V | 117 | 0.28/0.24 | 2.694 | 1.17/0.63 | IBIS |
| ZYNQ_LVCMOS18 | XQZU3EG (same model) | 1.8V | 117 | 0.28/0.24 | 2.694 | 1.17/0.63 | IBIS |
| LVC8T245_18 | SN74LVC8T245 at 1.8V | 1.8V | 22.7 | 2.99/2.41 | 6.34 | 1.2675/0.5775 | IBIS |
| TXS0104_A18 | TXS0104 A-side 1.8V | 1.8V | — | — | 4.18 | 1.17/0.63 | IBIS (passive, rx only) |
| DP83869_MDIO | DP83869 MDIO/MDC | 1.8V | 50 | 0.32/0.33 | 1.311 | 1.204/0.344 | IBIS (mid-range drive) |
| LM239A | Quad comparator | 1.8V | — | — | 5.0 | 1.17/0.63 | rx only (open-collector) |

## JEDEC Specifications (applied to Word reports)

### JESD8C.01 — 3.3V LVCMOS
| Parameter | Spec | Notes |
|---|---|---|
| VOH | ≥ 2.4V | |
| VOL | ≤ 0.4V | |
| VIH | 2.0V (fixed) | TTL-compatible |
| VIL | 0.8V (fixed) | TTL-compatible |
| NM | ≥ 0.4V | VOH-VIH or VIL-VOL |
| tr/tf | ≤ 10ns | 10-90% @ 50pF |
| Overshoot | Peak ≤ VDD+0.3V = 3.6V | Absolute max |
| Undershoot | Trough ≥ GND-0.3V = -0.3V | Absolute max |

### JESD8-7A — 1.8V LVCMOS
| Parameter | Spec | Notes |
|---|---|---|
| VOH | ≥ 1.35V (VDD-0.45V) | |
| VOL | ≤ 0.45V | |
| VIH | 0.65×VDD = 1.17V | Ratiometric |
| VIL | 0.35×VDD = 0.63V | Ratiometric |
| NM | ≥ 0.18V | |
| tr/tf | ≤ 10ns | 10-90% @ 50pF |
| Overshoot | Peak ≤ VDD+0.3V = 2.1V | Absolute max |
| Undershoot | Trough ≥ GND-0.3V = -0.3V | Absolute max |

## Key Results Summary (JEDEC specs, 2026-03-27)

**174 total failures** across both domains (down from 212 with old arbitrary specs).

### P3V3 (155 cases → ~110 failures)
- **HVD75 Run A (64 cases)**: ~10ns rise time — borderline FAIL at 10ns JEDEC limit
- **LVC8T245 Run B overshoot**: Many nets exceed 3.6V peak / -0.3V trough (Rout=14.5Ω << Z0≈45Ω)
- **SYNC_EN (8 cases)**: Peak 4.4-4.8V, Trough -1.2 to -1.6V — severe, latch-up risk
- **CBUS/MBUS TX/EN (4 cases)**: Peak ~4.9V, Trough ~-1.6V — worst offenders
- **LMK RST/SYNC (2 cases)**: Peak ~4.9V, NML=0.389V (barely <0.4V JEDEC min)

### P1V8 (172 cases → ~64 failures)
- **FPGA slow-slew long traces**: tr/tf 10-18ns on SRS0_UCD, SRS1_UCD, PRS_SPI nets
- **LVC8T245_18 inbound overshoot**: Peak 2.1-2.3V on TR16-39 Run B (Rout=22.7Ω << Z0≈46Ω)
- **Trough undershoot**: Several TR_EXT_SYNC Run B nets at -0.3 to -0.5V

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
                                              CSV output → update_docx.py → Word reports
```

## Session State

**Last session:** 2026-03-27

### What we did
**2026-03-27 (latest — IBIS audit + JEDEC specs + Word restructure):**
- **Full IBIS model audit**: Corrected all IC buffer models from actual IBIS files:
  - LVC8T245_B: rout 25→14.5Ω, tr/tf 2.1/2.0→1.44/1.16ns, cin 6.0→6.05pF
  - LVC8T245_18: rout 50→22.7Ω, tr/tf 1.5/1.5→2.99/2.41ns, cin 6.0→6.34pF
  - HVD75_R: rout 42→24.5Ω, tr/tf 3.0/3.0→10.1/9.1ns (intentionally slow RS-422 rx output), cin 3.0→2.78pF
  - HVD75_D/EN: cin 3.0→0.762pF
  - LMK04828_SDIO: rout 36→43.4Ω, vih 1.4→1.6V
  - NEW LMK04828_SYNC model (pin 6): cin=0.184pF, vih=1.2V — separate from SDIO
  - DP83869_GPIO (3.3V): rout 25→98Ω (mid-range, drive strength register unknown), cin 5.0→1.106pF
  - DP83869_MDIO (1.8V): tr/tf 2.0→0.32/0.33ns, cin 5.0→1.311pF, vih/vil updated
- **Applied JEDEC specs** (JESD8C.01 for 3.3V, JESD8-7A for 1.8V) replacing arbitrary thresholds
- **Restructured Word reports**: Per-signal subsections (§3.7.x P3V3, §3.8.x P1V8) with individual tables
- Overshoot now measured as absolute peak/trough voltage vs VDD+0.3V / GND-0.3V
- Generated `problem_nets.csv` with 174 JEDEC failures
- Created `.claude/settings.json` to allow all tools without permission prompts

**2026-03-27 (earlier — IBIS extraction + netlist audit):**
- Parsed XQVU13P pin parasitics (97 pins), XQZU3EG pin parasitics (4 pins)
- Audited master netlist vs Batch.RPT: 46.5% coverage, created `delta_batch_nets.txt`
- Traced AM26LV31E input signals through LVC8T245 level translators
- Identified 15 P1V8 feeder nets + 10 P3V3 nets needing new run cases

### In progress
Nothing in progress — session ended cleanly.

### Next up
- **DP83869 drive strength**: Determine IO_MUX_CFG register value (0x0170, bits [4:0]) from firmware or live register readback — affects both GPIO (3.3V) and MDIO (1.8V) Rout
- **Delta batch report**: User to produce HyperLynx export for 42 missing nets in `delta_batch_nets.txt`
- **New run cases**: Add 15 P1V8 AM26LV31E feeder nets + 2 P3V3 control nets (P48V_EN, FAN_EN) + 8 DIR_AB nets
- **Overshoot mitigation**: Consider series termination for SYNC_EN/CBUS/MBUS/LMK nets (peak 4-5V exceeds 3.6V abs max)
- **RS-422 analysis**: AM26LV31E differential output nets — separate analysis effort
- **HVD75 rise time**: 10ns tr is right at JEDEC limit — confirm acceptable for the application data rate

### Context & decisions
- FPGA (XQVU13P) and Zynq (XQZU3EG) both use HP_LVCMOS18_S_2 model per user direction
- TXS0104 is passive (open-drain with internal pullup) — modeled as receiver only
- TXS0104 and LM239A have no IBIS models available — skipped per user direction
- LVC8T245 at 1.8V now uses actual IBIS extraction (not scaled from 3.3V)
- HVD75_R ~10ns rise time is correct per IBIS — intentionally slow RS-422 receiver output
- DP83869 drive strength register (IO_MUX_CFG, reg 0x0170) default unknown — using mid-range Rout estimate
- LMK04828 SYNC pin (pin 6) uses completely different IBIS model than SDIO pins — lower Cin, different VIH
- Word reports use `update_docx.py` which rebuilds entirely from CSV (not in-place update)
- JEDEC specs applied: JESD8C.01 (3.3V) and JESD8-7A (1.8V) — overshoot is now absolute voltage, not percentage
- Batch.RPT contains 904 nets covering both P3V3 and P1V8 domains
