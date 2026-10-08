"""La verificación por voz y video del registro facial (decisión del dueño del producto, 2026-10-06).

Orden que no se altera: foto inicial válida → 32 fotos válidas con los movimientos (`POST /enrollment/face`) → video
con 3 preguntas al azar sobre los datos del empleado → validación de cada respuesta → fin. Este servicio es la parte del
video: `start` elige las preguntas y sella la sesión (al aceptar las fotos, por compatibilidad, y cada vez que la
persona abre el paso 3, `POST /enrollment/voice/start`, conservando las respuestas ya aceptadas: decisión del dueño,
2026-10-07); `answer` valida cada respuesta grabada; al pasar la última, el registro queda terminado (la empresa lo ve
en Validaciones y el empleado pasa a «en validación»).

Qué se valida de cada respuesta, en este orden (cada rechazo con su código estable; una respuesta rechazada se repite,
hasta `VOICE_MAX_RETRIES_PER_QUESTION` por pregunta, contados en la base):
1. El clip se puede leer (WebM, MP4...): si no, VIDEO_UNSUPPORTED_FORMAT. Dura entre SPEECH_MIN_ANSWER_SECONDS y
   SPEECH_MAX_ANSWER_SECONDS (ANSWER_TOO_SHORT / ANSWER_TOO_LONG) y suena (ANSWER_INAUDIBLE).
2. La voz se transcribe EN ESTE SERVIDOR en el idioma de la petición (`app/speech`); una transcripción vacía o de baja
   confianza es ANSWER_UNCLEAR y una que no corresponde al dato registrado, ANSWER_MISMATCH (`speech.matching`).
3. El rostro del video es el de las fotos del registro: unos fotogramas del clip contra las muestras (aún inactivas) de
   ESE registro, con la similitud que exige la empresa menos FACE_VIDEO_MATCH_MARGIN; si no, VIDEO_FACE_MISMATCH.

Transacciones cortas (§4 del AGENTS.md): lo que se lee de la base se confirma ANTES de decodificar, transcribir y
comparar (CPU de 1 a 3 s); el clip aceptado sube cifrado al bucket SIN transacción abierta y una transacción corta
guarda su referencia. Lo que no pasa no deja nada (ni fila ni objeto): solo suma un intento fallido.
"""

import itertools
import logging
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ServiceUnavailableError, UnprocessableError
from app.core.object_storage import StorageError
from app.core.observability import observed
from app.facial_recognition import FacePipeline
from app.facial_recognition.matcher import similarity_matrix
from app.i18n import current_locale, t
from app.models import (
    Employee,
    EnrollmentStatus,
    EnrollmentVoiceAnswer,
    FaceEnrollment,
    FaceStatus,
    User,
    VoiceQuestion,
)
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.schemas.enrollment import (
    EnrollmentVoiceRead,
    VoiceAnswerRead,
    VoiceAnswerResponse,
    VoiceChallengeResponse,
    VoiceClipRead,
    VoiceQuestionRead,
)
from app.services import image_storage
from app.services.catalog_service import Catalogs, get_catalogs
from app.services.face_service import engine_failure
from app.services.identity_core import required_similarity
from app.services.image_storage import ENROLLMENT_VOICE_CLIPS, ImageUnreadable
from app.services.policy_service import PolicyService
from app.services.voice_questions import (
    ANY_OF,
    Expected,
    VoiceSession,
    choose,
    eligible,
    expired,
    seal,
    unseal,
)
from app.speech import ClipUnreadable, SpeechUnavailable, backend, language_of
from app.speech.matching import Match, compare

logger = logging.getLogger(__name__)

#: Marcas del registro para la empresa (catalog.enrollment_flags): alguna pregunta tomó más de un intento, o en algún
#: intento el rostro del video no fue el de las fotos.
VOICE_RETRIES_FLAG = "VOICE_RETRIES"

