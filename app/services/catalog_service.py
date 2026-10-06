"""Catálogos de la base de datos (esquema `catalog`), en memoria por proceso y por idioma.

Son pocos registros y cambian rara vez: se leen todos de una vez y se renuevan cada
CATALOG_CACHE_SECONDS. La base es la única fuente: cambiar un nombre o un mensaje en la tabla se
refleja sin volver a desplegar.

**Idiomas** (regla 16 de la raíz): al recargar se leen también sus textos en inglés (`catalog.translations`, una
consulta más por recarga) y se arma una instantánea por idioma. `get_catalogs()` entrega la del idioma de la petición
(`current_locale()`): los nombres, descripciones y mensajes ya vienen traducidos, sin consultas ni trabajo por
petición. Lo que no es texto (códigos, permisos, orden, valores) es igual en todos los idiomas. Los países se ordenan
alfabéticamente en cada idioma (los destacados primero), como en el selector de la aplicación web.
"""

import logging
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.i18n import DEFAULT_LOCALE, LOCALES, Locale, current_locale, format_list, t
from app.models import (
    CatalogAccessory,
    CatalogAntispoofLevel,
    CatalogApiKeyStatus,
    CatalogApiScope,
    CatalogAssignmentState,
    CatalogAttendanceAction,
    CatalogAttendanceEditReason,
    CatalogAttendanceReviewStatus,
    CatalogBillingStatus,
    CatalogBoardState,
    CatalogChargeStatus,
    CatalogCompanyDocumentType,
    CatalogConfidenceLevel,
    CatalogCountry,
    CatalogCurrency,
    CatalogDayOffType,
    CatalogDeviceStatus,
    CatalogDiscountRecurrence,
    CatalogDiscountType,
    CatalogEmployeeDeviceMode,
    CatalogEnrollmentFlag,
    CatalogEnrollmentRejectionReason,
    CatalogEnrollmentStatus,
    CatalogErrorSeverity,
    CatalogErrorStatus,
    CatalogFaceError,
    CatalogFaceStatus,
    CatalogFlashMode,
    CatalogFraudCaseEventKind,
    CatalogFraudCaseStatus,
    CatalogFraudKind,
    CatalogLivenessAction,
    CatalogMenuModule,
    CatalogPaymentMethod,
    CatalogPaymentStatus,
    CatalogPolicyChangeStatus,
    CatalogPolicyPreset,
    CatalogPricePeriod,
    CatalogPricingMode,
    CatalogReverificationReason,
    CatalogReviewReason,
    CatalogRiskAction,
    CatalogRiskSignal,
    CatalogRiskTier,
    CatalogRole,
    CatalogScreen,
    CatalogSessionRevocationReason,
    CatalogShiftRequestStatus,
    CatalogSignalMode,
    CatalogSlowAlertStatus,
    CatalogStorageCategory,
    CatalogSuspensionReason,
    CatalogTaxIdType,
    CatalogTranslation,
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
    "slow_alert_statuses": CatalogSlowAlertStatus,
    "work_modes": CatalogWorkMode,
    "attendance_actions": CatalogAttendanceAction,
    "work_session_statuses": CatalogWorkSessionStatus,
    "shift_request_statuses": CatalogShiftRequestStatus,
    "board_states": CatalogBoardState,
    "assignment_states": CatalogAssignmentState,
    "day_off_types": CatalogDayOffType,
    "attendance_edit_reasons": CatalogAttendanceEditReason,
    "pricing_modes": CatalogPricingMode,
    "price_periods": CatalogPricePeriod,
    "discount_types": CatalogDiscountType,
    "discount_recurrences": CatalogDiscountRecurrence,
    "billing_statuses": CatalogBillingStatus,
    "suspension_reasons": CatalogSuspensionReason,
    "charge_statuses": CatalogChargeStatus,
    "payment_statuses": CatalogPaymentStatus,
    "payment_methods": CatalogPaymentMethod,
    "company_document_types": CatalogCompanyDocumentType,
    "currencies": CatalogCurrency,
    "storage_categories": CatalogStorageCategory,
    "verification_reasons": CatalogVerificationReason,
    "accessories": CatalogAccessory,
    "liveness_actions": CatalogLivenessAction,
    "countries": CatalogCountry,
    # Identificador fiscal de las empresas (migración 0074): tipos por país con su regla de formato.
    "tax_id_types": CatalogTaxIdType,
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
    # Antifraude (migración 0062). `risk_signals` solo lo usa el backend (no viaja en GET /catalogs).
    "fraud_kinds": CatalogFraudKind,
    "signal_modes": CatalogSignalMode,
    "review_reasons": CatalogReviewReason,
    "risk_signals": CatalogRiskSignal,
    "risk_tiers": CatalogRiskTier,
    "risk_actions": CatalogRiskAction,
    "attendance_review_statuses": CatalogAttendanceReviewStatus,
    "employee_device_modes": CatalogEmployeeDeviceMode,
    "policy_presets": CatalogPolicyPreset,
    "policy_change_statuses": CatalogPolicyChangeStatus,
    "fraud_case_statuses": CatalogFraudCaseStatus,
    "fraud_case_event_kinds": CatalogFraudCaseEventKind,
}
#: Motivo cuyo mensaje se usa si llega un código que no está en el catálogo.
FALLBACK_REASON = "NOT_FOUND"
#: Error de captura cuyo mensaje se usa si el motor reporta un código que no está en el catálogo.
FALLBACK_FACE_ERROR = "INVALID_IMAGE"
#: Motivo de cierre cuyo mensaje se usa si la sesión no existe o no tiene motivo registrado.
FALLBACK_SESSION_REASON = "LOGOUT"
#: Catálogos en orden alfabético (en cada idioma, por su nombre traducido): los destacados (`featured`) primero, en su
#: orden; los demás por nombre sin acentos ni mayúsculas. Su `sort_order` se renumera en cada idioma para que quien
#: ordene por él (la aplicación web) vea el mismo orden.
ALPHABETICAL = frozenset({"countries"})

