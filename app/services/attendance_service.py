"""Asistencia por turno: entrada, descansos y salida, cada uno con rostro y ubicación.

A prueba de trampas:
- La hora es SIEMPRE la del servidor (en la hora del negocio para decidir el día y la jornada),
  nunca la del dispositivo.
- Cada registro del empleado se confirma con su rostro y prueba de vida (`VerificationService`):
  nadie checa por otro. La verificación queda en la bitácora y se enlaza al registro.
- La ubicación se exige siempre: con la precisión mínima de la política y, los días que no son
  remotos, dentro de la geocerca de uno de los sitios de su turno (el turno dice dónde y cuándo: sus
  sitios y sus días remotos de hoy; editarlo aplica desde ese momento). Un registro más lejos de lo que se puede
  viajar desde el anterior se rechaza (ubicación falsificada o cuenta compartida).
- El estado lo decide el servidor: solo se permiten las acciones que tienen sentido ahora (no hay
  salida sin entrada ni dos entradas para el mismo turno). Dos registros simultáneos se atienden en
  orden (candado de fila del empleado) y los índices únicos de la base cierran cualquier carrera.
- Las identificaciones en un validador cuentan como registros en sitio (`from_validator`).
- Código de sitio (antifraude 2b, decisión D9): en un sitio que lo activó, la entrada y la salida llevan el código
  rotativo de su kiosco (`site_codes`): obligatorio, se revisa antes de usar el rostro; en «Solo medir», es una señal.

Solo dentro de sus turnos (decisión del dueño del producto):
- La entrada, en la ventana de la jornada: desde `early_check_in_minutes` antes de la entrada y antes
  de la salida programada (después de la salida ya no se entra a ese turno).
- El descanso es libre dentro del horario: el empleado lo toma cuando quiera, pero solo entre la
  entrada y la salida programadas (no en el margen para checar antes ni después de la salida) y
  mientras le queden descansos. Terminarlo siempre se puede. Un descanso que sigue abierto al salir
  termina al checar la salida (o en el límite de salida si no la checa): el tiempo en descanso nunca
  cuenta como trabajado.
- En un día libre (festivo o ausencia aprobada, salvo que la empresa se lo marque como laborable) no
  se checa la entrada: el servidor responde 409 `DAY_OFF` con el motivo. El día que cuenta es el de
  la jornada: un turno nocturno es del día en que entra.

Turnos nocturnos y turnos que se cruzan los resuelve `shift_rules` (la jornada de cada momento).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.clock import as_utc
from app.core.config import settings
from app.core.exceptions import AppError, ConflictError, PermissionDeniedError, UnprocessableError
from app.core.geo import distance_m
from app.core.ip_intel import IpInfo
from app.i18n import DayMonth, Text, t
from app.models import (
    AttendanceAction,
    AttendanceEvent,
    AttendanceReviewStatus,
    Employee,
    Shift,
    ShiftAssignment,
    SignalMode,
    User,
    VerificationLog,
    WorkBreak,
    WorkMode,
    WorkSession,
    WorkSessionStatus,
    WorkSite,
)
from app.repositories.attendance_repository import AttendanceRepository
from app.repositories.employee_repository import EmployeeRepository
from app.repositories.shift_repository import ShiftRepository
from app.schemas.attendance import (
    AttendanceActionResult,
    AttendanceToday,
    BreakRead,
    BreakWindowRead,
    OccurrenceRead,
    WorkSessionRead,
)
from app.schemas.auth import DeviceLocation
from app.schemas.capture import LocationSample
from app.schemas.verification import ValidatorAttendance, VerificationResult
from app.services import site_codes
from app.services.calendar_rules import DayOff, DaysOff
from app.services.calendar_service import CalendarService, day_off_read
from app.services.location_service import format_distance
from app.services.policy_service import PolicyService, PolicySnapshot
from app.services.risk_engine import LocationEvidence, PreviousPunch, location_context
from app.services.risk_rules import Hit
from app.services.shift_rules import (
    Occurrence,
    closest,
    day_bit,
    in_working_hours,
    late_minutes,
    minutes_between,
    occurrence_of,
    works_on,
)
from app.services.shift_service import shift_ref
from app.services.site_service import site_ref

#: Una identificación en el validador poco después de la entrada no es la salida (doble lectura).
VALIDATOR_MIN_SHIFT_MINUTES = 30
#: Distancias menores no se consideran viaje (el error normal del GPS entre dos lecturas).
TRAVEL_MIN_KM = 1.0
#: Media vuelta al mundo (km): la mayor distancia entre dos puntos; acota la búsqueda del registro anterior.
HALF_EARTH_KM = 20_038
#: Días hacia adelante en que se busca la siguiente jornada para mostrarla.
NEXT_SHIFT_HORIZON_DAYS = 8

#: Llave del mensaje de cada acción registrada (catálogo de mensajes, en el idioma de la petición).
ACTION_DONE = {
    AttendanceAction.CHECK_IN: "CHECK_IN_RECORDED",
    AttendanceAction.BREAK_START: "BREAK_STARTED",
    AttendanceAction.BREAK_END: "BREAK_ENDED",
    AttendanceAction.CHECK_OUT: "CHECK_OUT_RECORDED",
}


def done_text(action: AttendanceAction, review: bool) -> Text:
    """Lo que se le dice a la persona al registrar; si el motor de riesgo lo dejó "en revisión", también eso (sin
    decirle qué lo causó)."""
    done = Text(ACTION_DONE[action])
    return Text("ATTENDANCE_DONE_UNDER_REVIEW", {"done": done}) if review else done


@dataclass(frozen=True)
class Verified:
    """Lo que devuelve la verificación facial de un registro: el resultado, su intento en la bitácora, sus motivos de
    negocio si quedó "en revisión" (catalog.review_reasons) y la red de su IP (se guarda con el registro)."""

    result: VerificationResult
    log: VerificationLog | None = None
    review: tuple[str, ...] = ()
    network: IpInfo | None = None


@dataclass(frozen=True)
class _Travel:
    """El registro anterior con ubicación y lo recorrido desde él (km ya sin la incertidumbre de ambas lecturas)."""

    previous: AttendanceEvent
    km: float
    hours: float

    @property
    def speed_kmh(self) -> float:
        return self.km / self.hours


def flag_review(session: WorkSession, reasons: tuple[str, ...]) -> None:
    """La jornada queda "en revisión" (otra vez, si ya se había confirmado o rechazado) con sus motivos acumulados."""
    known = [code for code in (session.review_reasons or "").split(",") if code]
    merged = known + [code for code in reasons if code not in known]
    session.review_status = AttendanceReviewStatus.PENDING
    session.review_reasons = ",".join(merged)[:200] or None
    session.reviewed_by_id = session.reviewed_at = session.review_note = None


def now_utc() -> datetime:
    """La hora del servidor: la única que cuenta para la asistencia (las pruebas la fijan aquí)."""
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.APP_TIMEZONE)


def _clock(moment: datetime) -> time:
    """La hora del instante en la zona del negocio (se escribe como la acostumbra cada idioma: 07:55 o 7:55 AM)."""
    return as_utc(moment).astimezone(_zone()).time()


@dataclass(frozen=True)
class _Slot:
    """Una jornada programada con la asignación y el turno de los que sale."""

    assignment: ShiftAssignment
    shift: Shift
    occurrence: Occurrence


@dataclass
class _State:
    """Lo que hay ahora: la jornada abierta, o la que se puede checar (y si ya se registró o es libre)."""

    now: datetime
    session: WorkSession | None
    slot: _Slot | None
    #: La jornada de `slot` ya registrada (completa o sin salida): no se vuelve a checar.
    done: WorkSession | None
    open_break: WorkBreak | None
    breaks_used: int
    #: El día de `slot` no se trabaja (festivo o ausencia aprobada): no se checa la entrada.
    day_off: DayOff | None = None

    @property
    def can_break(self) -> bool:
        """Le quedan descansos y está dentro de su horario (de la entrada a la salida programadas)."""
        session = cast(WorkSession, self.session)
        start, end = as_utc(session.scheduled_start), as_utc(session.scheduled_end)
        return self.breaks_used < session.breaks_allowed and in_working_hours(start, end, self.now)

    @property
    def can_check_in(self) -> bool:
        """Una jornada en su ventana, sin registrar, que no es día libre y cuya salida no ha llegado."""
        slot = self.slot
        return (
            self.session is None
            and slot is not None
            and self.done is None
            and self.day_off is None
            and self.now < slot.occurrence.end
        )

    @property
    def actions(self) -> list[AttendanceAction]:
        if self.session is not None:
            if self.open_break is not None:
                return [AttendanceAction.BREAK_END, AttendanceAction.CHECK_OUT]
            return (
                [AttendanceAction.BREAK_START, AttendanceAction.CHECK_OUT]
                if self.can_break
                else [AttendanceAction.CHECK_OUT]
            )
        return [AttendanceAction.CHECK_IN] if self.can_check_in else []

    def day_off_reason(self, today: date) -> Text:
        slot, day_off = cast(_Slot, self.slot), cast(DayOff, self.day_off)
        return day_off.explain(slot.occurrence.work_date, today)

    def denial(self, action: AttendanceAction, today: date) -> AppError:
        """Por qué no se puede esa acción ahora (con su código estable)."""
        if self.session is None and self.day_off is not None:
            reason = self.day_off_reason(today)
            return ConflictError(
                code="DAY_OFF",
                key=reason.key,
                params=reason.params,
                details={"kind": self.day_off.kind, "name": self.day_off.name},
            )
        reasons: dict[AttendanceAction, Callable[[], Text]] = {
            AttendanceAction.CHECK_IN: self._no_check_in,
            AttendanceAction.BREAK_START: self._no_break,
            AttendanceAction.BREAK_END: lambda: Text("NO_BREAK_IN_PROGRESS"),
            AttendanceAction.CHECK_OUT: lambda: Text("NO_CHECK_IN_FOR_CHECK_OUT"),
        }
        reason = reasons[action]()
        return ConflictError(
            code="ATTENDANCE_ACTION_NOT_ALLOWED",
            key=reason.key,
            params=reason.params,
            details={"allowed": self.actions},
        )

    def _no_check_in(self) -> Text:
        if self.session is not None:
            return Text("ALREADY_CHECKED_IN")
        if self.done is not None:
            return Text("SHIFT_ALREADY_RECORDED")
        if self.slot is not None:
            return Text("SHIFT_ENDED_NO_CHECK_IN", {"time": _clock(self.slot.occurrence.end)})
        return Text("NO_SHIFT_NOW")

    def _no_break(self) -> Text:
        session = self.session
        if session is None:
            return Text("NOT_CHECKED_IN")
        if self.open_break is not None:
            return Text("ALREADY_ON_BREAK")
        if self.breaks_used >= session.breaks_allowed:
            return Text("BREAKS_USED")
        return Text("BREAK_WINDOW", {"start": _clock(session.scheduled_start), "end": _clock(session.scheduled_end)})


@dataclass(frozen=True)
class _Place:
    mode: WorkMode
    site: WorkSite | None
    distance: float | None


@dataclass(frozen=True)
class _Upcoming:
    """La siguiente jornada que sí se trabaja y el primer día libre que se saltó para encontrarla."""

    slot: _Slot | None
    skipped: tuple[DayOff, date] | None


def new_session(
    assignment: ShiftAssignment, shift: Shift, occurrence: Occurrence, check_in: datetime, mode: WorkMode
) -> WorkSession:
    """La jornada que empieza con su entrada, con la copia de lo programado (lo mismo en vivo que
    cuando la registra la empresa)."""
    return WorkSession(
        employee_id=assignment.employee_id,
        assignment_id=assignment.id,
        work_date=occurrence.work_date,
        shift_name=shift.name,
        # En UTC: así se guardan y se buscan igual en cualquier motor.
        scheduled_start=occurrence.start.astimezone(UTC),
        scheduled_end=occurrence.end.astimezone(UTC),
        check_out_deadline=occurrence.deadline.astimezone(UTC),
        breaks_allowed=shift.breaks_count,
        break_minutes_allowed=shift.break_minutes,
        early_check_out_minutes=shift.early_check_out_minutes,
        status=WorkSessionStatus.OPEN,
        check_in_at=check_in,
        check_in_mode=mode,
        late_minutes=late_minutes(shift, occurrence, check_in),
    )


def end_break(session: WorkSession, open_break: WorkBreak, end: datetime) -> None:
    """Termina un descanso: sus minutos se restan de lo trabajado y lo que pasa de lo permitido queda
    como exceso."""
    minutes = minutes_between(as_utc(open_break.started_at), end)
    open_break.ended_at = end
    open_break.exceeded_minutes = max(0, minutes - session.break_minutes_allowed)
    session.break_minutes += minutes


def finish(session: WorkSession, check_out: datetime, mode: WorkMode, site_id: int | None) -> None:
    """La salida: salida anticipada (más allá de su tolerancia) y minutos trabajados sin descansos."""
    session.check_out_at = check_out
    session.check_out_mode = mode
    session.check_out_site_id = site_id
    early = int((as_utc(session.scheduled_end) - check_out).total_seconds() // 60)
    session.early_leave_minutes = early if early > session.early_check_out_minutes else 0
    session.worked_minutes = max(0, minutes_between(as_utc(session.check_in_at), check_out) - session.break_minutes)
    session.status = WorkSessionStatus.CLOSED


class AttendanceService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.repo = AttendanceRepository(db, company_id)
        self.shifts = ShiftRepository(db, company_id)
        self.calendar = CalendarService(db, company_id)
        self.zone = _zone()

    # ---------- Qué hay ahora ----------

    def _slots(self, employee_id: int, days: list[date]) -> list[_Slot]:
        """Las jornadas programadas esos días (turno activo que trabaja ese día de la semana)."""
        assignments = self.shifts.assignments_on(employee_id, days)
        shifts = self.shifts.shifts_by_ids(a.shift_id for a in assignments.values())
        slots = []
        for day, assignment in assignments.items():
            shift = shifts[assignment.shift_id]
            if shift.active and works_on(shift, day):
                slots.append(_Slot(assignment, shift, occurrence_of(shift, day, self.zone)))
        return slots

    def _state(self, employee_id: int, now: datetime, *, lock: bool = False, calendar: DaysOff | None = None) -> _State:
        session = self.repo.open_session(employee_id, lock=lock)
        if session is not None and as_utc(session.check_out_deadline) < now:
            # Venció su límite sin salida. Al registrar (con candado) se marca de una vez para que no
            # estorbe a la jornada nueva; al solo consultar, la marca el mantenimiento.
            if lock:
                self.expire(session)
            session = None
        slot = done = day_off = None
        if session is None:
            today = now.astimezone(self.zone).date()
            slots = self._slots(employee_id, [today - timedelta(days=1), today])
            found = closest((s.occurrence for s in slots), now)
            slot = next((s for s in slots if s.occurrence == found), None)
            if slot is not None:
                done = self.repo.session_at(employee_id, slot.occurrence.start.astimezone(UTC))
                work_date = slot.occurrence.work_date
                days = calendar or self.calendar.days_off([employee_id], work_date, work_date)
                day_off = days.on(employee_id, work_date)
        breaks = self.repo.breaks_of([session.id])[session.id] if session is not None else []
        open_break = next((b for b in breaks if b.ended_at is None), None)
        return _State(now, session, slot, done, open_break, len(breaks), day_off)

    def expire(self, session: WorkSession) -> None:
        """Una jornada abierta cuyo límite de salida venció queda sin salida; su descanso abierto
        termina en ese límite."""
        session.status = WorkSessionStatus.MISSED_CHECKOUT
        breaks = self.repo.breaks_of([session.id])[session.id]
        open_break = next((b for b in breaks if b.ended_at is None), None)
        if open_break is not None:  # a lo más uno abierto (índice único): termina en el límite
            open_break.ended_at = session.check_out_deadline
        self.db.flush()

    def _next(self, employee_id: int, now: datetime, calendar: DaysOff) -> _Upcoming:
        """La siguiente jornada que se trabaja (se salta los días libres) y el primer día libre saltado."""
        today = now.astimezone(self.zone).date()
        days = [today + timedelta(days=offset) for offset in range(NEXT_SHIFT_HORIZON_DAYS)]
        upcoming = sorted(
            (s for s in self._slots(employee_id, days) if s.occurrence.opens > now), key=lambda s: s.occurrence.start
        )
        skipped: tuple[DayOff, date] | None = None
        for slot in upcoming:
            day_off = calendar.on(employee_id, slot.occurrence.work_date)
            if day_off is None:
                return _Upcoming(slot, skipped)
            skipped = skipped or (day_off, slot.occurrence.work_date)
        return _Upcoming(None, skipped)

    def today(self, employee: Employee) -> AttendanceToday:
        """Qué puede hacer ahora. Sin jornada que checar, el turno, los sitios y si es remoto son los
        de la siguiente jornada que se trabaja (el empleado sabe dónde y cuándo le toca); en un día
        libre, por qué no trabaja."""
        now = now_utc()
        today = now.astimezone(self.zone).date()
        calendar = self.calendar.days_off(
            [employee.id], today - timedelta(days=1), today + timedelta(days=NEXT_SHIFT_HORIZON_DAYS)
        )
        state = self._state(employee.id, now, calendar=calendar)
        checkable = state.slot if state.session is None and state.day_off is None else None
        upcoming = (
            _Upcoming(None, None)
            if state.session or state.done or state.can_check_in
            else self._next(employee.id, now, calendar)
        )
        occurrence = checkable.occurrence if checkable else None
        shift = self._shift_of(state, checkable) or (upcoming.slot.shift if upcoming.slot else None)
        sites = [site for site in self.shifts.sites_of_shifts([shift.id])[shift.id] if site.active] if shift else []
        shown = occurrence or (upcoming.slot.occurrence if upcoming.slot else None)
        work_date = state.session.work_date if state.session else shown.work_date if shown else None
        day_off = self._shown_day_off(state, calendar, upcoming, employee.id, today)
        return AttendanceToday(
            now=now,
            shift=shift_ref(shift) if shift else None,
            occurrence=self._occurrence(occurrence),
            next_occurrence=self._occurrence(upcoming.slot.occurrence if upcoming.slot else None),
            session=self._read(state.session) if state.session else self._read(state.done) if state.done else None,
            actions=[a.value for a in state.actions],
            remote_allowed=bool(shift and work_date and shift.remote_weekdays & day_bit(work_date)),
            sites=[site_ref(site) for site in sites],
            message=self._explain(state, upcoming, day_off, today),
            day_off=day_off_read(*day_off) if day_off else None,
            break_window=self._break_window(state),
            site_code=self._asks_site_code(sites),
        )

    def _asks_site_code(self, sites: list[WorkSite]) -> bool:
        """La app pide el código del sitio antes de la entrada y la salida: algún sitio del turno lo activó y la
        política lo revisa (antifraude 2b; la política está en la caché de cada proceso: sin consultas)."""
        if not any(site.presence_code for site in sites):
            return False
        return PolicyService(self.db, self.company_id).current().site_codes != SignalMode.OFF

    @staticmethod
    def _shown_day_off(
        state: _State, calendar: DaysOff, upcoming: _Upcoming, employee_id: int, today: date
    ) -> tuple[DayOff, date] | None:
        """El día libre que se le muestra: el de la jornada que no puede checar, el de hoy si ahora no
        tiene jornada, o el que se saltó al buscar su siguiente turno (si no encontró ninguno)."""
        if state.session is not None or state.done is not None:
            return None
        if state.day_off is not None:
            return state.day_off, cast(_Slot, state.slot).occurrence.work_date
        off_today = calendar.on(employee_id, today) if state.slot is None else None
        if off_today is not None:
            return off_today, today
        return upcoming.skipped if upcoming.slot is None else None

    def _shift_of(self, state: _State, checkable: _Slot | None) -> Shift | None:
        """El turno de la jornada abierta (el de hoy: dónde se checa la salida) o el de la que se puede
        checar."""
        if state.session is not None:
            return self.shifts.shift_of(state.session.assignment_id)
        return checkable.shift if checkable else None

    @staticmethod
    def _occurrence(occurrence: Occurrence | None) -> OccurrenceRead | None:
        if occurrence is None:
            return None
        return OccurrenceRead(
            work_date=occurrence.work_date,
            start=occurrence.start,
            end=occurrence.end,
            opens=occurrence.opens,
            deadline=occurrence.deadline,
        )

    @staticmethod
    def _break_window(state: _State) -> BreakWindowRead | None:
        session = state.session
        if session is None:
            return None
        return BreakWindowRead(
            starts_at=as_utc(session.scheduled_start),
            ends_at=as_utc(session.scheduled_end),
            minutes=session.break_minutes_allowed,
            remaining=max(0, session.breaks_allowed - state.breaks_used),
        )

    def _explain(self, state: _State, upcoming: _Upcoming, day_off: tuple[DayOff, date] | None, today: date) -> str:
        """Qué hay ahora, para la persona (en el idioma de la petición)."""
        if state.session is not None:
            if state.open_break is not None:
                return t("ON_BREAK_SINCE", {"time": _clock(state.open_break.started_at)})
            since, until = _clock(state.session.check_in_at), _clock(state.session.scheduled_end)
            return t("ON_SHIFT_SINCE", {"since": since, "until": until})
        if state.done is not None:
            return t("SHIFT_ALREADY_RECORDED_TODAY")
        if day_off is not None:
            return self._then(Text("SENTENCE", {"text": day_off[0].explain(day_off[1], today)}), upcoming)
        if state.can_check_in:
            occurrence = cast(_Slot, state.slot).occurrence
            return t("SHIFT_CHECK_IN_NOW", {"start": _clock(occurrence.start), "end": _clock(occurrence.end)})
        if state.slot is not None:
            return self._then(Text("SHIFT_ENDED_UNRECORDED", {"time": _clock(state.slot.occurrence.end)}), upcoming)
        upcoming_text = self._next_text(upcoming)
        return str(upcoming_text) if upcoming_text else t("NO_SHIFT_ASSIGNED")

    def _then(self, now: Text, upcoming: _Upcoming) -> str:
        """Lo de ahora y, si hay, la siguiente jornada."""
        upcoming_text = self._next_text(upcoming)
        return t("TWO_SENTENCES", {"first": now, "second": upcoming_text}) if upcoming_text else str(now)

    def _next_text(self, upcoming: _Upcoming) -> Text | None:
        if upcoming.slot is None:
            return None
        occurrence = upcoming.slot.occurrence
        day = DayMonth(occurrence.start.astimezone(self.zone).date())
        start, opens = _clock(occurrence.start), _clock(occurrence.opens)
        return Text("NEXT_SHIFT", {"day": day, "start": start, "opens": opens})

    # ---------- Registrar (empleado) ----------

    def act(
        self,
        employee: Employee,
        user: User,
        action: AttendanceAction,
        location: DeviceLocation,
        verify: Callable[[], Verified],
        samples: tuple[LocationSample, ...] = (),
        site_code: str | None = None,
    ) -> AttendanceActionResult:
        """Valida el momento y el lugar, verifica el rostro y, solo si pasó, registra. Con riesgo alto el registro se
        guarda "en revisión": la persona no se queda sin checar y su empresa lo confirma o lo rechaza (decisión D3).
        `samples`: las lecturas de la ubicación que tomó la app (señales del lugar; la que decide es `location`);
        `site_code`: el código del kiosco del sitio, escrito o escaneado (antifraude 2b)."""
        now = now_utc()
        policy = PolicyService(self.db, self.company_id).current()
        state = self._state(employee.id, now)
        self._ensure_allowed(state, action)
        place = self._place(state, location, policy)
        travel = self._travel(employee.id, location, now, policy)
        self._ensure_plausible(travel, policy)
        # Antes de usar el rostro: con el código obligatorio, lo que no vale se rechaza aquí (el reto sigue sin usarse).
        previous = travel.previous if travel else None
        presence = site_codes.check(
            place.site, action, site_code, previous=previous, mode=policy.site_codes, now=now.timestamp()
        )
        evidence = self._evidence(location, place, samples, travel, policy, presence.hits)
        with location_context(evidence):  # las señales del lugar y de la red para el motor de riesgo
            verified = verify()  # el rostro (registra su intento en la bitácora y confirma)
        result, log, review, network = verified.result, verified.log, verified.review, verified.network
        if not result.verified:
            return AttendanceActionResult(verified=False, message=result.message, action=action, verification=result)
        EmployeeRepository(self.db, self.company_id).get_by_id(employee.id, lock=True)  # en orden con otros registros
        state = self._state(employee.id, now, lock=True)  # pudo cambiar mientras se verificaba el rostro
        self._ensure_allowed(state, action)
        session = self._apply(state, action, place, now)
        if result.review:
            flag_review(session, review)
        self.repo.add(
            AttendanceEvent(
                employee_id=employee.id,
                session_id=session.id,
                action=action,
                mode=place.mode,
                site_id=place.site.id if place.site else None,
                occurred_at=now,
                latitude=location.latitude,
                longitude=location.longitude,
                accuracy_m=location.accuracy,
                distance_m=place.distance,
                verification_log_id=log.id if log else None,
                actor_id=user.id,
                under_review=result.review,
                ip_country=network.country if network else None,
                ip_asn=network.asn if network else None,
                presence_window=presence.window,
            )
        )
        self.db.commit()
        return AttendanceActionResult(
            verified=True,
            message=str(done_text(action, result.review)),
            action=action,
            verification=result,
            session=self._read(session),
        )

    def _ensure_allowed(self, state: _State, action: AttendanceAction) -> None:
        if action not in state.actions:
            raise state.denial(action, state.now.astimezone(self.zone).date())

    def _place(self, state: _State, location: DeviceLocation, policy: PolicySnapshot) -> _Place:
        """En sitio (dentro de la geocerca de un sitio activo de su turno), remoto (si su turno lo permite
        ese día) o rechazo."""
        accuracy = location.accuracy
        if accuracy is None or accuracy > policy.max_location_accuracy_m:
            raise UnprocessableError(
                code="LOCATION_INACCURATE",
                key="LOCATION_NOT_PRECISE",
                params={
                    "accuracy": format_distance(accuracy or 0),
                    "required": format_distance(policy.max_location_accuracy_m),
                },
                details={"accuracy_m": accuracy, "max_accuracy_m": policy.max_location_accuracy_m},
            )
        shift, work_date = self._target(state)
        sites = [site for site in self.shifts.sites_of_shifts([shift.id])[shift.id] if site.active]
        measured = [(site, self._distance(site, location)) for site in sites]
        margin = min(accuracy, settings.VALIDATOR_LOCATION_TOLERANCE_M)
        inside = [(site, d) for site, d in measured if d - margin <= site.radius_m]
        if inside:
            site, distance = min(inside, key=lambda pair: pair[1])
            return _Place(WorkMode.ON_SITE, site, distance)
        if shift.remote_weekdays & day_bit(work_date):
            return _Place(WorkMode.REMOTE, None, None)
        nearest = min(measured, key=lambda pair: pair[1], default=None)
        raise PermissionDeniedError(
            code="LOCATION_OUT_OF_SITE",
            key="LOCATION_OUT_OF_SITE_NEAREST" if nearest else "LOCATION_OUT_OF_SITE",
            params={"distance": format_distance(nearest[1]), "site": nearest[0].name} if nearest else None,
            details={
                "distance_m": round(nearest[1]) if nearest else None,
                "site": nearest[0].name if nearest else None,
            },
        )

    def _target(self, state: _State) -> tuple[Shift, date]:
        """El turno y el día de la jornada de la acción (ya permitida: hay jornada abierta o una jornada
        que se puede checar)."""
        if state.session is not None:
            return cast(Shift, self.shifts.shift_of(state.session.assignment_id)), state.session.work_date
        slot = cast(_Slot, state.slot)
        return slot.shift, slot.occurrence.work_date

    @staticmethod
    def _distance(site: WorkSite, location: DeviceLocation) -> float:
        # Un sitio siempre tiene su punto (CHECK ck_work_sites_coordinates).
        return distance_m(
            cast(float, site.latitude), cast(float, site.longitude), location.latitude, location.longitude
        )

    def _travel(
        self, employee_id: int, location: DeviceLocation, now: datetime, policy: PolicySnapshot
    ) -> _Travel | None:
        """El registro anterior del empleado con ubicación y lo recorrido desde él (UNA lectura: sirve al viaje
        imposible y a las señales LOCATION_JUMP y NETWORK_JUMP)."""
        # Más atrás de lo que se recorre a la velocidad máxima en media vuelta al mundo, ningún viaje es imposible:
        # esa es la ventana (el resultado no cambia y solo se leen sus meses de la bitácora).
        horizon = timedelta(hours=HALF_EARTH_KM / policy.max_travel_kmh)
        last = self.repo.last_located_event(employee_id, now - horizon)
        if last is None:
            return None
        # Solo se buscan registros con ubicación (y su latitud y longitud van juntas: CHECK).
        meters = distance_m(
            cast(float, last.latitude), cast(float, last.longitude), location.latitude, location.longitude
        )
        uncertainty = (last.accuracy_m or 0) + (location.accuracy or 0)
        hours = max((now - as_utc(last.occurred_at)).total_seconds() / 3600, 1 / 3600)
        return _Travel(last, max(0.0, meters - uncertainty) / 1000, hours)

    @staticmethod
    def _ensure_plausible(travel: _Travel | None, policy: PolicySnapshot) -> None:
        """Rechaza un registro más lejos de lo que se puede viajar desde el anterior."""
        if travel is None or not policy.detect_impossible_travel:
            return
        if travel.km > TRAVEL_MIN_KM and travel.speed_kmh > policy.max_travel_kmh:
            raise PermissionDeniedError(
                code="IMPOSSIBLE_TRAVEL",
                details={"distance_km": round(travel.km, 1), "minutes": round(travel.hours * 60)},
            )

    @staticmethod
    def _evidence(
        location: DeviceLocation,
        place: _Place,
        samples: tuple[LocationSample, ...],
        travel: _Travel | None,
        policy: PolicySnapshot,
        checks: tuple[Hit, ...] = (),
    ) -> LocationEvidence:
        """Lo que el motor de riesgo mide del lugar y de la red del registro (antifraude 1b) y lo que ya se verificó del
        lugar (el código de sitio, antifraude 2b)."""
        speed: float | None = None
        previous: PreviousPunch | None = None
        if travel is not None:
            speed = travel.speed_kmh if travel.km > TRAVEL_MIN_KM else None
            last = travel.previous
            previous = PreviousPunch(travel.hours * 60, last.ip_country, last.ip_asn)
        return LocationEvidence(
            accuracy_m=location.accuracy or 0.0,
            distance_m=place.distance,
            radius_m=place.site.radius_m if place.site else None,
            samples=samples,
            speed_kmh=speed,
            jump_kmh=policy.max_travel_kmh * settings.RISK_LOCATION_JUMP_RATIO,
            site_country=place.site.country_code if place.site else None,
            previous=previous,
            checks=checks,
        )

    # ---------- Aplicar la acción ----------

    def _apply(self, state: _State, action: AttendanceAction, place: _Place, now: datetime) -> WorkSession:
        """La acción ya está permitida: la entrada tiene su jornada; lo demás, la jornada abierta."""
        if action == AttendanceAction.CHECK_IN:
            return self._check_in(cast(_Slot, state.slot), place, now)
        session = cast(WorkSession, state.session)
        if action == AttendanceAction.BREAK_START:
            self.repo.add(WorkBreak(session_id=session.id, started_at=now))
        elif action == AttendanceAction.BREAK_END:
            end_break(session, cast(WorkBreak, state.open_break), now)
        else:
            self._check_out(session, state.open_break, place, now)
        return session

    def _check_in(self, slot: _Slot, place: _Place, now: datetime) -> WorkSession:
        session = new_session(slot.assignment, slot.shift, slot.occurrence, now, place.mode)
        session.check_in_site_id = place.site.id if place.site else None
        self.repo.add(session)
        return session

    @staticmethod
    def _check_out(session: WorkSession, open_break: WorkBreak | None, place: _Place, now: datetime) -> None:
        if open_break is not None:  # salir con un descanso en curso lo termina
            end_break(session, open_break, now)
        finish(session, now, place.mode, place.site.id if place.site else None)

    # ---------- Validadores ----------

    def from_validator(
        self, employee: Employee, operator: User, log: VerificationLog | None, *, review: tuple[str, ...] = ()
    ) -> ValidatorAttendance:
        """Una identificación exitosa en un validador registra la entrada o la salida (en sitio). Con riesgo alto
        (`review`: sus motivos de negocio) queda "en revisión"."""
        now = now_utc()
        EmployeeRepository(self.db, self.company_id).get_by_id(employee.id, lock=True)
        state = self._state(employee.id, now, lock=True)
        place = _Place(WorkMode.VALIDATOR, None, None)
        if state.session is not None:
            if minutes_between(as_utc(state.session.check_in_at), now) < VALIDATOR_MIN_SHIFT_MINUTES:
                return ValidatorAttendance(
                    message=t("CHECK_IN_ALREADY_AT", {"time": _clock(state.session.check_in_at)})
                )
            action, session = AttendanceAction.CHECK_OUT, state.session
            self._check_out(session, state.open_break, place, now)
        elif state.can_check_in:
            action, session = AttendanceAction.CHECK_IN, self._check_in(cast(_Slot, state.slot), place, now)
        elif state.day_off is not None:
            reason = state.day_off_reason(now.astimezone(self.zone).date())
            return ValidatorAttendance(message=t("NOT_RECORDED_REASON", {"reason": reason}))
        else:
            return ValidatorAttendance(message=t("NO_SHIFT_TO_RECORD"))
        if review:
            flag_review(session, review)
        self.repo.add(
            AttendanceEvent(
                employee_id=employee.id,
                session_id=session.id,
                action=action,
                mode=WorkMode.VALIDATOR,
                occurred_at=now,
                verification_log_id=log.id if log else None,
                actor_id=operator.id,
                under_review=bool(review),
            )
        )
        self.db.commit()
        done = done_text(action, bool(review))
        return ValidatorAttendance(action=action, message=t("RECORDED_AT", {"done": done, "time": _clock(now)}))

    # ---------- Lectura ----------

    def _read(self, session: WorkSession) -> WorkSessionRead:
        """La jornada como la ve el empleado que registra (sin los motivos de una revisión)."""
        return self.reads([session], reasons=False)[0]

    def reads(self, sessions: list[WorkSession], *, reasons: bool = True) -> list[WorkSessionRead]:
        """Las jornadas como las ve la empresa; `reasons=False` (el empleado) omite por qué quedó "en revisión"
        (nunca se le dice qué lo delató)."""
        breaks = self.repo.breaks_of(s.id for s in sessions)
        names = self.repo.site_names(site for s in sessions for site in (s.check_in_site_id, s.check_out_site_id))
        return [
            WorkSessionRead(
                id=s.id,
                work_date=s.work_date,
                shift_name=s.shift_name,
                scheduled_start=s.scheduled_start,
                scheduled_end=s.scheduled_end,
                check_out_deadline=s.check_out_deadline,
                status=s.status,
                check_in_at=s.check_in_at,
                check_in_mode=s.check_in_mode,
                check_in_site=names.get(s.check_in_site_id or 0),
                check_out_at=s.check_out_at,
                check_out_mode=s.check_out_mode,
                check_out_site=names.get(s.check_out_site_id or 0),
                late_minutes=s.late_minutes,
                early_leave_minutes=s.early_leave_minutes,
                break_minutes=s.break_minutes,
                worked_minutes=s.worked_minutes,
                breaks_allowed=s.breaks_allowed,
                break_minutes_allowed=s.break_minutes_allowed,
                breaks=[
                    BreakRead(
                        started_at=b.started_at,
                        ended_at=b.ended_at,
                        minutes=minutes_between(as_utc(b.started_at), as_utc(b.ended_at)) if b.ended_at else 0,
                        exceeded_minutes=b.exceeded_minutes,
                    )
                    for b in breaks[s.id]
                ],
                edited_at=s.edited_at,
                edit_reason=s.edit_reason,
                review_status=s.review_status,
                review_reasons=[code for code in (s.review_reasons or "").split(",") if code] if reasons else [],
                reviewed_at=s.reviewed_at,
                review_note=s.review_note,
            )
            for s in sessions
        ]
