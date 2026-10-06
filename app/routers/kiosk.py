"""La tableta del kiosco de un sitio (antifraude 2b, decisión D9): vincularse y mostrar el código rotativo del sitio.

Públicas a propósito (lista `PUBLIC` de `tests/test_authorization.py`): la tableta no tiene sesión ni cuenta; su
credencial es la llave NO exportable de su dispositivo, probada con una firma en cada petición (`kiosk_service`), y el
canje de la vinculación usa el código de un solo uso que la empresa le dio. Una sesión de la app no les da nada.
Limitadas por IP (`RATE_LIMIT_KIOSK_PER_MINUTE`) antes de leer el cuerpo.
"""

from fastapi import APIRouter, Depends, Request

from app.core.config import settings
from app.core.responses import ApiResponse, ok
from app.dependencies import DbSession, request_meta
from app.middleware.rate_limit import ip_rate_limit
from app.schemas.common import ErrorResponse
from app.schemas.kiosk import KioskCode, KioskCodeIn, KioskPairIn, KioskSession
from app.services.kiosk_service import KioskDevice

router = APIRouter(
    prefix="/kiosk",
    tags=["Kiosco del sitio (público, con la llave de su tableta)"],
    dependencies=[Depends(ip_rate_limit("kiosk", lambda: settings.RATE_LIMIT_KIOSK_PER_MINUTE))],
    responses={429: {"model": ErrorResponse}},
)


@router.post(
    "/pair",
    response_model=ApiResponse[KioskSession],
    summary="Vincular la tableta con el código de vinculación del kiosco",
    description=(
        "La tableta manda el código de un solo uso que recibió la empresa y la llave pública de su dispositivo (SPKI "
        "DER, base64). Responde de qué kiosco y sitio es y el reto que firma su primera petición del código."
    ),
    responses={422: {"model": ErrorResponse, "description": "`KIOSK_PAIRING_INVALID` o `DEVICE_KEY_INVALID`"}},
)
def pair(body: KioskPairIn, request: Request, db: DbSession) -> ApiResponse[KioskSession]:
    _, user_agent = request_meta(request)
    return ok(KioskDevice(db).pair(body, user_agent), code="KIOSK_PAIRED")


@router.post(
    "/code",
    response_model=ApiResponse[KioskCode],
    summary="El código vigente del sitio (firmado por la llave de la tableta)",
    description=(
        'La firma ECDSA P-256 (r||s, base64) es de `"{nonce}.kiosk.{kiosk_id}"`. Sin reto o con uno vencido: 403 '
        "`KIOSK_PROOF_REQUIRED` con uno nuevo en `details.nonce` (firmarlo y reintentar). Cada respuesta trae el "
        "siguiente (`device_nonce`) y cuántos segundos le quedan al código (`expires_in`)."
    ),
    responses={
        403: {"model": ErrorResponse, "description": "`KIOSK_PROOF_REQUIRED` o `KIOSK_PROOF_INVALID`"},
        404: {"model": ErrorResponse, "description": "`KIOSK_NOT_FOUND` (eliminado o sin vincular)"},
        409: {"model": ErrorResponse, "description": "`SITE_CODE_DISABLED` (el sitio apagó su código)"},
        503: {"model": ErrorResponse, "description": "`SITE_CODE_UNAVAILABLE`"},
    },
)
def code(body: KioskCodeIn, db: DbSession) -> ApiResponse[KioskCode]:
    return ok(KioskDevice(db).code(body), code="KIOSK_CODE")
