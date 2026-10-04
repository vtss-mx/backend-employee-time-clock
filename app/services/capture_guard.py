"""Protección contra engaños en las capturas faciales (además del anti-spoofing y el reto de giro).

Cada comprobación cierra una forma concreta de engañar al sistema:

| Engaño | Comprobación | Motivo |
|---|---|---|
| Video o foto inyectados por una cámara virtual (OBS, ManyCam...) | nombre de la cámara | VIRTUAL_CAMERA |
| Foto de la galería o archivo editado (no es la captura en vivo) | metadatos EXIF | IMAGE_NOT_FROM_CAMERA |
| Una foto fija enviada como varias capturas | fotogramas idénticos | STATIC_CAPTURE |
| Reenvío de capturas interceptadas o guardadas | huella ya recibida antes | REPLAY_DETECTED |
| Capturas armadas con imágenes de tomas distintas | misma resolución y rostro continuo | CAPTURE_INCONSISTENT |
| Programa que responde al reto al instante | tiempo humano mínimo para girar | CHALLENGE_TOO_FAST |
| Foto, pantalla o video frente a la cámara | anti-spoofing en frontales y en el giro | SPOOF_DETECTED |

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
from app.facial_recognition import FaceAnalysis, FacePolicy
from app.repositories.capture_repository import CaptureFingerprintRepository
from app.services.face_service import SuspiciousCapture, looks_spoofed, spoof_consensus
from app.services.liveness_service import Challenge
from app.services.policy_service import PolicySnapshot


def is_virtual_camera(label: str | None) -> bool:
    """El nombre de la cámara es el de un programa que finge ser una (palabra completa)."""
    if not label:
        return False
    text = label.lower()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) for name in settings.FACE_BLOCKED_CAMERAS)


def ensure_real_camera(camera_label: str | None, policy: PolicySnapshot) -> None:
    """La webapp informa el nombre de la cámara con que capturó; una cámara virtual no se acepta."""
    if policy.block_virtual_cameras and is_virtual_camera(camera_label):
        raise SuspiciousCapture("VIRTUAL_CAMERA")


def ensure_human_timing(challenge: Challenge | None, policy: PolicySnapshot) -> None:
    """Los giros no pueden llegar antes de lo que tarda una persona en leer el reto y girar
    (cada giro suma el mínimo: con dos, además vuelve al frente entre ellos)."""
    if challenge is None or not policy.enforce_human_timing:
        return
    elapsed = (datetime.now(UTC) - challenge.issued_at).total_seconds()
    if elapsed < settings.FACE_CHALLENGE_MIN_SECONDS * len(challenge.directions):
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


def ensure_not_static(frontal: Sequence[FaceAnalysis], turns: Sequence[FaceAnalysis]) -> None:
    """Ningún fotograma repetido: ni la misma imagen ni un rostro idéntico píxel a píxel."""
    digests = [c.capture_digest for c in (*frontal, *turns) if c.capture_digest is not None]
    if len(set(digests)) < len(digests):
        raise SuspiciousCapture("STATIC_CAPTURE")
    for a, b in combinations(frontal, 2):
        difference = thumb_difference(a, b)
        if difference is not None and difference < settings.FACE_STATIC_MIN_DIFFERENCE:
            raise SuspiciousCapture("STATIC_CAPTURE")


def ensure_same_take(frontal: Sequence[FaceAnalysis], turns: Sequence[FaceAnalysis]) -> None:
    """Las capturas de un intento son de la MISMA toma en vivo.

    - Misma resolución: salen del mismo flujo de la cámara (no de archivos distintos).
    - Cada giro continúa la toma: el rostro no cambia de tamaño, no salta de lugar y la luz no
      cambia de forma imposible al girar la cabeza.
    """
    if len({c.image_size for c in (*frontal, *turns) if c.image_size is not None}) > 1:
        raise SuspiciousCapture("CAPTURE_INCONSISTENT")
    if frontal:
        for turn in turns:
            ensure_continuity(frontal[-1], turn)


def ensure_continuity(frontal: FaceAnalysis, turn: FaceAnalysis) -> None:
    """Del último fotograma frontal al del giro la persona solo gira la cabeza."""
    fx, fy, fw, fh = frontal.face_box
    tx, ty, tw, th = turn.face_box
    scale = max(fw, tw) / max(min(fw, tw), 1)
    shift = math.hypot((fx + fw / 2) - (tx + tw / 2), (fy + fh / 2) - (ty + th / 2)) / max(fw, 1)
    light = abs(frontal.brightness - turn.brightness)
    if (
        scale > settings.FACE_CONTINUITY_MAX_SCALE
        or shift > settings.FACE_CONTINUITY_MAX_SHIFT
        or light > settings.FACE_CONTINUITY_MAX_BRIGHTNESS_DELTA
    ):
        details = {"scale": round(scale, 2), "shift": round(shift, 2), "brightness": round(light, 1)}
        raise SuspiciousCapture("CAPTURE_INCONSISTENT", details)


def ensure_not_replayed(db: Session, company_id: int, captures: Sequence[FaceAnalysis]) -> None:
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
) -> None:
    """Comprobaciones de la toma completa (las que la empresa tenga activas), después del análisis de
    cada captura. El reenvío al final: solo se recuerdan capturas que superaron lo demás."""
    if policy.detect_static_captures:
        ensure_not_static(frontal, turns)
    if policy.check_capture_continuity:
        ensure_same_take(frontal, turns)
    if policy.detect_replays:
        ensure_not_replayed(db, company_id, [*frontal, *turns])
