import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from zipfile import ZipFile

from regodit.ui.app import AppService, INDEX_HTML, make_server


class UiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = AppService(root / "profile.sqlite3", root / "exports")
        self.original_hash = hashlib.sha256(self.service.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_ui_explains_views_statuses_evidence_conflicts_and_corrections(self):
        for text in ("Investigation", "Questionnaire work queue", "Security profile", "Verified from company evidence",
                     "Confirmed by user", "Unknown / needs confirmation", "Conflict", "Correct this fact", "Export XLSX"):
            self.assertIn(text, INDEX_HTML)

    def test_dynamic_progress_and_visible_evidence(self):
        result = self.service.investigate("VSQ-020")
        dashboard = self.service.dashboard()
        self.assertEqual(dashboard["progress"]["total"], 66)
        self.assertEqual(dashboard["progress"]["verified"], 1)
        row = next(row for row in dashboard["questions"] if row["id"] == "VSQ-020")
        self.assertEqual(row["status_label"], "Verified from company evidence")
        self.assertTrue(row["evidence"])
        self.assertTrue(all(entry["source_name"] and entry["location"] and entry["text"] for entry in row["evidence"]))
        self.assertEqual(set(result.evidence_ids), {entry["id"] for entry in row["evidence"]})

    def test_user_confirmation_and_correction_persist_distinctly(self):
        result = self.service.submit_follow_up("VSQ-019", "United States and Canada", "alice@example.com")
        self.assertEqual(result.status, "USER_CONFIRMED")
        claim = result.claims[0]
        correction = self.service.correct(claim.id, "United States only", "Data residency changed to United States only.", "alice@example.com")
        reopened = AppService(self.service.profile.path, self.service.artifact_dir)
        active = reopened.profile.lookup("data_location", "Regodit")
        self.assertEqual([item.value for item in active.claims], ["United States only"])
        history = reopened.profile.claim_history("data_location")
        self.assertEqual([entry.status for entry in history], ["SUPERSEDED", "ACTIVE"])
        self.assertEqual(correction["superseded_claim_id"], claim.id)
        self.assertTrue(reopened.dashboard()["progress"]["user_confirmed"] >= 1)

    def test_exports_machine_readable_json_and_completed_xlsx_copy(self):
        self.service.investigate("VSQ-020")
        paths = self.service.export()
        payload = json.loads(Path(paths["json"]).read_text())
        self.assertEqual(payload["progress"]["total"], 66)
        with ZipFile(paths["xlsx"]) as workbook:
            self.assertIn("xl/worksheets/sheet3.xml", workbook.namelist())
            xml = workbook.read("xl/worksheets/sheet3.xml").decode()
            self.assertIn("Verified from company evidence", xml)
            self.assertIn("ev-", xml)
        self.assertEqual(hashlib.sha256(self.service.source.read_bytes()).hexdigest(), self.original_hash)
        self.assertNotEqual(Path(paths["xlsx"]).resolve(), self.service.source.resolve())

    def test_http_dashboard_and_error_paths(self):
        try:
            server = make_server(self.service, port=0)
        except PermissionError:
            self.skipTest("execution sandbox denies local socket creation")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
            dashboard = json.loads(urllib.request.urlopen(base + "/api/dashboard", timeout=5).read())
            self.assertIn("Regodit AI Security Analyst", html)
            self.assertEqual(dashboard["progress"]["total"], 66)
            request = urllib.request.Request(base + "/api/investigate", json.dumps({"question_id": "bad"}).encode(), {"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(context.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
