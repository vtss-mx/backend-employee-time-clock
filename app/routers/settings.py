"""Política de verificación de identidad vigente, para leerla desde cualquier sesión.

La configura el ADMIN de la plataforma para cada empresa (`/admin/companies/{id}/verification-policy`);
aquí la leen la empresa, sus empleados y sus validadores para saber qué se les exigirá antes de
escanear. Quién la cambió no se expone fuera de la consola de la plataforma.
"""

from fastapi import APIRouter

from app.core.responses import ApiResponse, ok
from app.dependencies import DbSession, MemberCompany
from app.schemas.common import ErrorResponse
from app.schemas.policy import VerificationPolicyRead
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
    return ok(PolicyService(db, company).read(), code="POLICY")
