"""Las referencias del registro facial elegidas entre muchas fotos (decisión del dueño del producto, 2026-10-06).

«Asegúrate de que se pidan varias fotos, por lo menos 36»: el registro (propio o en persona) manda hasta
`FACE_ENROLL_MAX_PHOTOS` fotos COMPLETAS (640 px, JPEG 92, en orden de la toma) y el servidor arma la plantilla con las
mejores. Una sola foto mala (un parpadeo, desenfoque, la cara a medio girar) ya no tumba el registro: se descarta. Y una
toma que no es UNA persona viva y constante se rechaza.

Cómo se decide (reglas que se conservan):

1. **Cada foto con las comprobaciones reales, salvo CLIP** (`analyze_frontal` sin accesorios bloqueados): un rostro,
   encuadre, pose de frente, luz, nitidez, calidad mínima de la empresa, anti-spoofing, embedding, huellas y señales
   físicas. CLIP es lo más caro (≈ 40 ms por foto) y solo importa en las que se guardan (punto 6). Una foto que no
   cumple se DESCARTA con su motivo; una con un motivo de seguridad (`SECURITY_REASONS`, p. ej. metadatos de cámara:
   no es una captura de la app) rechaza todo el intento como `SuspiciousCapture`, igual que `face_service._run`: lo
   que parece un engaño nunca se descarta en silencio. Una imagen ilegible es INVALID_IMAGE; cualquier otra falla del
   motor, 503 FACE_PROCESSING_ERROR.
2. **En paralelo** (`map_on_spares`): el worker de la petición y los de repuesto que estén libres en ese instante toman
   de una sola fila de fotos; lo que un repuesto no termina a tiempo (`FACE_ENROLL_SPARE_WAIT_SECONDS`) o falla lo
   analiza el de la petición. Solo CPU, sin transacción abierta (`take_challenge` ya confirmó la suya).
3. **Fotogramas repetidos** (la misma huella: una cámara que entregó dos veces el mismo cuadro) cuentan una vez.
4. **Suficientes**: al menos `FACE_ENROLL_MIN_USABLE` útiles; si no, 422 con el motivo de descarte MÁS FRECUENTE (a
   igual frecuencia, el que apareció primero), para que la persona sepa qué corregir ("hay poca luz", "no se ve el
   rostro"...).
5. **Una sola persona**: el medoide (la foto útil con la mayor mediana de parecido con las demás) representa a quien
   se registra; cada foto útil debe parecérsele al menos `FACE_ENROLL_CONSISTENCY_THRESHOLD`, y las referencias
   elegidas también entre sí (la regla de siempre sobre lo que se guarda). Si no, 422 ENROLL_INCONSISTENT: una toma
   con dos personas (aunque una sea mayoría) no se registra. Todo con una matriz (`similarity_matrix`).
6. **Las referencias**: por calidad (a igual calidad, mayor confianza del detector; luego la más temprana), sin repetir
   lo que ya se tiene (una casi idéntica a una elegida, `FACE_LEARNING_REDUNDANCY`, no aporta nada: el mismo criterio
   del aprendizaje) hasta `FACE_MAX_SAMPLES_PER_EMPLOYEE`; si no alcanzan, se completan con las mejores que se habían
   saltado. Van en el ORDEN DE LA TOMA: la última es la más cercana a los movimientos del reto (la continuidad se mide
   contra ella). CLIP corre solo en ellas y los accesorios se deciden por mayoría entre ellas (`accessory_consensus`).
7. **Suplantación**: la MAYORÍA de TODAS las útiles (`spoof_consensus` sin «una sola basta») o la regla de la empresa
   sobre las referencias (en el nivel Máximo, «una sola captura sospechosa basta» se aplica a las ≤ 5 referencias, como
   cuando el registro mandaba 5 fotos: sobre 36 fotos de la misma toma, una sola con 1.8 % de falsos por captura
   marcaría a casi la mitad de las personas reales). En el autoregistro marca al revisor, en persona bloquea
   (`confirm_live`).

Privacidad (regla 13): solo UNA foto, la de mejor calidad de las elegidas, va cifrada al bucket para el revisor
(`STORED_IMAGES`); las demás se analizan en memoria y se descartan (nunca BD, bucket ni logs). La toma única
(`capture_guard.inspect_take`) recuerda las huellas de TODAS las útiles: ninguna sirve después en otro registro.
"""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import cv2
import numpy as np

