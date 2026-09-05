"""Hackathon-ready local web UI for investigations and questionnaire completion."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import uuid
from collections import Counter
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from regodit.analyst import AnalystEngine, InvestigationResult, normalize_control
from regodit.config import ARTIFACT_DIR, DATA_DIR, HOST, PORT, PROFILE_DB
from regodit.ingestion import load_repository
from regodit.memory import SecurityProfile
from regodit.models import QuestionnaireItem
from regodit.questionnaire import QUESTIONNAIRE_NAME, parse_questionnaire

DEFAULT_DB = PROFILE_DB
ROOT = ARTIFACT_DIR.parent

LOGGER = logging.getLogger("regodit.ui")
M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
STATUS_LABELS = {
    "VERIFIED": "Verified from company evidence",
    "USER_CONFIRMED": "Confirmed by user",
    "UNKNOWN": "Unknown / needs confirmation",
    "CONFLICT": "Conflict",
}


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    value = 0
    for char in letters.group() if letters else "":
        value = value * 26 + ord(char) - 64
    return value


def _set_inline_cell(row: ET.Element, reference: str, value: str) -> None:
    cells = row.findall(f"{{{M}}}c")
    cell = next((item for item in cells if item.attrib.get("r") == reference), None)
    if cell is None:
        cell = ET.Element(f"{{{M}}}c", {"r": reference})
        insert_at = next((index for index, item in enumerate(cells) if _column_number(item.attrib["r"]) > _column_number(reference)), len(cells))
        row.insert(insert_at, cell)
    for child in list(cell):
        if child.tag in {f"{{{M}}}v", f"{{{M}}}f", f"{{{M}}}is"}:
            cell.remove(child)
    cell.attrib["t"] = "inlineStr"
    inline = ET.SubElement(cell, f"{{{M}}}is")
    text = ET.SubElement(inline, f"{{{M}}}t")
    text.text = value


def export_completed_xlsx(source: Path, output: Path, items: list[QuestionnaireItem], states: dict[str, Any]) -> Path:
    """Write answers to a new XLSX while preserving every other archive member."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as original:
        relationships = ET.fromstring(original.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in relationships}
        workbook = ET.fromstring(original.read("xl/workbook.xml"))
        target = None
        for sheet in workbook.findall(f".//{{{M}}}sheet"):
            if sheet.attrib["name"] == "Vendor Security Responses":
                target = str(PurePosixPath("xl") / targets[sheet.attrib[f"{{{R}}}id"]])
                break
        if target is None:
            raise ValueError("Vendor Security Responses sheet not found")
        worksheet = ET.fromstring(original.read(target))
        rows = {int(row.attrib["r"]): row for row in worksheet.findall(f".//{{{M}}}sheetData/{{{M}}}row")}
        for item in items:
            state = states.get(item.id)
            if state is None:
                continue
            row = rows[item.source_row]
            _set_inline_cell(row, f"C{item.source_row}", state.answer or "")
            _set_inline_cell(row, f"D{item.source_row}", f"{STATUS_LABELS[state.status]} | confidence {state.confidence:.2f}")
            _set_inline_cell(row, f"E{item.source_row}", ", ".join(state.evidence_ids))
        updated_sheet = ET.tostring(worksheet, encoding="utf-8", xml_declaration=True)
        with ZipFile(output, "w", ZIP_DEFLATED) as completed:
            for info in original.infolist():
                completed.writestr(info, updated_sheet if info.filename == target else original.read(info.filename))
    return output


