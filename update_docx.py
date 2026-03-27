#!/usr/bin/env python3
"""
Rebuild Word document SI analysis reports with JEDEC-compliant specs.

Structure:
  3.7  3.3V LVCMOS Signal Test (JESD8C.01)
    3.7.1  SIGNAL_NAME_1
      [7-row table]
    3.7.2  SIGNAL_NAME_2
      [7-row table]
    ...

Specs per JEDEC:
  JESD8C.01 (3.3V): VOH≥2.4V, VOL≤0.4V, VIH=2.0V, VIL=0.8V,
                     NM≥0.4V, tr/tf≤10ns, OS≤VDD+0.3V/≥-0.3V
  JESD8-7A  (1.8V): VOH≥1.35V, VOL≤0.45V, VIH=1.17V, VIL=0.63V,
                     NM≥0.18V, tr/tf≤10ns, OS≤VDD+0.3V/≥-0.3V

Usage:
    python3 update_docx.py
"""

import csv
import re
from collections import OrderedDict
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn


# ---------------------------------------------------------------------------
# JEDEC specifications
# ---------------------------------------------------------------------------

JEDEC_SPECS = {
    3.3: {
        "standard": "JESD8C.01",
        "voh": ("≥ 2.4V", 2.4, ">="),
        "vol": ("≤ 0.4V", 0.4, "<="),
        "tr":  ("≤ 10ns (JESD8C.01)", 10.0, "<="),
        "tf":  ("≤ 10ns (JESD8C.01)", 10.0, "<="),
        "nmh": ("≥ 0.4V", 0.4, ">="),
        "nml": ("≥ 0.4V", 0.4, ">="),
        "os_peak": 3.6,   # VDD + 0.3V
        "os_trough": -0.3,
        "os_spec": "Peak ≤ 3.6V, Trough ≥ −0.3V",
    },
    1.8: {
        "standard": "JESD8-7A",
        "voh": ("≥ 1.35V (VDD−0.45V)", 1.35, ">="),
        "vol": ("≤ 0.45V", 0.45, "<="),
        "tr":  ("≤ 10ns (JESD8-7A)", 10.0, "<="),
        "tf":  ("≤ 10ns (JESD8-7A)", 10.0, "<="),
        "nmh": ("≥ 0.18V", 0.18, ">="),
        "nml": ("≥ 0.18V", 0.18, ">="),
        "os_peak": 2.1,   # VDD + 0.3V
        "os_trough": -0.3,
        "os_spec": "Peak ≤ 2.1V, Trough ≥ −0.3V",
    },
}


def load_csv(path):
    """Load CSV preserving insertion order; group by base signal."""
    runs = OrderedDict()  # key = (net_name, run_id)
    with open(path) as f:
        for row in csv.DictReader(f):
            key = (row["net_name"], row["run_id"])
            runs[key] = row
    # Group into signal -> list of (run_id, row)
    signals = OrderedDict()
    for (net, rid), row in runs.items():
        signals.setdefault(net, []).append((rid, row))
    return signals


def pass_fail(comp, measured, threshold):
    if comp == ">=":
        return "PASS" if measured >= threshold else "FAIL"
    elif comp == "<=":
        return "PASS" if measured <= threshold else "FAIL"
    return "PASS"


def build_param_rows(run_data, specs, vdd):
    """Build 7 parameter rows for one run case. Returns list of
    (param_name, measured_str, spec_str, pf_str)."""
    voh = float(run_data["voh"])
    vol = float(run_data["vol"])
    tr = float(run_data["rise_time_ns"])
    tf = float(run_data["fall_time_ns"])
    nmh = float(run_data["nmh"])
    nml = float(run_data["nml"])
    peak = float(run_data["peak_v"])
    trough = float(run_data["trough_v"])

    os_peak_ok = peak <= specs["os_peak"]
    os_trough_ok = trough >= specs["os_trough"]
    os_pf = "PASS" if (os_peak_ok and os_trough_ok) else "FAIL"
    os_measured = f"Peak={peak:.3f}V, Trough={trough:.3f}V"

    rows = [
        ("VOH (V)",                  f"{voh:.3f}",
         specs["voh"][0], pass_fail(specs["voh"][2], voh, specs["voh"][1])),
        ("VOL (V)",                  f"{vol:.3f}",
         specs["vol"][0], pass_fail(specs["vol"][2], vol, specs["vol"][1])),
        ("Rise Time (ns)",           f"{tr:.2f}",
         specs["tr"][0],  pass_fail(specs["tr"][2], tr, specs["tr"][1])),
        ("Fall Time (ns)",           f"{tf:.2f}",
         specs["tf"][0],  pass_fail(specs["tf"][2], tf, specs["tf"][1])),
        ("Noise Margin High (V)",    f"{nmh:.3f}",
         specs["nmh"][0], pass_fail(specs["nmh"][2], nmh, specs["nmh"][1])),
        ("Noise Margin Low (V)",     f"{nml:.3f}",
         specs["nml"][0], pass_fail(specs["nml"][2], nml, specs["nml"][1])),
        ("Overshoot/Undershoot (V)", os_measured,
         specs["os_spec"], os_pf),
    ]
    return rows