#: Preguntas cuyo dato es texto libre (nombres propios): el modelo recibe el dato como vocabulario sugerido. Las de
#: cifras y fechas (número de empleado, fecha/mes/año/día, suma) se oyen bien solas y nunca se sugieren.
TEXT_QUESTIONS: frozenset[VoiceQuestion] = frozenset(
    {
        VoiceQuestion.FULL_NAME,
        VoiceQuestion.FIRST_NAME,
        VoiceQuestion.SURNAMES,
        VoiceQuestion.FIRST_SURNAME,
        VoiceQuestion.SECOND_SURNAME,
        VoiceQuestion.COMPANY_NAME,
        VoiceQuestion.DEPARTMENT,
        VoiceQuestion.WORK_SITE,
    }
)
VIDEO_FACE_MISMATCH_FLAG = "VIDEO_FACE_MISMATCH"
#: Códigos con que se rechaza una respuesta (la app repite la misma pregunta); cada uno con su mensaje en el catálogo.
REJECTIONS = (
    "VIDEO_UNSUPPORTED_FORMAT",
    "ANSWER_TOO_LONG",
    "ANSWER_TOO_SHORT",
    "ANSWER_INAUDIBLE",
    "ANSWER_UNCLEAR",
    "ANSWER_MISMATCH",
    "VIDEO_FACE_MISMATCH",
)


class AnswerRejected(UnprocessableError):
    """422 con el motivo: la app muestra el mensaje, conserva el token (`details.token`) y repite la pregunta."""

    def __init__(self, code: str, token: str, attempts_left: int, params: dict[str, Any] | None = None) -> None:
        super().__init__(code=code, params=params, details={"token": token, "attempts_left": attempts_left})


@dataclass(frozen=True)
class Verdict:
    """Lo que se midió de una respuesta y si pasó."""

    ok: bool
    code: str | None = None
    params: dict[str, Any] | None = None
    content_type: str | None = None
    transcript: str | None = None
    similarity: float | None = None
    face_similarity: float | None = None
    duration_ms: int = 0

    @staticmethod
    def rejected(code: str, **measured: Any) -> Verdict:
        return Verdict(ok=False, code=code, **measured)


def speech_unavailable() -> ServiceUnavailableError:
    """El motor de voz no respondió (sin modelo, cargando o una falla): 503 reintentable; la falla queda registrada."""
    return ServiceUnavailableError(code="SPEECH_SERVICE_UNAVAILABLE", retry_after=settings.FACE_ENGINE_RETRY_SECONDS)


