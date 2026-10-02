"""Configuración de la empresa editable desde el frontend (rol COMPANY)."""

from fastapi import APIRouter, Depends

from app.core.responses import ApiResponse, ok
from app.dependencies import CompanyScope, CompanyUser, DbSession, MemberCompany, require_screen
from app.models import Screen
from app.schemas.common import ErrorResponse
from app.schemas.policy import VerificationPolicyRead, VerificationPolicyUpdate
from app.services.policy_service import PolicyService

router = APIRouter(
    prefix="/settings",
    tags=["Configuración"],
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)


@router.get(
    "/verification",
    response_model=ApiResponse[VerificationPolicyRead],
    summary="Política de verificación vigente",
    description="La leen todos los usuarios autenticados: el empleado ve qué se le exigirá antes de escanear.",
)
def get_verification_policy(company: MemberCompany, db: DbSession) -> ApiResponse[VerificationPolicyRead]:
    return ok(PolicyService(db, company).read(), "Política de verificación", code="POLICY")


@router.put(
    "/verification",
    response_model=ApiResponse[VerificationPolicyRead],
    summary="COMPANY: actualizar la política de verificación",
    description=(
        "Activa o desactiva la exigencia de retirar lentes, gorra o cubrebocas, la prueba de vida, "
        "el anti-spoofing y la verificación por QR. Solo se modifican los campos enviados. Los cambios "
        "aplican en segundos a todos los procesos de la API."
    ),
    dependencies=[Depends(require_screen(Screen.COMPANY_SETTINGS))],
)
def update_verification_policy(
    payload: VerificationPolicyUpdate, user: CompanyUser, company: CompanyScope, db: DbSession
) -> ApiResponse[VerificationPolicyRead]:
    return ok(
        PolicyService(db, company).update(payload, user), "Política de verificación actualizada", code="POLICY_UPDATED"
    )
