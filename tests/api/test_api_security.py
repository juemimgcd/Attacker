from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.security import require_api_key
from conf.settings import settings
from main import create_app


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_api_key_is_optional_for_local_development(monkeypatch) -> None:
    monkeypatch.setattr(settings.security, "api_key", None)

    response = _client().get("/protected")

    assert response.status_code == 200


def test_configured_api_key_rejects_missing_and_incorrect_values(monkeypatch) -> None:
    monkeypatch.setattr(settings.security, "api_key", SecretStr("expected-key"))
    client = _client()

    assert client.get("/protected").status_code == 401
    assert client.get("/protected", headers={"X-API-Key": "wrong-key"}).status_code == 401


def test_configured_api_key_accepts_matching_header(monkeypatch) -> None:
    monkeypatch.setattr(settings.security, "api_key", SecretStr("expected-key"))

    response = _client().get("/protected", headers={"X-API-Key": "expected-key"})

    assert response.status_code == 200


def test_application_protects_business_routes_but_keeps_health_public() -> None:
    routes = {route.path: route for route in create_app().routes if isinstance(route, APIRoute)}

    assert routes["/runs/deterministic"].dependant.dependencies
    assert routes["/runs/{run_id}/approvals"].dependant.dependencies
    assert routes["/runs/{source_run_id}/replay"].dependant.dependencies
    assert routes["/health"].dependant.dependencies == []
