"""Retos de prueba de vida (liveness) emitidos por el servidor.

Flujo:
1. Quien opera la cámara solicita un reto: el servidor elige al azar de uno a tres movimientos
   (girar a la izquierda o a la derecha, mirar arriba o abajo, acercarse; nunca el mismo dos veces
   seguidas ni "acercarse" como único movimiento: una foto acercada a la cámara crece igual que un
   rostro) y, si la empresa lo usa, una secuencia de colores para el destello de la pantalla. El reto
   de "un paso más" (riesgo medio del motor de riesgo) pide el máximo de movimientos y el destello
   aunque la empresa solo lo mida. El reto del REGISTRO facial (propio o en persona) pide SIEMPRE los
   cuatro movimientos de la cabeza (`ENROLLMENT_ACTIONS`: derecha, izquierda, arriba y abajo, en orden
   al azar; decisión del dueño, 2026-10-07): la persona vuelve al frente entre uno y otro y termina
   centrada; "acercarse" no forma parte (una foto plana crece igual que un rostro).
2. El cliente captura frames frontales, uno por cada color del destello y uno por cada movimiento,
   en orden (entre movimientos la persona vuelve al frente).
3. Al verificar, el reto se consume (uso único), debe pertenecer al usuario y no haber vencido (la
   empresa decide cuánto dura). Un video o una foto preparados de antemano no conocen la secuencia,
   y un video inyectado no puede reflejar en el rostro colores que no conocía.

Almacenamiento en PostgreSQL (tabla `face_challenges`): compartido entre todos los procesos
de la API, por lo que se puede escalar horizontalmente sin Redis.

Dueño del reto (`ChallengeOwner`): una CUENTA (su id: el empleado, el validador o la empresa que opera la cámara) o, en
la API pública de verificación (SDK móviles, migración 0084), un DISPOSITIVO de una llave de la empresa (`ApiDevice`).
Uno vigente por dueño: el reto nuevo de una cuenta reemplaza el anterior de esa cuenta, y el de un dispositivo, el de
ese dispositivo (muchos teléfonos comparten la llave de la empresa: ninguno invalida el reto de otro).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.clock import as_utc, has_passed
from app.facial_recognition import LivenessAction
from app.facial_recognition.photometry import FLASH_PALETTE
from app.models import FaceChallenge
from app.repositories.challenge_repository import FaceChallengeRepository
from app.services.catalog_service import get_catalogs
from app.services.face_service import face_rejection

if TYPE_CHECKING:
    from app.services.policy_service import PolicySnapshot

#: Movimientos que puede pedir el reto de una VERIFICACIÓN (verification_policy.liveness_steps, 1 a 3).
MAX_STEPS = 3
#: Los movimientos de cabeza y su interruptor en la política (decisión del dueño, 2026-10-08: cada empresa elige
#: cuáles pide; voltear arriba/abajo se le dificulta a algunas personas). El REGISTRO pide TODOS los que la empresa
#: dejó activos, en orden al azar; la verificación elige `liveness_steps` de ese repertorio.
#: Reemplaza la decisión del 2026-10-07 («siempre los cuatro»).
HEAD_ACTIONS: tuple[tuple[LivenessAction, str], ...] = (
    (LivenessAction.TURN_RIGHT, "enable_turn_right"),
    (LivenessAction.TURN_LEFT, "enable_turn_left"),
    (LivenessAction.LOOK_UP, "enable_look_up"),
    (LivenessAction.LOOK_DOWN, "enable_look_down"),
)
#: Movimientos que caben en un reto (los cuatro de cabeza): columnas de `face_challenges` y capturas que se aceptan.
MAX_CHALLENGE_STEPS = max(MAX_STEPS, len(HEAD_ACTIONS))
#: Sin movimientos activos (configuración rota) se piden los giros.
FALLBACK_ACTIONS = (LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT)


@dataclass(frozen=True)
class ApiDevice:
    """Quien opera la cámara por la API pública de verificación: la llave de la empresa (`key_id`) en UN dispositivo
    (`device_hash`, la huella SHA-256 de la llave pública que el dispositivo probó con su firma)."""

    key_id: int
    device_hash: str


#: Dueño de un reto: el id de una cuenta o un dispositivo de la API.
type ChallengeOwner = int | ApiDevice


def user_of(owner: ChallengeOwner) -> int | None:
    """La cuenta que opera la cámara (None si es un dispositivo de la API)."""
    return owner if isinstance(owner, int) else None


def device_of(owner: ChallengeOwner) -> str | None:
    """La huella de la llave del dispositivo de la API (None si es una cuenta)."""
    return owner.device_hash if isinstance(owner, ApiDevice) else None


@dataclass(frozen=True)
class LivenessResponse:
    """Lo que la app envía como respuesta al reto (en el mismo formulario que las frontales)."""

    challenge_id: str | None = None
    #: Una captura por movimiento del reto, en orden.
    steps: tuple[bytes, ...] = ()
    #: Una captura por color del destello, en orden.
    flash: tuple[bytes, ...] = ()
    #: Antifraude 2a: la hoja de la ráfaga de recortes del rostro y su descripción (JSON, `schemas/capture.BurstMeta`),
    #: si llegó más grande de lo permitido (no se leyó: es una señal, nunca un 413) y el comprobante del destello
    #: dictado por el servidor (`flash_pacing`). Lo mal formado se mide; nunca rompe el intento.
    burst: bytes | None = None
    burst_meta: str | None = None
    burst_oversize: bool = False
    flash_receipt: str | None = None


#: Sin respuesta al reto (la empresa no exige prueba de vida).
NO_RESPONSE = LivenessResponse()


@dataclass(frozen=True)
class Challenge:
    id: str
    #: La cuenta dueña del reto; None si es de un dispositivo de la API (`api_device`).
    user_id: int | None
    #: Movimientos en orden (de uno a tres en una verificación; los cuatro del registro).
    actions: tuple[LivenessAction, ...]
    #: Cuándo se emitió (para exigir el tiempo humano mínimo de respuesta).
    issued_at: datetime
    expires_at: datetime
    #: Colores del destello en orden (códigos de FLASH_PALETTE); vacío sin destello.
    flash: tuple[str, ...] = ()
    #: Reto de "un paso más": su destello es obligatorio aunque la empresa solo lo mida.
    step_up: bool = False
    #: La empresa estaba reforzada por ataques al emitirlo (señal del motor de riesgo).
    reinforced: bool = False
    #: Destello dictado por el servidor (antifraude 2a): sus colores no viajaron con el reto; `flash` es el respaldo.
    flash_paced: bool = False
    #: El dispositivo de la API dueño del reto (None si es de una cuenta).
    api_device: ApiDevice | None = None


def random_sequence[T](options: Sequence[T], count: int) -> tuple[T, ...]:
    """`count` elementos al azar (criptográfico), nunca el mismo dos veces seguidas si hay de dónde
    elegir: un paso repetido se podría "cumplir" sin moverse entre uno y otro."""
    chosen: list[T] = []
    for _ in range(count):
        pool = [o for o in options if not chosen or o != chosen[-1]] or list(options)
        chosen.append(secrets.choice(pool))
    return tuple(chosen)


def active_actions() -> tuple[LivenessAction, ...]:
    """Movimientos activos del catálogo (la plataforma puede retirar uno sin desplegar código)."""
    catalogs = get_catalogs()
    return tuple(a for a in LivenessAction if catalogs.is_active("liveness_actions", a.value)) or FALLBACK_ACTIONS


def enabled_head_actions(policy: PolicySnapshot) -> tuple[LivenessAction, ...]:
    """Los movimientos de cabeza que la empresa dejó activos (decisión del dueño, 2026-10-08). Siempre quedan al menos
    dos (CHECK `liveness_moves_min`); si una configuración vieja dejara menos, se usan los giros de respaldo para no
    quedarse sin repertorio."""
    actions = tuple(action for action, field in HEAD_ACTIONS if getattr(policy, field))
    return actions if len(actions) >= 2 else FALLBACK_ACTIONS


def verification_pool(policy: PolicySnapshot) -> tuple[LivenessAction, ...]:
    """El repertorio de una VERIFICACIÓN: los movimientos de cabeza que la empresa dejó activos más «acercarse» si el
    catálogo lo tiene activo (es de la plataforma, no de la empresa)."""
    has_closer = get_catalogs().is_active("liveness_actions", LivenessAction.MOVE_CLOSER.value)
    closer = (LivenessAction.MOVE_CLOSER,) if has_closer else ()
    return enabled_head_actions(policy) + closer


def enrollment_actions(policy: PolicySnapshot) -> tuple[LivenessAction, ...]:
    """Los movimientos de cabeza que la empresa dejó activos, en orden al azar (criptográfico): todos aparecen, nunca se
    repite uno y el orden cambia en cada reto (un video grabado de antemano no conoce la secuencia)."""
    actions = enabled_head_actions(policy)
    return tuple(secrets.SystemRandom().sample(actions, len(actions)))


def is_enrollment_challenge(challenge: Challenge, policy: PolicySnapshot) -> bool:
    """¿El reto pidió exactamente los movimientos de cabeza que la empresa habilitó? Un reto de verificación incompleto
    (menos movimientos o con «acercarse») no sirve para registrarse: la prueba de vida del registro es completa."""
    enabled = set(enabled_head_actions(policy))
    return len(challenge.actions) == len(enabled) and set(challenge.actions) == enabled


def challenge_actions(options: Sequence[LivenessAction], count: int) -> tuple[LivenessAction, ...]:
    """Los movimientos de un reto: "acercarse" nunca es el único (fase 0 del antifraude). Es lo único que una foto
    plana sí reproduce (crece igual que un rostro); con un giro o un cabeceo, sus puntos coplanares la delatan.
    Con un solo movimiento se elige entre los demás; con dos o más ya hay al menos uno distinto (nunca se repite
    el mismo seguido)."""
    others = tuple(a for a in options if a != LivenessAction.MOVE_CLOSER)
    return random_sequence(others if count == 1 and others else options, count)


class ChallengeStore:
    def issue(
        self,
        db: Session,
        owner: ChallengeOwner,
        *,
        steps: int,
        lifetime_seconds: int,
        flash: int = 0,
        step_up: bool = False,
        reinforced: bool = False,
        paced: bool = False,
        actions: Sequence[LivenessAction] | None = None,
    ) -> Challenge:
        """Reto nuevo de `steps` movimientos al azar (y `flash` colores) que vence en `lifetime_seconds`; `paced`: sus
        colores los dicta el servidor uno por uno (los de `flash` quedan para el respaldo sin canal en vivo); `actions`:
        movimientos ya decididos (los cuatro del registro, `enrollment_actions`) en lugar de elegirlos."""
        now = datetime.now(UTC)
        device = owner if isinstance(owner, ApiDevice) else None
        challenge = Challenge(
            id=secrets.token_urlsafe(24),
            user_id=user_of(owner),
            actions=tuple(actions) if actions else challenge_actions(active_actions(), max(1, min(steps, MAX_STEPS))),
            issued_at=now,
            expires_at=now + timedelta(seconds=lifetime_seconds),
            flash=random_sequence(tuple(FLASH_PALETTE), flash) if flash > 0 else (),
            step_up=step_up,
            reinforced=reinforced,
            flash_paced=paced and flash > 0,
            api_device=device,
        )
        stored = [a.value for a in challenge.actions] + [None] * (MAX_CHALLENGE_STEPS - len(challenge.actions))
        FaceChallengeRepository(db).replace_for_owner(
            FaceChallenge(
                id=challenge.id,
                user_id=challenge.user_id,
                api_key_id=device.key_id if device is not None else None,
                device_hash=device.device_hash if device is not None else None,
                direction=stored[0],
                second_direction=stored[1],
                third_direction=stored[2],
                fourth_direction=stored[3],
                flash_colors=",".join(challenge.flash) or None,
                issued_at=challenge.issued_at,
                expires_at=challenge.expires_at,
                step_up=step_up,
                reinforced=reinforced,
                flash_paced=challenge.flash_paced,
            )
        )
        db.commit()
        return challenge

    def consume(self, db: Session, challenge_id: str, owner: ChallengeOwner) -> Challenge | None:
        """Devuelve el reto si es válido para su dueño (la misma cuenta, o la misma llave en el mismo dispositivo); en
        cualquier caso lo elimina (atómico)."""
        row = FaceChallengeRepository(db).take(challenge_id)
        db.commit()
        if row is None:
            return None
        (
            user_id,
            key_id,
            device_hash,
            first,
            second,
            third,
            fourth,
            colors,
            issued_at,
            expires_at,
            step_up,
            reinforced,
            paced,
        ) = row
        stored = ApiDevice(key_id, device_hash) if key_id is not None and device_hash is not None else user_id
        if stored != owner or has_passed(expires_at):
            return None
        return Challenge(
            id=challenge_id,
            user_id=user_id,
            actions=tuple(LivenessAction(a) for a in (first, second, third, fourth) if a),
            issued_at=as_utc(issued_at),
            expires_at=as_utc(expires_at),
            flash=tuple(colors.split(",")) if colors else (),
            step_up=step_up,
            reinforced=reinforced,
            flash_paced=paced,
            api_device=owner if isinstance(owner, ApiDevice) else None,
        )

    def require(
        self,
        db: Session,
        owner: ChallengeOwner,
        response: LivenessResponse,
        *,
        required: bool,
        flash_required: bool = False,
    ) -> Challenge | None:
        """Consume el reto obligatorio de prueba de vida (None si la política no lo exige).

        422 LIVENESS_REQUIRED si falta el reto o la captura de algún movimiento (o del destello, si la
        empresa lo exige); 422 CHALLENGE_INVALID si venció, ya se usó o pertenece a otro usuario.
        Mientras el destello solo se observa, una app que no lo envía (versión anterior) no se bloquea.
        """
        if not required:
            return None
        if not response.challenge_id or not response.steps:
            raise face_rejection("LIVENESS_REQUIRED")
        challenge = self.consume(db, response.challenge_id, owner)
        if challenge is None:
            raise face_rejection("CHALLENGE_INVALID")
        if len(response.steps) != len(challenge.actions):
            raise face_rejection("LIVENESS_REQUIRED")  # falta (o sobra) la captura de algún movimiento
        flash_missing = bool(challenge.flash) and not response.flash and flash_required
        if flash_missing or (response.flash and len(response.flash) != len(challenge.flash)):
            raise face_rejection("LIVENESS_REQUIRED")
        return challenge


challenge_store = ChallengeStore()
