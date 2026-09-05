#!/usr/bin/env python3
"""Re-apply the ElevenLabs voice integration to a fresh regodit checkout.

Run this EVERY TIME you replace the regodit folder with a newer version.
It is idempotent - running it twice is harmless.

    python3 voice_patch/apply_voice_patch.py

Adds to src/regodit/ui/app.py:
  * AppService.match_question / voice_ask / voice_record
  * POST /api/voice-ask and /api/voice-record
  * CORS headers + OPTIONS preflight (ElevenLabs calls from its cloud)
  * the <elevenlabs-convai> widget on the page
"""
import os, sys, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Works whether voice_patch/ sits NEXT TO the checkout (HackathonNYC/regodit) or
# INSIDE it (regobot-main/voice_patch). The first existing path wins.
CANDIDATES = (
    ROOT / "regodit" / "src" / "regodit" / "ui" / "app.py",
    ROOT / "src" / "regodit" / "ui" / "app.py",
)
TARGET = next((path for path in CANDIDATES if path.exists()), CANDIDATES[0])
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip() or "agent_1601m1s5w1r1e2zvq2zzp5ez0w26"

VOICE_METHODS = '''    # ------------------------------------------------------------------
    # Voice channel (ElevenLabs ConvAI server tool)
    # ------------------------------------------------------------------
    # A voice agent produces free text, not a question ID. We match the spoken
    # question to the closest questionnaire item and run the SAME investigation
    # the web UI runs, so spoken answers carry the same evidence guarantee.
    # If nothing matches well enough we say so rather than let the voice model
    # improvise - the golden rule applies on every channel.
    VOICE_STOPWORDS = frozenset({
        "does", "your", "organization", "organisation", "have", "the", "and", "you",
        "for", "are", "any", "with", "that", "this", "from", "what", "how", "who",
        "when", "where", "please", "provide", "describe", "list", "yes", "out",
        "there", "been", "will", "our", "its", "can", "than", "each", "such", "use",
        "performed", "perform", "process", "often", "conduct", "conducted", "place",
        "used", "level", "based", "within", "other", "must", "need", "ensure",
        "include", "including", "relevant", "appropriate", "least", "regarding",
        "organizations", "following", "provided", "available",
    })
    VOICE_ALIASES = {
        "mfa": "mfa", "2fa": "mfa", "multifactor": "mfa", "multi": "mfa",
        "factor": "mfa", "twofactor": "mfa", "otp": "mfa",
        "authenticate": "authentication", "authenticator": "authentication",
        "encrypt": "encryption", "encrypted": "encryption", "cryptography": "encryption",
        "backup": "backup", "backed": "backup", "restore": "backup", "recovery": "backup",
        "pentest": "penetration", "pentesting": "penetration",
        "vuln": "vulnerability", "vulnerabilities": "vulnerability", "scanning": "vulnerability",
        "offboard": "termination", "offboarding": "termination", "terminate": "termination",
        "onboard": "onboarding", "leaver": "termination",
        "prod": "production", "breach": "incident", "incidents": "incident",
        "vendor": "thirdparty", "supplier": "thirdparty", "subprocessor": "thirdparty",
        "background": "screening", "screen": "screening",
        "policies": "policy", "controls": "control", "employees": "employee",
        "staff": "employee", "personnel": "employee", "everyone": "employee",
    }
    VOICE_MIN_SCORE = 0.30
    VOICE_MIN_OVERLAP = 2
    VOICE_SOLO_SCORE = 0.60

    @classmethod
    def _stem(cls, word: str) -> str:
        word = word.replace("-", "")
        word = cls.VOICE_ALIASES.get(word, word)
        for suffix in ("ations", "ation", "ing", "ies", "ed", "es", "s"):
            if len(word) > 5 and word.endswith(suffix):
                base = word[: -len(suffix)]
                if suffix == "ies":
                    base += "y"
                return cls.VOICE_ALIASES.get(base, base)
        return word

    @classmethod
    def _voice_tokens(cls, text: str) -> set[str]:
        words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", text.casefold())
        return {cls._stem(w) for w in words
                if w not in cls.VOICE_STOPWORDS and len(w) > 2}

    def match_question(self, spoken: str) -> tuple[QuestionnaireItem | None, float]:
        asked = self._voice_tokens(spoken)
        if not asked:
            return None, 0.0
        best, best_score = None, 0.0
        for item in self.items:
            target = self._voice_tokens(f"{item.category} {item.question}")
            if not target:
                continue
            overlap = asked & target
            if not overlap:
                continue
            score = (len(overlap) / len(asked)) * 0.7 + (len(overlap) / len(target)) * 0.3
            if len(overlap) < self.VOICE_MIN_OVERLAP and score < self.VOICE_SOLO_SCORE:
                continue
            if score > best_score:
                best, best_score = item, score
        if best is None or best_score < self.VOICE_MIN_SCORE:
            return None, round(best_score, 3)
        return best, round(best_score, 3)

    def voice_ask(self, spoken: str, stakeholder: str | None = None) -> dict[str, Any]:
        spoken = (spoken or "").strip()
        if not spoken:
            raise ValueError("question is required")
        item, score = self.match_question(spoken)
        if item is None:
            return {
                "spoken_answer": "That is not something the questionnaire covers, and I could "
                                 "not find it in the company evidence. Could you rephrase it, or "
                                 "tell me the answer and I will record it?",
                "status": "UNKNOWN", "matched_question": None, "question_id": None,
                "match_score": score, "confidence": 0.0, "sources": [], "follow_up": None,
            }
        result = self.investigate(item.id)
        sources = [self.evidence_by_id[e].source_path for e in result.evidence_ids
                   if e in self.evidence_by_id]
        unique_sources = list(dict.fromkeys(sources))
        if result.status == "CONFLICT" or result.next_action == "RESOLVE_CONFLICT":
            detail = result.conflicts[0].description if result.conflicts else ""
            spoken_answer = (f"The company records disagree on this. {detail} "
                             f"{result.follow_up_question or ''}").strip()
        elif result.next_action == "ASK_FOLLOW_UP":
            spoken_answer = (f"The documents do not go far enough. "
                             f"{result.follow_up_question}").strip()
        elif result.answer:
            body = result.answer.rstrip(" .")
            if unique_sources:
                spoken_answer = f"{body}, according to {PurePosixPath(unique_sources[0]).name}."
            else:
                spoken_answer = f"{body}."
        else:
            spoken_answer = ("I could not verify that in the company evidence, so I am "
                             "marking it unknown rather than guessing.")
        return {
            "spoken_answer": spoken_answer,
            "status": result.status,
            "question_id": result.question_id,
            "matched_question": item.question,
            "match_score": score,
            "confidence": result.confidence,
            "sources": unique_sources[:4],
            "follow_up": result.follow_up_question,
        }

    def voice_record(self, question_id: str, response: str, stakeholder: str | None = None) -> dict[str, Any]:
        result = self.submit_follow_up(question_id, response, stakeholder)
        # A spoken confirmation is the same durable fact as a typed one, so it must fan out
        # through the central synchronizer. Without this the voice channel would update one
        # row while chat updated every row mapped to the control - exactly the drift the
        # questionnaire synchronization phase exists to prevent.
        affected: list[str] = []
        if result.status in {"USER_CONFIRMED", "VERIFIED"} and hasattr(self, "synchronizer"):
            control = normalize_control(self._item(question_id).normalized_control)
            changes = self.synchronizer.synchronize_control(control, "voice confirmation")
            affected = [change.question_id for change in changes]
        spoken = ("Recorded. I will not ask that again."
                  if result.status in {"USER_CONFIRMED", "VERIFIED"}
                  else f"I still need something more specific. {result.follow_up_question or ''}".strip())
        if len(affected) > 1:
            spoken = f"Recorded. That also answered {len(affected) - 1} related question" \
                     f"{'s' if len(affected) > 2 else ''}. I will not ask those again."
        return {
            "spoken_answer": spoken,
            "status": result.status,
            "question_id": result.question_id,
            "affected_questions": affected,
        }

'''

