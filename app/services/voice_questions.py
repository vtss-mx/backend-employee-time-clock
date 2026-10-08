"""Las preguntas de la verificación por voz del registro facial (decisión del dueño del producto, 2026-10-06).

«Tres preguntas distintas, elegidas al azar en cada sesión, solo de información que el empleado tiene registrada y
puede saber»: su nombre completo, su fecha de nacimiento y el nombre de su empresa siempre están; su número de empleado
solo si lo tiene (es opcional), su departamento solo si tiene uno asignado y su sitio de trabajo solo si su turno
vigente checa en algún sitio. Nunca se pregunta un dato que no exista (no habría respuesta correcta). El puesto no es
un dato del modelo: no es elegible.

La sesión viaja en un token SELLADO (`app/core/sealed.py`: cifrado y firmado con la llave de la plataforma) que el
cliente devuelve con cada respuesta: trae las preguntas con la respuesta esperada (que el cliente no puede leer), qué
preguntas ya pasaron y cuántos intentos llevó cada una, y vence a los `VOICE_SESSION_TTL_SECONDS`. Sin estado en el
servidor: funciona igual con N réplicas detrás de PgBouncer. Lo que el token no puede proteger —el tope de intentos
fallidos— vive en la base (`face_enrollments.voice_attempts`).
"""

import json
import secrets
from dataclasses import dataclass, replace
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from app.core.clock import business_today
from app.core.config import settings
from app.core.sealed import sealer
from app.models import Employee, VoiceQuestion
from app.repositories.department_repository import DepartmentRepository
from app.repositories.shift_repository import ShiftRepository

#: Etiqueta de la llave derivada: un token de la verificación por voz no abre nada más.
_PURPOSE = b"voice-questions"
_SEAL = sealer(_PURPOSE)
#: Separador de las respuestas válidas de una misma pregunta (varios sitios de un turno: cualquiera vale).
ANY_OF = "\n"


@dataclass(frozen=True)
class Expected:
    """Una pregunta elegible con el dato con que se compara su respuesta y, si hace falta, los parámetros para
    RENDERIZAR su texto en el idioma de la petición.

    `answer`: lo que se compara (la fecha de nacimiento y sus partes —mes, año, día— viajan en ISO; varios sitios de un
    turno, unidos con `ANY_OF`; la suma, su total ya calculado). `render`: pares que llenan los `{marcadores}` del texto
    del catálogo (solo `ARITHMETIC_SUM`: `a` y `b`); vacío en las demás. No viaja en el token sellado: el texto se arma
    una sola vez al emitir la sesión y la validación solo usa `answer`."""

    kind: VoiceQuestion
    answer: str
    render: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class VoiceSession:
    """La sesión de preguntas de un registro facial (lo que viaja sellado)."""

    enrollment_id: int
    user_id: int
    company_id: int
    questions: tuple[Expected, ...]
    #: Por pregunta: si ya pasó y cuántos intentos lleva (informa a la empresa; el tope real está en la base).
    passed: tuple[bool, ...]
    tries: tuple[int, ...]
    #: Vencimiento (segundos desde la época).
    expires: int

    @property
    def done(self) -> bool:
        return all(self.passed)

    def passing(self, position: int) -> VoiceSession:
        tries = list(self.tries)
        tries[position] += 1
        passed = list(self.passed)
        passed[position] = True
        return replace(self, passed=tuple(passed), tries=tuple(tries))

    def failing(self, position: int) -> VoiceSession:
        tries = list(self.tries)
        tries[position] += 1
        return replace(self, tries=tuple(tries))

    @property
    def next_position(self) -> int | None:
        return next((index for index, ok in enumerate(self.passed) if not ok), None)


def _arithmetic_sum() -> Expected:
    """La suma de dos números al azar en `[VOICE_ARITHMETIC_MIN, VOICE_ARITHMETIC_MAX]`: el servidor genera los
    sumandos (para renderizar «¿Cuánto es a más b?») y la respuesta esperada es su total. Cambia en cada intento:
    prueba cognitiva y antirreplay, no un dato de identidad (nunca viaja al cliente más que como texto ya armado)."""
    span = settings.VOICE_ARITHMETIC_MAX - settings.VOICE_ARITHMETIC_MIN + 1
    a = settings.VOICE_ARITHMETIC_MIN + secrets.randbelow(span)
    b = settings.VOICE_ARITHMETIC_MIN + secrets.randbelow(span)
    return Expected(VoiceQuestion.ARITHMETIC_SUM, str(a + b), render=(("a", a), ("b", b)))


