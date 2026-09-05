"""Ingest source documents into provenance-preserving evidence records."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from regodit.config import ARTIFACT_DIR, DATA_DIR, EVIDENCE_PATH, PROJECT_ROOT
from regodit.models import Evidence
from regodit.questionnaire import build_manifest

ROOT = PROJECT_ROOT

LOGGER = logging.getLogger("regodit.ingestion")
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
SUPPORTED = {".docx", ".xlsx", ".pdf", ".png"}

EVIDENCE_TYPE_BY_CATEGORY = {
    "policy": "POLICY_REQUIREMENT",
    "assessment": "ASSESSMENT_EVIDENCE",
    "contract": "CONTRACTUAL_REQUIREMENT",
    "operational/infrastructure": "OPERATIONAL_RECORD",
    "diagram": "OBSERVATION",
    "questionnaire": "OBSERVATION",
    "other": "OBSERVATION",
}


def _stable_id(source_path: str, location: str, record_kind: str) -> str:
    value = f"{source_path}\0{location}\0{record_kind}".encode()
    return "ev-" + hashlib.sha256(value).hexdigest()[:24]


def _entity_for_source(source_path: str, all_text: str) -> str:
    name = Path(source_path).name.casefold()
    if "solsphere" in name:
        return "Solsphere"
    if "regodit" in name:
        return "Regodit"
    has_regodit = bool(re.search(r"\bregodit\b", all_text, re.IGNORECASE))
    has_solsphere = bool(re.search(r"\bsolsphere\b", all_text, re.IGNORECASE))
    if has_regodit and has_solsphere:
        return "multiple"
    if has_regodit:
        return "Regodit"
    if has_solsphere:
        return "Solsphere"
    return "unknown"


def _record(
    source_path: str,
    source_category: str,
    organization: str,
    location: str,
    text: str,
    record_kind: str,
    metadata: dict[str, Any],
) -> Evidence:
    return Evidence(
        id=_stable_id(source_path, location, record_kind),
        source_name=Path(source_path).name,
        source_path=source_path,
        source_category=source_category,
        evidence_type=EVIDENCE_TYPE_BY_CATEGORY[source_category],
        organization=organization,
        location=location,
        text=text,
        metadata={"record_kind": record_kind, **metadata},
    )


def _word_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{W}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            parts.append("\t")
        elif node.tag in {f"{{{W}}}br", f"{{{W}}}cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _docx_parts(path: Path) -> list[dict[str, Any]]:
    with ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        styles: dict[str, str] = {}
        if "word/styles.xml" in archive.namelist():
            for style in ET.fromstring(archive.read("word/styles.xml")).findall(f"{{{W}}}style"):
                style_id = style.attrib.get(f"{{{W}}}styleId")
                name = style.find(f"{{{W}}}name")
                if style_id and name is not None:
                    styles[style_id] = name.attrib.get(f"{{{W}}}val", style_id)
        body = document.find(f"{{{W}}}body")
        if body is None:
            return []
        parts: list[dict[str, Any]] = []
        heading = "Document start"
        paragraph_number = 0
        table_number = 0
        for child in body:
            if child.tag == f"{{{W}}}p":
                text = _word_text(child)
                if not text:
                    continue
                paragraph_number += 1
                style_id = None
                props = child.find(f"{{{W}}}pPr")
                if props is not None:
                    style = props.find(f"{{{W}}}pStyle")
                    if style is not None:
                        style_id = style.attrib.get(f"{{{W}}}val")
                style_name = styles.get(style_id or "", style_id or "")
                is_heading = "heading" in style_name.casefold() or "title" in style_name.casefold()
                if is_heading:
                    heading = text
                parts.append({
                    "kind": "heading" if is_heading else "paragraph",
                    "location": f"paragraph {paragraph_number}",
                    "text": text,
                    "metadata": {"section_heading": heading, "paragraph_number": paragraph_number, "style": style_name or None},
                })
            elif child.tag == f"{{{W}}}tbl":
                table_number += 1
                rows = child.findall(f"{{{W}}}tr")
                for row_number, row in enumerate(rows, 1):
                    cells = [_word_text(cell) for cell in row.findall(f"{{{W}}}tc")]
                    if not any(cells):
                        continue
                    parts.append({
                        "kind": "table_row",
                        "location": f"table {table_number}, row {row_number}",
                        "text": " | ".join(cells),
                        "metadata": {
                            "section_heading": heading,
                            "table_number": table_number,
                            "row_number": row_number,
                            "cells": cells,
                        },
                    })
        return parts


def ingest_docx(path: Path, source_path: str, category: str) -> list[Evidence]:
    parts = _docx_parts(path)
    entity = _entity_for_source(source_path, "\n".join(part["text"] for part in parts))
    return [_record(source_path, category, entity, part["location"], part["text"], part["kind"], part["metadata"]) for part in parts]


def _xlsx_sheets(path: Path) -> Iterator[tuple[str, list[tuple[int, dict[str, Any]]]]]:
    with ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(n.text or "" for n in item.iter(f"{{{M}}}t")) for item in shared_root]
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.findall(f".//{{{M}}}sheet"):
            name = sheet.attrib["name"]
            target = targets[sheet.attrib[f"{{{R}}}id"]]
            sheet_path = str(PurePosixPath("xl") / target)
            root = ET.fromstring(archive.read(sheet_path))
            rows: list[tuple[int, dict[str, Any]]] = []
            for row in root.findall(f".//{{{M}}}sheetData/{{{M}}}row"):
                values: dict[str, Any] = {}
                for cell in row.findall(f"{{{M}}}c"):
                    match = re.match(r"[A-Z]+", cell.attrib["r"])
                    if not match:
                        continue
                    column = match.group()
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find(f"{{{M}}}v")
                    formula_node = cell.find(f"{{{M}}}f")
                    if cell_type == "inlineStr":
                        value: Any = "".join(n.text or "" for n in cell.iter(f"{{{M}}}t"))
                    elif value_node is None:
                        value = None
                    elif cell_type == "s":
                        value = shared[int(value_node.text or "0")]
                    elif cell_type == "b":
                        value = value_node.text == "1"
                    else:
                        value = value_node.text
                    if formula_node is not None:
                        value = {"formula": formula_node.text or "", "cached_value": value}
                    if value not in (None, ""):
                        values[column] = value
                if values:
                    rows.append((int(row.attrib["r"]), values))
            yield name, rows


def _header_mapping(first_row: dict[str, Any]) -> dict[str, str]:
    mapping = {}
    used: Counter[str] = Counter()
    for column, value in first_row.items():
        raw = str(value).strip() if not isinstance(value, dict) else column
        key = raw or column
        used[key] += 1
        mapping[column] = key if used[key] == 1 else f"{key} ({column})"
    return mapping


def ingest_xlsx(path: Path, source_path: str, category: str) -> list[Evidence]:
    sheet_rows = list(_xlsx_sheets(path))
    all_text = json.dumps(sheet_rows, ensure_ascii=False, default=str)
    entity = _entity_for_source(source_path, all_text)
    records: list[Evidence] = []
    for sheet, rows in sheet_rows:
        if not rows:
            continue
        header_row_number, header_values = rows[0]
        headers = _header_mapping(header_values)
        for row_number, cells in rows:
            data = {headers.get(column, column): value for column, value in cells.items()}
            text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
            records.append(_record(
                source_path, category, entity, f"sheet {sheet!r}, row {row_number}", text, "spreadsheet_row",
                {"sheet": sheet, "row": row_number, "data": data, "header_row": header_row_number},
            ))
    return records


def _pdf_page_count(path: Path) -> int | None:
    command = shutil.which("pdfinfo")
    if not command:
        return None
    result = subprocess.run([command, str(path)], capture_output=True, text=True, check=False)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def ingest_pdf(path: Path, source_path: str, category: str) -> list[Evidence]:
    command = shutil.which("pdftotext")
    if not command:
        raise RuntimeError("pdftotext is unavailable")
    result = subprocess.run([command, "-layout", str(path), "-"], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"pdftotext exited {result.returncode}")
    raw = result.stdout.decode("utf-8", errors="replace")
    pages = raw.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    expected_pages = _pdf_page_count(path)
    if expected_pages and len(pages) < expected_pages:
        pages.extend([""] * (expected_pages - len(pages)))
    entity = _entity_for_source(source_path, raw)
    architecture_heavy = "diagram" in path.name.casefold() or "architecture" in path.name.casefold()
    records = []
    for page_number, page in enumerate(pages, 1):
        text = page.strip()
        records.append(_record(
            source_path, category, entity, f"page {page_number}", text, "pdf_page",
            {
                "page": page_number,
                "text_extraction": "extracted" if text else "no_extractable_text",
                "visual_interpretation": "unsupported/partial" if architecture_heavy or not text else "not_required_for_extracted_text",
            },
        ))
    return records


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG signature")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def ingest_png(path: Path, source_path: str, category: str) -> list[Evidence]:
    width, height = _png_dimensions(path)
    entity = _entity_for_source(source_path, "")
    return [_record(
        source_path, category, entity, "image file", "", "visual_metadata",
        {
            "media_type": "image/png",
            "width": width,
            "height": height,
            "visual_evidence": True,
            "text_extraction": "not_attempted",
            "visual_interpretation": "unsupported/partial",
            "description": None,
        },
    )]


INGESTERS = {".docx": ingest_docx, ".xlsx": ingest_xlsx, ".pdf": ingest_pdf, ".png": ingest_png}


def ingest_all() -> tuple[list[Evidence], dict[str, Any]]:
    manifest = build_manifest()
    records: list[Evidence] = []
    files: list[dict[str, Any]] = []
    for entry in manifest:
        source_path = str(entry["path"])
        suffix = str(entry["extension"])
        if suffix not in SUPPORTED:
            files.append({"source_path": source_path, "status": "unsupported", "records": 0, "error": f"unsupported extension {suffix}"})
            continue
        try:
            extracted = INGESTERS[suffix](ROOT / source_path, source_path, str(entry["source_category"]))
            if not extracted:
                raise ValueError("parser produced no evidence records")
            records.extend(extracted)
            files.append({"source_path": source_path, "status": "ingested", "records": len(extracted), "error": None})
            LOGGER.info("Ingested %s (%d records)", source_path, len(extracted))
        except (OSError, ValueError, RuntimeError, KeyError, ET.ParseError, BadZipFile) as exc:
            LOGGER.exception("Failed to ingest %s", source_path)
            files.append({"source_path": source_path, "status": "failed", "records": 0, "error": f"{type(exc).__name__}: {exc}"})
    duplicate_ids = [item for item, count in Counter(record.id for record in records).items() if count > 1]
    if duplicate_ids:
        raise ValueError(f"duplicate evidence IDs: {duplicate_ids[:3]}")
    report = {
        "schema_version": 1,
        "source_file_count": len(manifest),
        "ingested_file_count": sum(row["status"] == "ingested" for row in files),
        "failed_file_count": sum(row["status"] == "failed" for row in files),
        "unsupported_file_count": sum(row["status"] == "unsupported" for row in files),
        "evidence_record_count": len(records),
        "records_by_format": dict(sorted(Counter(Path(record.source_path).suffix.casefold() for record in records).items())),
        "records_by_entity": dict(sorted(Counter(record.organization for record in records).items())),
        "files": files,
    }
    return records, report


def write_repository(records: Iterable[Evidence], report: dict[str, Any]) -> None:
    ARTIFACT_DIR.mkdir(exist_ok=True)
    with (ARTIFACT_DIR / "evidence.jsonl").open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
    (ARTIFACT_DIR / "ingestion_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_repository(path: Path | None = None) -> list[Evidence]:
    repository = path or EVIDENCE_PATH
    records = []
    with repository.open(encoding="utf-8") as lines:
        for line_number, line in enumerate(lines, 1):
            if line.strip():
                try:
                    records.append(Evidence(**json.loads(line)))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid evidence record at line {line_number}: {exc}") from exc
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="ingest", choices=("ingest", "summary"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.command == "ingest":
        records, report = ingest_all()
        write_repository(records, report)
        print(json.dumps({key: value for key, value in report.items() if key != "files"}, indent=2, sort_keys=True))
        if report["failed_file_count"] or report["unsupported_file_count"]:
            raise SystemExit(1)
    else:
        records = load_repository()
        print(f"Loaded {len(records)} evidence records from {ARTIFACT_DIR / 'evidence.jsonl'}")
        for source, count in sorted(Counter(item.source_name for item in records).items()):
            print(f"{count:5d}  {source}")


if __name__ == "__main__":
    main()