CORS = '''    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

'''

# The widget is fixed to the bottom-right, which is exactly where the chat composer's
# Send button lives. Reserve room for it instead of letting the two overlap on screen.
WIDGET_TAGS = (
    '\n<style>'
    'elevenlabs-convai{position:fixed;right:16px;bottom:16px;z-index:60}'
    '.composer{padding-bottom:150px}'
    '.workspace{padding-bottom:160px}'
    '@media(max-width:720px){.composer{padding-bottom:140px}}'
    '</style>\n'
    '<elevenlabs-convai agent-id="' + AGENT_ID + '"></elevenlabs-convai>\n'
    '<script src="https://unpkg.com/@elevenlabs/convai-widget-embed" async '
    'type="text/javascript"></script>\n'
)


def insert_widget(src: str) -> tuple[str, bool]:
    """Put the widget in the template that is ACTUALLY SERVED.

    Newer builds keep an unused LEGACY_HTML next to the live INDEX_HTML. A naive
    first-match replace lands in the dead one: the page then renders with no
    widget and nothing errors, which is the worst kind of bug.
    """
    if "elevenlabs-convai" in src:
        return src, False
    triple = chr(39) * 3
    start = src.find("INDEX_HTML = ")
    if start == -1:
        end_tag = src.rfind("</body>")
        if end_tag == -1:
            raise LookupError("no INDEX_HTML and no </body>")
        return src[:end_tag] + WIDGET_TAGS + src[end_tag:], True
    quote_open = src.find(triple, start)
    if quote_open == -1:
        raise LookupError("INDEX_HTML is not a triple-quoted literal")
    quote_close = src.find(triple, quote_open + 3)
    literal = src[quote_open:quote_close]
    at = literal.rfind("</body>")
    if at == -1:
        raise LookupError("no </body> inside INDEX_HTML")
    literal = literal[:at] + WIDGET_TAGS + literal[at:]
    return src[:quote_open] + literal + src[quote_close:], True


