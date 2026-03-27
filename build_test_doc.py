#!/usr/bin/env python3
"""
Build the combined test document: tempvt.docx with all three SI results
sections and corrected test procedure specifications.

Usage:
  python build_test_doc.py                         # all signals
  python build_test_doc.py --test 2                # 2 signals/pairs per section
  python build_test_doc.py --test 2 -o test_out.docx
"""

import argparse
from docx import Document
from update_docx import build_resistor_pin_map
from insert_into_tempvt import insert_si_data
from insert_rs422_into_tempvt import insert_rs422_data
from insert_rs485_into_tempvt import insert_rs485_data
from insert_lvds_into_tempvt import insert_lvds_data
from fix_test_procedures import fix_all


def main():
    parser = argparse.ArgumentParser(
        description="Build combined test document with SI results"
    )
    parser.add_argument("-o", "--output", default=None,
                        help="Output file (default: overwrite tempvt.docx)")
    parser.add_argument("--test", type=int, default=None,
                        help="Limit to first N signals/pairs per section")

    args = parser.parse_args()

    print("Loading tempvt.docx...")
    doc = Document("tempvt.docx")
    r_net_map = build_resistor_pin_map("temp.qcv")

    # 1. Fix test procedure spec values (sections 1.3.5, 1.3.8, 1.3.11)
    print("\n=== Fixing test procedure specifications ===")
    fix_all(doc)

    # 2. Insert RS-422 results into Appendix B
    print("\n=== Inserting RS-422 SI data ===")
    insert_rs422_data(doc, "rs422_results.csv", max_pairs=args.test)

    # 3. Insert RS-485 results into Appendix B
    print("\n=== Inserting RS-485 SI data ===")
    insert_rs485_data(doc, "rs485_results.csv", max_pairs=args.test)

    # 4. Insert LVDS results into Appendix B
    print("\n=== Inserting LVDS SI data ===")
    insert_lvds_data(doc, "lvds_results.csv", max_pairs=args.test)

    # 5. Insert P1V8 LVCMOS results
    print("\n=== Inserting P1V8 SI data ===")
    insert_si_data(doc, "1.8V LVCMOS Signal Test", "p1v8_results.csv",
                   1.8, r_net_map, max_signals=args.test)

    # 6. Insert P3V3 LVCMOS results
    print("\n=== Inserting P3V3 SI data ===")
    insert_si_data(doc, "3.3V LVCMOS Signal Test", "p3v3_results.csv",
                   3.3, r_net_map, max_signals=args.test)

    output_path = args.output or "tempvt.docx"
    doc.save(output_path)
    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
