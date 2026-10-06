"""Cobranza de las empresas (solo rol ADMIN, pantalla "Cobranza"): plan de cobro, vista previa, estimación
del periodo en curso, cargos, pagos registrados a mano (con su comprobante), estado de cuenta,
suspensión y reactivación, y el resumen de la plataforma.

El plan y su estimación también los usa la ficha de la empresa (pantalla "Empresas": el alta y la
edición llevan el plan). Todo lo demás es de la pantalla "Cobranza".
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.core.config import settings
from app.core.exceptions import PayloadTooLargeError, UnprocessableError
from app.core.responses import ApiResponse, ok
from app.dependencies import AdminUser, DbSession, Pagination, require_screen
from app.i18n import Megabytes
from app.models import ChargeStatus, PaymentStatus, Screen
from app.schemas.billing import (
    CURRENCY_CODE,
    MAX_PAYMENT,
    BillingAccount,
    BillingOverview,
    BillingPlanIn,
    ChargeDetail,
    ChargeList,
    ChargeVoidResult,
    CompanyBillingList,
    PaymentList,
    PaymentResult,
    PaymentVoidResult,
    PeriodEstimate,
    PlanPreview,
    PlanPreviewIn,
    ReactivateIn,
    ReasonIn,
    ReceiptFile,
    StatementList,
)
from app.schemas.common import ErrorResponse
from app.services.billing_service import BillingService, PaymentInput, Receipt

router = APIRouter(
    prefix="/admin/billing",
    tags=["Cobranza (ADMIN)"],
    responses={
        401: {"model": ErrorResponse, "description": "No autenticado"},
        403: {"model": ErrorResponse, "description": "Solo el administrador de la plataforma"},
    },
)
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Empresa (o cargo/pago) no encontrado"}
}
BILLING = [Depends(require_screen(Screen.ADMIN_BILLING))]
#: El plan y su estimación también los usa la ficha de la empresa (alta y edición con su plan).
PLAN = [Depends(require_screen(Screen.ADMIN_BILLING, Screen.ADMIN_COMPANIES))]

#: Comprobantes aceptados, reconocidos por su CONTENIDO (no por lo que declara el navegador).
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def receipt_type(data: bytes) -> str | None:
    """Tipo real del comprobante por sus primeros bytes (PDF, JPEG, PNG o WEBP); None si no es ninguno."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((kind for signature, kind in _SIGNATURES if data.startswith(signature)), None)


def read_receipt(receipt: UploadFile | None) -> Receipt | None:
    """El comprobante del pago (opcional): a lo más BILLING_RECEIPT_MAX_MB y solo PDF o imagen."""
    if receipt is None:
        return None
    limit = int(settings.BILLING_RECEIPT_MAX_MB * 1024 * 1024)
    data = receipt.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(code="RECEIPT_TOO_LARGE", params={"size": Megabytes(limit)})
    kind = receipt_type(data)
    if kind is None:
        raise UnprocessableError(
            code="RECEIPT_TYPE_NOT_ALLOWED",
            field="receipt",
        )
    name = (receipt.filename or "comprobante").replace("\\", "/").rsplit("/", 1)[-1][:200] or "comprobante"
    return Receipt(file_name=name, content_type=kind, data=data)


@router.post(
    "/preview",
    response_model=ApiResponse[PlanPreview],
    summary="Vista previa del cobro de un plan",
    description=(
        "Lo que costará el plan con N empleados y M validadores activos (cada validador cuenta como un empleado): "
        "el primer cargo (si el primer periodo no es todo demo) y un periodo completo después de la demo, con "
        "prorrateo, descuento e IVA. No guarda nada."
    ),
    dependencies=PLAN,
)
def preview_plan(payload: PlanPreviewIn, _: AdminUser) -> ApiResponse[PlanPreview]:
    return ok(BillingService.preview(payload.plan, payload.employees, payload.validators), code="BILLING_PREVIEW")


@router.get(
    "/overview",
    response_model=ApiResponse[BillingOverview],
    summary="Resumen de la cobranza de la plataforma",
    dependencies=BILLING,
)
def billing_overview(_: AdminUser, db: DbSession) -> ApiResponse[BillingOverview]:
    return ok(BillingService(db).overview(), code="BILLING_OVERVIEW")


