"""Casos de fraude (solo el ADMIN de la plataforma; decisión D10): la bandeja, el detalle con sus intentos, su
historial y su evidencia, y la revisión que alimenta la lista de bloqueo y la calibración (docs/rd §3)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.fraud import (
    FraudCaseCount,
    FraudCaseDecision,
    FraudCaseDetail,
    FraudCaseList,
    FraudCaseNote,
    FraudEvidenceImage,
)
from app.services.fraud_case_service import FraudCaseService

router = APIRouter(
    prefix="/admin/fraud-cases",
    tags=["Casos de fraude (ADMIN)"],
    dependencies=[Depends(require_screen(Screen.ADMIN_FRAUD_CASES))],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el ADMIN de la plataforma"},
    },
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Caso o evidencia no encontrados"}
}


@router.get(
    "",
    response_model=ApiResponse[FraudCaseList],
    summary="Bandeja de casos (por omisión, los que esperan revisión)",
    description="`status`: un estado del catálogo `fraud_case_statuses` o `ALL`; sin él, abiertos y en revisión.",
)
def list_cases(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    case_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    company_id: Annotated[int | None, Query(gt=0)] = None,
    kind: Annotated[str | None, Query(max_length=30)] = None,
) -> ApiResponse[FraudCaseList]:
    result = FraudCaseService(db).search(status=case_status, company_id=company_id, kind=kind, page=page)
    return ok(result, code="FRAUD_CASES", params={"count": result.total})


@router.get("/count", response_model=ApiResponse[FraudCaseCount], summary="Casos por revisar (contador del menú)")
def count_cases(_: AdminUser, db: DbSession) -> ApiResponse[FraudCaseCount]:
    return ok(FraudCaseCount(active=FraudCaseService(db).active_count()), code="FRAUD_CASES_COUNT")


@router.get(
    "/{case_id}",
    response_model=ApiResponse[FraudCaseDetail],
    summary="Un caso con sus intentos (señales y números), su historial y su evidencia",
    responses=NOT_FOUND,
)
def case_detail(case_id: int, _: AdminUser, db: DbSession) -> ApiResponse[FraudCaseDetail]:
    return ok(FraudCaseService(db).detail(case_id), code="FRAUD_CASE")


@router.post(
    "/{case_id}/decision",
    response_model=ApiResponse[FraudCaseDetail],
    summary="Revisar el caso: tomarlo, confirmar el fraude, descartarlo (falso positivo) o dejarlo no concluyente",
    description=(
        "Confirmar bloquea sus huellas (empresa y, si ya se confirmaron en varias, plataforma), etiqueta sus "
        "intentos como fraude (la calibración deja de contarlos) y olvida lo que el reconocimiento aprendió del "
        "empleado desde el intento. Falso positivo libera sus huellas y suma a la línea base de sus señales. "
        "Confirmar o descartar exige una nota (422 `FRAUD_NOTE_REQUIRED`)."
    ),
    responses={**NOT_FOUND, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def decide_case(
    case_id: int, payload: FraudCaseDecision, user: AdminUser, db: DbSession
) -> ApiResponse[FraudCaseDetail]:
    result = FraudCaseService(db).decide(case_id, payload, user)
    return ok(result, code="FRAUD_CASE_DECIDED")


@router.post(
    "/{case_id}/notes",
    response_model=ApiResponse[FraudCaseDetail],
    summary="Agregar una nota al historial del caso",
    responses={**NOT_FOUND, 422: {"model": ErrorResponse}},
)
def add_note(case_id: int, payload: FraudCaseNote, user: AdminUser, db: DbSession) -> ApiResponse[FraudCaseDetail]:
    return ok(FraudCaseService(db).add_note(case_id, payload.note, user), code="FRAUD_CASE_NOTE")


@router.get(
    "/{case_id}/evidence/{evidence_id}",
    response_model=ApiResponse[FraudEvidenceImage],
    summary="Un fotograma de evidencia (descifrado; nunca una URL del bucket)",
    description=(
        'Excepción documentada a "el ADMIN no ve biometría" (decisión D1): solo de casos de fraude, solo por la '
        "API y cada consulta queda en el historial del caso. 404 si ya venció; 503 `STORAGE_UNAVAILABLE` si el "
        "bucket no responde."
    ),
    responses={**NOT_FOUND, 503: {"model": ErrorResponse}},
)
def case_evidence(case_id: int, evidence_id: int, user: AdminUser, db: DbSession) -> ApiResponse[FraudEvidenceImage]:
    return ok(FraudCaseService(db).evidence(case_id, evidence_id, user), code="FRAUD_EVIDENCE")
