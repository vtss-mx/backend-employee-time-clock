"""Catálogos de la base de datos (esquema `catalog`), en memoria por proceso y por idioma.

Son pocos registros y cambian rara vez: se leen todos de una vez y se renuevan cada
CATALOG_CACHE_SECONDS. La base es la única fuente: cambiar un nombre o un mensaje en la tabla se
refleja sin volver a desplegar.

**Dos niveles de caché** (decisión del dueño del producto, 2026-10-07: «implementa redis para retener información de
catálogos»). El primero sigue siendo la memoria de cada proceso (ninguna petición paga red). Al vencer, la recarga
primero pide a Redis (`app/core/cache.py`) la instantánea compartida —los datos crudos de la base, `CatalogData`, UNA
llave y UNA lectura— y arma las vistas por idioma en el proceso; solo si no está va a la base (≈ 60 consultas) y la
escribe para las demás réplicas con vigencia `CATALOG_REDIS_SECONDS`. Así N réplicas leen la base una vez por ciclo,
no N veces. La llave lleva la **versión** (`catalog_version`): la huella de los catálogos que este código conoce (sus
columnas), de los idiomas y del conjunto de migraciones del código. Por qué esa fuente y no la base: una lectura de
Redis no debe costar consultas (la versión se calcula una vez por proceso, sin red), y es exactamente lo que define la
forma de la instantánea: durante un despliegue gradual el código viejo y el nuevo escriben llaves distintas y ninguno
lee una instantánea con otra forma (una tabla nueva que el viejo no conoce). Los datos cambian con las migraciones (que
es cuando cambia la llave) y el servicio `migrate` además borra las instantáneas al terminar (`python -m app.cli cache
clear-catalogs`); un cambio a mano en la tabla se ve en todas las réplicas a lo más en `CATALOG_REDIS_SECONDS`. Con
Redis apagado o caído todo sigue como antes (la base y la caché local): `tests/test_shared_cache.py`.

**Idiomas** (regla 16 de la raíz): al recargar se leen también sus textos en inglés (`catalog.translations`, una
consulta más por recarga) y se arma una instantánea por idioma. `get_catalogs()` entrega la del idioma de la petición
(`current_locale()`): los nombres, descripciones y mensajes ya vienen traducidos, sin consultas ni trabajo por
petición. Lo que no es texto (códigos, permisos, orden, valores) es igual en todos los idiomas. Los países se ordenan
alfabéticamente en cada idioma (los destacados primero), como en el selector de la aplicación web.
"""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, class_mapper

from app.core.cache import shared_cache
from app.core.config import API_DIR, settings
from app.core.database import SessionLocal
from app.core.text import fold_text
from app.i18n import DEFAULT_LOCALE, LOCALES, LazyText, Locale, current_locale, format_list, t
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
    CatalogEmployeeDocumentType,
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
    CatalogVoiceProfile,
    CatalogVoiceQuestion,
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
    "employee_document_types": CatalogEmployeeDocumentType,
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
    # Verificación por voz del registro facial (migración 0079): las preguntas y lo que la empresa compara.
    "voice_questions": CatalogVoiceQuestion,
    # Guía por audio del registro facial (migración 0088): las voces que la empresa puede elegir.
    "voice_profiles": CatalogVoiceProfile,
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

#: {catálogo: {código: {columna: texto}}} de un idioma.
type Texts = Mapping[str, Mapping[str, Mapping[str, str]]]


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


