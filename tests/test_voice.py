"""Voice channel (ElevenLabs ConvAI server tool) coverage.

The voice integration is applied to `ui/app.py` by `voice_patch/apply_voice_patch.py`,
because replacing the regodit folder wipes it. These tests skip cleanly on an unpatched
checkout so the suite stays green either way, and fail loudly once the patch is applied.
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from regodit.ui.app import AppService, INDEX_HTML, make_server

PATCHED = hasattr(AppService, "voice_ask")


@unittest.skipUnless(PATCHED, "voice patch not applied; run voice_patch/apply_voice_patch.py")
class VoiceChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.service = AppService(root / "profile.sqlite3", root / "exports")

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.temp.cleanup()

    def test_widget_is_embedded_in_the_served_page_not_the_legacy_template(self):
        from regodit.ui.app import LEGACY_HTML

        self.assertIn("elevenlabs-convai", INDEX_HTML)
        self.assertNotIn("elevenlabs-convai", LEGACY_HTML)

    def test_spoken_question_matches_the_right_questionnaire_item(self):
        item, score = self.service.match_question("Do you require multi factor authentication?")
        self.assertIsNotNone(item)
        self.assertGreater(score, self.service.VOICE_MIN_SCORE)
        self.assertIn("authentication", item.question.casefold())

    def test_off_questionnaire_question_is_refused_rather_than_improvised(self):
        result = self.service.voice_ask("What is the capital of France?")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["question_id"])
        self.assertIsNone(result["matched_question"])
        self.assertIn("not something the questionnaire covers", result["spoken_answer"])

    def test_blank_question_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.voice_ask("   ")

    def test_verified_answer_is_spoken_with_its_source(self):
        result = self.service.voice_ask("Do you require data at rest encryption for sensitive data?")
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["sources"])
        self.assertIn("according to", result["spoken_answer"])
        self.assertTrue(result["spoken_answer"].startswith("Yes"))

    def test_unverifiable_question_asks_a_follow_up_instead_of_guessing(self):
        result = self.service.voice_ask("Where do you store customer data?")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["follow_up"])
        self.assertIn("?", result["spoken_answer"])

    def test_spoken_answer_is_recorded_and_not_asked_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AppService(root / "profile.sqlite3", root / "exports")
            try:
                asked = service.voice_ask("Where do you store customer data?")
                self.assertEqual(asked["status"], "UNKNOWN")
                recorded = service.voice_record(
                    asked["question_id"], "Customer data is stored in the United States.", "voice@example.com")
                self.assertEqual(recorded["status"], "USER_CONFIRMED")
                repeated = service.voice_ask("Where do you store customer data?")
                self.assertEqual(repeated["status"], "USER_CONFIRMED")
                self.assertIn("United States", repeated["spoken_answer"])
            finally:
                service.close()

    def test_one_spoken_confirmation_updates_every_mapped_questionnaire_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AppService(root / "profile.sqlite3", root / "exports")
            try:
                asked = service.voice_ask("How frequently are employees trained on security?")
                recorded = service.voice_record(
                    asked["question_id"], "Employees are trained annually.", "voice@example.com")
                self.assertEqual(recorded["status"], "USER_CONFIRMED")
                # The voice channel must fan out exactly like the chat channel.
                self.assertGreater(len(recorded["affected_questions"]), 1)
                self.assertIn(asked["question_id"], recorded["affected_questions"])
                states = service.profile.questionnaire_states()
                for question_id in recorded["affected_questions"]:
                    self.assertIn(states[question_id].status, {"VERIFIED", "USER_CONFIRMED"})
            finally:
                service.close()

    def test_spoken_confirmation_is_written_to_evaluation_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AppService(root / "profile.sqlite3", root / "exports")
            try:
                asked = service.voice_ask("Where do you store customer data?")
                service.voice_record(asked["question_id"], "Customer data is stored in the United States.", "voice@example.com")
                history = service.history(asked["question_id"])
                self.assertEqual(history["current_status"], "USER_CONFIRMED")
                self.assertGreaterEqual(len(history["evaluations"]), 2)
                self.assertEqual(history["evaluations"][0]["status"], "UNKNOWN")
                self.assertEqual(history["evaluations"][-1]["status"], "USER_CONFIRMED")
            finally:
                service.close()


@unittest.skipUnless(PATCHED, "voice patch not applied; run voice_patch/apply_voice_patch.py")
class VoiceHttpTests(unittest.TestCase):
    """ElevenLabs calls these from its cloud, so CORS and the routes must both work."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = AppService(root / "profile.sqlite3", root / "exports")
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.close()
        self.temp.cleanup()

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response), dict(response.headers)

    def test_voice_ask_route_returns_a_spoken_answer_with_cors(self):
        status, payload, headers = self._post(
            "/api/voice-ask", {"question": "Do you require data at rest encryption for sensitive data?"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(payload["status"], "VERIFIED")
        self.assertTrue(payload["spoken_answer"])

    def test_preflight_is_answered_for_the_elevenlabs_cloud(self):
        request = urllib.request.Request(self.base + "/api/voice-ask", method="OPTIONS")
        request.add_header("Origin", "https://elevenlabs.io")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn("POST", response.headers.get("Access-Control-Allow-Methods", ""))

    def test_voice_record_route_persists_a_spoken_answer(self):
        _, asked, _ = self._post("/api/voice-ask", {"question": "Where do you store customer data?"})
        status, payload, _ = self._post("/api/voice-record", {
            "question_id": asked["question_id"],
            "response": "Customer data is stored in the United States.",
            "stakeholder": "voice@example.com",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "USER_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
