"""配置驱动的业务对话测试；未知协议和缺失验收条件在运行前拒绝。"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.equipment.security import validate_url_has_no_secrets
from app.schemas.target_schema import TargetConfig, TargetRequestTemplate


class BusinessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessStream(BusinessConfig):
    text_event: str = "message"
    text_pointer: str = "/choices/0/delta/content"
    text_mode: Literal["append", "replace"] = "append"
    done_data: str = "[DONE]"
    terminal_event: str | None = None
    terminal_pointer: str = "/phase"
    success_values: list[str] = Field(default_factory=lambda: ["end"])
    failure_values: list[str] = Field(default_factory=lambda: ["error", "timeout", "aborted"])
    error_events: list[str] = Field(default_factory=lambda: ["error"])

    @model_validator(mode="after")
    def validate_terminal(self) -> Self:
        if not self.done_data and (not self.terminal_event or not self.success_values):
            raise ValueError("SSE requires an explicit successful terminal signal")
        if set(self.success_values) & set(self.failure_values):
            raise ValueError("SSE success and failure values must be disjoint")
        for pointer in (self.text_pointer, self.terminal_pointer):
            if pointer and not pointer.startswith("/"):
                raise ValueError("SSE selectors must be JSON pointers")
        return self


class BusinessEndpoint(BusinessConfig):
    endpoint: HttpUrl
    protocol: Literal["json", "sse"] = "json"
    method: Literal["POST", "GET"] = "POST"
    path_suffix: str = ""
    stream: BusinessStream | None = None
    body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    credential_header: str = "Authorization"
    credential_prefix: str = "Bearer"
    timeout_seconds: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def check_target_contract(self) -> Self:
        if (self.protocol == "sse") != (self.stream is not None):
            raise ValueError("SSE protocol requires stream configuration; JSON must omit it")
        if self.path_suffix and not self.path_suffix.startswith("/"):
            raise ValueError("path_suffix must start with / and stay on the configured host")
        if self.path_suffix and (self.endpoint.query or self.endpoint.fragment):
            raise ValueError("endpoint with path_suffix must omit query and fragment")
        if self.method == "GET" and self.body:
            raise ValueError("GET endpoint body must be empty")
        TargetConfig(
            name="business-endpoint",
            endpoint=self.endpoint,
            headers=self.headers,
            request_template=TargetRequestTemplate(body_template=self.body),
        )
        if not self.credential_header.strip() or any(
            character in self.credential_header + self.credential_prefix for character in "\r\n"
        ):
            raise ValueError("invalid credential header or prefix")
        return self


class BusinessTarget(BusinessEndpoint):
    body: dict[str, Any] = Field(default_factory=lambda: {"messages": "${messages}"})
    completion: BusinessEndpoint | None = None
    session_setup_pointer: str | None = None
    response_text_pointer: str = "/choices/0/message/content"
    session_response_pointer: str | None = None
    done_pointer: str | None = None

    @model_validator(mode="after")
    def require_conversation_binding(self) -> Self:
        def values(value: Any):
            if isinstance(value, dict):
                for nested in value.values():
                    yield from values(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from values(nested)
            else:
                yield value

        if self.method != "POST":
            raise ValueError("conversation submission requires POST")
        bindings = list(values(self.body))
        if "${messages}" not in bindings and "${session_id}" not in bindings:
            raise ValueError("target body must bind ${messages} or ${session_id}")
        if "${messages}" not in bindings and "${query}" not in bindings:
            raise ValueError("session-based target body must also bind ${query}")
        return self


class BusinessModel(BusinessConfig):
    endpoint: HttpUrl
    model: str = Field(min_length=1)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=30, gt=0, le=300)

    @model_validator(mode="after")
    def reject_url_credentials(self) -> Self:
        validate_url_has_no_secrets(str(self.endpoint), label="business model")
        return self


class BusinessCriterion(BusinessConfig):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]+$", max_length=100)
    description: str = Field(min_length=1, max_length=8000)
    source: Literal["conversation", "target", "verification"]
    evaluator: Literal["semantic", "equals", "contains", "exists"] = "semantic"
    pointer: str = ""
    expected: Any = None

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        if self.source == "conversation" and self.evaluator != "semantic":
            raise ValueError("conversation criteria must use semantic evaluation")
        if self.evaluator in {"equals", "contains"} and "expected" not in self.model_fields_set:
            raise ValueError("equals/contains criteria require expected")
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("pointer must be an RFC 6901 JSON pointer")
        return self


class BusinessScenario(BusinessConfig):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]+$", max_length=100)
    task: str = Field(min_length=1, max_length=16000)
    user_facts: dict[str, Any] = Field(default_factory=dict)
    criteria: list[BusinessCriterion] = Field(min_length=1, max_length=100)
    setup: BusinessEndpoint | None = None
    verification: BusinessEndpoint | None = None
    cleanup: BusinessEndpoint | None = None

    @model_validator(mode="after")
    def validate_criteria(self) -> Self:
        ids = [criterion.id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion IDs must be unique within a scenario")
        if self.verification is None and any(
            criterion.source == "verification" for criterion in self.criteria
        ):
            raise ValueError("verification criteria require a verification endpoint")
        return self


class BusinessLimits(BusinessConfig):
    concurrency: int = Field(default=3, ge=1, le=100)
    max_tasks: int = Field(default=10, ge=1, le=10000)
    max_turns: int = Field(default=12, ge=1, le=100)
    task_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    run_timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    max_calls: int = Field(default=1000, ge=1, le=1000000)
    max_context_bytes: int = Field(default=131072, ge=1024, le=1048576)


class BusinessSuite(BusinessConfig):
    version: Literal["business-tests.v1"] = "business-tests.v1"
    name: str = Field(min_length=1, max_length=200)
    target_version: str = Field(min_length=1, max_length=200)
    target: BusinessTarget
    simulator: BusinessModel
    judge: BusinessModel
    limits: BusinessLimits = Field(default_factory=BusinessLimits)
    scenarios: list[BusinessScenario] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_scenarios(self) -> Self:
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario IDs must be unique")
        if self.limits.max_tasks < len(ids):
            raise ValueError("max_tasks must cover every configured scenario at least once")
        if self.target.session_setup_pointer is not None and any(
            scenario.setup is None for scenario in self.scenarios
        ):
            raise ValueError("session_setup_pointer requires setup in every scenario")
        for pointer in (
            self.target.response_text_pointer,
            self.target.session_setup_pointer,
            self.target.session_response_pointer,
            self.target.done_pointer,
        ):
            if pointer is not None and pointer and not pointer.startswith("/"):
                raise ValueError("response selectors must be RFC 6901 JSON pointers")
        return self


class UserDecision(BusinessConfig):
    action: Literal["reply", "finish", "blocked"]
    message: str = Field(default="", max_length=16000)
    reason: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def require_reply(self) -> Self:
        if self.action == "reply" and not self.message.strip():
            raise ValueError("reply action requires a message")
        return self


class CriterionVerdict(BusinessConfig):
    id: str
    status: Literal["passed", "failed", "inconclusive"]
    reason: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list)


class JudgeDecision(BusinessConfig):
    criteria: list[CriterionVerdict]