def eligible(db: Session, employee: Employee, company_name: str) -> list[Expected]:
    """Las preguntas que SÍ se le pueden hacer a este empleado, con su respuesta (tres consultas acotadas como mucho:
    el nombre de su departamento, su asignación de turno vigente y los sitios de ese turno).

    Un conjunto amplio para variar y al azar: siempre los datos fiables (nombre, apellidos, nombre completo, fecha de
    nacimiento y sus partes, empresa) más la suma; condicionales el número de empleado, el departamento vigente, los
    sitios activos del turno y —solo si los apellidos se separan en EXACTAMENTE dos palabras— el primer y el segundo
    apellido. La fecha y sus partes viajan en ISO (la comparación extrae lo que toca)."""
    birth = employee.birth_date.isoformat()
    options = [
        Expected(VoiceQuestion.FIRST_NAME, employee.first_name),
        Expected(VoiceQuestion.SURNAMES, employee.last_name),
        Expected(VoiceQuestion.FULL_NAME, employee.full_name),
        Expected(VoiceQuestion.BIRTH_DATE, birth),
        Expected(VoiceQuestion.BIRTH_MONTH, birth),
        Expected(VoiceQuestion.BIRTH_YEAR, birth),
        Expected(VoiceQuestion.BIRTH_DAY, birth),
        Expected(VoiceQuestion.COMPANY_NAME, company_name),
        _arithmetic_sum(),
    ]
    # Primer y segundo apellido solo cuando `last_name` tiene exactamente dos palabras («Pérez López»): un apellido
    # compuesto («De la Cruz») no se puede partir con certeza, así que esas dos preguntas se omiten y queda `SURNAMES`.
    surnames = employee.last_name.split()
    if len(surnames) == 2:
        options.append(Expected(VoiceQuestion.FIRST_SURNAME, surnames[0]))
        options.append(Expected(VoiceQuestion.SECOND_SURNAME, surnames[1]))
    if employee.employee_number:
        options.append(Expected(VoiceQuestion.EMPLOYEE_NUMBER, employee.employee_number))
    if employee.department_id is not None:
        # `get` solo ve lo vigente: un departamento en «Eliminados» ya no es un dato de la persona (los nombres del
        # historial, `names`, sí lo incluirían).
        department = DepartmentRepository(db, employee.company_id).get(employee.department_id)
        if department is not None:
            options.append(Expected(VoiceQuestion.DEPARTMENT, department.name))
    shifts = ShiftRepository(db, employee.company_id)
    assignment = shifts.current_assignments([employee.id], business_today()).get(employee.id)
    if assignment is not None:
        sites = shifts.sites_of_shifts([assignment.shift_id]).get(assignment.shift_id, [])
        names_of_sites = [site.name for site in sites if site.active and not site.deleted]
        if names_of_sites:
            options.append(Expected(VoiceQuestion.WORK_SITE, ANY_OF.join(names_of_sites)))
    return options


def choose(options: list[Expected], count: int) -> list[Expected]:
    """`count` preguntas distintas al azar (criptográfico); si hay menos elegibles, todas."""
    return secrets.SystemRandom().sample(options, min(count, len(options)))


def seal(session: VoiceSession) -> str:
    payload: dict[str, Any] = {
        "e": session.enrollment_id,
        "u": session.user_id,
        "c": session.company_id,
        "q": [[question.kind.value, question.answer] for question in session.questions],
        "p": list(session.passed),
        "t": list(session.tries),
        "x": session.expires,
    }
    return _SEAL.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode()


def unseal(token: str, user_id: int, now: int) -> VoiceSession | None:
    """La sesión del token si es íntegra, de este usuario y no venció; None si no (quien llama decide el código: un
    token vencido se distingue con `expired`)."""
    session = _open(token)
    if session is None or session.user_id != user_id or session.expires <= now:
        return None
    return session


def expired(token: str, user_id: int, now: int) -> bool:
    """¿El token es válido y de este usuario, pero ya venció? (para decir «venció» en lugar de «no es válido»)."""
    session = _open(token)
    return session is not None and session.user_id == user_id and session.expires <= now


def _open(token: str) -> VoiceSession | None:
    try:
        payload = json.loads(_SEAL.decrypt(token.encode()))
        return VoiceSession(
            enrollment_id=int(payload["e"]),
            user_id=int(payload["u"]),
            company_id=int(payload["c"]),
            questions=tuple(Expected(VoiceQuestion(kind), str(answer)) for kind, answer in payload["q"]),
            passed=tuple(bool(flag) for flag in payload["p"]),
            tries=tuple(int(count) for count in payload["t"]),
            expires=int(payload["x"]),
        )
    except InvalidToken, ValueError, KeyError, TypeError:
        return None
