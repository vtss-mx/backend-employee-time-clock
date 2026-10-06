"""Destello dictado por el servidor (antifraude fase 2a; docs/rd/antifraude-identidad.md §2.3).

Hasta la fase 1b el reto entregaba los colores del destello en claro: un programa tenía toda la vida del reto para
fabricar un fotograma teñido de cada color (prototipo P1: un tinte del 3 % pasaba el 100 %). Con
`verification_policy.flash_paced` el reto NO trae los colores: la app los pide UNO POR UNO por el canal en vivo
(`routers/realtime.py`, mensaje `flash`) y el servidor:

1. revela un color al azar (nunca el mismo dos veces seguidas) y anota el instante en que lo reveló;
2. recibe la HUELLA (SHA-256 de los bytes del JPEG) del fotograma que la app capturó con ese color y anota cuánto tardó;
3. revela el siguiente… y, con la última huella, entrega un COMPROBANTE.

La app manda el comprobante con las capturas (`flash_receipt`) y el servidor verifica que cada fotograma del destello
sea exactamente el que se comprometió y que llegó dentro de su ventana (`FACE_FLASH_PACE_WINDOW_MS`). Un atacante ya no
puede preparar los fotogramas con calma: debe producir cada uno en tiempo real, después de conocer su color.

**Sin estado en el servidor (N réplicas detrás de PgBouncer)**: la secuencia viaja en un TOKEN SELLADO (Fernet: cifrado
y autenticado con una llave derivada de cada `DATA_ENCRYPTION_KEY` solo para esto): reto, cuenta, colores revelados,
huellas, tiempos, el instante de la última revelación y el vencimiento del reto. Cualquier réplica continúa la secuencia
o verifica el comprobante sin consultar la base: ninguna escritura por color, ningún `LISTEN/NOTIFY`, ningún candado de
sesión. (La alternativa, una fila por reto con un UPDATE por color, costaba 3-6 escrituras por intento en la ruta más
caliente; los tiempos los mide la réplica que atiende el canal con su reloj de pared, sincronizado por NTP.)
Reiniciar la secuencia desde el token inicial da colores NUEVOS (se eligen al revelarse): aprender una vuelta no sirve
para la siguiente, y repetir un paso para "reiniciar el reloj" deja anotada la tardanza del paso anterior.

**Respaldo**: sin canal (un proxy que bloquea WebSocket, una red inestable) la app pide los colores de siempre
(`POST /api/face/challenge/flash`, los del token inicial) y hace el destello estático: el intento lleva la señal
FLASH_UNPACED (medida, nunca un rechazo). Límite honesto (§2.3): el cliente siempre conoce cada color al pintarlo; esto
obliga a la inyección a ser en tiempo real, no la impide.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.facial_recognition.photometry import FLASH_PALETTE, flash_hex
from app.services.face_signals import PaceOutcome, PaceVerdict
from app.services.liveness_service import Challenge

#: Huella de un fotograma: SHA-256 en hexadecimal (minúsculas).
DIGEST = re.compile(r"^[0-9a-f]{64}$")
#: Etiqueta de la llave derivada (separa este uso del cifrado de los datos).
_PURPOSE = b"flash-pacing"


def _fernet(secret: str) -> Fernet:
    digest = hmac.new(secret.encode(), _PURPOSE, hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


#: La llave vigente sella; las anteriores (DATA_ENCRYPTION_PREVIOUS_KEYS) solo abren: rotar no rompe un reto en curso.
_SEAL = MultiFernet([_fernet(key) for key in settings.data_encryption_keys])


class FlashTokenInvalid(UnprocessableError):
    """422 FLASH_TOKEN_INVALID: el token del destello venció, se alteró, es de otra cuenta o ya terminó (la app usa el
    destello de siempre o pide otro reto)."""

    def __init__(self) -> None:
        super().__init__(code="FLASH_TOKEN_INVALID")


@dataclass(frozen=True)
class PaceState:
    """El estado de una secuencia del destello dictado (lo que viaja sellado)."""

    challenge_id: str
    user_id: int
    total: int
    #: Vencimiento del reto (segundos desde 1970).
    expires: int
    colors: tuple[str, ...] = ()
    digests: tuple[str, ...] = ()
    #: Cuánto tardó cada color (ms desde que se reveló hasta que llegó su huella).
    latencies: tuple[int, ...] = ()
    #: Cuándo se reveló el color en curso (ms desde 1970); None al empezar y al terminar.
    revealed_at: int | None = None
    #: Los colores del respaldo sin canal (solo en el token inicial).
    fallback: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return len(self.digests) == self.total


def seal(state: PaceState) -> str:
    data = {
        "c": state.challenge_id,
        "u": state.user_id,
        "n": state.total,
        "x": state.expires,
        "k": list(state.colors),
        "d": list(state.digests),
        "l": list(state.latencies),
        "r": state.revealed_at,
        "f": list(state.fallback),
    }
    return _SEAL.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("lista de textos")
    return tuple(value)


def unseal(token: str, user_id: int, now_ms: int | None) -> PaceState | None:
    """El estado del token, o None si no abre, no es de esta cuenta o venció (`now_ms=None`: sin revisar el vencimiento,
    para un comprobante cuyo reto ya se consumió a tiempo)."""
    try:
        data = json.loads(_SEAL.decrypt(token.encode()))
        state = PaceState(
            challenge_id=str(data["c"]),
            user_id=int(data["u"]),
            total=int(data["n"]),
            expires=int(data["x"]),
            colors=_strings(data["k"]),
            digests=_strings(data["d"]),
            latencies=tuple(int(value) for value in data["l"]),
            revealed_at=None if data["r"] is None else int(data["r"]),
            fallback=_strings(data["f"]),
        )
    except InvalidToken, ValueError, TypeError, KeyError:
        return None
    if state.user_id != user_id or (now_ms is not None and state.expires * 1000 < now_ms):
        return None
    return state


def start_token(challenge: Challenge) -> str:
    """El token inicial que viaja con el reto: aún sin colores revelados (solo los del respaldo, sellados)."""
    return seal(
        PaceState(
            challenge_id=challenge.id,
            user_id=challenge.user_id,
            total=len(challenge.flash),
            expires=int(challenge.expires_at.timestamp()),
            fallback=challenge.flash,
        )
    )


@dataclass(frozen=True)
class PaceStep:
    """La respuesta a un mensaje del destello: el color a pintar (con su paso) o el comprobante final."""

    step: int
    total: int
    token: str
    color: str | None = None

    @property
    def done(self) -> bool:
        return self.color is None


def _reveal(state: PaceState, now_ms: int) -> PaceStep:
    """Revela el siguiente color, elegido AHORA al azar (criptográfico), distinto del anterior."""
    previous = state.colors[-1] if state.colors else None
    code = secrets.choice([color for color in FLASH_PALETTE if color != previous])
    revealed = replace(state, colors=(*state.colors, code), revealed_at=now_ms, fallback=())
    return PaceStep(step=len(revealed.colors) - 1, total=state.total, token=seal(revealed), color=flash_hex(code))


def advance(token: str, user_id: int, digest: str | None, now_ms: int) -> PaceStep:
    """Un paso de la secuencia: sin huella la empieza (primer color); con la huella del color en curso la anota con su
    tardanza y revela el siguiente, o entrega el comprobante tras el último."""
    state = unseal(token, user_id, now_ms)
    if state is None or state.done or state.total < 1:
        raise FlashTokenInvalid()
    if state.revealed_at is None:
        if digest is not None or state.colors:
            raise FlashTokenInvalid()
        return _reveal(state, now_ms)
    if digest is None or not DIGEST.match(digest):
        raise FlashTokenInvalid()
    answered = replace(
        state,
        digests=(*state.digests, digest),
        latencies=(*state.latencies, now_ms - state.revealed_at),
        revealed_at=None,
    )
    if answered.done:
        return PaceStep(step=answered.total, total=answered.total, token=seal(answered))
    return _reveal(answered, now_ms)


def fallback_colors(token: str, user_id: int, now_ms: int) -> list[str]:
    """Los colores del destello de siempre (#RRGGBB), del token inicial: para la app sin canal en vivo."""
    state = unseal(token, user_id, now_ms)
    if state is None or not state.fallback:
        raise FlashTokenInvalid()
    return [flash_hex(code) for code in state.fallback]


def verify(receipt: str | None, challenge: Challenge, user_id: int, frames: Sequence[bytes]) -> PaceOutcome:
    """¿Las capturas del destello son las que se comprometieron a tiempo? Sin comprobante, el destello no fue dictado
    (respaldo sin canal o un programa); uno que no abre, de otro reto, incompleto o con otra imagen, alterado; con
    algún color fuera de su ventana, a destiempo. Un comprobante válido da los colores que de verdad se pintaron."""
    if not receipt:
        return PaceOutcome(PaceVerdict.UNPACED)
    state = unseal(receipt, user_id, None)
    if state is None or state.challenge_id != challenge.id or not state.done or len(frames) != state.total:
        return PaceOutcome(PaceVerdict.MISMATCH, value=-1.0)
    mismatched = sum(
        not hmac.compare_digest(hashlib.sha256(frame).hexdigest(), digest)
        for frame, digest in zip(frames, state.digests, strict=True)
    )
    if mismatched:
        return PaceOutcome(PaceVerdict.MISMATCH, value=float(mismatched))
    slowest, fastest = max(state.latencies), min(state.latencies)
    window, quickest = settings.FACE_FLASH_PACE_WINDOW_MS, settings.FACE_FLASH_PACE_MIN_MS
    if slowest > window:
        verdict, value, threshold = PaceVerdict.TIMING, float(slowest), float(window)
    elif fastest < quickest:
        verdict, value, threshold = PaceVerdict.TIMING, float(fastest), float(quickest)
    else:
        verdict, value, threshold = PaceVerdict.PACED, None, None
    return PaceOutcome(verdict, colors=state.colors, slowest_ms=slowest, value=value, threshold=threshold)