class VoiceVerificationService:
    """Las preguntas en video de un registro facial de UNA empresa."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = FaceEnrollmentRepository(db, company_id)
        self.embeddings = FaceEmbeddingRepository(db)

    # ------------------------------------------------------------------ empezar (al aceptar las fotos)

    def plan(self, employee: Employee, answered: Sequence[EnrollmentVoiceAnswer] = ()) -> list[Expected]:
        """Las preguntas de la sesión completa, en orden (al azar, solo de datos registrados).

        `answered` (decisión del dueño, 2026-10-07: el paso 3 se retoma otro día y las respuestas aceptadas se
        conservan): cada una mantiene su pregunta en su posición (con la respuesta vacía: ya pasó y no se vuelve a
        comparar); solo las posiciones que faltan reciben preguntas nuevas al azar, de datos que no se preguntaron ya
        (si no alcanzan, de cualquiera). La sesión tiene `VOICE_QUESTIONS_PER_SESSION` preguntas (o las elegibles, si
        son menos; nunca menos de las ya respondidas)."""
        options = eligible(self.db, employee, self.repo.company_name())
        taken = {row.position: VoiceQuestion(row.question) for row in answered}
        count = max(min(settings.VOICE_QUESTIONS_PER_SESSION, len(options)), *(position + 1 for position in taken), 0)
        missing = [position for position in range(count) if position not in taken]
        fresh = choose([option for option in options if option.kind not in taken.values()], len(missing))
        fresh += choose([option for option in options if option not in fresh], len(missing) - len(fresh))
        # Con más posiciones que datos (cambió la configuración) se repite alguno: nunca se queda una sin pregunta.
        picks = dict(zip(missing, itertools.cycle(fresh), strict=False))
        return [Expected(taken[position], "") if position in taken else picks[position] for position in range(count)]

    def start(
        self, enrollment: FaceEnrollment, employee: Employee, answered: Sequence[EnrollmentVoiceAnswer] = ()
    ) -> VoiceChallengeResponse:
        """La sesión de preguntas (`plan`) con su token sellado; nada se guarda. Las ya respondidas van como pasadas
        (con sus intentos) y la respuesta solo trae las que faltan."""
        questions = self.plan(employee, answered)
        tries = {row.position: row.attempts for row in answered}
        session = VoiceSession(
            enrollment_id=enrollment.id,
            user_id=employee.user_id,
            company_id=self.company_id,
            questions=tuple(questions),
            passed=tuple(position in tries for position in range(len(questions))),
            tries=tuple(tries.get(position, 0) for position in range(len(questions))),
            expires=int(time.time()) + settings.VOICE_SESSION_TTL_SECONDS,
        )
        # El servidor arma el TEXTO de cada pregunta en el idioma de la petición (la suma, con sus números ya puestos):
        # la app y los SDK solo lo muestran. El catálogo está en memoria (ni una consulta por sesión).
        catalogs = get_catalogs()
        return VoiceChallengeResponse(
            token=seal(session),
            questions=[
                VoiceQuestionRead(position=index, question=q.kind, text=_prompt(catalogs, q))
                for index, q in enumerate(questions)
                if index not in tries
            ],
            total=len(questions),
            answered=len(tries),
            min_seconds=settings.SPEECH_MIN_ANSWER_SECONDS,
            max_seconds=settings.SPEECH_MAX_ANSWER_SECONDS,
            retries=settings.VOICE_MAX_RETRIES_PER_QUESTION,
            expires_in=settings.VOICE_SESSION_TTL_SECONDS,
        )

    # ------------------------------------------------------------------ responder

    def answer(self, user: User, token: str, position: int, clip: bytes, pipeline: FacePipeline) -> VoiceAnswerResponse:
        """Valida la respuesta grabada a la pregunta `position` de la sesión del token (ver el módulo)."""
        session = self._session(user, token, position)
        enrollment = self._pending(session, user)
        max_attempts = settings.VOICE_MAX_RETRIES_PER_QUESTION * len(session.questions)
        if enrollment.voice_attempts >= max_attempts:
            raise UnprocessableError(code="VOICE_RETRIES_EXHAUSTED")
        references = self.embeddings.vectors_of_enrollment(enrollment.id)
        required = (
            required_similarity(PolicyService(self.db, self.company_id).current()) - settings.FACE_VIDEO_MATCH_MARGIN
        )
        enrollment_id, employee_id = enrollment.id, enrollment.employee_id
        locale = current_locale()
        # Nada abierto mientras se decodifica, transcribe y compara (CPU): detrás de PgBouncer sería una conexión real.
        self.db.commit()
        verdict = self._verdict(clip, session.questions[position], references, required, locale, pipeline)
        if not verdict.ok:
            return self._reject(session, position, enrollment_id, verdict, max_attempts)
        return self._accept(session, position, enrollment_id, employee_id, clip, verdict)

    def _session(self, user: User, token: str, position: int) -> VoiceSession:
        now = int(time.time())
        session = unseal(token, user.id, now)
        if session is None:
            raise UnprocessableError(
                code="VOICE_SESSION_EXPIRED" if expired(token, user.id, now) else "VOICE_SESSION_INVALID"
            )
        if session.company_id != self.company_id or not 0 <= position < len(session.questions):
            raise UnprocessableError(code="VOICE_SESSION_INVALID")
        if session.passed[position]:
            raise ConflictError(code="ANSWER_ALREADY_ACCEPTED")
        return session

    def _pending(self, session: VoiceSession, user: User) -> FaceEnrollment:
        """El registro de la sesión, de este empleado, aún con la voz pendiente (uno reemplazado por otro envío,
        404)."""
        enrollment = self.repo.get_any(session.enrollment_id)
        if (
            enrollment is None
            or enrollment.employee.user_id != user.id
            or enrollment.status != EnrollmentStatus.PENDING
            or not enrollment.voice_pending
        ):
            raise NotFoundError(code="ENROLLMENT_NOT_FOUND")
        return enrollment

    @observed("voice.verdict")
    def _verdict(
        self,
        clip: bytes,
        question: Expected,
        references: list[np.ndarray],
        required: float,
        locale: str,
        pipeline: FacePipeline,
    ) -> Verdict:
        """Lo que se midió de la respuesta: clip legible y con voz, transcripción que corresponde al dato y el rostro
        del video que es el del registro. Solo CPU; sin transacción abierta."""
        speech = backend()
        try:
            info = speech.inspect(
                clip, max_seconds=settings.VOICE_CLIP_MAX_SECONDS, frames=settings.FACE_VIDEO_SAMPLE_FRAMES
            )
        except ClipUnreadable as exc:
            logger.info("Clip de la verificación por voz ilegible: %s", exc)
            return Verdict.rejected("VIDEO_UNSUPPORTED_FORMAT")
        measured: dict[str, Any] = {"content_type": info.content_type, "duration_ms": int(info.seconds * 1000)}
        if info.truncated or info.seconds > settings.SPEECH_MAX_ANSWER_SECONDS:
            params = {"seconds": int(settings.SPEECH_MAX_ANSWER_SECONDS)}
            return Verdict.rejected("ANSWER_TOO_LONG", params=params, **measured)
        if info.seconds < settings.SPEECH_MIN_ANSWER_SECONDS:
            return Verdict.rejected("ANSWER_TOO_SHORT", **measured)
        if info.rms_dbfs < settings.SPEECH_MIN_RMS_DBFS or info.speech_ratio < settings.SPEECH_MIN_SPEECH_RATIO:
            return Verdict.rejected("ANSWER_INAUDIBLE", **measured)
        try:
            transcript = speech.transcribe(info.samples, language_of(locale), suggested_vocabulary(question))
        except SpeechUnavailable as exc:
            raise speech_unavailable() from exc
        except Exception as exc:
            logger.exception("El motor de voz falló al transcribir una respuesta")
            raise speech_unavailable() from exc
        measured["transcript"] = transcript.text[:200] or None
        unclear = (
            not transcript.text
            or transcript.no_speech_prob > settings.SPEECH_MAX_NO_SPEECH_PROB
            or transcript.avg_logprob < settings.SPEECH_MIN_AVG_LOGPROB
        )
        if unclear:
            return Verdict.rejected("ANSWER_UNCLEAR", **measured)
        match = _best_match(question, transcript.text, locale)
        measured["similarity"] = match.score
        if not match.ok:
            return Verdict.rejected("ANSWER_MISMATCH", **measured)
        face = _face_similarity(pipeline, info.frames, references)
        measured["face_similarity"] = face
        if face is None or face < required:
            return Verdict.rejected("VIDEO_FACE_MISMATCH", **measured)
        return Verdict(ok=True, **measured)

    def _reject(
        self, session: VoiceSession, position: int, enrollment_id: int, verdict: Verdict, max_attempts: int
    ) -> VoiceAnswerResponse:
        """Suma el intento fallido en la base (y la marca de rostro distinto si fue eso) y repite la pregunta, o acaba
        la sesión al agotarse los intentos. Siempre lanza."""
        attempts = self.repo.count_failed_attempt(enrollment_id)
        if verdict.code == "VIDEO_FACE_MISMATCH":
            self.repo.add_flag(enrollment_id, VIDEO_FACE_MISMATCH_FLAG)
        self.db.commit()
        logger.info(
            "Respuesta por voz rechazada (%s) en el registro %s: intento %s de %s",
            verdict.code,
            enrollment_id,
            attempts,
            max_attempts,
        )
        if attempts >= max_attempts:
            raise UnprocessableError(code="VOICE_RETRIES_EXHAUSTED")
        assert verdict.code is not None
        raise AnswerRejected(verdict.code, seal(session.failing(position)), max_attempts - attempts, verdict.params)

    def _accept(
        self,
        session: VoiceSession,
        position: int,
        enrollment_id: int,
        employee_id: int,
        clip: bytes,
        verdict: Verdict,
    ) -> VoiceAnswerResponse:
        """El clip sube cifrado al bucket (sin transacción) y una transacción corta guarda la respuesta; con la última,
        el registro queda terminado y el empleado pasa a «en validación»."""
        advanced = session.passing(position)
        now = datetime.now(UTC)
        row = EnrollmentVoiceAnswer(
            company_id=self.company_id,
            enrollment_id=enrollment_id,
            employee_id=employee_id,
            question=session.questions[position].kind.value,
            position=position,
            attempts=advanced.tries[position],
            transcript=verdict.transcript,
            similarity=verdict.similarity,
            face_similarity=verdict.face_similarity,
            duration_ms=verdict.duration_ms,
            created_at=now,
            uid=secrets.token_hex(8),
            content_type=verdict.content_type,
        )
        image_storage.store(self.db, ENROLLMENT_VOICE_CLIPS, row, clip)
        try:
            self.repo.add_voice_answer(row)
            if advanced.done:
                self._finish(enrollment_id, advanced, now)
            self.db.commit()
        except Exception:
            self.db.rollback()
            image_storage.abandon(self.db)
            raise
        return VoiceAnswerResponse(
            token=seal(advanced), position=position, done=advanced.done, next_position=advanced.next_position
        )

    def _finish(self, enrollment_id: int, session: VoiceSession, now: datetime) -> None:
        """La última respuesta pasó: el registro termina (la empresa ya lo ve) y el empleado queda en validación."""
        enrollment = self.repo.get_any(enrollment_id, for_update=True)
        if enrollment is None or not enrollment.voice_pending:
            raise NotFoundError(code="VOICE_NOT_PENDING")
        enrollment.voice_passed_at = now
        if any(count > 1 for count in session.tries):
            self.repo.add_flag(enrollment_id, VOICE_RETRIES_FLAG)
        enrollment.employee.face_status = FaceStatus.PENDING_REVIEW
        enrollment.employee.face_rejection_reason = None

    # ------------------------------------------------------------------ la empresa revisa

    def voice_of(self, enrollment: FaceEnrollment) -> EnrollmentVoiceRead | None:
        """Lo que la empresa ve del video de un registro: sus preguntas con cómo pasaron (una consulta); None si el
        registro no la llevó."""
        if not enrollment.voice_required:
            return None
        answers = self.repo.voice_answers(enrollment.id)
        return EnrollmentVoiceRead(
            required=True,
            passed_at=enrollment.voice_passed_at,
            failed_attempts=enrollment.voice_attempts,
            answers=[
                VoiceAnswerRead(
                    id=answer.id,
                    position=answer.position,
                    question=answer.question,
                    attempts=answer.attempts,
                    transcript=answer.transcript,
                    similarity=answer.similarity,
                    face_similarity=answer.face_similarity,
                    duration_ms=answer.duration_ms,
                    created_at=answer.created_at,
                    has_clip=answer.object_name is not None,
                )
                for answer in answers
            ],
        )

    def clip(self, enrollment_id: int, answer_id: int) -> VoiceClipRead:
        """El clip de una respuesta, descifrado en memoria y en base64 dentro del contrato (nunca una URL del bucket).
        La lectura de la base se cierra antes de esperar al bucket."""
        enrollment = self.repo.get(enrollment_id)
        answer = self.repo.voice_answer(enrollment_id, answer_id) if enrollment is not None else None
        if answer is None or answer.object_name is None:
            raise NotFoundError(code="VOICE_CLIP_NOT_FOUND")
        content_type, duration_ms, byte_size = answer.content_type or "video/webm", answer.duration_ms, answer.byte_size
        self.db.commit()
        try:
            raw = image_storage.read(ENROLLMENT_VOICE_CLIPS, answer)
        except ImageUnreadable as exc:
            logger.error("El clip %s de la verificación por voz es ilegible: %s", answer_id, exc)
            raise NotFoundError(code="VOICE_CLIP_NOT_FOUND") from exc
        except StorageError as exc:
            raise image_storage.unavailable(exc) from exc
        assert raw is not None
        return VoiceClipRead(
            content_type=content_type, data=raw, byte_size=byte_size or len(raw), duration_ms=duration_ms
        )


def _prompt(catalogs: Catalogs, question: Expected) -> str:
    """El texto de la pregunta, listo para mostrarse en el idioma de la petición. La suma lo arma con sus números
    (`VOICE_SUM_PROMPT` del catálogo de mensajes: «¿Cuánto es 7 más 4?»), para que la empresa vea en la revisión la
    pregunta genérica del catálogo (`voice_questions.name`, sin marcadores) y el empleado, el reto concreto. Las demás
    salen tal cual del catálogo de la base, en su idioma."""
    if question.kind == VoiceQuestion.ARITHMETIC_SUM:
        return t("VOICE_SUM_PROMPT", dict(question.render))
    return catalogs.name("voice_questions", question.kind.value)


def _best_match(question: Expected, heard: str, locale: str) -> Match:
    """La respuesta contra el dato (o contra cualquiera de los datos válidos: los sitios de un turno)."""
    matches = [compare(question.kind, option, heard, locale) for option in question.answer.split(ANY_OF)]
    return max(matches, key=lambda match: (match.ok, match.score))


def suggested_vocabulary(question: Expected) -> str | None:
    """El dato registrado como vocabulario sugerido al modelo (`hotwords`) en las preguntas de TEXTO (nombre, empresa,
    departamento, sitio: lo que un modelo general no conoce); nunca en fechas ni números, que se oyen bien solos. Medido
    el 2026-10-06: 28/28 nombres propios sintéticos frente a 12/28 sin él, y con un dato ajeno sugerido el modelo lo
    "oyó" en 1 de 28; la comparación sigue exigiendo las palabras y el rostro del video, el de las fotos. Apagable con
    `SPEECH_HOTWORDS_ENABLED`."""
    if not settings.SPEECH_HOTWORDS_ENABLED or question.kind not in TEXT_QUESTIONS:
        return None
    return ", ".join(answer for answer in question.answer.split(ANY_OF) if answer)


def _face_similarity(pipeline: FacePipeline, frames: tuple[bytes, ...], references: list[np.ndarray]) -> float | None:
    """La mejor similitud de los fotogramas del clip contra las muestras del registro; None sin rostro medible. Una
    imagen que el motor no puede leer cuenta como sin rostro; cualquier otra falla del motor, 503."""
    if not references:
        return None
    vectors: list[np.ndarray] = []
    for frame in frames:
        try:
            vector = pipeline.identity_of(frame)
        except cv2.error, ValueError:
            vector = None
        except Exception as exc:
            raise engine_failure() from exc
        if vector is not None:
            vectors.append(vector)
    if not vectors:
        return None
    return round(float(similarity_matrix(vectors, references).max()), 4)
