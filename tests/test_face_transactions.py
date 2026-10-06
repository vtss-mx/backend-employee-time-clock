"""Ninguna conexión de la base queda ocupada mientras se analiza un rostro (`backend AGENTS.md` §4, transacciones
cortas): detrás de PgBouncer una transacción abierta ocupa una conexión REAL del servidor, y el análisis es CPU de 0.3
a 3 s. `take_challenge` y `confirm_live` cierran la transacción de lo leído antes de analizar; lo que se registra va
después en su propia transacción (la bitácora con sus métricas). Aquí el motor facial anota cuántas conexiones del
pool estaban prestadas en cada análisis: siempre 0, en la verificación del empleado (1:1), en la identificación del
validador (1:N) y en el registro facial.
"""

from typing import ClassVar

import pytest
from sqlalchemy import update

from app.core.database import SessionLocal, engine
from app.dependencies import get_pipeline
from app.main import app
from app.models import VerificationPolicy
from app.services.policy_service import clear_policy_cache
from tests.conftest import FakePipeline, create_employee, login, turn_files
from tests.test_validators import approved, identify_face, validator_headers


class _WatchingPipeline(FakePipeline):
    """El motor falso de las pruebas que, en cada análisis, anota las conexiones prestadas del pool."""

    held: ClassVar[list[int]] = []

    def analyze_frontal(self, image_bytes, **kwargs):
        self.held.append(engine.pool.checkedout())
        return super().analyze_frontal(image_bytes, **kwargs)

    def analyze_step(self, image_bytes, action, target, **kwargs):
        self.held.append(engine.pool.checkedout())
        return super().analyze_step(image_bytes, action, target, **kwargs)


@pytest.fixture
def watched(client):
    _WatchingPipeline.held = []
    app.dependency_overrides[get_pipeline] = _WatchingPipeline
    yield _WatchingPipeline.held
    app.dependency_overrides[get_pipeline] = FakePipeline


def _verify(client, headers):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", b"face:ana", "image/jpeg")) for i in range(2)] + turn_files(challenge, "ana")
    return client.post(
        "/api/verification/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


def test_verifying_an_employee_holds_no_connection_while_analyzing(client, company_headers, watched):
    approved(client, company_headers, "ana", number="EMP-001")
    watched.clear()  # el registro facial de arriba también se analizó (lo revisa la prueba del registro)
    response = _verify(client, login(client, "ana@empresa.com", "Empleado123"))
    assert response.status_code == 200 and response.json()["data"]["verified"] is True
    assert watched and set(watched) == {0}


def test_identifying_at_a_validator_holds_no_connection_while_analyzing(client, company_headers, watched):
    approved(client, company_headers, "ana", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    watched.clear()
    response = identify_face(client, headers, "ana")
    assert response.status_code == 200 and response.json()["data"]["verified"] is True
    assert watched and set(watched) == {0}


def test_enrolling_without_liveness_holds_no_connection_while_analyzing(client, company_headers, watched):
    """Sin prueba de vida no hay reto que consumir: la transacción de lo leído también se cierra antes de analizar."""
    with SessionLocal() as db:
        db.execute(update(VerificationPolicy).values(liveness_challenge=False))
        db.commit()
    clear_policy_cache()
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    files = [("images", (f"f{i}.jpg", b"face:juan", "image/jpeg")) for i in range(3)]
    assert client.post("/api/enrollment/face", files=files, headers=headers).status_code == 201
    assert watched and set(watched) == {0}


def test_the_one_to_n_of_each_verification_holds_no_connection(client, company_headers, monkeypatch):
    """Antifraude 1b: el 1:N en cada 1:1 (¿a qué otro empleado se parece?) es CPU: la galería se lee antes, en la
    transacción de lo leído, y la comparación corre sin ninguna conexión prestada."""
    from app.services import verification_service

    held: list[int] = []
    original = verification_service.rival_of

    def watched(*args, **kwargs):
        held.append(engine.pool.checkedout())
        return original(*args, **kwargs)

    monkeypatch.setattr(verification_service, "rival_of", watched)
    approved(client, company_headers, "ana", number="EMP-001")
    response = _verify(client, login(client, "ana@empresa.com", "Empleado123"))
    assert response.status_code == 200 and response.json()["data"]["verified"] is True
    assert held == [0]