from app.core.config import settings
from app.core.exceptions import UnprocessableError
from app.core.observability import observed
from app.facial_recognition import FaceAnalysis, FacePipeline, FacePolicy, FaceValidationError, map_on_spares
from app.facial_recognition.matcher import similarity_matrix
from app.facial_recognition.pipeline import accessory_consensus
from app.services.face_service import (
    SECURITY_REASONS,
    SPOOF_FLAG,
    SuspiciousCapture,
    engine_failure,
    face_rejection,
    spoof_consensus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Discard:
    """Una foto que no sirve como referencia: su motivo (catálogo `face_errors`) y sus datos para el mensaje."""

    code: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class EnrollmentSelection:
    """Lo elegido de un registro con muchas fotos (solo análisis: ninguna imagen)."""

    #: Las referencias de la plantilla, en el orden de la toma (con sus accesorios medidos).
    references: tuple[FaceAnalysis, ...]
    #: Todas las fotos útiles, sin repetidas (sus huellas cuentan para el reenvío).
    usable: tuple[FaceAnalysis, ...]
    #: Posición (en lo recibido) de la foto para el revisor: la de mejor calidad de las referencias.
    photo: int
    #: Marcas para el revisor: accesorios por mayoría de las referencias y SPOOF por consenso de las útiles.
    flags: tuple[str, ...]
    #: Fotos descartadas por motivo, repetidas y cuántas llegaron.
    discarded: dict[str, int]
    duplicates: int
    received: int


def _candidate(pipeline: FacePipeline, image: bytes, policy: FacePolicy) -> FaceAnalysis | Discard:
    """Una foto con las comprobaciones reales (sin CLIP: `policy` no bloquea accesorios): su análisis o su descarte.
    Cualquier otra excepción es una falla del motor y sigue su camino."""
    try:
        return pipeline.analyze_frontal(image, policy=policy, enforce_accessories=False)
    except FaceValidationError as exc:
        return Discard(exc.code, exc.details)
    except cv2.error, ValueError:
        return Discard("INVALID_IMAGE")


def _usable(results: Sequence[FaceAnalysis | Discard]) -> tuple[list[tuple[int, FaceAnalysis]], list[Discard], int]:
    """(posición y análisis de cada foto útil sin repetidas, descartes en orden, repetidas). Un motivo de seguridad
    rechaza todo el intento (el primero en el orden de la toma)."""
    discards = [result for result in results if isinstance(result, Discard)]
    security = next((d for d in discards if d.code in SECURITY_REASONS), None)
    if security is not None:
        raise SuspiciousCapture(security.code, security.details)
    usable: list[tuple[int, FaceAnalysis]] = []
    seen: set[str | None] = set()
    for index, result in enumerate(results):
        if isinstance(result, FaceAnalysis) and (result.capture_digest is None or result.capture_digest not in seen):
            seen.add(result.capture_digest)
            usable.append((index, result))
    analyzed = len(results) - len(discards)
    return usable, discards, analyzed - len(usable)


def _ensure_enough(usable: Sequence[tuple[int, FaceAnalysis]], discards: Sequence[Discard]) -> None:
    """Fotos útiles suficientes; si no, el motivo de descarte más frecuente (o, sin descartes, cuántas mandar)."""
    if len(usable) >= settings.FACE_ENROLL_MIN_USABLE:
        return
    if not discards:
        params = {"min": settings.FACE_ENROLL_MIN_USABLE, "max": settings.FACE_ENROLL_MAX_PHOTOS}
        raise UnprocessableError(code="INVALID_FRAME_COUNT", params=params)
    code = Counter(d.code for d in discards).most_common(1)[0][0]
    raise face_rejection(code, next(d.details for d in discards if d.code == code))


def _ensure_one_person(similarity: np.ndarray) -> None:
    """Cada foto útil se parece al medoide lo que exige un registro (una sola persona en toda la toma)."""
    count = len(similarity)
    others = similarity[~np.eye(count, dtype=bool)].reshape(count, count - 1) if count > 1 else similarity
    medoid = int(np.argmax(np.median(others, axis=1)))
    if float(similarity[medoid].min()) < settings.FACE_ENROLL_CONSISTENCY_THRESHOLD:
        raise face_rejection("ENROLL_INCONSISTENT")


def _rank(analyses: Sequence[FaceAnalysis]) -> list[int]:
    """Posiciones de la mejor a la peor: calidad, confianza del detector y, al final, la más temprana."""
    return sorted(range(len(analyses)), key=lambda k: (-analyses[k].quality_score, -analyses[k].detection_score, k))


def _pick(ranked: Sequence[int], similarity: np.ndarray) -> list[int]:
    """Hasta FACE_MAX_SAMPLES_PER_EMPLOYEE, sin casi idénticas a una ya elegida; completadas con las mejores saltadas.
    En el orden de la toma."""
    target = settings.FACE_MAX_SAMPLES_PER_EMPLOYEE
    picked: list[int] = []
    skipped: list[int] = []
    for position in ranked:
        if len(picked) == target:
            break
        if picked and float(similarity[position, picked].max()) >= settings.FACE_LEARNING_REDUNDANCY:
            skipped.append(position)
        else:
            picked.append(position)
    return sorted(picked + skipped[: target - len(picked)])


def _spread[R](pipeline: FacePipeline, count: int, task: Callable[[FacePipeline, int], R]) -> list[R]:
    """`task` para cada foto, repartido entre el worker de la petición y los de repuesto libres (`map_on_spares`). Lo
    que la tarea no clasifica es una falla del motor: 503 FACE_PROCESSING_ERROR (registrada), nunca un 500."""
    try:
        return map_on_spares(pipeline, count, task, wait=settings.FACE_ENROLL_SPARE_WAIT_SECONDS)
    except Exception as exc:
        raise engine_failure() from exc


def _with_accessories(
    pipeline: FacePipeline, images: Sequence[bytes], chosen: Sequence[tuple[int, FaceAnalysis]], policy: FacePolicy
) -> tuple[FaceAnalysis, ...]:
    """Las referencias con sus accesorios (CLIP solo en ellas, también en paralelo). Sin accesorios bloqueados no se
    corre nada. La foto ya pasó la misma detección: una falla aquí es del motor (503)."""
    if not policy.any_accessory:
        return tuple(analysis for _, analysis in chosen)
    checks = _spread(
        pipeline, len(chosen), lambda worker, k: worker.accessories_of(images[chosen[k][0]], policy=policy)
    )
    return tuple(
        replace(analysis, accessories=scores, lower_face_skin=skin, accessories_found=found)
        for (_, analysis), (scores, skin, found) in zip(chosen, checks, strict=True)
    )


@observed("face.enroll_select")
def select_references(pipeline: FacePipeline, images: Sequence[bytes], policy: FacePolicy) -> EnrollmentSelection:
    """Las referencias del registro entre sus fotos (ver el módulo). `policy`: la de la empresa para el empleado (sus
    accesorios bloqueados, anti-spoofing y calidad mínima). Lanza 422 (pocas útiles, otra persona), SuspiciousCapture
    (un engaño) o 503 (el motor)."""
    candidate_policy = replace(policy, block_glasses=False, block_headwear=False, block_mask=False)
    results = _spread(pipeline, len(images), lambda worker, index: _candidate(worker, images[index], candidate_policy))
    usable, discards, duplicates = _usable(results)
    _ensure_enough(usable, discards)
    analyses = [analysis for _, analysis in usable]
    embeddings = [analysis.embedding for analysis in analyses]
    similarity = similarity_matrix(embeddings, embeddings)
    _ensure_one_person(similarity)
    ranked = _rank(analyses)
    picked = _pick(ranked, similarity)
    if float(similarity[np.ix_(picked, picked)].min()) < settings.FACE_ENROLL_CONSISTENCY_THRESHOLD:
        raise face_rejection("ENROLL_INCONSISTENT")
    references = _with_accessories(pipeline, images, [usable[k] for k in picked], policy)
    found = accessory_consensus([reference.accessories_found for reference in references])
    # La mayoría de toda la toma o la regla de la empresa sobre lo que se guarda (ver el punto 7 del módulo).
    majority = replace(policy, spoof_any_frame=False)
    spoof = spoof_consensus(list(references), policy) or spoof_consensus(analyses, majority)
    discarded = dict(Counter(d.code for d in discards))
    logger.info(
        "Registro facial: %s fotos, %s útiles, %s repetidas, descartadas %s; %s referencias",
        len(images),
        len(usable),
        duplicates,
        discarded,
        len(references),
    )
    return EnrollmentSelection(
        references=references,
        usable=tuple(analyses),
        # La mejor de todas (la primera del orden) siempre queda elegida: es la foto del revisor.
        photo=usable[ranked[0]][0],
        flags=tuple(a.value for a in found) + ((SPOOF_FLAG,) if spoof else ()),
        discarded=discarded,
        duplicates=duplicates,
        received=len(images),
    )
