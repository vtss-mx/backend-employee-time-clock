"""Catálogos de la base de datos (esquema `catalog`), en memoria por proceso.

Son pocos registros y cambian rara vez: se leen todos de una vez y se renuevan cada
CATALOG_CACHE_SECONDS. La base es la única fuente: cambiar un nombre o un mensaje en la tabla se
refleja sin volver a desplegar.
"""

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    CatalogAccessory,
    CatalogAntispoofLevel,
    CatalogApiKeyStatus,
    CatalogApiScope,
    CatalogAssignmentState,
    CatalogAttendanceAction,
    CatalogAttendanceEditReason,
    CatalogBoardState,
    CatalogConfidenceLevel,
    CatalogCountry,
    CatalogDayOffType,
    CatalogDeviceStatus,
    CatalogEnrollmentFlag,
    CatalogEnrollmentRejectionReason,
    CatalogEnrollmentStatus,
    CatalogErrorSeverity,
    CatalogErrorStatus,
    CatalogFaceError,
    CatalogFaceStatus,
    CatalogFlashMode,
    CatalogLivenessAction,
    CatalogMenuModule,
    CatalogReverificationReason,
    CatalogRole,
    CatalogScreen,
    CatalogSessionRevocationReason,
    CatalogShiftRequestStatus,
    CatalogValidatorMode,
    CatalogVerificationMethod,
    CatalogVerificationReason,
    CatalogWorkMode,
    CatalogWorkSessionStatus,
    MenuModuleScreen,
    RoleScreen,
    ValidatorModeMethod,
)
from app.models.catalog import CatalogEntry

#: Nombre público de cada catálogo → su tabla.
CATALOG_MODELS: dict[str, type[CatalogEntry]] = {
    "roles": CatalogRole,
    "verification_methods": CatalogVerificationMethod,
    "validator_modes": CatalogValidatorMode,
    "face_statuses": CatalogFaceStatus,
    "enrollment_statuses": CatalogEnrollmentStatus,
    "device_statuses": CatalogDeviceStatus,
    "api_scopes": CatalogApiScope,
    "api_key_statuses": CatalogApiKeyStatus,
    "error_statuses": CatalogErrorStatus,
    "error_severities": CatalogErrorSeverity,
    "work_modes": CatalogWorkMode,
    "attendance_actions": CatalogAttendanceAction,
    "work_session_statuses": CatalogWorkSessionStatus,
    "shift_request_statuses": CatalogShiftRequestStatus,
    "board_states": CatalogBoardState,
    "assignment_states": CatalogAssignmentState,
    "day_off_types": CatalogDayOffType,
    "attendance_edit_reasons": CatalogAttendanceEditReason,
    "verification_reasons": CatalogVerificationReason,
    "accessories": CatalogAccessory,
    "liveness_actions": CatalogLivenessAction,
    "countries": CatalogCountry,
    "enrollment_rejection_reasons": CatalogEnrollmentRejectionReason,
    "reverification_reasons": CatalogReverificationReason,
    "confidence_levels": CatalogConfidenceLevel,
    "antispoof_levels": CatalogAntispoofLevel,
    "flash_modes": CatalogFlashMode,
    "session_revocation_reasons": CatalogSessionRevocationReason,
    "face_errors": CatalogFaceError,
    "enrollment_flags": CatalogEnrollmentFlag,
    "screens": CatalogScreen,
    "menu_modules": CatalogMenuModule,
}
#: Motivo cuyo mensaje se usa si llega un código que no está en el catálogo.
FALLBACK_REASON = "NOT_FOUND"
#: Error de captura cuyo mensaje se usa si el motor reporta un código que no está en el catálogo.
FALLBACK_FACE_ERROR = "INVALID_IMAGE"
#: Motivo de cierre cuyo mensaje se usa si la sesión no existe o no tiene motivo registrado.
FALLBACK_SESSION_REASON = "LOGOUT"


