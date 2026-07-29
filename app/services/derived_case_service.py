import hashlib
import json

from app.schemas.adaptive_agent_schema import DerivedCase
from app.schemas.graybox_schema import GrayBoxCase


class DerivedCaseService:
    @staticmethod
    def freeze(
        *,
        parent_case_id: str,
        generator_id: str,
        generator_version: str,
        input_fact_refs: tuple[str, ...],
        output: dict[str, object],
    ) -> DerivedCase:
        validated_output = GrayBoxCase.model_validate(output).model_dump(mode="json")
        payload = {
            "parent_case_id": parent_case_id,
            "generator_id": generator_id,
            "generator_version": generator_version,
            "input_fact_refs": sorted(input_fact_refs),
            "output": validated_output,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        checksum = hashlib.sha256(canonical.encode()).hexdigest()
        return DerivedCase(
            derived_case_id=checksum,
            parent_case_id=parent_case_id,
            generator_id=generator_id,
            generator_version=generator_version,
            input_fact_refs=tuple(sorted(input_fact_refs)),
            output=validated_output,
            checksum=checksum,
        )

    @staticmethod
    def mark_verified(
        derived_case: DerivedCase,
        evidence_refs: tuple[str, ...],
        *,
        replay_mode: str,
        evidence_complete: bool,
    ) -> DerivedCase:
        if replay_mode != "deterministic":
            raise ValueError("derived case verification requires deterministic replay")
        if not evidence_complete:
            raise ValueError("derived case verification requires complete replay evidence")
        if not evidence_refs:
            raise ValueError("deterministic verification requires persisted evidence refs")
        return derived_case.model_copy(
            update={
                "verification_mode": "deterministic",
                "deterministic_evidence_refs": tuple(sorted(set(evidence_refs))),
                "deterministic_verified": True,
            }
        )