@router.get(
    "/companies",
    response_model=ApiResponse[CompanyBillingList],
    summary="Empresas con su saldo (la de cargo vencido más antiguo primero)",
    dependencies=BILLING,
)
def billing_companies(
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    search: Annotated[
        str | None, Query(max_length=100, description="Nombre, razón social o identificador fiscal")
    ] = None,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(ACTIVE|SUSPENDED)$")] = None,
    overdue: Annotated[bool | None, Query(description="Solo con (o sin) cargos vencidos")] = None,
) -> ApiResponse[CompanyBillingList]:
    result = BillingService(db).companies(search=search, status=status_filter, overdue=overdue, page=page)
    return ok(result, code="BILLING_COMPANIES", params={"count": result.total})


@router.get(
    "/companies/{company_id}",
    response_model=ApiResponse[BillingAccount],
    summary="Plan, estado de servicio y saldo de una empresa",
    responses=NOT_FOUND,
    dependencies=PLAN,
)
def billing_account(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[BillingAccount]:
    return ok(BillingService(db).account(company_id), code="BILLING_ACCOUNT")


@router.put(
    "/companies/{company_id}/plan",
    response_model=ApiResponse[BillingAccount],
    summary="Crear o cambiar el plan de cobro (aplica desde el próximo cargo)",
    description="Los cargos ya emitidos no cambian y un periodo ya cortado nunca se vuelve a cobrar.",
    responses=NOT_FOUND,
    dependencies=PLAN,
)
def save_plan(company_id: int, payload: BillingPlanIn, user: AdminUser, db: DbSession) -> ApiResponse[BillingAccount]:
    service = BillingService(db)
    service.save_plan(company_id, payload, user.email)
    return ok(service.account(company_id), code="BILLING_PLAN_SAVED")


@router.get(
    "/companies/{company_id}/estimate",
    response_model=ApiResponse[PeriodEstimate],
    summary="Estimación en vivo del periodo en curso",
    description="Lo devengado hasta hoy y el pronóstico del cargo del periodo con la plantilla de hoy. No guarda nada.",
    responses={404: {"model": ErrorResponse, "description": "Empresa no encontrada o sin plan"}},
    dependencies=PLAN,
)
def billing_estimate(company_id: int, _: AdminUser, db: DbSession) -> ApiResponse[PeriodEstimate]:
    return ok(BillingService(db).estimate(company_id), code="BILLING_ESTIMATE")


@router.get(
    "/companies/{company_id}/charges",
    response_model=ApiResponse[ChargeList],
    summary="Cargos de una empresa (el corte más reciente primero)",
    responses=NOT_FOUND,
    dependencies=BILLING,
)
def list_charges(
    company_id: int,
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    status_filter: Annotated[ChargeStatus | None, Query(alias="status")] = None,
) -> ApiResponse[ChargeList]:
    result = BillingService(db).charges(company_id, status_filter, page)
    return ok(result, code="CHARGES_LISTED", params={"count": result.total})


@router.get(
    "/companies/{company_id}/charges/{charge_id}",
    response_model=ApiResponse[ChargeDetail],
    summary="Detalle de un cargo: líneas por mes y pagos aplicados",
    responses=NOT_FOUND,
    dependencies=BILLING,
)
def get_charge(company_id: int, charge_id: int, _: AdminUser, db: DbSession) -> ApiResponse[ChargeDetail]:
    return ok(BillingService(db).charge(company_id, charge_id), code="CHARGE_FOUND")


@router.post(
    "/companies/{company_id}/charges/{charge_id}/void",
    response_model=ApiResponse[ChargeVoidResult],
    summary="Anular un cargo (con motivo): lo aplicado queda a favor",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "El cargo ya estaba anulado"}},
    dependencies=BILLING,
)
def void_charge(
    company_id: int, charge_id: int, payload: ReasonIn, user: AdminUser, db: DbSession
) -> ApiResponse[ChargeVoidResult]:
    result = BillingService(db).void_charge(company_id, charge_id, payload.reason, user.email)
    return ok(result, code="CHARGE_VOIDED")


@router.get(
    "/companies/{company_id}/payments",
    response_model=ApiResponse[PaymentList],
    summary="Pagos de una empresa (el más reciente primero)",
    responses=NOT_FOUND,
    dependencies=BILLING,
)
def list_payments(
    company_id: int,
    _: AdminUser,
    db: DbSession,
    page: Pagination,
    status_filter: Annotated[PaymentStatus | None, Query(alias="status")] = None,
) -> ApiResponse[PaymentList]:
    result = BillingService(db).payments(company_id, status_filter, page)
    return ok(result, code="PAYMENTS_LISTED", params={"count": result.total})


