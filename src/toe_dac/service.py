from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

from .context import utc_now
from .experience import ExperienceStore
from .graph import build_td_graph
from .states import TDState, TERMINAL_STATES
from .state_machine import Machine, TransitionError
from .storage import TDRepository, short_id
from .validation import (
    ValidationError,
    required_checks_pass,
    validate_action_result,
    validate_estimate,
    validate_observation,
    validate_plan,
    validate_target,
)


class TDService:
    def __init__(self, repository: TDRepository, context: dict[str, Any]):
        self.repository = repository
        self.context = context
        self.machine = Machine(build_td_graph(), context=context)
        self.machine.state = TDState(context["state"])
        self.experience = ExperienceStore(repository.root)

    @classmethod
    def create(
        cls,
        repository: TDRepository,
        user_thread_id: str | None = None,
        retry_budget: int = 3,
    ) -> "TDService":
        return cls(repository, repository.create(user_thread_id, retry_budget))

    @classmethod
    def load(cls, repository: TDRepository, user_thread_id: str, td_id: str) -> "TDService":
        return cls(repository, repository.load(user_thread_id, td_id))

    @property
    def state(self) -> TDState:
        return self.machine.state

    @property
    def available_events(self) -> list[str]:
        return self.machine.available_events

    def start(self) -> TDState:
        return self._transition("start")

    def submit_target(self, target: dict[str, Any]) -> TDState:
        self._require(TDState.TARGETING)
        try:
            validate_target(target)
        except ValidationError as exc:
            self.repository.record_rejection(self.context, "submit_target", exc.errors)
            raise

        def mutation() -> None:
            revision = len(self.context["target_revisions"]) + 1
            value = copy.deepcopy(target)
            value["revision"] = revision
            self.context["target"] = value
            self.context["target_revisions"].append({
                "revision": revision,
                "target": value,
                "created_at": utc_now(),
            })
            if self._active_failure_phase() == "target":
                self._finish_active_treatment(True, {"target_revision": revision})
            self._clear_waiting()

        state = self._transition(
            "target_accepted", mutation,
            {"target_revision": len(self.context["target_revisions"]) + 1},
        )
        positives = self.context["target"].get("positive") or []
        if positives:
            summary = str(positives[0])
            self.repository.update_thread_meta(
                self.context, {"title": summary, "target_summary": summary},
            )
        return state

    def target_needs_input(self, question: str, reason: str) -> TDState:
        self._require(TDState.TARGETING)
        if not question.strip() or not reason.strip():
            raise ValidationError(["question and reason are required"])

        def mutation() -> None:
            self.context["control"].update({
                "waiting_reason": reason,
                "return_to": "targeting",
                "human_question": question,
                "human_response": None,
            })

        return self._transition("target_needs_input", mutation, {"reason": reason})

    def request_human(self, question: str, reason: str) -> TDState:
        event_by_state = {
            TDState.TARGETING: "target_needs_input",
            TDState.OBSERVING: "observe_needs_input",
            TDState.ESTIMATING: "estimate_needs_input",
            TDState.DECIDING: "decide_needs_input",
            TDState.ACTING: "act_needs_input",
            TDState.CHECKING_ACTION: "action_check_needs_input",
            TDState.CHECKING_TARGET: "target_check_needs_input",
        }
        event = event_by_state.get(self.state)
        if event is None:
            raise TransitionError(self.state, "request_human", "current phase does not support conversational input")
        if not question.strip() or not reason.strip():
            raise ValidationError(["question and reason are required"])
        return_to = self.state.value

        def mutation() -> None:
            self.context["control"].update({
                "waiting_reason": reason,
                "return_to": return_to,
                "human_question": question,
                "human_response": None,
            })

        return self._transition(event, mutation, {"reason": reason, "return_to": return_to})

    def fail_targeting(self, cause: str, message: str) -> TDState:
        self._require(TDState.TARGETING)
        return self._fail("target", cause, message, "target_failed")

    def record_resolved_exception(
        self,
        phase: str,
        cause: str,
        message: str,
        *,
        strategy: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Persist an exception that was handled without a state transition."""
        signature = {"phase": phase, "cause": cause}
        experience_id = self.experience.observe_exception(
            scope_id=self._scope_id,
            user_thread_id=self.context["user_thread_id"],
            td_id=self.context["td_id"],
            session_id=self.context["session_id"],
            exception={
                "phase": phase,
                "cause": cause,
                "message": message,
                "target_summary": " ".join(self.context.get("target", {}).get("positive", [])),
                "action_summary": "",
                "occurred_at": utc_now(),
            },
            visibility=self._experience_visibility(cause),
            signature=signature,
            source_refs=self._source_refs_from_details(details),
        )
        treatment_id = self.experience.treatment_started(
            experience_id, self._scope_id, strategy, details,
        )
        self.experience.treatment_finished(
            experience_id, self._scope_id, True, details or {}, treatment_id=treatment_id,
        )
        return experience_id

    def record_active_treatment(
        self,
        strategy: str,
        success: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an attempted treatment while retaining the failure for recovery."""
        experience_id = self.context["recovery"].get("active_experience_id")
        if not experience_id:
            return
        treatment_id = self.experience.treatment_started(
            experience_id, self._scope_id, strategy, details,
        )
        self.experience.treatment_finished(
            experience_id, self._scope_id, success, details or {}, treatment_id=treatment_id,
        )

    def record_semantic_validation_attempt(
        self,
        phase: str,
        message: str,
        *,
        operation_id: str,
        error_code: str,
        final_attempt: bool,
    ) -> str:
        """Record each model repair attempt before a semantic failure becomes terminal."""
        recovery = self.context["recovery"]
        experience_id = recovery.get("active_experience_id")
        signature = {
            "phase": phase,
            "cause": "semantic_validation_failed",
            "error_code": error_code,
        }
        if not experience_id:
            experience_id = self._observe_failure(
                phase,
                "semantic_validation_failed",
                message,
                visibility="system",
                signature=signature,
                source_refs={"operation_ids": [operation_id]},
            )
        treatment_id = recovery.get("active_treatment_id")
        if not treatment_id:
            treatment_id = self.experience.treatment_started(
                experience_id,
                self._scope_id,
                "repair_phase_output",
                {
                    "phase": phase,
                    "source_refs": {"operation_ids": [operation_id]},
                },
            )
            recovery["active_treatment_id"] = treatment_id
        if final_attempt:
            self.experience.treatment_finished(
                experience_id,
                self._scope_id,
                False,
                {
                    "reason": message,
                    "source_refs": {"operation_ids": [operation_id]},
                },
                treatment_id=treatment_id,
            )
            recovery.pop("active_treatment_id", None)
            recovery["active_treatment_finished"] = True
        self.repository.save(self.context)
        return experience_id

    def apply_experience_decisions(self, decisions: list[dict[str, Any]]) -> None:
        candidates = {
            str(item.get("experience_id")): item
            for item in self.context["recovery"].get("experience_candidates", [])
        }
        for decision in decisions:
            experience_id = str(decision.get("experience_id", ""))
            if experience_id not in candidates:
                continue
            choice = str(decision.get("decision", ""))
            reason = str(decision.get("reason", "")).strip() or "model experience decision"
            if choice == "adopt":
                confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.0))))
                self.experience.adopt(experience_id, self._scope_id, reason, confidence)
                self.context["recovery"]["adopted_experience_id"] = experience_id
                self.experience.treatment_started(
                    experience_id,
                    self._scope_id,
                    "adopted_experience_resolution",
                    {"reason": reason, "confidence": confidence},
                )
            elif choice == "reject":
                self.experience.reject(experience_id, self._scope_id, reason)
        self.repository.save(self.context)

    def begin_runtime_treatment(
        self,
        phase: str,
        cause: str,
        message: str,
        *,
        strategy: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        """Persist a recoverable runtime failure without forcing a state transition."""
        experience_id = self._observe_failure(
            phase, cause, message,
            source_refs=self._source_refs_from_details(details),
        )
        treatment_id = self.experience.treatment_started(
            experience_id, self._scope_id, strategy, details,
        )
        self.context["recovery"]["active_treatment_id"] = treatment_id
        if details:
            self.context["recovery"]["treatment_details"] = copy.deepcopy(details)
        self.repository.save(self.context)
        return experience_id

    def finish_runtime_treatment(self, success: bool, details: dict[str, Any] | None = None) -> None:
        self._finish_active_treatment(success, details or {})
        self.context["recovery"].pop("treatment_details", None)
        self.repository.save(self.context)

    def runtime_retry_count(self, phase: str) -> int:
        counts = self.context["recovery"].setdefault("runtime_retry_counts", {})
        return int(counts.get(phase, 0))

    def register_runtime_retry(self, phase: str) -> int:
        counts = self.context["recovery"].setdefault("runtime_retry_counts", {})
        counts[phase] = int(counts.get(phase, 0)) + 1
        self.repository.save(self.context)
        return counts[phase]

    def fail_runtime_terminal(self, phase: str, cause: str, message: str) -> TDState:
        """End an internally unrecoverable run and leave a material failure artifact."""
        if not self.context["recovery"].get("active_experience_id"):
            experience_id = self._observe_failure(phase, cause, message)
            self.experience.treatment_started(
                experience_id, self._scope_id, "terminal_failure",
                {"phase": phase, "cause": cause},
            )
        last_failure = self.context["recovery"].get("last_failure") or {}
        last_failure.update({
            "terminal_phase": phase,
            "terminal_cause": cause,
            "terminal_message": message,
            "terminal_at": utc_now(),
        })
        self.context["recovery"]["last_failure"] = last_failure
        payload = {
            "type": "toe_dac_failure_report",
            "user_thread_id": self.context["user_thread_id"],
            "td_id": self.context["td_id"],
            "session_id": self.context["session_id"],
            "failed_at": utc_now(),
            "phase": phase,
            "cause": cause,
            "message": message,
            "target": self.context.get("target"),
            "last_failure": self.context.get("recovery", {}).get("last_failure"),
            "retry_counts": self.context.get("recovery", {}).get("runtime_retry_counts", {}),
        }
        artifact_ref = self.repository.write_artifact(
            self.context,
            f"failure-{self.context['session_id']}.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        active_experience_id = self.context["recovery"].get("active_experience_id")
        if active_experience_id:
            self.experience.record_outcome(
                active_experience_id,
                self._scope_id,
                "failed",
                {
                    "phase": phase,
                    "cause": cause,
                    "source_refs": {"artifact_refs": [artifact_ref]},
                },
            )

        def mutation() -> None:
            self._finish_active_treatment(False, {
                "terminal": True,
                "artifact_ref": artifact_ref,
                "source_refs": {"artifact_refs": [artifact_ref]},
            })
            if artifact_ref not in self.context.setdefault("artifacts", []):
                self.context["artifacts"].append(artifact_ref)

        terminal_event = (
            "estimate_not_feasible"
            if self.state == TDState.ESTIMATING and cause == "not_feasible"
            else "runtime_budget_exhausted"
        )
        return self._transition(
            terminal_event,
            mutation,
            {"phase": phase, "cause": cause, "artifact_ref": artifact_ref},
        )

    def submit_observation(self, observation: dict[str, Any]) -> TDState:
        self._require(TDState.OBSERVING)
        def mutation() -> None:
            previous = self.context.get("observation") or {}
            if previous:
                self.context.setdefault("observation_history", []).append(copy.deepcopy(previous))
            self.context["observation"] = copy.deepcopy(observation)

        return self._validated_transition(
            "submit_observation", observation, validate_observation,
            "observation_accepted", mutation,
        )

    def register_evidence(self, records: list[dict[str, Any]]) -> None:
        """Register controller-created evidence without asking a model to manage paths."""
        registry = self.context.setdefault("evidence_registry", [])
        known = {str(item.get("evidence_id")) for item in registry}
        for record in records:
            evidence_id = str(record.get("evidence_id", "")).strip()
            if not evidence_id or evidence_id in known:
                continue
            registry.append(copy.deepcopy(record))
            known.add(evidence_id)
        self.repository.save(self.context)

    def submit_estimate(self, estimate: dict[str, Any]) -> TDState:
        self._require(TDState.ESTIMATING)
        try:
            validate_estimate(estimate)
        except ValidationError as exc:
            self.repository.record_rejection(self.context, "submit_estimate", exc.errors)
            raise
        verdict = estimate.get("verdict")
        if verdict == "needs_observation":
            history = self.context.get("observation_history") or []
            if history and self.context.get("observation") == history[-1]:
                errors = [
                    "re-observation produced no new facts; do not repeat the same Observe path",
                ]
                self.repository.record_rejection(self.context, "submit_estimate", errors)
                raise ValidationError(errors)
            recovery = self.context["recovery"]
            if recovery["retry_count"] >= recovery["retry_budget"]:
                errors = ["TD re-observation budget exhausted"]
                self.repository.record_rejection(self.context, "submit_estimate", errors)
                raise ValidationError(errors)

            def reobserve_mutation() -> None:
                self.context["estimate"] = copy.deepcopy(estimate)
                recovery["retry_count"] += 1
                self.context["control"]["waiting_reason"] = "; ".join(estimate.get("information_gaps", []))

            return self._transition(
                "estimate_requests_observation", reobserve_mutation,
                {"information_gaps": estimate.get("information_gaps", [])},
            )
        return self._validated_transition(
            "submit_estimate", estimate, validate_estimate,
            "estimate_accepted", lambda: self.context.__setitem__("estimate", copy.deepcopy(estimate)),
        )

    def submit_plan(self, plan: dict[str, Any]) -> TDState:
        self._require(TDState.DECIDING)
        try:
            validate_plan(plan)
        except ValidationError as exc:
            self.repository.record_rejection(self.context, "submit_plan", exc.errors)
            raise


        def mutation() -> None:
            if self.context.get("plan"):
                previous = copy.deepcopy(self.context["plan"])
                previous["status"] = "superseded"
                self.context["plan_history"].append(previous)
            normalized = copy.deepcopy(plan)
            normalized.setdefault("plan_id", short_id("plan"))
            normalized.setdefault("version", len(self.context["plan_history"]) + 1)
            normalized["status"] = "active"
            for action in normalized["actions"]:
                action.setdefault("depends_on", [])
                action.setdefault("max_attempts", 1)
                action["status"] = "pending"
            self.context["plan"] = normalized
            self.context["execution"].update({
                "current_action_id": self._first_ready_action(normalized),
                "completed_action_ids": [],
            })
            if self._active_failure_phase() == "decide":
                self._finish_active_treatment(True, {
                    "plan_id": normalized["plan_id"],
                    "plan_version": normalized["version"],
                })

        return self._transition("plan_accepted", mutation, {"action_count": len(plan["actions"])})

    def submit_action_result(self, result: dict[str, Any]) -> TDState:
        self._require(TDState.ACTING)
        try:
            validate_action_result(result)
        except ValidationError as exc:
            self.repository.record_rejection(self.context, "submit_action_result", exc.errors)
            raise
        action = self._current_action()
        attempts = [
            item for item in self.context["execution"]["attempts"]
            if item["action_id"] == action["action_id"]
        ]
        if len(attempts) >= action["max_attempts"]:
            errors = [f"action {action['action_id']} exhausted max_attempts"]
            self.repository.record_rejection(self.context, "submit_action_result", errors)
            raise ValidationError(errors)

        def mutation() -> None:
            action["status"] = "executed"
            self.context["execution"]["attempts"].append({
                "attempt_id": short_id("at"),
                "action_id": action["action_id"],
                "result": copy.deepcopy(result["result"]),
                "evidence_refs": copy.deepcopy(result.get("evidence_refs", [])),
                "status": "submitted",
                "created_at": utc_now(),
            })
            for evidence_ref in result.get("evidence_refs", []):
                if evidence_ref not in self.context["artifacts"]:
                    self.context["artifacts"].append(evidence_ref)

        return self._transition("action_submitted", mutation, {"action_id": action["action_id"]})

    def current_action(self) -> dict[str, Any]:
        return copy.deepcopy(self._current_action())

    def user_reobserve(self, reason: str) -> TDState:
        if self.state == TDState.OBSERVING:
            return self.state
        if self.state == TDState.ESTIMATING:
            return self._transition(
                "estimate_requests_observation",
                lambda: self.context["control"].update({
                    "waiting_reason": reason or "user requested another observation pass",
                    "return_to": None,
                    "human_question": None,
                }),
                {"reason": reason, "control_override": "reobserve"},
            )
        if self.state == TDState.WAITING_HUMAN:
            return self._transition(
                "user_reobserve_requested",
                lambda: self.context["control"].update({
                    "waiting_reason": reason or "user requested another observation pass",
                    "return_to": None,
                    "human_question": None,
                }),
                {"reason": reason, "control_override": "reobserve"},
            )
        allowed = {
            TDState.ESTIMATING, TDState.DECIDING, TDState.ACTING,
            TDState.CHECKING_ACTION, TDState.CHECKING_TARGET, TDState.WAITING_HUMAN,
        }
        if self.state not in allowed:
            raise TransitionError(self.state, "user_reobserve_requested", "state cannot return to Observe")

        def mutation() -> None:
            self.context["control"].update({
                "waiting_reason": reason or "user requested another observation pass",
                "return_to": None,
                "human_question": None,
            })

        return self._transition("user_reobserve_requested", mutation, {"reason": reason})

    def user_replan(self, reason: str) -> TDState:
        if self.state == TDState.WAITING_HUMAN:
            def waiting_replan_mutation() -> None:
                self.context["control"].update({
                    "waiting_reason": reason or "user requested a new plan",
                    "return_to": None,
                    "human_question": None,
                })
                if self.context.get("plan"):
                    self.context["plan"]["status"] = "revision_requested"

            return self._transition(
                "user_replan_requested",
                waiting_replan_mutation,
                {"reason": reason, "control_override": "replan"},
            )
        allowed = {
            TDState.DECIDING, TDState.ACTING, TDState.CHECKING_ACTION,
            TDState.CHECKING_TARGET, TDState.WAITING_HUMAN,
        }
        if self.state not in allowed:
            raise TransitionError(self.state, "user_replan_requested", "state cannot return to Decide")
        if self.state == TDState.DECIDING:
            self.context["control"]["waiting_reason"] = reason
            self.repository.save(self.context)
            return self.state

        def mutation() -> None:
            self.context["control"].update({
                "waiting_reason": reason or "user requested a new plan",
                "return_to": None,
                "human_question": None,
            })
            if self.context.get("plan"):
                self.context["plan"]["status"] = "revision_requested"

        return self._transition("user_replan_requested", mutation, {"reason": reason})

    def check_action(self, checks: list[dict[str, Any]]) -> TDState:
        self._require(TDState.CHECKING_ACTION)
        action = self._current_action()
        passed = required_checks_pass(checks)

        def mutation() -> None:
            self.context["checks"]["action_checks"].append({
                "action_id": action["action_id"],
                "checks": copy.deepcopy(checks),
                "passed": passed,
                "checked_at": utc_now(),
            })
            latest_attempt = self._latest_attempt(action["action_id"])
            latest_attempt["status"] = "passed" if passed else "failed"
            action["status"] = "passed" if passed else "failed"

        if not passed:
            def failed_mutation() -> None:
                mutation()
                self._finish_active_treatment(False, {"action_id": action["action_id"]})
                self._observe_failure(
                    "act", "assertion_failed",
                    f"action assertions failed: {action['action_id']}",
                    action_summary=action["objective"],
                )

            return self._transition("action_failed", failed_mutation, {"action_id": action["action_id"]})

        completed = self.context["execution"]["completed_action_ids"]
        pending_after = [item for item in self.context["plan"]["actions"] if item["action_id"] != action["action_id"] and item["status"] != "passed"]

        def passed_mutation() -> None:
            mutation()
            if action["action_id"] not in completed:
                completed.append(action["action_id"])
            if self._active_failure_phase() == "act":
                self._finish_active_treatment(True, {"action_id": action["action_id"]})
            if pending_after:
                self.context["execution"]["current_action_id"] = self._next_ready_action(completed)

        event = "advance_action" if pending_after else "actions_completed"
        return self._transition(event, passed_mutation, {"action_id": action["action_id"]})

    def check_target(self, checks: list[dict[str, Any]]) -> TDState:
        self._require(TDState.CHECKING_TARGET)
        passed = required_checks_pass(checks)

        artifact_ref = self._ensure_completion_artifact(checks) if passed else None

        def mutation() -> None:
            self.context["checks"]["target_check"] = {
                "checks": copy.deepcopy(checks),
                "passed": passed,
                "checked_at": utc_now(),
            }
            if passed:
                self._finish_active_treatment(True, {"target_check_passed": True})

        if passed:
            return self._transition("target_passed", mutation, {"artifact_ref": artifact_ref})

        def failed_mutation() -> None:
            mutation()
            self._finish_active_treatment(False, {"target_check_passed": False})
            self._observe_failure("check", "assertion_failed", "target acceptance criteria failed")

        return self._transition("target_failed", failed_mutation)

    def _ensure_completion_artifact(self, checks: list[dict[str, Any]]) -> str:
        """Enforce that every successful TD has at least one material Artifact."""
        for reference in self.context.get("artifacts", []):
            path = Path(str(reference))
            resolved = path if path.is_absolute() else self.repository.root / path
            if resolved.is_file():
                return str(reference)

        payload = {
            "type": "toe_dac_completion_report",
            "user_thread_id": self.context["user_thread_id"],
            "td_id": self.context["td_id"],
            "session_id": self.context["session_id"],
            "completed_at": utc_now(),
            "target": self.context.get("target"),
            "plan": self.context.get("plan"),
            "action_attempts": self.context.get("execution", {}).get("attempts", []),
            "target_checks": copy.deepcopy(checks),
        }
        artifact_ref = self.repository.write_artifact(
            self.context,
            f"completion-{self.context['session_id']}.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        self.context.setdefault("artifacts", []).append(artifact_ref)
        self.repository.record_operation(
            self.context,
            "completion_artifact",
            "succeeded",
            phase="target_check",
            data={"artifact_ref": artifact_ref, "auto_generated": True},
        )
        return artifact_ref

    def recover(
        self,
        decision: str,
        *,
        adopted_experience_id: str | None = None,
        reason: str = "",
        confidence: float = 0.0,
        human_question: str = "",
    ) -> TDState:
        self._require(TDState.RECOVERING)
        allowed = {"retry_targeting", "retry_action", "replan", "reobserve", "escalate", "give_up"}
        if decision not in allowed:
            raise ValidationError([f"unsupported recovery decision: {decision}"])
        automated_decisions = {"retry_targeting", "retry_action", "replan", "reobserve"}
        if decision in automated_decisions:
            recovery = self.context["recovery"]
            if recovery["retry_count"] >= recovery["retry_budget"]:
                errors = ["TD retry budget exhausted"]
                self.repository.record_rejection(self.context, decision, errors)
                raise ValidationError(errors)
            if decision == "retry_action":
                action = self._current_action()
                attempts = [item for item in self.context["execution"]["attempts"] if item["action_id"] == action["action_id"]]
                if len(attempts) >= action["max_attempts"]:
                    errors = [f"action {action['action_id']} exhausted max_attempts"]
                    self.repository.record_rejection(self.context, decision, errors)
                    raise ValidationError(errors)

        def mutation() -> None:
            recovery = self.context["recovery"]
            recovery["decision"] = {"type": decision, "reason": reason, "decided_at": utc_now()}
            if decision in automated_decisions:
                recovery["retry_count"] += 1
            if decision == "retry_action":
                self._current_action()["status"] = "pending"
            if decision == "escalate":
                self.context["control"].update({
                    "waiting_reason": reason or "recovery decision requires human input",
                    "return_to": "recovering",
                    "human_question": human_question or "Choose the recovery path",
                    "human_response": None,
                })
            experience_id = recovery.get("active_experience_id")
            if adopted_experience_id:
                self.experience.adopt(adopted_experience_id, self._scope_id, reason, confidence)
                recovery["adopted_experience_id"] = adopted_experience_id
                self.experience.treatment_started(adopted_experience_id, self._scope_id, decision)
            elif experience_id and decision not in {"escalate", "give_up"}:
                self.experience.treatment_started(experience_id, self._scope_id, decision)

        return self._transition(decision, mutation, {"decision": decision})

    def human_reply(self, response: dict[str, Any]) -> TDState:
        self._require(TDState.WAITING_HUMAN)
        return_to = self.context["control"].get("return_to")
        event_by_return = {
            "targeting": "target_input_received",
            "observing": "observation_input_received",
            "estimating": "estimate_input_received",
            "deciding": "decision_input_received",
            "acting": "act_input_received",
            "checking_action": "action_check_input_received",
            "checking_target": "target_check_input_received",
            "recovering": "recovery_input_received",
        }
        if return_to not in event_by_return:
            raise ValidationError([f"unsupported human return state: {return_to}"])

        def mutation() -> None:
            self.context["control"]["human_response"] = copy.deepcopy(response)

        event = event_by_return[return_to]
        return self._transition(event, mutation, {"return_to": return_to})

    def pause(self) -> TDState:
        if self.state in TERMINAL_STATES or self.state in {TDState.IDLE, TDState.PAUSED}:
            raise TransitionError(self.state, "pause", "state cannot be paused")
        paused_from = self.state.value
        return self._transition(
            f"pause_from_{paused_from}",
            lambda: self.context["control"].__setitem__("paused_from", paused_from),
        )

    def resume(self) -> TDState:
        self._require(TDState.PAUSED)
        paused_from = self.context["control"].get("paused_from")
        if not paused_from:
            raise ValidationError(["paused_from is missing"])

        def mutation() -> None:
            self.context["control"]["paused_from"] = None

        return self._transition(f"resume_to_{paused_from}", mutation)

    def cancel(self) -> TDState:
        if self.state in TERMINAL_STATES:
            raise TransitionError(self.state, "cancel", "terminal state cannot be cancelled")
        return self._transition("cancel")

    def _validated_transition(
        self,
        operation: str,
        value: dict[str, Any],
        validator: Callable[[dict[str, Any]], None],
        event: str,
        mutation: Callable[[], None],
    ) -> TDState:
        try:
            validator(value)
        except ValidationError as exc:
            self.repository.record_rejection(self.context, operation, exc.errors)
            raise
        return self._transition(event, mutation)

    def _transition(
        self,
        event: str,
        mutation: Callable[[], None] | None = None,
        log_data: dict[str, Any] | None = None,
    ) -> TDState:
        old_context = copy.deepcopy(self.context)
        old_state = self.state
        try:
            if mutation:
                mutation()
            new_state = self.machine.send(event)
            self.repository.commit_transition(
                self.context, event, old_state.value, new_state.value, log_data,
            )
            return new_state
        except Exception:
            self.context.clear()
            self.context.update(old_context)
            self.machine.state = old_state
            raise

    def _fail(self, phase: str, cause: str, message: str, event: str) -> TDState:
        return self._transition(
            event,
            lambda: self._observe_failure(phase, cause, message),
            {"phase": phase, "cause": cause},
        )

    def _observe_failure(
        self,
        phase: str,
        cause: str,
        message: str,
        action_summary: str = "",
        *,
        visibility: str | None = None,
        signature: dict[str, Any] | None = None,
        source_refs: dict[str, Any] | None = None,
    ) -> str:
        if self.context["recovery"].get("active_experience_id"):
            self._finish_active_treatment(False, {
                "superseded_by_failure": {"phase": phase, "cause": cause},
            })
        failure = {
            "phase": phase,
            "cause": cause,
            "message": message,
            "target_summary": " ".join(self.context.get("target", {}).get("positive", [])),
            "action_summary": action_summary,
            "occurred_at": utc_now(),
        }
        effective_signature = signature or {"phase": phase, "cause": cause}
        self.context["recovery"]["experience_candidates"] = self.experience.match(
            self._scope_id,
            effective_signature,
            limit=3,
        )
        experience_id = self.experience.observe_exception(
            scope_id=self._scope_id,
            user_thread_id=self.context["user_thread_id"],
            td_id=self.context["td_id"],
            session_id=self.context["session_id"],
            exception=failure,
            visibility=visibility or self._experience_visibility(cause),
            signature=effective_signature,
            source_refs=source_refs,
        )
        self.context["recovery"]["last_failure"] = failure
        self.context["recovery"]["active_experience_id"] = experience_id
        self.context["recovery"].pop("adopted_experience_id", None)
        return experience_id

    @staticmethod
    def _experience_visibility(cause: str) -> str:
        return "system" if cause in {
            "invalid_model_output",
            "skill_or_model_runtime_failed",
            "skill_execution_failed",
            "semantic_validation_exhausted",
        } else "thread"

    @staticmethod
    def _source_refs_from_details(details: dict[str, Any] | None) -> dict[str, Any]:
        if not details:
            return {}
        refs = copy.deepcopy(details.get("source_refs", {}))
        mapping = {
            "operation_id": "operation_ids",
            "artifact_ref": "artifact_refs",
            "evidence_ref": "evidence_refs",
        }
        for source_key, target_key in mapping.items():
            value = details.get(source_key)
            if value:
                refs.setdefault(target_key, []).append(value)
        return refs

    def _finish_active_treatment(self, success: bool, details: dict[str, Any]) -> None:
        recovery = self.context["recovery"]
        active = recovery.get("active_experience_id")
        adopted = recovery.get("adopted_experience_id")
        for experience_id in dict.fromkeys(item for item in (active, adopted) if item):
            if experience_id == active and recovery.get("active_treatment_finished"):
                continue
            self.experience.treatment_finished(
                experience_id,
                self._scope_id,
                success,
                details,
                treatment_id=(
                    recovery.get("active_treatment_id") if experience_id == active else None
                ),
            )
        if active or adopted:
            recovery["active_experience_id"] = None
            recovery.pop("active_treatment_id", None)
            recovery.pop("active_treatment_finished", None)
            recovery.pop("adopted_experience_id", None)
            recovery["experience_candidates"] = []

    def _active_failure_phase(self) -> str | None:
        if not self.context["recovery"].get("active_experience_id"):
            return None
        failure = self.context["recovery"].get("last_failure") or {}
        return failure.get("phase")

    @property
    def _scope_id(self) -> str:
        return self.context["user_thread_id"]

    def _current_action(self) -> dict[str, Any]:
        current = self.context["execution"].get("current_action_id")
        for action in self.context.get("plan", {}).get("actions", []):
            if action["action_id"] == current:
                return action
        raise RuntimeError("current action is missing")

    def _latest_attempt(self, action_id: str) -> dict[str, Any]:
        for attempt in reversed(self.context["execution"]["attempts"]):
            if attempt["action_id"] == action_id:
                return attempt
        raise RuntimeError(f"attempt missing for {action_id}")

    @staticmethod
    def _first_ready_action(plan: dict[str, Any]) -> str:
        for action in plan["actions"]:
            if not action.get("depends_on"):
                return action["action_id"]
        raise ValidationError(["plan has no root action"])

    def _next_ready_action(self, completed: list[str]) -> str:
        completed_set = set(completed)
        for action in self.context["plan"]["actions"]:
            if action["status"] != "passed" and set(action.get("depends_on", [])) <= completed_set:
                return action["action_id"]
        raise RuntimeError("no ready action remains")

    def _clear_waiting(self) -> None:
        self.context["control"].update({
            "waiting_reason": None,
            "return_to": None,
            "human_question": None,
        })

    def _require(self, expected: TDState) -> None:
        if self.state != expected:
            raise TransitionError(self.state, "operation", f"expected state {expected.value}")
