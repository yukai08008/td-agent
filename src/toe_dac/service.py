from __future__ import annotations

import copy
from typing import Any, Callable

from state_machine import Machine, TransitionError

from .context import utc_now
from .experience import ExperienceStore
from .graph import build_td_graph
from .states import TDState, TERMINAL_STATES
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

        return self._transition("target_accepted", mutation, {"target_revision": len(self.context["target_revisions"]) + 1})

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

    def submit_observation(self, observation: dict[str, Any]) -> TDState:
        self._require(TDState.OBSERVING)
        return self._validated_transition(
            "submit_observation", observation, validate_observation,
            "observation_accepted", lambda: self.context.__setitem__("observation", copy.deepcopy(observation)),
        )

    def submit_estimate(self, estimate: dict[str, Any]) -> TDState:
        self._require(TDState.ESTIMATING)
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

        return self._transition("action_submitted", mutation, {"action_id": action["action_id"]})

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

        def mutation() -> None:
            self.context["checks"]["target_check"] = {
                "checks": copy.deepcopy(checks),
                "passed": passed,
                "checked_at": utc_now(),
            }
            if passed:
                self._finish_active_treatment(True, {"target_check_passed": True})

        if passed:
            return self._transition("target_passed", mutation)

        def failed_mutation() -> None:
            mutation()
            self._finish_active_treatment(False, {"target_check_passed": False})
            self._observe_failure("check", "assertion_failed", "target acceptance criteria failed")

        return self._transition("target_failed", failed_mutation)

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
        if decision.startswith("retry_"):
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
            if decision.startswith("retry_"):
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

    def _observe_failure(self, phase: str, cause: str, message: str, action_summary: str = "") -> str:
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
        experience_id = self.experience.observe_exception(
            scope_id=self._scope_id,
            user_thread_id=self.context["user_thread_id"],
            td_id=self.context["td_id"],
            session_id=self.context["session_id"],
            exception=failure,
        )
        self.context["recovery"]["last_failure"] = failure
        self.context["recovery"]["active_experience_id"] = experience_id
        self.context["recovery"].pop("adopted_experience_id", None)
        return experience_id

    def _finish_active_treatment(self, success: bool, details: dict[str, Any]) -> None:
        recovery = self.context["recovery"]
        active = recovery.get("active_experience_id")
        adopted = recovery.get("adopted_experience_id")
        for experience_id in dict.fromkeys(item for item in (active, adopted) if item):
            self.experience.treatment_finished(experience_id, self._scope_id, success, details)
        if active or adopted:
            recovery["active_experience_id"] = None
            recovery.pop("adopted_experience_id", None)

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