def _alphabetical(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Los destacados en su orden y los demás por nombre; `sort_order` renumerado en ese orden."""
    ordered = sorted(
        rows, key=lambda row: (0, row["sort_order"], "") if row["featured"] else (1, 0, fold_text(row["name"]))
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
        name: [{**row, **texts.get(name, {}).get(row["code"], {})} for row in rows] for name, rows in entries.items()
    }
    return {name: _alphabetical(rows) if name in ALPHABETICAL else tuple(rows) for name, rows in localized.items()}


def _texts(db: Session) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
    """Las traducciones de todos los catálogos: {idioma: {catálogo: {código: {columna: texto}}}} (una consulta)."""
    texts: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for row in db.scalars(select(CatalogTranslation)):
        texts.setdefault(row.locale, {}).setdefault(row.catalog, {}).setdefault(row.code, {})[row.field] = row.text
    return texts


@dataclass(frozen=True)
class CatalogData:
    """Lo que se lee de la base, tal cual (solo tipos JSON): es lo que viaja a la caché compartida. Las vistas por
    idioma (`Catalogs`) se arman en cada proceso con `build_catalogs`: una sola copia de los datos, no siete."""

    entries: dict[str, list[dict[str, Any]]]
    mode_methods: dict[str, list[str]]
    role_screens: dict[str, list[str]]
    screen_modules: dict[str, str]
    #: {idioma: {catálogo: {código: {columna: texto}}}}.
    translations: dict[str, dict[str, dict[str, dict[str, str]]]]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Any) -> CatalogData | None:
        """La instantánea que otra réplica escribió; None si no tiene la forma esperada (se ignora y se recarga)."""
        try:
            data = cls(**payload)
            if not all(isinstance(getattr(data, name), dict) for name in data.__dataclass_fields__):
                return None
            if set(data.entries) != set(CATALOG_MODELS) or not all(
                isinstance(rows, list) and all(isinstance(row, dict) and "code" in row for row in rows)
                for rows in data.entries.values()
            ):
                return None
        except TypeError:
            return None
        return data


def read_catalog_data(db: Session) -> CatalogData:
    """Todos los catálogos, sus relaciones y sus traducciones, de la base (≈ 60 consultas cortas)."""
    entries = {
        name: [_as_dict(row) for row in db.scalars(select(model).order_by(model.sort_order, model.code))]
        for name, model in CATALOG_MODELS.items()
    }
    mode_methods: dict[str, list[str]] = {}
    for link in db.scalars(select(ValidatorModeMethod).order_by(ValidatorModeMethod.sort_order)):
        mode_methods.setdefault(link.mode_code, []).append(link.method_code)
    role_screens: dict[str, list[str]] = {}
    for grant in db.scalars(select(RoleScreen)):
        role_screens.setdefault(grant.role_code, []).append(grant.screen_code)
    screen_modules = {link.screen_code: link.module_code for link in db.scalars(select(MenuModuleScreen))}
    return CatalogData(entries, mode_methods, role_screens, screen_modules, _texts(db))


def build_catalogs(data: CatalogData) -> dict[Locale, Catalogs]:
    """Una instantánea de los catálogos por idioma (los textos del idioma de omisión son las columnas de cada uno)."""
    entries = {name: tuple(rows) for name, rows in data.entries.items()}
    methods = {mode: tuple(codes) for mode, codes in data.mode_methods.items()}
    grants = {role: frozenset(screens) for role, screens in data.role_screens.items()}
    return {
        locale: Catalogs(
            _localized(entries, locale, data.translations.get(locale, {})),
            methods,
            grants,
            data.screen_modules,
            locale=locale,
        )
        for locale in LOCALES
    }


#: Carpeta de las migraciones: su conjunto de archivos es parte de la versión de la instantánea compartida.
MIGRATIONS_DIR = API_DIR / "alembic" / "versions"


@lru_cache
def catalog_version() -> str:
    """Versión de la instantánea compartida: huella de los catálogos que este código conoce (nombre y columnas de cada
    uno), de los idiomas y del conjunto de migraciones del código (sus archivos). Se calcula una vez por proceso, sin
    red ni consultas: dos réplicas con el mismo código leen y escriben la misma llave; una con código distinto (un
    despliegue gradual), otra. Un valor desconocido en Redis queda bajo una llave que ya nadie lee y vence solo."""
    schema = [
        (name, sorted(column.key for column in class_mapper(model).column_attrs))
        for name, model in sorted(CATALOG_MODELS.items())
    ]
    migrations = sorted(path.name for path in MIGRATIONS_DIR.glob("*.py"))
    digest = hashlib.sha256(json.dumps([schema, list(LOCALES), migrations]).encode()).hexdigest()
    return digest[:16]


def catalog_key() -> str:
    """La llave de la instantánea compartida de este código (bajo `REDIS_KEY_PREFIX`)."""
    return f"catalogs:{catalog_version()}"


def _load_from_shared_or_database() -> dict[Locale, Catalogs]:
    """Primero la instantánea compartida (una lectura, cero consultas); si no está, la base, y entonces se escribe
    para las demás réplicas. Con la caché apagada o caída, `get_json` responde None sin esperar."""
    cache = shared_cache()
    shared = cache.enabled and settings.CATALOG_REDIS_SECONDS > 0
    data = CatalogData.from_payload(payload) if shared and (payload := cache.get_json(catalog_key())) else None
    if data is None:
        # Siempre con una sesión propia: si la BD falla a media recarga, la transacción de la
        # petición que la disparó queda intacta (y sigue con los catálogos anteriores).
        with SessionLocal() as session:
            data = read_catalog_data(session)
        if shared:
            cache.set_json(catalog_key(), data.to_payload(), settings.CATALOG_REDIS_SECONDS)
    return build_catalogs(data)


logger = logging.getLogger(__name__)
#: Tras no poder recargar, cuándo reintentar (s).
_RETRY_AFTER_FAILURE_SECONDS = 5.0


class _CatalogCache:
    """Catálogos (una instantánea por idioma) en memoria por CATALOG_CACHE_SECONDS. Al vencer los recarga UNA sola
    petición (de la caché compartida o de la base); las demás siguen con los anteriores mientras tanto (sin ráfagas
    de ~60 consultas por proceso). **En frío** (sin catálogos aún: el arranque de una réplica bajo carga) también
    recarga UNA sola: las demás esperan a que termine y usan lo mismo. Medido en `perf/scale/run.sh` antes de esto:
    decenas de peticiones concurrentes recargaban a la vez y agotaban el pool de Redis (y antes, la base)."""

    def __init__(self) -> None:
        self._ready = threading.Condition()
        self._loaded_at = 0.0
        self._value: Mapping[Locale, Catalogs] | None = None
        self._reloading = False
        #: Con qué falló la última recarga en frío: quien esperaba responde con el mismo error (no vuelve a intentar
        #: en fila: con la base caída cada intento tardaría su tiempo límite).
        self._error: SQLAlchemyError | None = None

    def _take_turn(self) -> Mapping[Locale, Catalogs] | None:
        """Los catálogos vigentes (o los anteriores mientras otro recarga), o None si a esta petición le toca
        recargar. En frío espera a quien ya recarga; si esa recarga falla, falla igual."""
        with self._ready:
            while True:
                if self._value is not None and time.monotonic() - self._loaded_at < settings.CATALOG_CACHE_SECONDS:
                    return self._value
                if self._value is not None and self._reloading:
                    return self._value  # otra petición ya los está recargando
                if not self._reloading:
                    self._reloading, self._error = True, None
                    return None
                self._ready.wait(settings.REQUEST_QUEUE_TIMEOUT_SECONDS)
                if self._value is not None:
                    return self._value
                if self._error is not None:
                    raise self._error

    def get(self) -> Mapping[Locale, Catalogs]:
        current = self._take_turn()
        if current is not None:
            return current
        try:
            value = _load_from_shared_or_database()
        except SQLAlchemyError as exc:
            # BD caída al recargar: se siguen sirviendo los catálogos anteriores (los mensajes y
            # códigos de error no cambian por eso) y se reintenta en unos segundos.
            with self._ready:
                self._reloading = False
                if self._value is None:
                    self._error = exc
                    self._ready.notify_all()
                    raise
                logger.warning("No se pudieron recargar los catálogos: se usan los anteriores")
                self._loaded_at = time.monotonic() - settings.CATALOG_CACHE_SECONDS + _RETRY_AFTER_FAILURE_SECONDS
                return self._value
        except BaseException:
            # Cualquier otra falla (un error de programación, una interrupción): nadie se queda esperando a una
            # recarga que ya no existe; la siguiente petición vuelve a intentar.
            with self._ready:
                self._reloading = False
                self._ready.notify_all()
            raise
        with self._ready:
            # Los catálogos quedan listos ANTES de soltar el turno: quien despierta los encuentra y no recarga de nuevo.
            self._value, self._loaded_at, self._reloading = value, time.monotonic(), False
            self._ready.notify_all()
        return value

    def clear(self) -> None:
        with self._ready:
            self._value = None


_cache = _CatalogCache()


def get_catalogs() -> Catalogs:
    """Catálogos vigentes en el idioma de la petición (en memoria; al vencer se recargan con una sesión propia)."""
    return _cache.get()[current_locale()]


def clear_shared_catalogs() -> int | None:
    """Borra TODAS las instantáneas compartidas (de cualquier versión): lo hace el servicio `migrate` al terminar y
    `clear_catalog_cache`. Cuántas borró; None si Redis está apagado o no respondió (vencen solas)."""
    return shared_cache().delete_prefix("catalogs:*")


def clear_catalog_cache() -> None:
    """La siguiente lectura vuelve a cargar: la de este proceso de la base (la compartida también se borra, así las
    demás réplicas recargan de la base al vencer su copia local)."""
    _cache.clear()
    clear_shared_catalogs()


# ---------------------------------------------------------------- textos diferidos de los catálogos
# Un error o un resultado con un texto de un catálogo de la BD lo lleva DIFERIDO (`LazyText`): el sobre de la API lo
# arma en cada idioma (`i18n`, regla 16) leyendo la instantánea de ese idioma, que ya está en memoria (ni una consulta
# más). Con el texto ya armado, el sobre diría lo mismo en los dos idiomas.


def face_error_text(code: str, details: Mapping[str, Any] | None = None) -> LazyText:
    """El mensaje de un error de captura (`face_errors`) con sus datos."""
    return lambda: get_catalogs().face_error_message(code, details)


def accessories_text(codes: Iterable[str]) -> LazyText:
    """«Quítate los lentes y el cubrebocas para continuar», con las frases del catálogo de accesorios."""
    found = tuple(codes)
    return lambda: get_catalogs().accessories_message(found)


def reason_text(code: str | None) -> LazyText:
    """El mensaje de un motivo de rechazo de una identificación (`verification_reasons`)."""
    return lambda: get_catalogs().reason_message(code)


def session_text(reason: str | None) -> LazyText:
    """Lo que se le dice a la persona cuando su sesión se cerró por `reason` (`session_revocation_reasons`)."""
    return lambda: get_catalogs().session_message(reason)


def instruction_text(action: str) -> LazyText:
    """La instrucción de un movimiento de la prueba de vida (`liveness_actions`)."""
    return lambda: get_catalogs().liveness_instruction(action)


def catalog_name_text(catalog: str, code: str, column: str = "name") -> LazyText:
    """Un texto de un registro de un catálogo (su nombre o, p. ej., su `short_name`) dentro de un mensaje: «El RFC debe
    tener 12 caracteres», «Ya tienes vacaciones aprobadas…». Sin el registro, su código."""
    return lambda: str((get_catalogs().get(catalog, code) or {}).get(column) or code)
