"""Best-effort PRISM tracing for the real Regodit investigation path."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from regodit.config import (
    PRISMTRACE_AGENT_NAME,
    PRISMTRACE_API_KEY,
    PRISMTRACE_HOST,
    PRISMTRACE_PROJECT_ID,
)

LOGGER = logging.getLogger("regodit.prism")
AGENT_ID = "regodit-security-analyst-v1"


@dataclass
class PrismObserver:
    """Small adapter around prismtrace-sdk; missing tracing never breaks analysis."""

    session_id: str

    def __post_init__(self) -> None:
        self._client: Any | None = None
        self.trajectory_ids: list[str] = []
        if not all((PRISMTRACE_API_KEY, PRISMTRACE_PROJECT_ID, PRISMTRACE_HOST)):
            LOGGER.info("PRISM tracing disabled: configure API key, project ID, and host")
            return
        try:
            from prismtrace import PRISMtrace

            self._client = PRISMtrace(
                api_key=PRISMTRACE_API_KEY,
                project_id=PRISMTRACE_PROJECT_ID,
                host=PRISMTRACE_HOST,
            )
        except Exception as exc:  # observability is deliberately fail-open
            LOGGER.warning("PRISM tracing could not initialize: %s", type(exc).__name__)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def model_call(
        self,
        *,
        request_id: str,
        model: str,
        messages: list[dict[str, str]],
        output: str,
        latency_ms: int,
        input_tokens: int,
        output_tokens: int,
        metadata: dict[str, Any],
    ) -> None:
        if not self._client:
            return
        try:
            self._client.trace_llm(
                model=model,
                input_messages=messages,
                output=output,
                latency_ms=latency_ms,
                token_count_input=input_tokens,
                token_count_output=output_tokens,
                trace_id=request_id,
                agent_id=AGENT_ID,
                agent_name=PRISMTRACE_AGENT_NAME,
                metadata={"session_id": self.session_id, **metadata},
            )
        except Exception as exc:
            LOGGER.warning("PRISM model trace was skipped: %s", type(exc).__name__)

    def investigation(
        self,
        *,
        request_id: str,
        question_id: str,
        control: str,
        model: str,
        retrieval_ms: int,
        model_ms: int,
        evidence_count: int,
        status: str,
        conflict: bool,
        follow_up: bool,
    ) -> None:
        if not self._client:
            return
        steps = [
            {
                "step_type": "tool_call",
                "label": "Retrieve Regodit evidence",
                "tool_name": "hybrid_evidence_retrieval",
                "input_summary": f"question_id={question_id}; control={control}",
                "output_summary": f"retrieved_evidence_count={evidence_count}",
                "duration_ms": retrieval_ms,
                "status": "success",
            },
            {
                "step_type": "reasoning",
                "label": "Grounded structured model analysis",
                "input_summary": "Analyze only retrieved evidence and persisted user claims",
                "output_summary": f"validated_status={status}; conflict={conflict}; follow_up={follow_up}",
                "duration_ms": model_ms,
                "status": "success",
            },
            {
                "step_type": "final_answer",
                "label": "Validated investigation result",
                "output_summary": f"question_id={question_id}; status={status}",
                "duration_ms": 0,
                "status": "success",
            },
        ]
        try:
            receipt = self._client.submit_trajectory(
                steps,
                agent_name=PRISMTRACE_AGENT_NAME,
                agent_id=AGENT_ID,
                conversation_id=self.session_id,
                request_id=request_id,
                model=model,
                async_send=False,
            )
            if isinstance(receipt, dict) and receipt.get("id"):
                self.trajectory_ids.append(str(receipt["id"]))
        except Exception as exc:
            LOGGER.warning("PRISM investigation trajectory was skipped: %s", type(exc).__name__)

    def flush(self) -> None:
        if not self._client:
            return
        try:
            self._client.flush(timeout=10.0)
        except Exception as exc:
            LOGGER.warning("PRISM flush failed: %s", type(exc).__name__)

    def evaluations(self) -> list[dict[str, Any]]:
        """Fetch evaluations for receipt-backed trajectories when available."""
        if not self._client:
            return []
        results: list[dict[str, Any]] = []
        for trajectory_id in self.trajectory_ids:
            try:
                value = self._client.get_trajectory_evaluation(trajectory_id)
                if isinstance(value, dict):
                    results.append({"trajectory_id": trajectory_id, "evaluation": value})
            except Exception as exc:
                LOGGER.warning("PRISM evaluation fetch failed: %s", type(exc).__name__)
        return results
