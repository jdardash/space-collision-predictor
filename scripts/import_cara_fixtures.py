"""Regenerate tests/data/cara/ from the vendored NASA CARA reference clone.

Source: reference/CARA_Analysis_Tools (nasa/CARA_Analysis_Tools at commit 86ba28a),
released under the NASA Open Source Agreement v1.3. The agreement permits use,
modification, distribution and redistribution with attribution and inclusion of
the agreement text, so the fixtures below are vendored alongside one NOSA PDF and
a NOTICE.md naming the source.

What gets imported:

* cdms/                          53 KVN CDMs with CARA's own Foster-1992 Pc at line 16
* sample_cdms/                   34 adversarial KVN CDMs (Alfano, Omitron, Frisbee, SingleCov)
* pc_method_test_conjunctions.csv  CARA_PcMethod_Test_Conjunctions.xlsx, 53 rows x 35 columns
* alfano_expected_pc.json        the 11 Alfano Pc literals from Pc2D_Foster_UnitTest.m:31-42
* alfano_pc3d_reference.csv      A09TestCases_01_12_Distributed.csv (3D-Nc cross-check)
* NOSA_GSC-18593-1.pdf           the NASA Open Source Agreement text
* NOTICE.md                      source repo, commit, license and file manifest

The xlsx is converted with zipfile plus xml.etree so no new dependency is needed;
openpyxl is used only if it happens to be importable. Numeric cells are written
verbatim from the sheet XML so full double precision survives the round trip.

Run:  .venv/Scripts/python scripts/import_cara_fixtures.py [--check]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CARA_ROOT = REPO_ROOT / "reference" / "CARA_Analysis_Tools"
OUT_ROOT = REPO_ROOT / "tests" / "data" / "cara"

CARA_REPO = "https://github.com/nasa/CARA_Analysis_Tools"
CARA_COMMIT = "86ba28a"
CARA_LICENSE = "NASA Open Source Agreement (NOSA) v1.3"
NOSA_PDFS = (
    "NOSA_GSC-18593-1.pdf",
    "NOSA_GSC-18848-1.pdf",
    "NOSA_GSC-19027-1.pdf",
    "NOSA_GSC-19374-1.pdf",
)
NOSA_COPIED = NOSA_PDFS[0]

SIZE_BUDGET_BYTES = 3 * 1024 * 1024

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_MATLAB_UNIT_TEST = (
    CARA_ROOT / "DistributedMatlab" / "ProbabilityOfCollision" / "UnitTests"
    / "Pc2D_Foster_UnitTest.m"
)
_A09_PC3D_CSV = (
    CARA_ROOT / "DistributedMatlab" / "ProbabilityOfCollision" / "Pc3D_Hall_Utils"
    / "Output_AlfanoTestCases_Pc3D" / "A09TestCases_01_12_Distributed.csv"
)


# ---------------------------------------------------------------------------
# xlsx -> CSV without new dependencies
# ---------------------------------------------------------------------------


def _column_index(cell_ref: str) -> int:
    """Zero-based column index from an A1-style cell reference."""
    index = 0
    for char in cell_ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index - 1


def _read_xlsx_with_openpyxl(path: Path) -> list[list[str]] | None:
    """Read the first worksheet via openpyxl if that package is installed."""
    try:
        import openpyxl  # type: ignore[import-not-found]
    except ImportError:
        return None
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = book[book.sheetnames[0]]
    rows: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        rows.append(
            ["" if v is None else repr(v) if isinstance(v, float) else str(v) for v in row]
        )
    book.close()
    return rows


def read_xlsx_first_sheet(path: Path) -> list[list[str]]:
    """Return the first worksheet of an xlsx as a rectangular list of strings.

    Uses zipfile plus xml.etree on xl/worksheets/sheet1.xml and
    xl/sharedStrings.xml. Numeric cells keep the exact text the writer stored,
    so the reference Pc values survive at full double precision.

    The stdlib XML parser is used deliberately: the only input is the pinned
    NASA spreadsheet inside reference/, checked out at a fixed commit and never
    supplied by a caller, so the XXE and entity-expansion vectors that would
    justify defusedxml (not a dependency of this project) do not apply.
    """
    from_openpyxl = _read_xlsx_with_openpyxl(path)
    if from_openpyxl is not None:
        return from_openpyxl

    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            sst_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in sst_root:
                shared.append("".join(t.text or "" for t in item.iter(f"{_SHEET_NS}t")))
        sheet_root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    sheet_data = sheet_root.find(f"{_SHEET_NS}sheetData")
    if sheet_data is None:
        raise ValueError(f"{path.name}: worksheet has no sheetData element")

    parsed: list[dict[int, str]] = []
    width = 0
    for row in sheet_data:
        cells: dict[int, str] = {}
        for cell in row:
            ref = cell.get("r")
            if ref is None:
                continue
            cell_type = cell.get("t")
            if cell_type == "inlineStr":
                inline = cell.find(f"{_SHEET_NS}is")
                value = (
                    "".join(t.text or "" for t in inline.iter(f"{_SHEET_NS}t"))
                    if inline is not None
                    else ""
                )
            else:
                node = cell.find(f"{_SHEET_NS}v")
                value = "" if node is None or node.text is None else node.text
                if cell_type == "s":
                    value = shared[int(value)]
            cells[_column_index(ref)] = value
        parsed.append(cells)
        if cells:
            width = max(width, max(cells) + 1)

    return [[cells.get(i, "") for i in range(width)] for cells in parsed if cells]


# ---------------------------------------------------------------------------
# Alfano literals out of the MATLAB unit test
# ---------------------------------------------------------------------------


def _matlab_cell_array(source: str, name: str) -> list[str]:
    """Extract the entries of a MATLAB `name = {a, b, c};` cell array."""
    match = re.search(rf"{name}\s*=\s*\{{(.*?)\}}\s*;", source, re.DOTALL)
    if match is None:
        raise ValueError(f"{name} not found in {_MATLAB_UNIT_TEST.name}")
    body = match.group(1)
    body = re.sub(r"\.\.\.\s*\n", " ", body)
    return [part.strip().strip("'\"") for part in body.split(",") if part.strip()]


def extract_alfano_literals() -> dict[str, Any]:
    """Pull the 11 Alfano expected-Pc literals and their HBRs from the MATLAB test."""
    source = _MATLAB_UNIT_TEST.read_text(encoding="utf-8", errors="replace")
    file_nums = _matlab_cell_array(source, "AlfanoFileNum")
    expected = _matlab_cell_array(source, "AlfanoExpSolution")
    hbrs = _matlab_cell_array(source, "AlfanoHBR")
    accuracy = _matlab_cell_array(source, "AlfanoAccuracy")
    t_limit = _matlab_cell_array(source, "AlfanoTLimit")

    counts = {len(file_nums), len(expected), len(hbrs), len(accuracy), len(t_limit)}
    if len(counts) != 1:
        raise ValueError(f"Alfano parameter arrays disagree in length: {counts}")

    cases = [
        {
            "case": num,
            "cdm": f"AlfanoTestCase{num}.cdm",
            "hbr_m": float(hbr),
            "expected_pc": float(pc),
            "rel_tolerance": float(acc),
            "t_limit_s": float(tlim),
        }
        for num, pc, hbr, acc, tlim in zip(
            file_nums, expected, hbrs, accuracy, t_limit, strict=True
        )
    ]
    return {
        "source": (
            "DistributedMatlab/ProbabilityOfCollision/UnitTests/Pc2D_Foster_UnitTest.m"
            " lines 31-45 (AlfanoHBR, AlfanoTLimit, AlfanoAccuracy, AlfanoExpSolution,"
            " AlfanoFileNum)"
        ),
        "repository": CARA_REPO,
        "commit": CARA_COMMIT,
        "license": CARA_LICENSE,
        "note": (
            "Expected values are 2D-Pc from Pc2D_Foster with a circular hard body and"
            " rel tolerance 0.001 (AlfanoAccuracy). CDM states sit at the millisecond"
            " TCA and must be advanced to the true close approach first."
        ),
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _copy_tree(src_dir: Path, dst_dir: Path, pattern: str) -> list[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for stale in dst_dir.glob(pattern):
        stale.unlink()
    copied = []
    for path in sorted(src_dir.glob(pattern)):
        target = dst_dir / path.name
        shutil.copyfile(path, target)
        copied.append(target)
    return copied


def _write_notice(manifest: dict[str, int], total_bytes: int) -> Path:
    lines = [
        "# NASA CARA test fixtures",
        "",
        f"Source repository: {CARA_REPO}",
        f"Pinned commit: {CARA_COMMIT}",
        f"License: {CARA_LICENSE} (full text in {NOSA_COPIED})",
        "",
        "Copyright (c) United States Government as represented by the Administrator",
        "of the National Aeronautics and Space Administration. All Rights Reserved.",
        "",
        "The NOSA v1.3 permits use, reproduction, modification, distribution and",
        "redistribution of the Original Software provided the agreement text and this",
        "notice travel with it. Nothing in this directory has been modified apart from",
        "the xlsx-to-CSV conversion and the JSON extraction of MATLAB literals noted",
        "below; the CDM and PDF bytes are verbatim copies.",
        "",
        "The four NOSA agreements published at the root of the source repository are",
        f"{', '.join(NOSA_PDFS)}. One of them ({NOSA_COPIED}) is",
        "vendored here; the others are identical in terms and differ only in the NASA",
        "case number they cover.",
        "",
        "## Files",
        "",
        "| Path | Count | Source in CARA_Analysis_Tools |",
        "| --- | ---: | --- |",
        f"| `cdms/*.cdm` | {manifest['cdms']} | `DataFiles/PcTestCaseCDMs/*.cdm` |",
        f"| `sample_cdms/*.cdm` | {manifest['sample_cdms']} | `DataFiles/SampleCDMs/*.cdm` |",
        "| `pc_method_test_conjunctions.csv` | 1 |"
        " `DataFiles/PcTestCaseCDMs/CARA_PcMethod_Test_Conjunctions.xlsx` (converted) |",
        "| `alfano_expected_pc.json` | 1 |"
        " `DistributedMatlab/ProbabilityOfCollision/UnitTests/Pc2D_Foster_UnitTest.m`"
        " lines 31-45 (extracted) |",
        "| `alfano_pc3d_reference.csv` | 1 |"
        " `DistributedMatlab/ProbabilityOfCollision/Pc3D_Hall_Utils/"
        "Output_AlfanoTestCases_Pc3D/A09TestCases_01_12_Distributed.csv` |",
        f"| `{NOSA_COPIED}` | 1 | repository root |",
        "",
        f"Total vendored size: {total_bytes / 1024:.0f} KiB.",
        "",
        "## Regenerating",
        "",
        "```bash",
        ".venv/Scripts/python scripts/import_cara_fixtures.py",
        "```",
        "",
        "The importer is the only thing that writes this directory. Do not hand-edit",
        "the fixtures; re-run the importer against the pinned clone instead.",
        "",
    ]
    path = OUT_ROOT / "NOTICE.md"
    path.write_text("\n".join(lines), encoding="ascii")
    return path


def import_fixtures(check_only: bool = False) -> int:
    """Copy and convert every CARA fixture into tests/data/cara/."""
    if not CARA_ROOT.is_dir():
        print(f"error: reference clone not found at {CARA_ROOT}", file=sys.stderr)
        return 2

    if check_only:
        missing = [
            str(p)
            for p in (
                OUT_ROOT / "NOTICE.md",
                OUT_ROOT / "pc_method_test_conjunctions.csv",
                OUT_ROOT / "alfano_expected_pc.json",
                OUT_ROOT / "alfano_pc3d_reference.csv",
                OUT_ROOT / NOSA_COPIED,
            )
            if not p.exists()
        ]
        n_cdm = len(list((OUT_ROOT / "cdms").glob("*.cdm"))) if (OUT_ROOT / "cdms").is_dir() else 0
        if missing or n_cdm != 53:
            print(f"fixtures incomplete: missing={missing} cdms={n_cdm}", file=sys.stderr)
            return 1
        print(f"fixtures present in {OUT_ROOT} ({n_cdm} CDMs)")
        return 0

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    cdms = _copy_tree(CARA_ROOT / "DataFiles" / "PcTestCaseCDMs", OUT_ROOT / "cdms", "*.cdm")
    samples = _copy_tree(
        CARA_ROOT / "DataFiles" / "SampleCDMs", OUT_ROOT / "sample_cdms", "*.cdm"
    )

    xlsx = CARA_ROOT / "DataFiles" / "PcTestCaseCDMs" / "CARA_PcMethod_Test_Conjunctions.xlsx"
    rows = read_xlsx_first_sheet(xlsx)
    csv_path = OUT_ROOT / "pc_method_test_conjunctions.csv"
    with csv_path.open("w", newline="", encoding="ascii", errors="replace") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)

    alfano = extract_alfano_literals()
    alfano_path = OUT_ROOT / "alfano_expected_pc.json"
    alfano_path.write_text(json.dumps(alfano, indent=2) + "\n", encoding="ascii")

    pc3d_path = OUT_ROOT / "alfano_pc3d_reference.csv"
    shutil.copyfile(_A09_PC3D_CSV, pc3d_path)

    nosa_path = OUT_ROOT / NOSA_COPIED
    shutil.copyfile(CARA_ROOT / NOSA_COPIED, nosa_path)

    total = sum(p.stat().st_size for p in OUT_ROOT.rglob("*") if p.is_file())
    manifest = {"cdms": len(cdms), "sample_cdms": len(samples)}
    _write_notice(manifest, total)
    total = sum(p.stat().st_size for p in OUT_ROOT.rglob("*") if p.is_file())

    print(f"cdms                 : {len(cdms)}")
    print(f"sample_cdms          : {len(samples)}")
    print(f"csv rows (with head) : {len(rows)} x {len(rows[0]) if rows else 0}")
    print(f"alfano literals      : {len(alfano['cases'])}")
    print(f"pc3d reference rows  : {len(pc3d_path.read_text().splitlines()) - 1}")
    print(f"total vendored bytes : {total} ({total / 1024:.0f} KiB)")

    if len(cdms) != 53:
        print(f"warning: expected 53 test-case CDMs, copied {len(cdms)}", file=sys.stderr)
    if len(alfano["cases"]) != 11:
        print(f"warning: expected 11 Alfano literals, got {len(alfano['cases'])}", file=sys.stderr)
    if total > SIZE_BUDGET_BYTES:
        print(
            f"warning: vendored set is {total / 1024 / 1024:.2f} MiB, over the 3 MB budget",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="verify the fixtures exist without rewriting them"
    )
    args = parser.parse_args(argv)
    return import_fixtures(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
