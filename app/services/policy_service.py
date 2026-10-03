"""Política de verificación de cada empresa (editable por su COMPANY desde el frontend).

Se lee en cada operación facial; para no consultar la BD en cada petición se mantiene en una
caché por proceso y por empresa de pocos segundos (los cambios se ven en todos los procesos en
≤ TTL). La caché es acotada (LRU): con miles de empresas no crece sin límite.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.devices import classify_device
from app.core.exceptions import PermissionDeniedError, UnprocessableError
from app.facial_recognition import FacePolicy
from app.models import Employee, User, UserRole, VerificationPolicy
from app.schemas.policy import VerificationPolicyRead, VerificationPolicyUpdate
from app.services.catalog_service import get_catalogs

TOUCH_ONLY_MESSAGE = (
    "Por políticas de tu empresa, la validación de identidad solo está disponible desde una tableta o "
    "un teléfono. Ingresa desde el navegador de ese dispositivo con tu mismo correo y contraseña."
)
MOBILE_ONLY_MESSAGE = (
    "Por políticas de tu empresa, el registro de asistencia solo está disponible desde tu teléfono "
    "celular. Ingresa desde el navegador de tu teléfono con tu mismo correo y contraseña."
)

_CACHE_TTL_SECONDS = 5.0
_CACHE_MAX_COMPANIES = 10_000


@dataclass(frozen=True)
class PolicySnapshot:
    block_glasses: bool = True
    block_headwear: bool = True
    block_mask: bool = True
    liveness_challenge: bool = True
    anti_spoofing: bool = True
    qr_enabled: bool = True
    employee_mobile_only: bool = True
    validator_mobile_only: bool = True
    min_confidence: float = 0.99999
    # --- Candados contra engaños ---
    anti_spoofing_level: str = "STANDARD"
    liveness_steps: int = 2
    block_virtual_cameras: bool = True
    reject_foreign_images: bool = True
    detect_static_captures: bool = True
    detect_replays: bool = True
    check_capture_continuity: bool = True
    enforce_human_timing: bool = True
    detect_duplicate_faces: bool = True
    lockout_enabled: bool = True
    lockout_max_failures: int = 5
    lockout_minutes: int = 15
    validator_device_approval: bool = True
    #: Del nivel de anti-spoofing (catálogo): umbral y si basta una captura sospechosa.
    antispoof_threshold: float = field(default=0.05, compare=False)
    antispoof_any_frame: bool = field(default=False, compare=False)

    def face_policy(self, employee: Employee | None = None) -> FacePolicy:
        """Política facial efectiva para un empleado (aplica su excepción de prenda de cabeza)."""
        exempt = bool(employee and employee.headwear_exempt)
        return FacePolicy(
            block_glasses=self.block_glasses,
            block_headwear=self.block_headwear and not exempt,
            block_mask=self.block_mask,
            anti_spoofing=self.anti_spoofing,
            reject_foreign_images=self.reject_foreign_images,
            spoof_threshold=self.antispoof_threshold,
            spoof_any_frame=self.antispoof_any_frame,
        )

    @property
    def liveness_required(self) -> bool:
        return settings.FACE_LIVENESS_ENABLED and self.liveness_challenge


#: Columnas de la política (lo demás de PolicySnapshot se deriva del catálogo).
POLICY_COLUMNS = tuple(f.name for f in fields(PolicySnapshot) if f.compare)


_cache: OrderedDict[int, tuple[float, PolicySnapshot]] = OrderedDict()
_lock = threading.Lock()


def _snapshot(row: VerificationPolicy) -> PolicySnapshot:
    level = get_catalogs().get("antispoof_levels", row.anti_spoofing_level)
    return PolicySnapshot(
        **{name: getattr(row, name) for name in POLICY_COLUMNS},
        antispoof_threshold=float(level["threshold"]) if level else settings.FACE_ANTISPOOF_THRESHOLD,
        antispoof_any_frame=bool(level["any_frame"]) if level else False,
    )


def clear_policy_cache(company_id: int | None = None) -> None:
    """Descarta la política en caché de una empresa (o de todas)."""
    with _lock:
        if company_id is None:
            _cache.clear()
        else:
            _cache.pop(company_id, None)


class PolicyService:
    def __init__(self, db: Session, company_id: int) -> None:
        self.db = db
        self.company_id = company_id

    def _row(self) -> VerificationPolicy:
        row = self.db.scalar(select(VerificationPolicy).where(VerificationPolicy.company_id == self.company_id))
        if row is None:  # empresa sin política aún: valores seguros por defecto
            defaults = PolicySnapshot()
            row = VerificationPolicy(company_id=self.company_id, **{n: getattr(defaults, n) for n in POLICY_COLUMNS})
            self.db.add(row)
            self.db.flush()
        return row

    def ensure(self) -> VerificationPolicy:
        """Crea la política de la empresa con valores seguros si aún no existe (alta de empresa)."""
        return self._row()

    def current(self) -> PolicySnapshot:
        now = time.monotonic()
        with _lock:
            cached = _cache.get(self.company_id)
            if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                _cache.move_to_end(self.company_id)
                return cached[1]
        snapshot = _snapshot(self._row())
        with _lock:
            _cache[self.company_id] = (now, snapshot)
            _cache.move_to_end(self.company_id)
            while len(_cache) > _CACHE_MAX_COMPANIES:
                _cache.popitem(last=False)
        return snapshot

    def read(self) -> VerificationPolicyRead:
        row = self._row()
        updated_by = None
        if row.updated_by_id:
            user = self.db.get(User, row.updated_by_id)
            updated_by = user.email if user else None
        return VerificationPolicyRead.model_validate(row).model_copy(update={"updated_by": updated_by})

    def update(self, data: VerificationPolicyUpdate, user: User) -> VerificationPolicyRead:
        row = self._row()
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        if "min_confidence" in changes:
            level = get_catalogs(self.db).confidence_level(changes["min_confidence"])
            if level is None:
                raise UnprocessableError(
                    "Elige uno de los niveles de confianza disponibles",
                    code="INVALID_CONFIDENCE_LEVEL",
                    field="min_confidence",
                )
            changes["min_confidence"] = level["value"]
        if "anti_spoofing_level" in changes and not get_catalogs(self.db).is_active(
            "antispoof_levels", changes["anti_spoofing_level"]
        ):
            raise UnprocessableError(
                "Elige uno de los niveles de anti-spoofing disponibles",
                code="INVALID_ANTISPOOF_LEVEL",
                field="anti_spoofing_level",
            )
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_by_id = user.id
        self.db.commit()
        clear_policy_cache(self.company_id)
        return self.read()

    def ensure_qr_enabled(self) -> None:
        if not self.current().qr_enabled:
            raise PermissionDeniedError(
                "La verificación por QR está deshabilitada por tu empresa. Usa el reconocimiento facial.",
                code="QR_DISABLED",
            )


def ensure_device_allowed(db: Session, user: User, headers: Mapping[str, str]) -> None:
    """Dispositivos permitidos según la política de la empresa en la que opera el usuario.

    - EMPLOYEE: solo desde un teléfono celular (`employee_mobile_only`).
    - VALIDATOR: solo desde una tableta o un teléfono (`validator_mobile_only`).
    ADMIN y COMPANY no tienen esta restricción.
    """
    company = user.current_company
    if user.role not in (UserRole.EMPLOYEE, UserRole.VALIDATOR) or company is None:
        return  # sin empresa elegida todavía: se exige al elegirla
    policy = PolicyService(db, company.id).current()
    device = classify_device(headers.get("user-agent"), headers.get("sec-ch-ua-mobile"))
    if user.role == UserRole.EMPLOYEE and policy.employee_mobile_only and device != "phone":
        raise PermissionDeniedError(MOBILE_ONLY_MESSAGE, code="MOBILE_DEVICE_REQUIRED", details={"device": device})
    if user.role == UserRole.VALIDATOR and policy.validator_mobile_only and device == "desktop":
        raise PermissionDeniedError(TOUCH_ONLY_MESSAGE, code="TOUCH_DEVICE_REQUIRED", details={"device": device})
