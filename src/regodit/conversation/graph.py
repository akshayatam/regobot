"""Thin LangGraph coordinator around the existing Regodit application service."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


Intent = Literal["SECURITY_QUESTION", "ANSWER_PENDING", "CORRECTION", "CONTINUE", "NAVIGATE"]


class AnalystState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    thread_id: str
    user_message: str
    intent: Intent
    active_question_id: str | None
    active_question_text: str | None
    active_control: str | None
    retrieved_evidence_ids: list[str]
    current_claims: list[dict[str, Any]]
    investigation_status: str | None
    missing_fields: list[str]
    conflict_ids: list[str]
    pending_question: str | None
    pending_question_type: str | None
    pending_payload: dict[str, Any] | None
    collected_fields: dict[str, Any]
    last_user_confirmation: dict[str, Any] | None
    assistant_message: dict[str, Any] | None


class ConversationGateway(Protocol):
    def select_chat_question(self, message: str, intent: Intent) -> str | None: ...
    def investigate_for_chat(self, question_id: str) -> dict[str, Any]: ...
    def process_chat_response(self, state: AnalystState, response: str) -> dict[str, Any]: ...
    def navigate_chat(self, message: str) -> dict[str, Any]: ...


def _intent(message: str) -> Intent:
    lower = message.strip().casefold()
    if any(term in lower for term in ("actually", "correction", "correct ", "changed", "now run", "now runs")):
        return "CORRECTION"
    if any(term in lower for term in ("continue questionnaire", "continue security", "next question", "ask an unresolved", "resolve next conflict")):
        return "CONTINUE"
    if any(term in lower for term in ("show me", "what conflicts", "what is left", "progress", "generate questionnaire", "export questionnaire", "view questionnaire", "security profile")):
        return "NAVIGATE"
    return "SECURITY_QUESTION"


def _append(state: AnalystState, role: str, content: str, **extra: Any) -> list[dict[str, Any]]:
    return [*state.get("messages", []), {"role": role, "content": content, **extra}]


class ConversationService:
    """Owns the conversation graph/checkpointer, not durable security knowledge."""

    def __init__(self, gateway: ConversationGateway, checkpoint_path: Path):
        self.gateway = gateway
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.connection)
        self._openings: dict[str, dict[str, Any]] = {}
        builder = StateGraph(AnalystState)
        builder.add_node("determine_intent", self._determine_intent)
        builder.add_node("select_task", self._select_task)
        builder.add_node("investigate", self._investigate)
        builder.add_node("navigate", self._navigate)
        builder.add_node("wait_for_employee", self._wait_for_employee)
        builder.add_node("process_response", self._process_response)
        builder.add_edge(START, "determine_intent")
        builder.add_conditional_edges("determine_intent", self._after_intent, {
            "task": "select_task", "navigate": "navigate",
        })
        builder.add_edge("select_task", "investigate")
        builder.add_conditional_edges("investigate", self._needs_employee, {"wait": "wait_for_employee", "done": END})
        builder.add_edge("wait_for_employee", "process_response")
        builder.add_conditional_edges("process_response", self._needs_employee, {"wait": "wait_for_employee", "done": END})
        builder.add_edge("navigate", END)
        self.graph = builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _determine_intent(state: AnalystState) -> dict[str, Any]:
        message = state.get("user_message", "")
        intent = _intent(message)
        return {"intent": intent, "messages": _append(state, "user", message)}

    @staticmethod
    def _after_intent(state: AnalystState) -> str:
        return "navigate" if state["intent"] in {"NAVIGATE", "CORRECTION"} else "task"

    def _select_task(self, state: AnalystState) -> dict[str, Any]:
        question_id = self.gateway.select_chat_question(state.get("user_message", ""), state["intent"])
        if question_id:
            return {"active_question_id": question_id}
        message = "I couldn't map that request to a questionnaire control. Try asking about MFA, encryption, backups, access, incidents, or another security topic."
        return {"active_question_id": None, "assistant_message": {"role": "assistant", "content": message, "action": "NAVIGATE"}, "messages": _append(state, "assistant", message)}

    def _investigate(self, state: AnalystState) -> dict[str, Any]:
        question_id = state.get("active_question_id")
        if not question_id:
            return {}
        payload = self.gateway.investigate_for_chat(question_id)
        assistant = payload["message"]
        return {
            "active_question_id": question_id,
            "active_question_text": payload.get("question"),
            "active_control": payload.get("control"),
            "retrieved_evidence_ids": payload.get("evidence_ids", []),
            "current_claims": payload.get("claims", []),
            "investigation_status": payload.get("status"),
            "missing_fields": payload.get("missing_fields", []),
            "conflict_ids": payload.get("conflict_ids", []),
            "pending_question": payload.get("pending_question"),
            "pending_question_type": payload.get("pending_question_type"),
            "pending_payload": payload.get("pending_payload"),
            "collected_fields": payload.get("collected_fields", {}),
            "assistant_message": assistant,
            "messages": _append(state, "assistant", assistant["content"], **{k: v for k, v in assistant.items() if k not in {"role", "content"}}),
        }

    @staticmethod
    def _needs_employee(state: AnalystState) -> str:
        return "wait" if state.get("pending_question") else "done"

    @staticmethod
    def _wait_for_employee(state: AnalystState) -> dict[str, Any]:
        response = interrupt(state.get("pending_payload") or {"question": state.get("pending_question")})
        return {"user_message": str(response), "messages": _append(state, "user", str(response))}

    def _process_response(self, state: AnalystState) -> dict[str, Any]:
        payload = self.gateway.process_chat_response(state, state["user_message"])
        assistant = payload["message"]
        return {
            **{key: value for key, value in payload.items() if key != "message"},
            "assistant_message": assistant,
            "messages": _append(state, "assistant", assistant["content"], **{k: v for k, v in assistant.items() if k not in {"role", "content"}}),
        }

    def _navigate(self, state: AnalystState) -> dict[str, Any]:
        payload = self.gateway.navigate_chat(state.get("user_message", ""))
        assistant = payload["message"]
        return {"assistant_message": assistant, "pending_question": None, "pending_payload": None,
                "messages": _append(state, "assistant", assistant["content"], **{k: v for k, v in assistant.items() if k not in {"role", "content"}})}

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def opening(self, thread_id: str, message: dict[str, Any]) -> dict[str, Any]:
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        messages = list(snapshot.values.get("messages", [])) if snapshot.values else []
        if not messages:
            self._openings[thread_id] = message
            messages = [message]
        return {"thread_id": thread_id, "messages": messages, "assistant": messages[-1]}

    def send(self, thread_id: str, message: str) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("message cannot be blank")
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if snapshot.next:
            result = self.graph.invoke(Command(resume=message.strip()), config=config)
        else:
            initial: AnalystState = {"thread_id": thread_id, "user_message": message.strip()}
            if not snapshot.values and thread_id in self._openings:
                initial["messages"] = [self._openings[thread_id]]
            result = self.graph.invoke(initial, config=config)
        interrupts = result.get("__interrupt__", ())
        pending = None
        if interrupts:
            pending = interrupts[0].value
        return {
            "thread_id": thread_id,
            "messages": result.get("messages", []),
            "assistant": result.get("assistant_message"),
            "pending": pending,
            "state": {key: result.get(key) for key in (
                "active_question_id", "active_control", "investigation_status", "missing_fields",
                "pending_question", "pending_question_type", "retrieved_evidence_ids",
            )},
        }

    def close(self) -> None:
        self.connection.close()

    def __del__(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass
