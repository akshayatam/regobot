"""Audit source data and parse the vendor questionnaire into a work queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Iterator
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from regodit.config import ARTIFACT_DIR, DATA_DIR, PROJECT_ROOT
from regodit.models import QuestionnaireItem

ROOT = PROJECT_ROOT
QUESTIONNAIRE_NAME = "Regodit_Comprehensive_Vendor_Security_Questionnaire_Clean.xlsx"
QUESTIONNAIRE_SHEET = "Vendor Security Responses"
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

PARSER_BY_EXTENSION = {
    ".docx": "python-docx",
    ".xlsx": "openpyxl",
    ".pdf": "PDF text parser with OCR fallback",
    ".png": "image/OCR or vision parser",
}

TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("MFA", ("multi-factor", "multifactor", "mfa", "otp", "replay-resistant", "external authenticators")),
    ("single sign-on", ("single sign-on", "sso")),
    ("privileged access", ("privileged access", "administrator access", "admin access")),
    ("access reviews", ("access review", "review user access", "review user access privileges")),
    ("least privilege", ("least privilege",)),
    ("access control", ("identity and access", "role-based access", "access controls", "authorized personnel")),
    ("encryption at rest", ("data-at-rest", "encryption at rest")),
    ("encryption in transit", ("data-in-transit", "encryption in transit", "ssl/tls")),
    ("backups", ("backup",)),
    ("disaster recovery", ("disaster recovery", "bc/dr")),
    ("incident response", ("incident response", "security event", "incident management")),
    ("vulnerability management", ("vulnerability scan", "security vulnerabilities")),
    ("patching", ("patch", "remediation timeline")),
    ("employee onboarding", ("onboarding", "new hire")),
    ("background checks", ("background check",)),
    ("security training", ("security awareness", "security training", "employees trained", "role based security")),
    ("asset inventory", ("inventory of information technology", "track assets", "critical assets")),
    ("vendor risk", ("third-party risk", "third party risk", "third-party providers", "subcontract", "sub-contract", "supply chain")),
    ("secure development", ("secure development", "secure coding")),
    ("logging", ("record of security events", "detailed logging")),
    ("monitoring", ("monitor the security", "monitoring")),
    ("data retention", ("data retention",)),
    ("data deletion", ("secure disposal", "data disposal", "deletion")),
    ("data location", ("store sensitive information", "where", "stored on site", "data center")),
    ("privacy", ("privacy", "sensitive data", "pii", "phi")),
    ("physical security", ("physical security", "physical access", "visitor management", "physical safeguards")),
    ("network security", ("network architecture", "accessing client xyz's network", "firewall", "dns security", "wireless network", "anti virus", "antivirus")),
    ("penetration testing", ("penetration test",)),
    ("risk assessment", ("risk assessment",)),
    ("information security governance", ("information security program", "information security polic", "cybersecurity and data protection controls", "role descriptions")),
    ("application inventory", ("name of your web application", "function/purpose of your web application", "using a web application")),
    ("data flow", ("data flow diagram",)),
]


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _xlsx_rows(path: Path, wanted_sheet: str) -> Iterator[tuple[int, dict[str, str]]]:
    """Read strings from an OOXML worksheet using only the Python standard library."""
    with ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t")) for item in root]

        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        target = None
        for sheet in workbook.findall(f".//{{{NS_MAIN}}}sheet"):
            if sheet.attrib["name"] == wanted_sheet:
                target = targets[sheet.attrib[f"{{{NS_REL}}}id"]]
                break
        if target is None:
            raise ValueError(f"worksheet not found: {wanted_sheet}")
        sheet_path = str(PurePosixPath("xl") / target)
        root = ET.fromstring(archive.read(sheet_path))
        for row in root.findall(f".//{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row"):
            values: dict[str, str] = {}
            for cell in row.findall(f"{{{NS_MAIN}}}c"):
                column = re.match(r"[A-Z]+", cell.attrib["r"])
                if not column:
                    continue
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{NS_MAIN}}}v")
                if cell_type == "inlineStr":
                    value = "".join(n.text or "" for n in cell.iter(f"{{{NS_MAIN}}}t"))
                elif value_node is None:
                    value = ""
                elif cell_type == "s":
                    value = shared[int(value_node.text or "0")]
                else:
                    value = value_node.text or ""
                values[column.group()] = value
            yield int(row.attrib["r"]), values


def normalize_topic(question: str) -> str:
    lowered = question.casefold()
    for topic, phrases in TOPIC_RULES:
        if any(phrase in lowered for phrase in phrases):
            return topic
    return "unclassified"


def parse_questionnaire(path: Path) -> list[QuestionnaireItem]:
    items: list[QuestionnaireItem] = []
    category: str | None = None
    seen_ids: set[str] = set()
    for row_number, cells in _xlsx_rows(path, QUESTIONNAIRE_SHEET):
        source_id = _text_or_none(cells.get("A"))
        question = _text_or_none(cells.get("B"))
        if source_id == "Topic":
            category = question
            continue
        if not source_id or not re.fullmatch(r"\d+(?:\.\d+)?", source_id) or not question:
            continue
        if category is None:
            raise ValueError(f"question row {row_number} has no preceding category")
        stable_id = f"VSQ-{int(float(source_id)):03d}"
        if stable_id in seen_ids:
            raise ValueError(f"duplicate stable questionnaire ID: {stable_id}")
        seen_ids.add(stable_id)
        answer = _text_or_none(cells.get("C"))
        comments = _text_or_none(cells.get("D"))
        evidence = _text_or_none(cells.get("E"))
        items.append(QuestionnaireItem(
            id=stable_id,
            source_question_id=source_id,
            category=category,
            question=question,
            normalized_control=normalize_topic(question),
            source_file=path.relative_to(ROOT).as_posix(),
            source_sheet=QUESTIONNAIRE_SHEET,
            source_row=row_number,
            source_cell=f"B{row_number}",
            answer=answer,
            comments=comments,
            evidence_reference=evidence,
            missing_fields=[] if answer is not None else ["answer"],
        ))
    if not items:
        raise ValueError("no questionnaire items were parsed")
    return items


def _source_category(path: Path) -> str:
    text = path.as_posix().casefold()
    if "questionnaire" in text:
        return "questionnaire"
    if "company policies" in text or "policy" in path.name.casefold():
        return "policy"
    if "assessment" in text or "vapt" in text or "soc2" in text:
        return "assessment"
    if "contracts" in text or "contract" in text or "agreement" in text:
        return "contract"
    if path.suffix.casefold() == ".png" or "diagram" in path.name.casefold():
        return "diagram"
    if "infrastructure" in text:
        return "operational/infrastructure"
    return "other"


def _entity(path: Path) -> tuple[str, bool, str | None]:
    name = path.name.casefold()
    if "solsphere" in name:
        return "Solsphere", True, "Filename explicitly refers to Solsphere; do not use as Regodit evidence without relevance validation."
    if "regodit" in name:
        return "Regodit", False, None
    return "unknown", True, "Entity is not established by the filename; validate document contents before using as Regodit evidence."


def build_manifest() -> list[dict[str, object]]:
    manifest = []
    for path in sorted(p for p in DATA_DIR.rglob("*") if p.is_file()):
        entity, ambiguous, note = _entity(path)
        manifest.append({
            "path": path.relative_to(ROOT).as_posix(),
            "extension": path.suffix.casefold(),
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_category": _source_category(path),
            "likely_organization": entity,
            "entity_ambiguous_or_potentially_irrelevant": ambiguous,
            "review_note": note,
            "parser_required": PARSER_BY_EXTENSION.get(path.suffix.casefold(), "manual/unknown parser"),
        })
    return manifest


def _audit_report(manifest: list[dict[str, object]], items: list[QuestionnaireItem]) -> str:
    extensions = Counter(str(row["extension"]) for row in manifest)
    sources = Counter(str(row["source_category"]) for row in manifest)
    categories = Counter(item.category for item in items)
    topics = Counter(item.normalized_control for item in items)
    ambiguous = [row for row in manifest if row["entity_ambiguous_or_potentially_irrelevant"]]
    solsphere = [row for row in manifest if row["likely_organization"] == "Solsphere"]

    def bullets(counter: Counter[str]) -> str:
        return "\n".join(f"- `{key}`: {value}" for key, value in sorted(counter.items()))

    ambiguous_lines = "\n".join(
        f"- `{row['path']}` — likely entity: **{row['likely_organization']}**. {row['review_note']}"
        for row in ambiguous
    )
    return f"""# Dataset Audit

