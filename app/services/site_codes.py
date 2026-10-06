"""Código de sitio: presencia física con un código rotativo (antifraude fase 2b, decisión D9 del dueño del producto).

Riesgo que ataca (docs/rd/antifraude-identidad.md R1 y §2.10): la ubicación la informa el navegador y se puede simular;
el rostro es real, así que ningún candado facial lo detecta. Un sitio puede activar su código (apagado por omisión: la
empresa lo enciende sitio por sitio, `work_sites.presence_code`): una tableta en el sitio (su kiosco, `kiosk_service`)
muestra un código de 6 dígitos y su QR que cambian cada `SITE_CODE_PERIOD_SECONDS`, y la entrada y la salida EN ese
sitio llevan el código vigente (escaneado con la cámara o escrito).

Cómo se calcula (como TOTP, RFC 6238, con HMAC-SHA256): cada sitio tiene un secreto de 32 bytes al azar guardado CIFRADO
con `DATA_ENCRYPTION_KEY` (Fernet; nunca sale del servidor ni se escribe en un log) y el código de un periodo es el
truncamiento dinámico de HMAC(secreto, número del periodo). Sin estado: cualquier réplica lo calcula igual.

Cómo se verifica (`check`):
- tolerancia de reloj: vale el periodo actual, los `SITE_CODE_GRACE_WINDOWS` anteriores (el tiempo de leerlo y
  escribirlo) y el siguiente (la diferencia de reloj entre réplicas);
- un solo uso por empleado y periodo: el periodo con que se confirmó queda en el registro (`attendance_events
  .presence_window`, nunca el código) y el siguiente registro del empleado en ese sitio no puede repetirlo (se compara
  con el registro anterior que la asistencia YA leyó para el viaje imposible: cero consultas);
- nunca de otro sitio: el secreto es de cada sitio y el QR dice de qué sitio es (`TC-SITE:{sitio}:{código}`).

Política (`verification_policy.site_codes`, modos de `signal_modes`): apagado (no se pide), «Solo medir» (por omisión:
sin código o con uno que no vale es una señal del motor de riesgo, `SITE_CODE_MISSING` / `SITE_CODE_INVALID`, nunca un
rechazo) u obligatorio (403 `SITE_CODE_REQUIRED` / `SITE_CODE_INVALID` o 409 `SITE_CODE_USED`, ANTES de usar el reto
de la prueba de vida: la app pide el código y vuelve a enviar). Solo cuenta en la entrada y la salida EN un sitio con
código (un registro remoto o en otro sitio no lo lleva).

Límite honesto: el código prueba que alguien vio la tableta en el último minuto; quien se lo dicta en ese momento a un
compañero que no está lo rodea (para eso siguen la geocerca, las señales del lugar y el validador con QR + rostro).
"""

import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import settings
from app.core.crypto import encrypt_bytes, try_decrypt
from app.core.exceptions import ConflictError, PermissionDeniedError
from app.models import AttendanceAction, AttendanceEvent, RiskSignal, SignalMode, WorkSite
from app.services.risk_rules import Hit

logger = logging.getLogger(__name__)

#: Dígitos del código: parte del protocolo (la app y el kiosco los muestran y los piden así), como en TOTP.
SITE_CODE_DIGITS = 6
#: Lo que lleva el QR del kiosco: de qué sitio es y su código.
QR_PREFIX = "TC-SITE"
_QR = re.compile(rf"^{QR_PREFIX}:(\d{{1,10}}):(\d{{{SITE_CODE_DIGITS}}})$")
_TYPED = re.compile(rf"^\d{{{SITE_CODE_DIGITS}}}$")
#: Las acciones que confirman presencia: llegar y salir del sitio (los descansos no).
PRESENCE_ACTIONS = frozenset({AttendanceAction.CHECK_IN, AttendanceAction.CHECK_OUT})
#: Lo que vale el código cuando no coincide (en la señal): equivocado o vencido, ya usado, de otro sitio.
WRONG, USED, OTHER_SITE = 1.0, 2.0, 3.0


def new_secret() -> str:
    """El secreto de un sitio que activa su código: 32 bytes al azar, cifrados (texto Fernet)."""
    return encrypt_bytes(secrets.token_bytes(32)).decode()


def secret_of(site: WorkSite) -> bytes | None:
    """El secreto descifrado; None si no tiene o ya no se puede leer (una llave retirada de DATA_ENCRYPTION_KEY): quien
    llama registra la falla una vez a su manera (la asistencia, en el log de errores; el kiosco, con su 503)."""
    return try_decrypt(site.presence_secret.encode()) if site.presence_secret else None