@router.post(
    "/companies/{company_id}/payments",
    response_model=ApiResponse[PaymentResult],
    status_code=status.HTTP_201_CREATED,
    summary="Registrar un pago confirmado (con su comprobante opcional)",
    description=(
        "Se aplica al cargo abierto más antiguo y lo que sobra queda a favor. Si la empresa estaba suspendida por "
        "falta de pago y ya no debe nada vencido, se reactiva. Comprobante: PDF o imagen (JPG, PNG, WEBP). El pago "
        "va en la moneda de la empresa (`currency`): otra responde 422 CURRENCY_MISMATCH."
    ),
    responses={**NOT_FOUND, 413: {"model": ErrorResponse, "description": "Comprobante demasiado grande"}},
    dependencies=BILLING,
)
def register_payment(
    company_id: int,
    user: AdminUser,
    db: DbSession,
    amount: Annotated[Decimal, Form(gt=0, le=MAX_PAYMENT, max_digits=14, decimal_places=2)],
    paid_on: Annotated[date, Form(description="Fecha en que se recibió el pago")],
    method: Annotated[str, Form(min_length=1, max_length=20, description="Forma de pago (catálogo payment_methods)")],
    currency: Annotated[
        str, Form(pattern=CURRENCY_CODE, description="Moneda del pago: la de la empresa (catálogo currencies)")
    ],
    reference: Annotated[str | None, Form(max_length=120)] = None,
    note: Annotated[str | None, Form(max_length=300)] = None,
    receipt: Annotated[UploadFile | None, File(description="Comprobante (PDF o imagen)")] = None,
) -> ApiResponse[PaymentResult]:
    data = PaymentInput(
        amount=amount,
        paid_on=paid_on,
        method=method,
        reference=(reference or "").strip() or None,
        note=(note or "").strip() or None,
        currency=currency,
    )
    result = BillingService(db).register_payment(company_id, data, read_receipt(receipt), user.email)
    return ok(result, code="PAYMENT_REGISTERED", status_code=201)


@router.get(
    "/companies/{company_id}/payments/{payment_id}/receipt",
    response_model=ApiResponse[ReceiptFile],
    summary="Comprobante de un pago (en base64)",
    responses=NOT_FOUND,
    dependencies=BILLING,
)
def payment_receipt(company_id: int, payment_id: int, _: AdminUser, db: DbSession) -> ApiResponse[ReceiptFile]:
    return ok(BillingService(db).receipt(company_id, payment_id), code="PAYMENT_RECEIPT")


@router.post(
    "/companies/{company_id}/payments/{payment_id}/void",
    response_model=ApiResponse[PaymentVoidResult],
    summary="Anular un pago (con motivo): sus cargos vuelven a quedar por pagar",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "El pago ya estaba anulado"}},
    dependencies=BILLING,
)
def void_payment(
    company_id: int, payment_id: int, payload: ReasonIn, user: AdminUser, db: DbSession
) -> ApiResponse[PaymentVoidResult]:
    result = BillingService(db).void_payment(company_id, payment_id, payload.reason, user.email)
    return ok(result, code="PAYMENT_VOIDED")


@router.post(
    "/companies/{company_id}/suspend",
    response_model=ApiResponse[BillingAccount],
    summary="Suspender a la empresa (cierra al momento todas sus sesiones)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "Ya estaba suspendida"}},
    dependencies=BILLING,
)
def suspend_company(company_id: int, payload: ReasonIn, user: AdminUser, db: DbSession) -> ApiResponse[BillingAccount]:
    account = BillingService(db).suspend(company_id, payload.reason, user.email)
    return ok(account, code="COMPANY_SUSPENDED_BY_ADMIN")


@router.post(
    "/companies/{company_id}/reactivate",
    response_model=ApiResponse[BillingAccount],
    summary="Reactivar a la empresa (el acceso vuelve al momento)",
    responses={**NOT_FOUND, 409: {"model": ErrorResponse, "description": "No estaba suspendida"}},
    dependencies=BILLING,
)
def reactivate_company(
    company_id: int, payload: ReactivateIn, user: AdminUser, db: DbSession
) -> ApiResponse[BillingAccount]:
    account = BillingService(db).reactivate(company_id, payload.note, user.email)
    return ok(account, code="COMPANY_REACTIVATED")


@router.get(
    "/companies/{company_id}/statement",
    response_model=ApiResponse[StatementList],
    summary="Estado de cuenta (el movimiento más reciente primero)",
    responses=NOT_FOUND,
    dependencies=BILLING,
)
def billing_statement(company_id: int, _: AdminUser, db: DbSession, page: Pagination) -> ApiResponse[StatementList]:
    result = BillingService(db).statement(company_id, page)
    return ok(result, code="BILLING_STATEMENT", params={"count": result.total})
