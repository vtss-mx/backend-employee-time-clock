"""Dispositivos de los empleados (decisión D2 del dueño del producto; antifraude 1b, docs/rd §2.6 y §2.7).

El mismo mecanismo de los validadores (`device_service`): la app genera en el navegador una llave ECDSA P-256 NO
exportable (IndexedDB, `utils/deviceKey.ts`) y, con cada registro de asistencia o verificación propia, firma el reto
(`device_nonce`) que recibió junto con el de la prueba de vida. El servidor verifica la firma con la llave que llega y
guarda solo su hash (`workforce.employee_devices`). Así "este navegador en este teléfono" queda probado en cada intento
sin datos de la persona ni huella del navegador.

Modo de la empresa (`verification_policy.employee_device_mode`, lo fija el ADMIN; por omisión «Solo medir»):

- `OFF`: no se vincula nada (ni reto ni señales).
- `OBSERVE`: un dispositivo que el empleado no había usado (`DEVICE_NEW`) o un intento sin llave válida
  (`DEVICE_KEY_MISSING`) son señales del motor de riesgo; nada más.
- `STEP_UP`: un dispositivo que no es de confianza pide un paso más; al superarlo ahí, queda de confianza.
- `APPROVAL`: mientras la empresa no apruebe el dispositivo, sus registros quedan "en revisión" (decisión D10: los
  revisa la empresa). Ninguno de los modos niega.

En cualquier modo, la misma llave usada por varios empleados de la empresa en `RISK_DEVICE_SHARED_WINDOW_MINUTES` es
`DEVICE_SHARED` (un teléfono que checa por varios compañeros). La empresa ve, aprueba y revoca los dispositivos desde
la ficha del empleado; el empleado ve los suyos en Mi perfil.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.devices import classify_device, device_label
from app.core.exceptions import ConflictError, NotFoundError
from app.i18n import stored
from app.models import DeviceStatus, Employee, EmployeeDevice, EmployeeDeviceMode, User
from app.repositories.employee_device_repository import EmployeeDeviceRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common import PageParams
from app.schemas.employee_device import EmployeeDeviceList, EmployeeDeviceRead
from app.services.client_evidence import DeviceCheck, DeviceProofInput
from app.services.device_service import key_hash, nonce_is_valid, signature_is_valid

#: Llave del nombre de un dispositivo que el navegador no permite reconocer (se traduce al leerse).
_DEVICE_KINDS = {"phone": "DEVICE_PHONE", "tablet": "DEVICE_TABLET", "desktop": "DEVICE_DESKTOP"}
#: Estados en que la empresa dijo que no (nunca son de confianza).
_REFUSED = frozenset({DeviceStatus.REVOKED, DeviceStatus.REJECTED})
#: Cambios que hace la empresa: aprobar (desde cualquiera que no lo esté) y revocar (uno por decidir o aprobado).
TRANSITIONS: dict[str, frozenset[str]] = {
    DeviceStatus.APPROVED: frozenset({DeviceStatus.PENDING, DeviceStatus.REVOKED, DeviceStatus.REJECTED}),
    DeviceStatus.REVOKED: frozenset({DeviceStatus.PENDING, DeviceStatus.APPROVED}),
}


def _proven_key(user_id: int, proof: DeviceProofInput) -> str | None:
    """El hash de la llave si la firma del reto de ESTA cuenta verifica; None si no."""
    if not (proof.public_key and proof.nonce and proof.signature):
        return None
    if len(proof.public_key) > 300 or len(proof.nonce) > 200 or len(proof.signature) > 200:
        return None
    signed = signature_is_valid(proof.public_key, proof.nonce, proof.signature)
    if not nonce_is_valid(user_id, proof.nonce) or not signed:
        return None
    return key_hash(proof.public_key)  # la firma ya validó la llave (base64 y curva)


def _trusted(mode: str, status: str, stepped_up: bool) -> bool:
    if status in _REFUSED:
        return False
    if mode == EmployeeDeviceMode.APPROVAL:
        return status == DeviceStatus.APPROVED
    if mode == EmployeeDeviceMode.STEP_UP:
        return status == DeviceStatus.APPROVED or stepped_up
    return True  # OBSERVE: ya lo había usado


def check(
    db: Session,
    company_id: int,
    employee_id: int,
    user_id: int,
    proof: DeviceProofInput,
    *,
    mode: str,
    user_agent: str | None,
    now: datetime,
) -> DeviceCheck | None:
    """El dispositivo del intento (UNA consulta: su fila y quién más de la empresa usó la misma llave); None con el
    modo apagado."""
    if mode == EmployeeDeviceMode.OFF:
        return None
    hashed = _proven_key(user_id, proof)
    if hashed is None:
        return DeviceCheck(mode, invalid=proof.sent)
    since = now - timedelta(minutes=settings.RISK_DEVICE_SHARED_WINDOW_MINUTES)
    rows = EmployeeDeviceRepository(db, company_id).sightings(
        hashed, employee_id, since, settings.RISK_DEVICE_SHARED_MIN_EMPLOYEES + 1
    )
    mine = next((row for row in rows if row.employee_id == employee_id), None)
    others = {row.employee_id for row in rows if row.employee_id != employee_id}
    name = device_label(user_agent) or stored(_DEVICE_KINDS[classify_device(user_agent)])
    if mine is None:
        return DeviceCheck(mode, hashed, shared=1 + len(others), name=name)
    return DeviceCheck(
        mode,
        hashed,
        trusted=_trusted(mode, mine.status, mine.stepped_up_at is not None),
        uses=mine.uses,
        shared=1 + len(others),
        name=name,
    )


def remember(db: Session, company_id: int, employee_id: int, device: DeviceCheck, *, stepped_up: bool) -> None:
    """Anota el uso en la transacción del intento (fila nueva por decidir, o último uso y uno más)."""
    if device.key_hash is None:
        return
    repo = EmployeeDeviceRepository(db, company_id)
    repo.record(employee_id, device.key_hash, device.name, datetime.now(UTC), stepped_up=stepped_up)


class EmployeeDeviceService:
    """La empresa ve, aprueba y revoca los dispositivos de un empleado; el empleado ve los suyos."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id
        self.devices = EmployeeDeviceRepository(db, company_id)

    def list(self, employee: Employee, page: PageParams, *, reviewers: bool = True) -> EmployeeDeviceList:
        """Los dispositivos del empleado, el más reciente primero; `reviewers=False` (Mi perfil) sin quién decidió."""
        items, total = self.devices.page(employee.id, offset=page.offset, limit=page.size)
        emails = UserRepository(self.db).emails_by_ids(d.reviewed_by_id for d in items) if reviewers else {}
        return EmployeeDeviceList.of([self._read(d, emails) for d in items], total, page)

    def set_status(self, employee: Employee, device_id: int, status: str, reviewer: User) -> EmployeeDeviceRead:
        device = self.devices.get(employee.id, device_id)
        if device is None:
            raise NotFoundError(code="DEVICE_NOT_FOUND")
        if device.status not in TRANSITIONS[status]:
            raise ConflictError(code="DEVICE_INVALID_TRANSITION")
        device.status = status
        device.reviewed_at = datetime.now(UTC)
        device.reviewed_by_id = reviewer.id
        self.db.commit()
        return self._read(device, {reviewer.id: reviewer.email})

    @staticmethod
    def _read(device: EmployeeDevice, emails: dict[int, str]) -> EmployeeDeviceRead:
        data = EmployeeDeviceRead.model_validate(device)
        return data.model_copy(update={"reviewed_by": emails.get(device.reviewed_by_id or 0)})