class AppService:
    def __init__(self, db_path: Path | str = DEFAULT_DB, artifact_dir: Path = ARTIFACT_DIR, session_id: str | None = None):
        source = next(DATA_DIR.rglob(QUESTIONNAIRE_NAME))
        self.source = source
        self.items = parse_questionnaire(source)
        self.items_by_id = {item.id: item for item in self.items}
        self.profile = SecurityProfile(db_path)
        self.session_id = session_id or str(uuid.uuid4())
        self.engine = AnalystEngine(profile=self.profile, top_k=24, session_id=self.session_id)
        self.artifact_dir = artifact_dir
        self.repository = load_repository()
        self.evidence_by_id = {record.id: record for record in self.repository}

    def investigate(self, question_id: str) -> InvestigationResult:
        item = self._item(question_id)
        result = self.engine.investigate(item)
        self.profile.save_questionnaire_result(result)
        return result

    def analyze_all(self) -> dict[str, Any]:
        for item in self.items:
            result = self.engine.investigate(item)
            self.profile.save_questionnaire_result(result)
        self.export()
        return self.dashboard()

    def submit_follow_up(self, question_id: str, response: str, stakeholder: str | None) -> InvestigationResult:
        item = self._item(question_id)
        try:
            result = self.profile.answer_follow_up(item, response, stakeholder, analyst=self.engine)
        except ValueError as exc:
            # Vague answers must not become claims. Re-run the investigation so the precise missing-field
            # question remains visible instead of accepting or embellishing the response.
            LOGGER.info("Follow-up for %s was insufficient (%s); requesting specificity", question_id, exc)
            result = self.engine.investigate(item)
            self.profile.save_questionnaire_result(result)
        self.export()
        return result

    def correct(self, claim_id: str, value: str, raw_response: str, stakeholder: str | None) -> dict[str, Any]:
        history = {entry.claim.id: entry for entry in self.profile.claim_history()}
        if claim_id not in history or history[claim_id].status != "ACTIVE":
            raise ValueError("an active claim must be selected")
        prior = history[claim_id].claim
        if isinstance(prior.value, bool):
            normalized = value.strip().casefold()
            if normalized not in {"yes", "true", "no", "false"}:
                raise ValueError("boolean corrections must be yes/no or true/false")
            corrected_value: bool | str = normalized in {"yes", "true"}
        else:
            corrected_value = value.strip()
            if not corrected_value:
                raise ValueError("corrected value cannot be blank")
        new_claim = self.profile.correct_claim(claim_id, corrected_value, raw_response, stakeholder)
        affected = []
        for item in self.items:
            if normalize_control(item.normalized_control) == prior.control:
                result = self.engine.investigate(item)
                self.profile.save_questionnaire_result(result)
                affected.append(item.id)
        self.export()
        return {"new_claim": new_claim.to_dict(), "superseded_claim_id": claim_id, "affected_questions": affected}

    def evidence(self, identifiers: list[str]) -> list[dict[str, Any]]:
        profile_records = {item.id: item for item in self.profile.get_evidence(identifiers)}
        result = []
        for identifier in identifiers:
            item = self.evidence_by_id.get(identifier) or profile_records.get(identifier)
            if item:
                result.append(asdict(item))
        return result

    def dashboard(self, status_filter: str | None = None) -> dict[str, Any]:
        rows = []
        counts: Counter[str] = Counter()
        for item in self.items:
            state = self.profile.questionnaire_state(item.id)
            status = state.status if state else "UNKNOWN"
            counts[status] += 1
            if status_filter and status_filter != "ALL" and status != status_filter:
                continue
            evidence_ids = list(state.evidence_ids) if state else []
            rows.append({
                "id": item.id, "category": item.category, "question": item.question,
                "answer": state.answer if state else None, "status": status,
                "status_label": STATUS_LABELS[status], "confidence": state.confidence if state else 0.0,
                "evidence_ids": evidence_ids, "evidence": self.evidence(evidence_ids),
                "missing_information": list(state.missing_information) if state else ["not investigated"],
            })
        completed = counts["VERIFIED"] + counts["USER_CONFIRMED"]
        active_user_claims = [
            {**entry.claim.to_dict(), "status": entry.status}
            for entry in self.profile.claim_history()
            if entry.status == "ACTIVE" and entry.claim.evidence_type == "USER_CONFIRMATION"
        ]
        return {
            "progress": {
                "completed": completed, "total": len(self.items), "verified": counts["VERIFIED"],
                "user_confirmed": counts["USER_CONFIRMED"], "unknown": counts["UNKNOWN"], "conflicts": counts["CONFLICT"],
            },
            "questions": rows,
            "active_user_claims": active_user_claims,
            "status_labels": STATUS_LABELS,
        }

    def export(self) -> dict[str, str]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        states = {item.id: self.profile.questionnaire_state(item.id) for item in self.items}
        states = {key: value for key, value in states.items() if value is not None}
        json_path = self.artifact_dir / "completed_questionnaire.json"
        payload = self.dashboard()
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        xlsx_path = self.artifact_dir / "completed_questionnaire.xlsx"
        export_completed_xlsx(self.source, xlsx_path, self.items, states)
        return {"json": str(json_path), "xlsx": str(xlsx_path)}

    def _item(self, question_id: str) -> QuestionnaireItem:
        try:
            return self.items_by_id[question_id]
        except KeyError as exc:
            raise ValueError(f"unknown question ID: {question_id}") from exc


