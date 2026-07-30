from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Request

from app.schemas.equipment_schema import (
    PackageType,
    ProviderInstanceCreate,
    SkillDryRunRequest,
)

router = APIRouter(prefix="/equipment", tags=["equipment"])
EQUIPMENT_ERRORS = (LookupError, ValueError, PermissionError, RuntimeError, OSError)


def _raise_http(exc: Exception) -> NoReturn:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _list(
    request: Request,
    package_type: PackageType,
    *,
    package_id: str | None = None,
    version: str | None = None,
    enabled: bool | None = None,
    validation_status: str | None = None,
    capability: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    return await request.app.state.equipment_repository.list_packages(
        package_type=package_type,
        package_id=package_id,
        version=version,
        enabled=enabled,
        validation_status=validation_status,
        capability=capability,
        tag=tag,
    )


@router.get("/provider-packages")
async def list_provider_packages(
    request: Request,
    version: str | None = None,
    enabled: bool | None = None,
    validation_status: str | None = None,
    capability: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    return await _list(
        request,
        PackageType.provider,
        version=version,
        enabled=enabled,
        validation_status=validation_status,
        capability=capability,
        tag=tag,
    )


@router.get("/provider-packages/{package_id}")
async def get_provider_package(package_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.get_package(
            PackageType.provider, package_id
        )
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.get("/provider-instances")
async def list_provider_instances(
    request: Request,
    instance_id: str | None = None,
    environment: str | None = None,
    health_status: str | None = None,
    enabled: bool | None = None,
    include_history: bool = False,
) -> list[dict[str, Any]]:
    return await request.app.state.equipment_repository.list_provider_instances(
        instance_id=instance_id,
        environment=environment,
        health_status=health_status,
        enabled=enabled,
        include_history=include_history,
    )


@router.get("/provider-instances/{instance_id}")
async def get_provider_instance(instance_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.get_provider_instance(instance_id)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.get("/skills")
async def list_skills(
    request: Request,
    enabled: bool | None = None,
    validation_status: str | None = None,
    capability: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    return await _list(
        request,
        PackageType.skill,
        enabled=enabled,
        validation_status=validation_status,
        capability=capability,
        tag=tag,
    )


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.get_package(PackageType.skill, skill_id)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.get("/casepacks")
async def list_casepacks(
    request: Request,
    enabled: bool | None = None,
    validation_status: str | None = None,
    capability: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    return await _list(
        request,
        PackageType.casepack,
        enabled=enabled,
        validation_status=validation_status,
        capability=capability,
        tag=tag,
    )


@router.get("/contracts")
async def list_contracts(request: Request) -> list[dict[str, Any]]:
    return await _list(request, PackageType.contract)


@router.post("/reload")
async def reload_equipment(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_service.reload()
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


async def _validate(request: Request, package_type: PackageType, package_id: str) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_service.validate_package(package_type, package_id)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


async def _enable(
    request: Request,
    package_type: PackageType,
    package_id: str,
    *,
    enabled: bool,
) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.set_package_enabled(
            package_type, package_id, enabled
        )
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.post("/provider-packages/{package_id}/validate")
async def validate_provider(package_id: str, request: Request) -> dict[str, Any]:
    return await _validate(request, PackageType.provider, package_id)


@router.post("/provider-packages/{package_id}/enable")
async def enable_provider(package_id: str, request: Request) -> dict[str, Any]:
    return await _enable(request, PackageType.provider, package_id, enabled=True)


@router.post("/provider-packages/{package_id}/disable")
async def disable_provider(package_id: str, request: Request) -> dict[str, Any]:
    return await _enable(request, PackageType.provider, package_id, enabled=False)


@router.post("/skills/{skill_id}/validate")
async def validate_skill(skill_id: str, request: Request) -> dict[str, Any]:
    return await _validate(request, PackageType.skill, skill_id)


@router.post("/skills/{skill_id}/enable")
async def enable_skill(skill_id: str, request: Request) -> dict[str, Any]:
    return await _enable(request, PackageType.skill, skill_id, enabled=True)


@router.post("/skills/{skill_id}/disable")
async def disable_skill(skill_id: str, request: Request) -> dict[str, Any]:
    return await _enable(request, PackageType.skill, skill_id, enabled=False)


@router.post("/provider-instances")
async def create_provider_instance(
    payload: ProviderInstanceCreate, request: Request
) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_service.create_provider_instance(payload)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.post("/provider-instances/{instance_id}/enable")
async def enable_provider_instance(instance_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.set_instance_enabled(instance_id, True)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.post("/provider-instances/{instance_id}/disable")
async def disable_provider_instance(instance_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.equipment_repository.set_instance_enabled(instance_id, False)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.post("/provider-instances/{instance_id}/healthcheck")
async def healthcheck_provider_instance(instance_id: str, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.harness_service.healthcheck(instance_id)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)


@router.post("/skills/{skill_id}/dry-run")
async def dry_run_skill(
    skill_id: str,
    payload: SkillDryRunRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return await request.app.state.harness_service.dry_run_skill(skill_id, payload)
    except EQUIPMENT_ERRORS as exc:
        _raise_http(exc)
