"""Política de verificación de cada empresa: la configura el ADMIN de la plataforma (`policy_governance`: historial,
niveles predefinidos y regla de dos personas); la empresa y su personal solo la leen.

Se lee en cada operación facial; para no consultar la BD en cada petición se mantiene en una
caché por proceso y por empresa de pocos segundos (`POLICY_CACHE_SECONDS`: los cambios se ven en todos los
procesos en ese tiempo). La caché es acotada (LRU de `POLICY_CACHE_COMPANIES`): con miles de empresas no
crece sin límite.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.devices import classify_device
from app.core.exceptions import PermissionDeniedError
from app.facial_recognition import FacePolicy
from app.models import Employee, User, UserRole, VerificationPolicy
from app.repositories.policy_repository import PolicyRepository
from app.schemas.policy import VerificationPolicyRead
from app.services.catalog_service import get_catalogs
from app.services.face_security import thresholds


@dataclass(frozen=True)
class PolicySnapshot:
    block_glasses: bool = True
    block_headwear: bool = True
    block_mask: bool = True
    liveness_challenge: bool = True
    anti_spoofing: bool = True
    qr_enabled: bool = True
    validator_mobile_only: bool = True
    min_confidence: float = 0.99999
    identify_confidence: float = 0.99999
    min_capture_quality: float = 0.4
    max_location_accuracy_m: int = 100
    detect_impossible_travel: bool = True
    max_travel_kmh: int = 200
    # --- Candados contra engaños ---
    anti_spoofing_level: str = "STANDARD"
    liveness_steps: int = 2
    liveness_timeout_seconds: int = 60
    flash_liveness: str = "OBSERVE"
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
    qr_lifetime_seconds: int = 30
    adaptive_learning: bool = True
    # --- Antifraude (migración 0062) ---
    #: Decisión D4: las empresas nuevas no registran asistencia con el QR solo.
    qr_only_attendance: bool = False
    duplicate_confidence: float = 0.99
    employee_device_mode: str = "OBSERVE"
    preset: str | None = None
    risk_engine: bool = True
    risk_medium_score: int = 30
    risk_high_score: int = 60
    risk_critical_score: int = 80
    risk_medium_action: str = "STEP_UP"
    risk_high_action: str = "REVIEW"
    risk_critical_action: str = "DENY"
    risk_fallback_action: str = "ALLOW"
    #: Ajustes por señal sobre los de la plataforma: {código: {"mode", "points"}}.
    risk_signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    fraud_evidence: bool = True
    # --- Protocolo de captura (antifraude 2a, migración 0066): nacen midiendo ---
    flash_paced: bool = True
    capture_burst: bool = True
    # --- Presencia (antifraude 2b, migración 0070): firma y ubicación de los validadores, código de sitio ---
    validator_signing: str = "OBSERVE"
    validator_location: str = "OBSERVE"
    site_codes: str = "OBSERVE"
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
            min_quality=self.min_capture_quality,
        )

    @property
    def liveness_required(self) -> bool:
        return settings.FACE_LIVENESS_ENABLED and self.liveness_challenge


#: Columnas de la política (lo demás de PolicySnapshot se deriva del catálogo).
POLICY_COLUMNS = tuple(f.name for f in fields(PolicySnapshot) if f.compare)


_cache: OrderedDict[int, tuple[float, PolicySnapshot]] = OrderedDict()
_lock = threading.Lock()


def _snapshot(row: VerificationPolicy, real_floor: float = 0.0) -> PolicySnapshot:
    """La política en memoria. El umbral del anti-spoofing es el del nivel de la empresa o, si es más
    estricto, el piso que la plataforma calibró sola (face_security): nada automático lo relaja."""
    level = get_catalogs().get("antispoof_levels", row.anti_spoofing_level)
    threshold = float(level["threshold"]) if level else settings.FACE_ANTISPOOF_THRESHOLD
    return PolicySnapshot(
        **{name: getattr(row, name) for name in POLICY_COLUMNS},
        antispoof_threshold=max(threshold, real_floor),
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
        policies = PolicyRepository(self.db, self.company_id)
        row = policies.get()
        if row is None:  # empresa sin política aún: valores seguros por defecto
            defaults = PolicySnapshot()
            row = policies.add(
                VerificationPolicy(company_id=self.company_id, **{n: getattr(defaults, n) for n in POLICY_COLUMNS})
            )
        return row

    def ensure(self) -> VerificationPolicy:
        """Crea la política de la empresa con valores seguros si aún no existe (alta de empresa)."""
        return self._row()

    def current(self) -> PolicySnapshot:
        now = time.monotonic()
        with _lock:
            cached = _cache.get(self.company_id)
            if cached is not None and now - cached[0] < settings.POLICY_CACHE_SECONDS:
                _cache.move_to_end(self.company_id)
                return cached[1]
        snapshot = _snapshot(self._row(), thresholds(self.db).min_real_probability)
        with _lock:
            _cache[self.company_id] = (now, snapshot)
            _cache.move_to_end(self.company_id)
            while len(_cache) > settings.POLICY_CACHE_COMPANIES:
                _cache.popitem(last=False)
        return snapshot

    def read(self) -> VerificationPolicyRead:
        """La política que leen la empresa y su personal: sin quién la cambió (es una cuenta de la plataforma; la
        versión completa del ADMIN la arma `policy_governance`)."""
        return VerificationPolicyRead.model_validate(self._row())

    def values(self, *, lock: bool = False) -> dict[str, Any]:
        """Los valores vigentes de cada columna de la política (`lock`: bloqueada hasta el commit, para cambiarla sin
        carreras entre dos ADMIN)."""
        # Sin política aún, se crea con valores seguros (nueva: nadie más la está cambiando).
        row = PolicyRepository(self.db, self.company_id).get(lock=lock) or self._row()
        return {name: getattr(row, name) for name in POLICY_COLUMNS}

    def apply(self, changes: Mapping[str, Any], user_id: int | None) -> None:
        """Escribe los cambios ya validados y decididos (`policy_governance`); el commit y la caché, de quien llama."""
        row = self._row()
        for name, value in changes.items():
            setattr(row, name, value)
        row.updated_by_id = user_id

    def ensure_qr_enabled(self) -> None:
        if not self.current().qr_enabled:
            raise PermissionDeniedError(
                code="QR_DISABLED",
            )


def ensure_device_allowed(db: Session, user: User, headers: Mapping[str, str]) -> None:
    """Solo los VALIDADORES tienen restricción de dispositivo: operan desde una tableta o un teléfono
    (`validator_mobile_only`) y, además, desde un dispositivo que su empresa autorizó (device_service).
    Empleados, administradores de empresa y de la plataforma usan la aplicación desde cualquier
    dispositivo.
    """
    company = user.current_company
    if user.role != UserRole.VALIDATOR or company is None:
        return
    if not PolicyService(db, company.id).current().validator_mobile_only:
        return
    device = classify_device(headers.get("user-agent"), headers.get("sec-ch-ua-mobile"))
    if device == "desktop":
        raise PermissionDeniedError(code="TOUCH_DEVICE_REQUIRED", details={"device": device})