class _KeepMissing(dict[str, Any]):
    """Un marcador sin dato se deja tal cual en lugar de fallar."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _as_dict(row: Any) -> dict[str, Any]:
    """Todas las columnas del registro (cada catálogo tiene las suyas además de las comunes)."""
    return {attr.key: getattr(row, attr.key) for attr in row.__mapper__.column_attrs}


@dataclass(frozen=True)
class Catalogs:
    """Instantánea de todos los catálogos: {catálogo: [registro, ...]} en su orden."""

    entries: Mapping[str, tuple[dict[str, Any], ...]]
    #: Modo de validador → métodos de identificación permitidos, en orden.
    mode_methods: Mapping[str, tuple[str, ...]]
    #: Rol → pantallas que tiene (catalog.role_screens).
    role_screens: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: Pantalla → módulo del menú en que va (catalog.menu_module_screens).
    screen_modules: Mapping[str, str] = field(default_factory=dict)
    _by_code: dict[str, dict[str, dict[str, Any]]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        index = {name: {row["code"]: row for row in rows} for name, rows in self.entries.items()}
        object.__setattr__(self, "_by_code", index)

    def get(self, catalog: str, code: str) -> dict[str, Any] | None:
        return self._by_code[catalog].get(code)

    def grants(self, role: str, screens: Iterable[str]) -> bool:
        """¿El rol tiene alguna de esas pantallas (activas)? Es el permiso de los endpoints que la usan."""
        granted = self.role_screens.get(role, frozenset())
        return any(screen in granted and self.is_active("screens", screen) for screen in screens)

    def is_active(self, catalog: str, code: str) -> bool:
        row = self.get(catalog, code)
        return bool(row and row["active"])

    def name(self, catalog: str, code: str) -> str:
        row = self.get(catalog, code)
        return row["name"] if row else code

    def confidence_level(self, value: float) -> dict[str, Any] | None:
        """Nivel de confianza activo con ese valor (a 5 decimales, como se guarda)."""
        return next(
            (
                row
                for row in self.entries["confidence_levels"]
                if row["active"] and round(row["value"], 5) == round(value, 5)
            ),
            None,
        )

    def reason_message(self, code: str | None) -> str:
        """Mensaje para la persona según el motivo del rechazo."""
        row = self.get("verification_reasons", code or "") or self.get("verification_reasons", FALLBACK_REASON)
        return row["message"] if row else "No fue posible identificarte"

    def liveness_instruction(self, action: str) -> str:
        row = self.get("liveness_actions", action)
        return row["instruction"] if row else "Gira lentamente la cabeza"

    def face_error_message(self, code: str, details: Mapping[str, Any] | None = None) -> str:
        """Mensaje de un error de captura; los datos del error llenan sus {marcadores}."""
        row = self.get("face_errors", code) or self.get("face_errors", FALLBACK_FACE_ERROR)
        template: str = row["message"] if row else "No se pudo procesar la imagen"
        return template.format_map(_KeepMissing(details or {}))

    def accessories_message(self, codes: Iterable[str]) -> str:
        """Arma «Quítate los lentes y el cubrebocas para continuar» con las frases del catálogo."""
        phrases = [(self.get("accessories", code) or {}).get("phrase", code.lower()) for code in codes]
        listed = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + " y " + phrases[-1]
        return self.face_error_message("ACCESSORIES_DETECTED", {"accessories": listed})

    def session_message(self, reason: str | None) -> str:
        """Qué se le dice a la persona cuando su sesión se cerró por `reason`."""
        row = self.get("session_revocation_reasons", reason or "") or self.get(
            "session_revocation_reasons", FALLBACK_SESSION_REASON
        )
        return row["message"] if row else "Tu sesión ya no es válida. Inicia sesión nuevamente."


def load_catalogs(db: Session) -> Catalogs:
    entries = {
        name: tuple(_as_dict(row) for row in db.scalars(select(model).order_by(model.sort_order, model.code)))
        for name, model in CATALOG_MODELS.items()
    }
    mode_methods: dict[str, list[str]] = {}
    for link in db.scalars(select(ValidatorModeMethod).order_by(ValidatorModeMethod.sort_order)):
        mode_methods.setdefault(link.mode_code, []).append(link.method_code)
    role_screens: dict[str, set[str]] = {}
    for grant in db.scalars(select(RoleScreen)):
        role_screens.setdefault(grant.role_code, set()).add(grant.screen_code)
    screen_modules = {link.screen_code: link.module_code for link in db.scalars(select(MenuModuleScreen))}
    return Catalogs(
        entries,
        {mode: tuple(methods) for mode, methods in mode_methods.items()},
        {role: frozenset(screens) for role, screens in role_screens.items()},
        screen_modules,
    )


logger = logging.getLogger(__name__)
#: Tras no poder recargar, cuándo reintentar (s).
_RETRY_AFTER_FAILURE_SECONDS = 5.0


class _CatalogCache:
    """Catálogos en memoria por CATALOG_CACHE_SECONDS. Al vencer los recarga UNA sola petición; las
    demás siguen con los anteriores mientras tanto (sin ráfagas de ~20 consultas por proceso)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._value: Catalogs | None = None
        self._reloading = False

    def get(self) -> Catalogs:
        with self._lock:
            if self._value is not None and time.monotonic() - self._loaded_at < settings.CATALOG_CACHE_SECONDS:
                return self._value
            if self._value is not None and self._reloading:
                return self._value  # otra petición ya los está recargando
            self._reloading = True
        try:
            # Siempre con una sesión propia: si la BD falla a media recarga, la transacción de la
            # petición que la disparó queda intacta (y sigue con los catálogos anteriores).
            with SessionLocal() as session:
                value = load_catalogs(session)
        except SQLAlchemyError:
            # BD caída al recargar: se siguen sirviendo los catálogos anteriores (los mensajes y
            # códigos de error no cambian por eso) y se reintenta en unos segundos.
            with self._lock:
                self._reloading = False
                if self._value is None:
                    raise
                logger.warning("No se pudieron recargar los catálogos: se usan los anteriores")
                self._loaded_at = time.monotonic() - settings.CATALOG_CACHE_SECONDS + _RETRY_AFTER_FAILURE_SECONDS
                return self._value
        finally:
            with self._lock:
                self._reloading = False
        with self._lock:
            self._value, self._loaded_at = value, time.monotonic()
        return value

    def clear(self) -> None:
        with self._lock:
            self._value = None


_cache = _CatalogCache()


def get_catalogs() -> Catalogs:
    """Catálogos vigentes (en memoria; al vencer se recargan con una sesión propia)."""
    return _cache.get()


def clear_catalog_cache() -> None:
    _cache.clear()