#: {(catálogo, código): {columna: texto}} de un idioma.
type Texts = Mapping[tuple[str, str], Mapping[str, str]]


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
    #: Idioma de los textos de esta instantánea.
    locale: Locale = DEFAULT_LOCALE
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
        return row["message"] if row else t("IDENTIFICATION_FAILED", locale=self.locale)

    def liveness_instruction(self, action: str) -> str:
        row = self.get("liveness_actions", action)
        return row["instruction"] if row else t("LIVENESS_FALLBACK_INSTRUCTION", locale=self.locale)

    def face_error_message(self, code: str, details: Mapping[str, Any] | None = None) -> str:
        """Mensaje de un error de captura; los datos del error llenan sus {marcadores}."""
        row = self.get("face_errors", code) or self.get("face_errors", FALLBACK_FACE_ERROR)
        template: str = row["message"] if row else t("IMAGE_NOT_PROCESSED", locale=self.locale)
        return template.format_map(_KeepMissing(details or {}))

    def accessories_message(self, codes: Iterable[str]) -> str:
        """Arma «Quítate los lentes y el cubrebocas para continuar» («Remove your glasses and your face mask to
        continue») con las frases del catálogo, unidas como se dice en su idioma."""
        phrases = [(self.get("accessories", code) or {}).get("phrase", code.lower()) for code in codes]
        listed = format_list(phrases, self.locale)
        return self.face_error_message("ACCESSORIES_DETECTED", {"accessories": listed})

    def session_message(self, reason: str | None) -> str:
        """Qué se le dice a la persona cuando su sesión se cerró por `reason`."""
        row = self.get("session_revocation_reasons", reason or "") or self.get(
            "session_revocation_reasons", FALLBACK_SESSION_REASON
        )
        return row["message"] if row else t("SESSION_INVALID", locale=self.locale)


def _fold(text: str) -> str:
    """El texto sin acentos ni mayúsculas, para ordenar alfabéticamente («Åland» junto a «Albania»)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def _alphabetical(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Los destacados en su orden y los demás por nombre; `sort_order` renumerado en ese orden."""
    ordered = sorted(
        rows, key=lambda row: (0, row["sort_order"], "") if row["featured"] else (1, 0, _fold(row["name"]))
    )
    return tuple({**row, "sort_order": position} for position, row in enumerate(ordered, start=1))


def _localized(
    entries: Mapping[str, tuple[dict[str, Any], ...]], locale: Locale, texts: Texts
) -> Mapping[str, tuple[dict[str, Any], ...]]:
    """Los catálogos con los textos de un idioma encima de los del idioma de omisión (un texto sin traducir se queda
    en español: `tests/test_i18n.py` exige que no falte ninguno). En el de omisión, tal cual (su orden es el de la
    BD)."""
    if locale == DEFAULT_LOCALE:
        return entries
    localized = {
        name: [{**row, **texts.get((name, row["code"]), {})} for row in rows] for name, rows in entries.items()
    }
    return {name: _alphabetical(rows) if name in ALPHABETICAL else tuple(rows) for name, rows in localized.items()}


def _texts(db: Session) -> dict[str, dict[tuple[str, str], dict[str, str]]]:
    """Las traducciones de todos los catálogos, por idioma (una consulta)."""
    texts: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
    for row in db.scalars(select(CatalogTranslation)):
        texts.setdefault(row.locale, {}).setdefault((row.catalog, row.code), {})[row.field] = row.text
    return texts


def load_catalogs(db: Session) -> dict[Locale, Catalogs]:
    """Una instantánea de los catálogos por idioma (los textos del idioma de omisión son las columnas de cada uno)."""
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
    methods = {mode: tuple(codes) for mode, codes in mode_methods.items()}
    grants = {role: frozenset(screens) for role, screens in role_screens.items()}
    texts = _texts(db)
    return {
        locale: Catalogs(
            _localized(entries, locale, texts.get(locale, {})), methods, grants, screen_modules, locale=locale
        )
        for locale in LOCALES
    }


logger = logging.getLogger(__name__)
#: Tras no poder recargar, cuándo reintentar (s).
_RETRY_AFTER_FAILURE_SECONDS = 5.0


class _CatalogCache:
    """Catálogos (una instantánea por idioma) en memoria por CATALOG_CACHE_SECONDS. Al vencer los recarga UNA sola
    petición; las demás siguen con los anteriores mientras tanto (sin ráfagas de ~60 consultas por proceso)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._value: Mapping[Locale, Catalogs] | None = None
        self._reloading = False

    def get(self) -> Mapping[Locale, Catalogs]:
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
    """Catálogos vigentes en el idioma de la petición (en memoria; al vencer se recargan con una sesión propia)."""
    return _cache.get()[current_locale()]


def clear_catalog_cache() -> None:
    _cache.clear()
