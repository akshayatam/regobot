import json
import unittest
from collections import defaultdict
from pathlib import Path

from regodit.questionnaire import ARTIFACT_DIR, DATA_DIR
from regodit.ingestion import ingest_all, load_repository, write_repository


class IngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_hashes = {p: p.read_bytes() for p in DATA_DIR.rglob("*") if p.is_file()}
        cls.records, cls.report = ingest_all()
        write_repository(cls.records, cls.report)
        cls.by_source = defaultdict(list)
        for record in cls.records:
            cls.by_source[record.source_name].append(record)

    def test_every_supported_file_is_ingested_or_reported(self):
        sources = [p for p in DATA_DIR.rglob("*") if p.is_file()]
        self.assertEqual(self.report["source_file_count"], len(sources))
        self.assertEqual(len(self.report["files"]), len(sources))
        self.assertEqual(self.report["failed_file_count"], 0)
        self.assertEqual(self.report["unsupported_file_count"], 0)
        self.assertEqual(self.report["ingested_file_count"], 26)

    def test_provenance_and_stable_unique_ids(self):
        self.assertEqual(len({r.id for r in self.records}), len(self.records))
        for record in self.records:
            self.assertTrue(record.source_name)
            self.assertTrue(record.source_path.startswith("data/"))
            self.assertTrue(record.location)
            self.assertIn(record.organization, {"Regodit", "Solsphere", "unknown", "multiple"})
            self.assertIn("record_kind", record.metadata)
        second, _ = ingest_all()
        self.assertEqual([r.id for r in self.records], [r.id for r in second])

    def test_spreadsheet_rows_retain_semantics(self):
        for source in ("Access_Review_Records.xlsx", "Asset_Inventory_Regodit.xlsx"):
            rows = self.by_source[source]
            self.assertTrue(rows)
            self.assertTrue(all(r.metadata["record_kind"] == "spreadsheet_row" for r in rows))
            self.assertTrue(all("sheet" in r.metadata and "row" in r.metadata and isinstance(r.metadata["data"], dict) for r in rows))

    def test_docx_paragraphs_sections_and_tables_are_represented(self):
        docx = [r for r in self.records if r.source_path.endswith(".docx")]
        self.assertTrue(any(r.metadata["record_kind"] == "paragraph" for r in docx))
        self.assertTrue(any(r.metadata["record_kind"] == "heading" for r in docx))
        self.assertTrue(any(r.metadata["record_kind"] == "table_row" for r in docx))
        self.assertTrue(all("section_heading" in r.metadata for r in docx))

    def test_pdf_pages_and_visual_limitations_are_explicit(self):
        architecture = self.by_source["network_architecture_diagrams.pdf"]
        w9 = self.by_source["Solsphere W-9.pdf"]
        self.assertEqual(len(architecture), 2)
        self.assertEqual(len(w9), 6)
        self.assertTrue(all("page" in r.metadata for r in architecture + w9))
        self.assertTrue(all(r.metadata["visual_interpretation"] == "unsupported/partial" for r in architecture))
        self.assertTrue(all(r.metadata["text_extraction"] == "no_extractable_text" for r in w9))

    def test_png_is_metadata_only_without_generated_description(self):
        png = [r for r in self.records if r.source_path.endswith(".png")]
        self.assertEqual(len(png), 2)
        self.assertTrue(all(r.text == "" for r in png))
        self.assertTrue(all(r.metadata["visual_evidence"] is True for r in png))
        self.assertTrue(all(r.metadata["description"] is None for r in png))

    def test_ambiguous_entities_and_solsphere_are_explicit(self):
        self.assertTrue(all(r.organization == "Solsphere" for r in self.by_source["BCP_DR_Plan_Solsphere.docx"]))
        self.assertTrue(all(r.organization == "Solsphere" for r in self.by_source["Solsphere W-9.pdf"]))
        self.assertTrue(any(r.organization in {"unknown", "multiple"} for r in self.records))

    def test_repository_round_trip_and_sources_unchanged(self):
        loaded = load_repository()
        self.assertEqual(self.records, loaded)
        json.loads((ARTIFACT_DIR / "ingestion_report.json").read_text())
        for path, original in self.source_hashes.items():
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