def set_cell(cell, text, bold=False, size_pt=8, align=None):
    """Set cell text with formatting, clearing existing content."""
    para = cell.paragraphs[0]
    para.clear()
    run = para.add_run(text)
    run.font.size = Pt(size_pt)
    run.bold = bold
    if align:
        para.alignment = align


def add_signal_table(doc, sig_label, run_cases, specs, vdd):
    """Add a single signal's parameter table to the document."""
    # Determine columns: Signal, Parameter, Measured, Spec, P/F, Driver, Receiver
    headers = ["Signal", "Parameter", "Measured Value",
               "Specification", "Pass/Fail", "Driver Pin", "Receiver Pin"]

    # Collect all rows
    all_rows = []
    for run_id, run_data in run_cases:
        if run_id in ("A", "B"):
            label = f"{sig_label} (Run {run_id})"
        else:
            label = sig_label
        driver = run_data["driver_pin"]
        rx = run_data["rx_pins"]
        for param, meas, spec, pf in build_param_rows(run_data, specs, vdd):
            all_rows.append((label, param, meas, spec, pf, driver, rx))

    # Create table
    table = doc.add_table(rows=1 + len(all_rows), cols=7)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Header row
    for ci, h in enumerate(headers):
        set_cell(table.rows[0].cells[ci], h, bold=True, size_pt=9,
                 align=WD_ALIGN_PARAGRAPH.CENTER)

    # Data rows
    for ri, (sig, param, meas, spec, pf, drv, rx) in enumerate(all_rows):
        row = table.rows[ri + 1]
        set_cell(row.cells[0], sig, size_pt=8)
        set_cell(row.cells[1], param, size_pt=8)
        set_cell(row.cells[2], meas, size_pt=8, align=WD_ALIGN_PARAGRAPH.CENTER)
        set_cell(row.cells[3], spec, size_pt=8)
        # Color-code pass/fail
        set_cell(row.cells[4], pf, size_pt=8, bold=True,
                 align=WD_ALIGN_PARAGRAPH.CENTER)
        if pf == "FAIL":
            row.cells[4].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
        else:
            row.cells[4].paragraphs[0].runs[0].font.color.rgb = RGBColor(0x00, 0x80, 0x00)
        set_cell(row.cells[5], drv, size_pt=8)
        set_cell(row.cells[6], rx, size_pt=8)

    # Set column widths (approximate)
    widths = [Inches(1.8), Inches(1.5), Inches(1.4),
              Inches(1.6), Inches(0.6), Inches(1.2), Inches(1.4)]
    for row in table.rows:
        for ci, w in enumerate(widths):
            row.cells[ci].width = w

    # Add spacing after table
    doc.add_paragraph("")


def build_document(csv_path, vdd, section_num, section_title, output_path):
    """Build a complete restructured Word document."""
    specs = JEDEC_SPECS[vdd]
    signals = load_csv(csv_path)

    doc = Document()

    # Set default font
    style = doc.styles["Normal"]
    style.font.size = Pt(10)

    # Section heading: e.g. "3.7  3.3V LVCMOS Signal Test (JESD8C.01)"
    h1 = doc.add_heading(f"{section_num}    {section_title} ({specs['standard']})", level=1)

    # Brief spec summary paragraph
    if vdd == 3.3:
        doc.add_paragraph(
            "Per JESD8C.01: VIH ≥ 2.0V, VIL ≤ 0.8V, VOH ≥ 2.4V, VOL ≤ 0.4V, "
            "NM ≥ 0.4V, tr/tf ≤ 10ns @ 50pF, "
            "Absolute max: VDD+0.3V = 3.6V (overshoot), GND−0.3V = −0.3V (undershoot)."
        )
    else:
        doc.add_paragraph(
            "Per JESD8-7A: VIH ≥ 0.65×VDD = 1.17V, VIL ≤ 0.35×VDD = 0.63V, "
            "VOH ≥ VDD−0.45V = 1.35V, VOL ≤ 0.45V, "
            "NM ≥ 0.18V, tr/tf ≤ 10ns @ 50pF, "
            "Absolute max: VDD+0.3V = 2.1V (overshoot), GND−0.3V = −0.3V (undershoot)."
        )

    # Per-signal subsections
    for idx, (sig_name, run_cases) in enumerate(signals.items(), 1):
        # Subsection heading: e.g. "3.7.1  TR0_EXT_SYNC_P3V3"
        h2 = doc.add_heading(
            f"{section_num}.{idx}    {sig_name}", level=2)

        add_signal_table(doc, sig_name, run_cases, specs, vdd)

    doc.save(output_path)
    print(f"Built {output_path}: {len(signals)} signal subsections")


if __name__ == "__main__":
    print("=== Building P3V3 document ===")
    build_document(
        "p3v3_results.csv",
        3.3,
        "3.7",
        "3.3V LVCMOS Signal Test",
        "HyperLynx_SI_Analysis.docx",
    )

    print("\n=== Building P1V8 document ===")
    build_document(
        "p1v8_results.csv",
        1.8,
        "3.8",
        "1.8V LVCMOS Signal Test",
        "HyperLynx_P1V8_SI_Analysis.docx",
    )

    print("\nDone.")