def window_at(epoch_seconds: float) -> int:
    """El número del periodo de un instante."""
    return int(epoch_seconds // settings.SITE_CODE_PERIOD_SECONDS)


def code_for(secret: bytes, window: int) -> str:
    """El código de un periodo: truncamiento dinámico de HMAC-SHA256 (RFC 4226 §5.3) a `SITE_CODE_DIGITS` dígitos."""
    digest = hmac.new(secret, window.to_bytes(8, "big"), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(number % 10**SITE_CODE_DIGITS).zfill(SITE_CODE_DIGITS)


@dataclass(frozen=True)
class CurrentCode:
    """Lo que muestra el kiosco: el código vigente, su QR y cuántos segundos le quedan."""

    code: str
    qr: str
    expires_in: int


def current(site: WorkSite, secret: bytes, now: float | None = None) -> CurrentCode:
    moment = time.time() if now is None else now
    period = settings.SITE_CODE_PERIOD_SECONDS
    code = code_for(secret, window_at(moment))
    return CurrentCode(code, f"{QR_PREFIX}:{site.id}:{code}", max(1, int(period - moment % period)))


class CodeVerdict(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    INVALID = "INVALID"
    USED = "USED"


@dataclass(frozen=True)
class SiteCodeCheck:
    """El código de un registro: el periodo con que se confirmó (None si no se confirmó) y su señal (si la hay)."""

    window: int | None = None
    hits: tuple[Hit, ...] = ()


def _parse(raw: str) -> tuple[int | None, str | None]:
    """(sitio del QR o None si se escribió, dígitos) o (None, None) si no tiene la forma de un código."""
    text = raw.strip()
    if match := _QR.match(text):
        return int(match.group(1)), match.group(2)
    if _TYPED.match(text):
        return None, text
    return None, None


def _matched(secret: bytes, digits: str, now: float) -> int | None:
    """El periodo cuyo código es `digits` (actual, anteriores en gracia o el siguiente), o None."""
    window = window_at(now)
    for candidate in range(window + 1, window - settings.SITE_CODE_GRACE_WINDOWS - 1, -1):
        if hmac.compare_digest(code_for(secret, candidate), digits):
            return candidate
    return None


def _verdict(
    site: WorkSite, secret: bytes, raw: str | None, previous: AttendanceEvent | None, now: float
) -> tuple[CodeVerdict, float | None, int | None]:
    if not raw:
        return CodeVerdict.MISSING, None, None
    site_id, digits = _parse(raw)
    if digits is None:
        return CodeVerdict.INVALID, WRONG, None
    if site_id is not None and site_id != site.id:
        return CodeVerdict.INVALID, OTHER_SITE, None
    window = _matched(secret, digits, now)
    if window is None:
        return CodeVerdict.INVALID, WRONG, None
    if previous is not None and previous.site_id == site.id and previous.presence_window == window:
        return CodeVerdict.USED, USED, window
    return CodeVerdict.VALID, None, window


def required(site: WorkSite, action: AttendanceAction, mode: str) -> bool:
    """El registro lleva el código: entrada o salida EN un sitio que lo activó, con la política encendida."""
    return mode != SignalMode.OFF and site.presence_code and action in PRESENCE_ACTIONS


def check(
    site: WorkSite | None,
    action: AttendanceAction,
    raw: str | None,
    *,
    previous: AttendanceEvent | None,
    mode: str,
    now: float | None = None,
) -> SiteCodeCheck:
    """El código de un registro de asistencia (ver el módulo). Con la política obligatoria, lo que no vale se rechaza
    aquí; con «Solo medir», es una señal del motor de riesgo."""
    if site is None or not required(site, action, mode):
        return SiteCodeCheck()
    secret = secret_of(site)
    if secret is None:
        # Degradar en vez de caer: sin su secreto el sitio queda sin código (nadie se queda sin checar) y se registra.
        logger.error("No se pudo leer el secreto del código del sitio %s (¿cambió DATA_ENCRYPTION_KEY?)", site.id)
        return SiteCodeCheck()
    verdict, value, window = _verdict(site, secret, raw, previous, time.time() if now is None else now)
    if verdict == CodeVerdict.VALID:
        return SiteCodeCheck(window)
    if mode == SignalMode.ENFORCE:
        raise _rejection(verdict, site)
    if verdict == CodeVerdict.MISSING:
        return SiteCodeCheck(hits=(Hit(RiskSignal.SITE_CODE_MISSING),))
    return SiteCodeCheck(hits=(Hit(RiskSignal.SITE_CODE_INVALID, value),))


def _rejection(verdict: CodeVerdict, site: WorkSite) -> PermissionDeniedError | ConflictError:
    if verdict == CodeVerdict.MISSING:
        return PermissionDeniedError(
            code="SITE_CODE_REQUIRED", params={"site": site.name}, details={"site": site.name, "site_id": site.id}
        )
    if verdict == CodeVerdict.USED:
        return ConflictError(code="SITE_CODE_USED", details={"site_id": site.id})
    return PermissionDeniedError(code="SITE_CODE_INVALID", details={"site_id": site.id})