class Handler(BaseHTTPRequestHandler):
    service: AppService

    def _json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/dashboard":
            status = parse_qs(parsed.query).get("status", [None])[0]
            self._json(self.service.dashboard(status))
        elif parsed.path.startswith("/download/"):
            kind = parsed.path.rsplit("/", 1)[-1]
            paths = self.service.export()
            if kind not in paths:
                self._json({"error": "unknown export"}, 404)
                return
            path = Path(paths[kind])
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            if self.path == "/api/analyze-all":
                payload = self.service.analyze_all()
            elif self.path == "/api/investigate":
                payload = self.service.investigate(body["question_id"]).to_dict()
            elif self.path == "/api/follow-up":
                payload = self.service.submit_follow_up(body["question_id"], body["response"], body.get("stakeholder")).to_dict()
            elif self.path == "/api/correct":
                payload = self.service.correct(body["claim_id"], body["value"], body["raw_response"], body.get("stakeholder"))
            elif self.path == "/api/export":
                payload = self.service.export()
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json(payload)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            LOGGER.exception("Unhandled UI request failure")
            self._json({"error": "The request failed safely. Check the application logs for details."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info(format, *args)


def make_server(service: AppService, host: str = "127.0.0.1", port: int = 8501) -> ThreadingHTTPServer:
    handler = type("RegoditHandler", (Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--analyze", action="store_true", help="investigate all questions before serving")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    service = AppService(args.db)
    if args.analyze:
        service.analyze_all()
    server = make_server(service, args.host, args.port)
    print(f"Regodit UI: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Regodit AI Security Analyst</title>
<style>
:root{--bg:#07111f;--panel:#101d2e;--line:#26374d;--text:#edf4ff;--muted:#91a4bd;--blue:#5ba7ff;--green:#45d19a;--amber:#f7b955;--red:#ff6477;--violet:#b89cff}*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07111f,#0c1625);color:var(--text);font:14px Inter,system-ui,sans-serif}header{padding:26px 4vw 14px;display:flex;justify-content:space-between;align-items:center}h1{font-size:25px;margin:0}header p{color:var(--muted);margin:5px 0}.shell{padding:0 4vw 50px}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric,.panel{background:rgba(16,29,46,.94);border:1px solid var(--line);border-radius:14px}.metric{padding:16px}.metric b{font-size:24px;display:block}.metric span{color:var(--muted)}nav{display:flex;gap:8px;margin:20px 0}.tab,button,select,input,textarea{font:inherit}.tab,button{border:1px solid var(--line);border-radius:9px;background:#17273b;color:var(--text);padding:9px 13px;cursor:pointer}.tab.active,button.primary{background:var(--blue);color:#06101e;border-color:var(--blue);font-weight:700}.view{display:none}.view.active{display:block}.panel{padding:18px;margin-bottom:14px}select,input,textarea{width:100%;background:#091525;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px;margin:6px 0 12px}textarea{min-height:95px}label{color:var(--muted);font-weight:600}.status{display:inline-block;border-radius:99px;padding:5px 9px;font-size:12px;font-weight:800}.VERIFIED{background:#163f36;color:var(--green)}.USER_CONFIRMED{background:#29244c;color:var(--violet)}.UNKNOWN{background:#453719;color:var(--amber)}.CONFLICT{background:#481f2a;color:var(--red)}.conflict{border:2px solid var(--red);background:#2a1520}.evidence{border-left:3px solid var(--blue);padding:9px 12px;margin:8px 0;background:#0a1627}.evidence small{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}.scroll{max-height:620px;overflow:auto}.question{max-width:390px}.muted{color:var(--muted)}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.error{color:var(--red);white-space:pre-wrap}@media(max-width:850px){.metrics{grid-template-columns:1fr 1fr}.row{grid-template-columns:1fr}table{font-size:12px}}
</style></head><body><header><div><h1>Regodit AI Security Analyst</h1><p>Evidence-first vendor security investigations</p></div><div class="actions"><a href="/download/json"><button>Export JSON</button></a><a href="/download/xlsx"><button>Export XLSX</button></a></div></header>
<main class="shell"><section class="metrics" id="metrics"></section><div class="panel"><span class="status VERIFIED">Verified from company evidence</span> <span class="status USER_CONFIRMED">Confirmed by user</span> <span class="status UNKNOWN">Unknown / needs confirmation</span> <span class="status CONFLICT">Conflict</span></div><nav><button class="tab active" data-view="investigation">Investigation</button><button class="tab" data-view="questionnaire">Questionnaire</button><button class="tab" data-view="profile">Security profile</button></nav>
<section id="investigation" class="view active"><div class="panel"><h2>Investigate a questionnaire item</h2><label>Active question</label><select id="questionSelect"></select><div class="actions"><button class="primary" onclick="investigate()">Search evidence & investigate</button><button onclick="analyzeAll()">Analyze all 66 questions</button></div></div><div id="result"></div></section>
<section id="questionnaire" class="view"><div class="panel"><div class="row"><div><h2>Questionnaire work queue</h2><p class="muted">Every answer retains status, confidence, and evidence.</p></div><div><label>Filter by status</label><select id="filter" onchange="load()"><option>ALL</option><option>VERIFIED</option><option>USER_CONFIRMED</option><option>UNKNOWN</option><option>CONFLICT</option></select></div></div><div class="scroll"><table><thead><tr><th>ID</th><th>Category / question</th><th>Answer</th><th>Status</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody id="questions"></tbody></table></div></div></section>
<section id="profile" class="view"><div class="panel"><h2>Confirmed security profile</h2><p class="muted">Corrections preserve the previous claim as superseded.</p><div id="claims"></div></div></section></main>
<script>
let data;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.view).classList.add('active')});
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
function badge(s){return `<span class="status ${s}">${esc(data.status_labels[s])}</span>`}
function evidence(items){return items.map(e=>`<div class="evidence"><b>${esc(e.source_name)}</b><br><small>${esc(e.location)} · ${esc(e.evidence_type)} · ${esc(e.id)}</small><p>${esc(e.text||'[Visual evidence — no generated description]')}</p></div>`).join('')||'<p class="muted">No supporting evidence available.</p>'}
async function load(){let f=document.getElementById('filter')?.value||'ALL';data=await api('/api/dashboard?status='+f);let p=data.progress;document.getElementById('metrics').innerHTML=[['Completion',p.completed+' / '+p.total],['Verified',p.verified],['User confirmed',p.user_confirmed],['Unknown',p.unknown],['Conflicts',p.conflicts]].map(x=>`<div class="metric"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');let sel=document.getElementById('questionSelect'),old=sel.value;sel.innerHTML=data.questions.map(q=>`<option value="${q.id}">${q.id} · ${esc(q.question)}</option>`).join('');if(old)sel.value=old;document.getElementById('questions').innerHTML=data.questions.map(q=>`<tr class="${q.status==='CONFLICT'?'conflict':''}"><td>${q.id}</td><td class="question"><b>${esc(q.category)}</b><br>${esc(q.question)}</td><td>${esc(q.answer||'—')}</td><td>${badge(q.status)}</td><td>${Math.round(q.confidence*100)}%</td><td>${q.evidence.map(e=>`<small>${esc(e.source_name)}<br>${esc(e.location)}</small>`).join('<hr>')||'—'}</td></tr>`).join('');renderClaims()}
function renderClaims(){document.getElementById('claims').innerHTML=data.active_user_claims.map(c=>`<div class="evidence"><b>${esc(c.control)} · ${esc(c.attribute)}</b> ${badge('USER_CONFIRMED')}<p>Current value: <strong>${esc(c.value)}</strong> · Scope: ${esc(c.scope)}</p><details><summary>Correct this fact</summary><label>Corrected value</label><input id="v-${c.id}"><label>Reason / raw correction</label><textarea id="r-${c.id}"></textarea><label>Stakeholder</label><input id="s-${c.id}"><button onclick="correctClaim('${c.id}')">Save correction</button></details></div>`).join('')||'<p class="muted">No user-confirmed facts yet.</p>'}
async function investigate(){let id=document.getElementById('questionSelect').value;try{let r=await api('/api/investigate',{question_id:id});await load();showResult(r)}catch(e){showError(e)}}
function showResult(r){let q=data.questions.find(x=>x.id===r.question_id);let cls=r.status==='CONFLICT'?'panel conflict':'panel';let form=r.follow_up_question&&r.next_action==='ASK_FOLLOW_UP'?`<div><h3>${esc(r.follow_up_question)}</h3><textarea id="response" placeholder="Provide a specific answer"></textarea><input id="stakeholder" placeholder="Stakeholder name or work email (optional)"><button class="primary" onclick="followUp('${r.question_id}')">Confirm answer</button></div>`:'';let conflicts=(r.conflicts||[]).map(c=>`<p><strong>Conflict:</strong> ${esc(c.description)}</p>`).join('');document.getElementById('result').innerHTML=`<div class="${cls}"><h2>${esc(q?.question||r.question_id)}</h2>${badge(r.status)}<h3>${esc(r.answer||r.follow_up_question||'No reliable answer is available.')}</h3>${conflicts}${evidence(q?.evidence||[])}${form}</div>`}
async function followUp(id){try{let r=await api('/api/follow-up',{question_id:id,response:document.getElementById('response').value,stakeholder:document.getElementById('stakeholder').value});await load();showResult(r)}catch(e){showError(e)}}
async function correctClaim(id){try{await api('/api/correct',{claim_id:id,value:document.getElementById('v-'+id).value,raw_response:document.getElementById('r-'+id).value,stakeholder:document.getElementById('s-'+id).value});await load()}catch(e){showError(e)}}
async function analyzeAll(){document.getElementById('result').innerHTML='<div class="panel">Analyzing all questions against the security profile and company evidence…</div>';try{await api('/api/analyze-all',{});await load();document.getElementById('result').innerHTML='<div class="panel"><h2>Analysis complete</h2><p>Progress, statuses, evidence, and exports are updated.</p></div>'}catch(e){showError(e)}}
function showError(e){document.getElementById('result').innerHTML=`<div class="panel error">${esc(e.message)}</div>`}load();
</script></body></html>'''


if __name__ == "__main__":
    main()