Generated deterministically by `python -m regodit initialize`. No source file under `data/` was modified.

## Questionnaire summary

- Parsed questions: **{len(items)}**
- Source sheet: `{QUESTIONNAIRE_SHEET}`
- Existing populated answer fields: **{sum(item.answer is not None for item in items)}**
- Stable IDs: `VSQ-001` through `VSQ-{len(items):03d}`

### Questionnaire categories

{bullets(categories)}

### Initial normalized control/topic distribution

{bullets(topics)}

`unclassified` is intentional where the wording does not support a reliable mapping. In particular, source row 67 (ID 52.0) contains only `52.0` as its question text and is retained verbatim rather than reconstructed.

## Dataset inventory

- Total source files: **{len(manifest)}**

### File counts by extension

{bullets(extensions)}

### File counts by source category

{bullets(sources)}

## Ambiguous files and entities

Two files explicitly refer to Solsphere and are isolated from Regodit evidence pending a relevance determination:

{chr(10).join(f"- `{row['path']}`" for row in solsphere)}

All files whose entity cannot be established safely from the filename are also flagged in the manifest:

{ambiguous_lines}

## Ingestion risks

- The questionnaire has formulas, hidden administrative sheets, drawings, validations, and formatting. Regodit parses only real numbered questions from the visible `Vendor Security Responses` sheet.
- Question 52.0 has no substantive wording in the source workbook. It remains `unclassified` and requires source-owner clarification.
- DOCX files may contain tables, headers, images, or embedded objects that a paragraph-only parser can miss.
- PDF files may contain scanned pages and require OCR fallback; diagrams require visual interpretation rather than text extraction alone.
- Generic filenames do not establish an entity. Their contents must be checked before any claim is attributed to Regodit.
- Policy documents establish requirements, not operational implementation. Later ingestion must retain evidence type.
- File hashes in `data_manifest.json` support provenance and detection of later source changes.
"""


def generate() -> tuple[list[dict[str, object]], list[QuestionnaireItem]]:
    questionnaire = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
    manifest = build_manifest()
    items = parse_questionnaire(questionnaire)
    ARTIFACT_DIR.mkdir(exist_ok=True)
    (ARTIFACT_DIR / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (ARTIFACT_DIR / "questionnaire_items.json").write_text(
        json.dumps([item.to_dict() for item in items], indent=2) + "\n", encoding="utf-8"
    )
    (ARTIFACT_DIR / "dataset_audit.md").write_text(_audit_report(manifest, items), encoding="utf-8")
    return manifest, items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "list"), nargs="?", default="generate")
    args = parser.parse_args()
    if args.command == "generate":
        manifest, items = generate()
        print(f"Generated audit artifacts for {len(manifest)} source files and {len(items)} questions.")
    else:
        questionnaire = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
        for item in parse_questionnaire(questionnaire):
            print(f"{item.id}\t{item.category}\t{item.normalized_control}\t{item.question}")


if __name__ == "__main__":
    main()