EDITS = [
    ("voice methods",
     "    def submit_follow_up(self, question_id: str, response: str, stakeholder: str | None) -> InvestigationResult:",
     VOICE_METHODS + "    def submit_follow_up(self, question_id: str, response: str, stakeholder: str | None) -> InvestigationResult:",
     "def voice_ask("),
    ("routes",
     '''            elif self.path == "/api/export":
                payload = self.service.export()''',
     '''            elif self.path == "/api/export":
                payload = self.service.export()
            elif self.path == "/api/voice-ask":
                payload = self.service.voice_ask(body.get("question", ""), body.get("stakeholder"))
            elif self.path == "/api/voice-record":
                payload = self.service.voice_record(body["question_id"], body["response"], body.get("stakeholder"))''',
     "/api/voice-ask"),
    ("cors on json",
     '''        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()''',
     '''        self.send_header("Content-Length", str(len(encoded)))
        self._cors()
        self.end_headers()''',
     'str(len(encoded)))\n        self._cors()'),
    ("cors handler",
     "    def do_POST(self) -> None:  # noqa: N802",
     CORS + "    def do_POST(self) -> None:  # noqa: N802",
     "def do_OPTIONS("),
]


def main() -> int:
    if not TARGET.exists():
        print("ERROR: could not find src/regodit/ui/app.py. Looked in:")
        for path in CANDIDATES:
            print(f"       {path}")
        return 1
    print(f"target  : {TARGET}")
    print(f"agent   : {AGENT_ID}")
    src = TARGET.read_text()
    backup = TARGET.with_suffix(".py.pre-voice")
    if not backup.exists():
        shutil.copy(TARGET, backup)

    applied, skipped = [], []
    for name, anchor, replacement, marker in EDITS:
        if marker in src:
            skipped.append(name)
            continue
        if anchor not in src:
            print(f"ERROR: anchor for '{name}' not found. The upstream file changed too much.")
            print(f"       Compare against voice_patch/app.patched.reference.py and merge by hand.")
            return 1
        src = src.replace(anchor, replacement, 1)
        applied.append(name)

    try:
        src, widget_added = insert_widget(src)
    except LookupError as exc:
        print(f"ERROR: could not place the widget ({exc}).")
        print("       Merge it by hand from voice_patch/app.patched.reference.py")
        return 1
    (applied if widget_added else skipped).append("widget")

    import ast
    try:
        ast.parse(src)
    except SyntaxError as exc:
        print(f"ERROR: patched file would not parse ({exc}). Nothing written.")
        return 1

    TARGET.write_text(src)
    print(f"applied : {', '.join(applied) or 'nothing (already patched)'}")
    if skipped:
        print(f"skipped : {', '.join(skipped)} (already present)")
    print(f"backup  : {backup.name}")
    print("\nRestart the server:  PYTHONPATH=src python3 -m regodit serve --port 8501")
    return 0


if __name__ == "__main__":
    sys.exit(main())
