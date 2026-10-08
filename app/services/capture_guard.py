"""Protección contra engaños en las capturas faciales (además del anti-spoofing y el reto de giro).

Cada comprobación cierra una forma concreta de engañar al sistema:

| Engaño | Comprobación | Motivo |
|---|---|---|
| Video o foto inyectados por una cámara virtual (OBS, ManyCam...) | nombre de la cámara | VIRTUAL_CAMERA |
| Foto de la galería o archivo editado (no es la captura en vivo) | metadatos EXIF | IMAGE_NOT_FROM_CAMERA |
| Una foto fija enviada como varias capturas | fotogramas idénticos | STATIC_CAPTURE |
| Reenvío de capturas interceptadas o guardadas | huella ya recibida antes | REPLAY_DETECTED |
| Capturas armadas con imágenes de tomas distintas | misma resolución y rostro continuo | CAPTURE_INCONSISTENT |
| Programa que responde al reto al instante | tiempo humano mínimo por movimiento y color | CHALLENGE_TOO_FAST |
| Foto, pantalla o video frente a la cámara | anti-spoofing en frontales y en cada movimiento | SPOOF_DETECTED |
| Video inyectado o generado que no ve la pantalla | el rostro refleja los colores del destello | FLASH_MISMATCH |
| Pantalla o papel frente a la cámara bajo el destello | el rostro responde más que el fondo | FLASH_FLAT |
| Captura reenviada con cambios mínimos (recomprimida) | pHash + embedding (motor de riesgo) | REPLAY_PERCEPTUAL |
| Artefacto de un ataque ya confirmado (otra empresa incluida) | lista de bloqueo (motor de riesgo) | KNOWN_ATTACK |

Todas lanzan `SuspiciousCapture`: se registran en la bitácora y cuentan para el bloqueo temporal
(attempt_guard). Ninguna sustituye a las demás: un atacante tendría que superarlas todas a la vez.
Cada empresa activa o desactiva cada una en su política de verificación (todas activas por defecto).
"""

import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import combinations

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.text import fold_text
from app.facial_recognition import FaceAnalysis, FacePolicy, LivenessAction
from app.facial_recognition.pipeline import FlashCapture
from app.repositories.capture_repository import CaptureFingerprintRepository
from app.services.face_service import SuspiciousCapture, looks_spoofed, spoof_consensus
from app.services.liveness_service import Challenge
from app.services.policy_service import PolicySnapshot


def is_virtual_camera(label: str | None) -> bool:
    """El nombre de la cámara es el de un programa que finge ser una: palabra completa de `FACE_BLOCKED_CAMERAS`, sin
    distinguir mayúsculas ni acentos («Câmera virtual», «Caméra virtuelle», «Virtuelle Kamera», «Fotocamera virtuale»:
    el sistema la nombra en SU idioma, con o sin acentos; la lista también se pliega, por si llegó con ellos)."""
    if not label:
        return False
    text = fold_text(label)
    return any(re.search(rf"(?<!\w){re.escape(fold_text(name))}(?!\w)", text) for name in settings.FACE_BLOCKED_CAMERAS)


def ensure_real_camera(camera_label: str | None, policy: PolicySnapshot) -> None:
    """La webapp informa el nombre de la cámara con que capturó; una cámara virtual no se acepta."""
    if policy.block_virtual_cameras and is_virtual_camera(camera_label):
        raise SuspiciousCapture("VIRTUAL_CAMERA")


def ensure_human_timing(challenge: Challenge | None, policy: PolicySnapshot, flash_frames: int = 0) -> None:
    """Las capturas no pueden llegar antes de lo que tarda una persona en leer el reto y hacer cada
    movimiento (entre uno y otro vuelve al frente), más un instante por cada color del destello."""
    if challenge is None or not policy.enforce_human_timing:
        return
    elapsed = (datetime.now(UTC) - challenge.issued_at).total_seconds()
    needed = (
        settings.FACE_CHALLENGE_MIN_SECONDS * len(challenge.actions) + settings.FACE_FLASH_MIN_SECONDS * flash_frames
    )
    if elapsed < needed:
        raise SuspiciousCapture("CHALLENGE_TOO_FAST")


def spoofed(frontal: Sequence[FaceAnalysis], turns: Sequence[FaceAnalysis], policy: FacePolicy) -> bool:
    """Foto, pantalla o video: lo decide el nivel de la empresa sobre las frontales; además, un giro
    sospechoso junto con alguna frontal sospechosa (dos indicios independientes)."""
    if spoof_consensus(list(frontal), policy):
        return True
    if policy.spoof_any_frame and any(looks_spoofed(t, policy) for t in turns):
        return True
    return any(looks_spoofed(t, policy) for t in turns) and any(looks_spoofed(f, policy) for f in frontal)


def thumb_difference(a: FaceAnalysis, b: FaceAnalysis) -> float | None:
    """Diferencia media (niveles de gris) entre los rostros de dos capturas; None si no se midió."""
    if a.face_thumb is None or b.face_thumb is None or a.face_thumb.shape != b.face_thumb.shape:
        return None
    return float(np.abs(a.face_thumb - b.face_thumb).mean())


def ensure_not_static(
    frontal: Sequence[FaceAnalysis], turns: Sequence[FaceAnalysis], flash: Sequence[FlashCapture] = ()
) -> None:
    """Ningún fotograma repetido: ni la misma imagen ni un rostro idéntico píxel a píxel."""
    captures: list[FaceAnalysis | FlashCapture] = [*frontal, *turns, *flash]
    digests = [c.capture_digest for c in captures if c.capture_digest is not None]
    if len(set(digests)) < len(digests):
        raise SuspiciousCapture("STATIC_CAPTURE")
    for a, b in combinations(frontal, 2):
        difference = thumb_difference(a, b)
        if difference is not None and difference < settings.FACE_STATIC_MIN_DIFFERENCE:
            raise SuspiciousCapture("STATIC_CAPTURE")


