"""构建可校验、可重建且区分可信事实与不可信观察的模型 Prompt。"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.schemas.prompt_schema import (
    GovernedObservation,
    PromptBuildRequest,
    PromptBuildResult,
    PromptMessage,
    PromptProfile,
    PromptSnapshot,
    PromptTask,
)

_PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts"
_SAFE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?P<quote>[\"']?)(?P<key>\b(?:authorization|proxy-authorization|"
    r"x-api-key|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|secret|password)\b)"
    r"(?P=quote)\s*[:=]\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]*)"
)
_AUTH_SCHEME_PATTERN = re.compile(
    r"(?i)\b(bearer|basic|api[-_ ]?key|negotiate|ntlm)\s+[A-Za-z0-9._~+/=-]+"
)
_DIGEST_AUTH_PATTERN = re.compile(r"(?i)\bdigest\s+[^\r\n]+")
_AWS_AUTH_PATTERN = re.compile(r"(?i)\bAWS4-HMAC-SHA256\s+[^\r\n]+")
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)")


def redact_sensitive_text(value: str, secret_values: set[str] | None = None) -> str:
    """清理常见凭据/PII 形态和当前运行已知 Secret。"""

    redacted = value
    for secret in sorted(secret_values or (), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('key')}=<redacted>",
        redacted,
    )
    redacted = _AUTH_SCHEME_PATTERN.sub(
        lambda match: f"{match.group(1)} <redacted>",
        redacted,
    )
    redacted = _DIGEST_AUTH_PATTERN.sub("Digest <redacted>", redacted)
    redacted = _AWS_AUTH_PATTERN.sub("AWS4-HMAC-SHA256 <redacted>", redacted)
    redacted = _EMAIL_PATTERN.sub("<redacted-email>", redacted)
    return _PHONE_PATTERN.sub("<redacted-phone>", redacted)


@dataclass(frozen=True)
class CorePromptTemplate:
    """Core 随代码发布的 Prompt 模板及其校验和。"""

    task: PromptTask
    name: str
    version: str
    filename: str
    checksum: str


_CORE_TEMPLATES = {
    PromptTask.planner: CorePromptTemplate(
        task=PromptTask.planner,
        name="core_planner",
        version="1.0.0",
        filename="planner_v1.txt",
        checksum="dbbffcd3e03f01d1c4d4c882b3b361d2b36703ad502c90800a734f957715f834",
    ),
    PromptTask.model_judge: CorePromptTemplate(
        task=PromptTask.model_judge,
        name="core_model_judge",
        version="1.0.0",
        filename="model_judge_v1.txt",
        checksum="2e6285d2613423d62d88c6f8d8599c96f7e1d31c356bfbf1be41ebbc39c5bf8b",
    ),
}


# 只从 Core 模板和已批准 Profile 构建模型输入，调用方无法覆盖 system prompt。
class PromptGovernanceService:
    """执行模板固定、引用校验、观察限长、脱敏和总输入预算。"""

    def __init__(self, profiles: list[PromptProfile] | None = None) -> None:
        selected_profiles = profiles if profiles is not None else self._default_profiles()
        self._profiles = {profile.profile_id: profile for profile in selected_profiles}
        if len(self._profiles) != len(selected_profiles):
            raise ValueError("prompt profile IDs must be unique")

    # 对输入脱敏并执行条数、长度和保守 token 上限后生成可重建快照。
    def build(self, request: PromptBuildRequest) -> PromptBuildResult:
        """生成消息与不可变快照，供模型调用和事后审计复建。"""

        profile = self._validate_profile(
            profile_id=request.profile_id,
            task=request.task,
            caller_id=request.caller_id,
            schema_version=request.schema_version,
        )
        system_prompt = self._load_core_template(profile)
        observations = self._govern_observations(request, profile)
        fact_refs = self._validate_fact_refs(request.fact_refs, profile)
        snapshot_data = {
            "task": request.task,
            "profile_id": profile.profile_id,
            "template_name": profile.template_name,
            "template_version": profile.template_version,
            "template_checksum": profile.template_checksum,
            "schema_version": request.schema_version,
            "trusted_payload": request.trusted_payload,
            "observations": observations,
            "fact_refs": fact_refs,
            "model_id": request.model_id,
            "provider_id": request.provider_id,
            "model_parameters": request.model_parameters,
        }
        input_checksum = self._checksum_json(snapshot_data)
        snapshot = PromptSnapshot(
            **snapshot_data,
            input_checksum=input_checksum,
        )
        messages = self._render_messages(system_prompt, snapshot)
        self._validate_total_input_limit(messages, profile)
        return PromptBuildResult(snapshot=snapshot, messages=messages)

    # 使用相同 Profile、Core 模板和规范化事实重建逐字相同的消息。
    def rebuild(
        self,
        snapshot: PromptSnapshot,
        caller_id: str,
    ) -> list[PromptMessage]:
        profile = self._validate_profile(
            profile_id=snapshot.profile_id,
            task=snapshot.task,
            caller_id=caller_id,
            schema_version=snapshot.schema_version,
        )
        snapshot_data = snapshot.model_dump(exclude={"input_checksum"})
        if self._checksum_json(snapshot_data) != snapshot.input_checksum:
            raise ValueError("prompt snapshot input checksum mismatch")
        system_prompt = self._load_core_template(profile)
        messages = self._render_messages(system_prompt, snapshot)
        self._validate_total_input_limit(messages, profile)
        return messages

    # Profile 必须同时通过批准、调用权限、任务和 Schema 兼容性检查。
    def _validate_profile(
        self,
        profile_id: str,
        task: PromptTask,
        caller_id: str,
        schema_version: str,
    ) -> PromptProfile:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ValueError("unknown prompt profile")
        if not profile.approved:
            raise ValueError("prompt profile is not approved")
        if caller_id not in profile.allowed_callers:
            raise ValueError("caller is not allowed to use prompt profile")
        if profile.task != task:
            raise ValueError("prompt profile task is incompatible")
        if schema_version not in profile.compatible_schema_versions:
            raise ValueError("prompt profile schema is incompatible")

        template = _CORE_TEMPLATES.get(task)
        if template is None:
            raise ValueError("prompt task has no Core template")
        if (
            profile.template_name != template.name
            or profile.template_version != template.version
            or profile.template_checksum != template.checksum
        ):
            raise ValueError("prompt profile does not match Core template")
        return profile

    # 读取版本控制资源并在每次构建前验证内容 checksum。
    def _load_core_template(self, profile: PromptProfile) -> str:
        template = _CORE_TEMPLATES[profile.task]
        content = (_PROMPT_ROOT / template.filename).read_text(encoding="utf-8")
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if checksum != profile.template_checksum:
            raise ValueError("Core prompt template checksum mismatch")
        return content

    def _govern_observations(
        self,
        request: PromptBuildRequest,
        profile: PromptProfile,
    ) -> list[GovernedObservation]:
        if len(request.observations) > profile.limits.max_observations:
            raise ValueError("observation count exceeds prompt profile limit")

        governed: list[GovernedObservation] = []
        seen_refs: set[str] = set()
        for observation in request.observations:
            self._validate_ref(observation.observation_ref)
            if observation.observation_ref in seen_refs:
                raise ValueError("observation references must be unique")
            seen_refs.add(observation.observation_ref)

            summary = self._redact(observation.summary)
            if len(summary) > profile.limits.max_chars_per_observation:
                raise ValueError("observation summary exceeds prompt profile limit")
            governed.append(
                GovernedObservation(
                    observation_ref=observation.observation_ref,
                    summary=summary,
                )
            )
        return governed

    def _validate_fact_refs(
        self,
        fact_refs: list[str],
        profile: PromptProfile,
    ) -> list[str]:
        if len(fact_refs) > profile.limits.max_fact_refs:
            raise ValueError("fact reference count exceeds prompt profile limit")
        if len(fact_refs) != len(set(fact_refs)):
            raise ValueError("fact references must be unique")
        for fact_ref in fact_refs:
            self._validate_ref(fact_ref)
        return list(fact_refs)

    def _validate_ref(self, value: str) -> None:
        if _SAFE_REF_PATTERN.fullmatch(value) is None:
            raise ValueError("fact references must be opaque safe identifiers")

    # 统一清除常见凭据、认证值和直接 PII，避免进入 Prompt 与快照。
    def _redact(self, value: str) -> str:
        return redact_sensitive_text(value)

    def _render_messages(
        self,
        system_prompt: str,
        snapshot: PromptSnapshot,
    ) -> list[PromptMessage]:
        user_payload = {
            "schema_version": snapshot.schema_version,
            "trusted_core_payload": snapshot.trusted_payload,
            "trusted_fact_refs": snapshot.fact_refs,
            "untrusted_observations": [
                observation.model_dump() for observation in snapshot.observations
            ],
        }
        return [
            PromptMessage(role="system", content=system_prompt),
            PromptMessage(
                role="user",
                content=json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        ]

    # UTF-8 字节数是无 tokenizer 依赖的保守上界，确保不突破配置 token 数。
    def _validate_total_input_limit(
        self,
        messages: list[PromptMessage],
        profile: PromptProfile,
    ) -> None:
        input_bytes = sum(len(message.content.encode("utf-8")) for message in messages)
        if input_bytes > profile.limits.max_total_input_tokens:
            raise ValueError("prompt input exceeds conservative token limit")

    def _checksum_json(self, value: object) -> str:
        payload = json.dumps(
            value,
            default=lambda item: item.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _default_profiles(self) -> list[PromptProfile]:
        return [
            PromptProfile(
                profile_id="core.planner.v1",
                task=PromptTask.planner,
                template_name=_CORE_TEMPLATES[PromptTask.planner].name,
                template_version=_CORE_TEMPLATES[PromptTask.planner].version,
                template_checksum=_CORE_TEMPLATES[PromptTask.planner].checksum,
                approved=True,
                allowed_callers={"attacker_core"},
                compatible_schema_versions={"planner-decision-v1"},
            ),
            PromptProfile(
                profile_id="core.model_judge.v1",
                task=PromptTask.model_judge,
                template_name=_CORE_TEMPLATES[PromptTask.model_judge].name,
                template_version=_CORE_TEMPLATES[PromptTask.model_judge].version,
                template_checksum=_CORE_TEMPLATES[PromptTask.model_judge].checksum,
                approved=True,
                allowed_callers={"attacker_core"},
                compatible_schema_versions={"model-judge-v1"},
            ),
        ]


prompt_governance_service = PromptGovernanceService()