def ensure_same_take(
    frontal: Sequence[FaceAnalysis],
    turns: Sequence[FaceAnalysis],
    actions: Sequence[LivenessAction] = (),
    flash: Sequence[FlashCapture] = (),
) -> None:
    """Las capturas de un intento son de la MISMA toma en vivo.

    - Misma resolución: salen del mismo flujo de la cámara (no de archivos distintos).
    - Cada movimiento y cada color del destello continúan la toma: el rostro no cambia de tamaño
      (salvo lo que pide "acercarse"), no salta de lugar y la luz no cambia de forma imposible (el
      destello cambia el color a propósito: ahí no se mide la luz).
    """
    captures: list[FaceAnalysis | FlashCapture] = [*frontal, *turns, *flash]
    sizes = {c.image_size for c in captures if c.image_size is not None}
    if len(sizes) > 1:
        raise SuspiciousCapture("CAPTURE_INCONSISTENT")
    if not frontal:
        return
    for index, turn in enumerate(turns):
        closer = index < len(actions) and actions[index] == LivenessAction.MOVE_CLOSER
        ensure_continuity(frontal[-1], turn, closer=closer)
    for capture in flash:
        ensure_framing(frontal[-1].face_box, capture.face_box)


def _box_change(before: tuple[int, int, int, int], after: tuple[int, int, int, int]) -> tuple[float, float]:
    """Cuánto cambió el tamaño del rostro (proporción) y cuánto se movió su centro (en anchos de rostro)."""
    fx, fy, fw, fh = before
    tx, ty, tw, th = after
    scale = max(fw, tw) / max(min(fw, tw), 1)
    shift = math.hypot((fx + fw / 2) - (tx + tw / 2), (fy + fh / 2) - (ty + th / 2)) / max(fw, 1)
    return scale, shift


def ensure_framing(before: tuple[int, int, int, int], after: tuple[int, int, int, int]) -> None:
    """El rostro sigue en el mismo lugar y del mismo tamaño (fotogramas del destello)."""
    scale, shift = _box_change(before, after)
    if scale > settings.FACE_CONTINUITY_MAX_SCALE or shift > settings.FACE_CONTINUITY_MAX_SHIFT:
        raise SuspiciousCapture("CAPTURE_INCONSISTENT", {"scale": round(scale, 2), "shift": round(shift, 2)})


def ensure_continuity(frontal: FaceAnalysis, turn: FaceAnalysis, *, closer: bool = False) -> None:
    """Del último fotograma frontal al del movimiento la persona solo mueve la cabeza (o se acerca:
    entonces el rostro puede crecer hasta lo que permite el tope de "acercarse")."""
    scale, shift = _box_change(frontal.face_box, turn.face_box)
    light = abs(frontal.brightness - turn.brightness)
    max_scale = settings.FACE_CONTINUITY_MAX_SCALE * (settings.FACE_LIVENESS_MAX_CLOSER_SCALE if closer else 1.0)
    if (
        scale > max_scale
        or shift > settings.FACE_CONTINUITY_MAX_SHIFT
        or light > settings.FACE_CONTINUITY_MAX_BRIGHTNESS_DELTA
    ):
        details = {"scale": round(scale, 2), "shift": round(shift, 2), "brightness": round(light, 1)}
        raise SuspiciousCapture("CAPTURE_INCONSISTENT", details)


def ensure_not_replayed(db: Session, company_id: int, captures: Sequence[FaceAnalysis | FlashCapture]) -> None:
    """Ninguna captura se había recibido antes; se recuerdan para rechazar su reenvío.

    Las huellas se guardan con la bitácora del intento (mismo commit) y cuentan durante
    FACE_REPLAY_RETENTION_DAYS días; las más viejas las depura el mantenimiento (fuera de la petición).
    """
    digests = {c.capture_digest for c in captures if c.capture_digest is not None}
    if not digests:
        return
    now = datetime.now(UTC)
    since = now - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS)
    if not CaptureFingerprintRepository(db).claim(digests, company_id, now, since):
        raise SuspiciousCapture("REPLAY_DETECTED")


def inspect_take(
    db: Session,
    company_id: int,
    frontal: Sequence[FaceAnalysis],
    turns: Sequence[FaceAnalysis],
    policy: PolicySnapshot,
    *,
    actions: Sequence[LivenessAction] = (),
    flash: Sequence[FlashCapture] = (),
    others: Sequence[FaceAnalysis] = (),
) -> None:
    """Comprobaciones de la toma completa (las que la empresa tenga activas), después del análisis de
    cada captura. El reenvío al final: solo se recuerdan capturas que superaron lo demás. Los fotogramas
    del destello cuentan solo cuando el destello es obligatorio (mientras se observa no bloquean).

    `others` (las fotos útiles del registro que no quedaron como referencia, decisión del dueño 2026-10-06) solo
    cuentan para el reenvío: sin recordarlas, cualquiera de las 31 no elegidas serviría en otro registro. Las fotos
    fijas y la continuidad se revisan solo en las frontales: 36 fotos seguidas de una persona quieta pueden quedar,
    entre sí, bajo FACE_STATIC_MIN_DIFFERENCE y jamás deben llamarse STATIC_CAPTURE."""
    if policy.detect_static_captures:
        ensure_not_static(frontal, turns, flash)
    if policy.check_capture_continuity:
        ensure_same_take(frontal, turns, actions, flash)
    if policy.detect_replays:
        ensure_not_replayed(db, company_id, [*frontal, *turns, *flash, *others])
