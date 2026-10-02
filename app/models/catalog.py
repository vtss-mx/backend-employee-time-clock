"""Catálogos (esquema `catalog`): las listas de valores de la aplicación viven en la base de datos.

Cada catálogo tiene un código estable (lo usan la lógica y las llaves foráneas), el nombre que se
muestra, una descripción, su orden y si está activo. La estructura y los datos los crean las
migraciones de Alembic (0020 en adelante; datos en alembic/seed/); estos modelos deben coincidir. Los
Enum de `app/models/enums.py` solo nombran los códigos que la lógica necesita; una prueba
garantiza que coincidan con estas tablas.
"""

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, SmallInteger, String, false, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import CATALOG


class CatalogEntry:
    """Columnas comunes de todos los catálogos."""

    __table_args__ = ({"schema": CATALOG},)

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class ToneMixin:
    #: Color con que se muestra el estado: muted | info | success | warning | danger.
    tone: Mapped[str] = mapped_column(String(20), default="muted", server_default="muted", nullable=False)


class CatalogRole(CatalogEntry, Base):
    """Roles de usuario (ADMIN, COMPANY, EMPLOYEE, VALIDATOR)."""

    __tablename__ = "roles"


class CatalogVerificationMethod(CatalogEntry, Base):
    """Métodos de identificación: rostro, QR, o QR + rostro."""

    __tablename__ = "verification_methods"


class CatalogValidatorMode(CatalogEntry, Base):
    """Modos de un validador de identidad; sus métodos permitidos, en `validator_mode_methods`."""

    __tablename__ = "validator_modes"


class ValidatorModeMethod(Base):
    """Qué métodos de identificación permite cada modo de validador."""

    __tablename__ = "validator_mode_methods"
    __table_args__ = (
        # FK hacia el método: la llave primaria empieza por el modo y no la cubre.
        Index("ix_validator_mode_methods_method_code", "method_code"),
        {"schema": CATALOG},
    )

    mode_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.validator_modes.code", ondelete="CASCADE"), primary_key=True
    )
    method_code: Mapped[str] = mapped_column(ForeignKey(f"{CATALOG}.verification_methods.code"), primary_key=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)


class CatalogFaceStatus(ToneMixin, CatalogEntry, Base):
    """Estados del registro facial del empleado (description: explicación para la empresa)."""

    __tablename__ = "face_statuses"

    #: Cómo se le explica el estado al propio empleado.
    employee_note: Mapped[str] = mapped_column(String(120), nullable=False)


class CatalogEnrollmentStatus(ToneMixin, CatalogEntry, Base):
    """Estados de una solicitud de registro facial."""

    __tablename__ = "enrollment_statuses"


class CatalogVerificationReason(CatalogEntry, Base):
    """Motivos de un intento fallido (bitácora); `message` es lo que se le muestra a la persona."""

    __tablename__ = "verification_reasons"

    message: Mapped[str] = mapped_column(String(200), nullable=False)


class CatalogAccessory(CatalogEntry, Base):
    """Accesorios que la empresa puede pedir retirar (lentes, gorra, cubrebocas)."""

    __tablename__ = "accessories"

    #: Para armar "Quítate {phrase} para continuar".
    phrase: Mapped[str] = mapped_column(String(60), nullable=False)


class CatalogLivenessAction(CatalogEntry, Base):
    """Retos de la prueba de vida (girar la cabeza) con la instrucción que se muestra."""

    __tablename__ = "liveness_actions"

    instruction: Mapped[str] = mapped_column(String(120), nullable=False)


class CatalogCountry(CatalogEntry, Base):
    """Países para los teléfonos (code = ISO 3166-1 alfa-2) con su lada internacional."""

    __tablename__ = "countries"

    dial_code: Mapped[str] = mapped_column(String(6), nullable=False)
    #: Se muestran primero en el selector de lada.
    featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class CatalogEnrollmentRejectionReason(CatalogEntry, Base):
    """Motivos sugeridos al rechazar un registro facial (la empresa también puede escribir otro)."""

    __tablename__ = "enrollment_rejection_reasons"


class CatalogReverificationReason(CatalogEntry, Base):
    """Motivos sugeridos al pedir que un empleado registre su rostro de nuevo."""

    __tablename__ = "reverification_reasons"


class CatalogConfidenceLevel(CatalogEntry, Base):
    """Niveles de confianza que la empresa puede exigir (code = posición 80-100 del control).

    Cifras medidas en LFW (6 000 pares) con el modelo de producción: similitud exigida, % de
    impostores aceptados y % de capturas legítimas rechazadas (una sola captura).
    """

    __tablename__ = "confidence_levels"

    #: Único: la política de cada empresa lo referencia (verification_policy.min_confidence).
    value: Mapped[float] = mapped_column(Numeric(7, 5, asdecimal=False), unique=True, nullable=False)
    similarity: Mapped[float] = mapped_column(Numeric(5, 3, asdecimal=False), nullable=False)
    false_accept_rate: Mapped[float] = mapped_column(Numeric(6, 3, asdecimal=False), nullable=False)
    rejection_rate: Mapped[float] = mapped_column(Numeric(5, 1, asdecimal=False), nullable=False)


class CatalogSessionRevocationReason(CatalogEntry, Base):
    """Por qué se cerró una sesión (auth_sessions.revoked_reason) y qué se le dice a la persona."""

    __tablename__ = "session_revocation_reasons"

    message: Mapped[str] = mapped_column(String(200), nullable=False)


class CatalogFaceError(CatalogEntry, Base):
    """Errores de la captura facial que devuelve la API (pose, luz, accesorios, prueba de vida...).

    `message` puede llevar datos del error entre llaves ({max_dimension}); `retryable`: la persona
    puede corregir y volver a intentar.
    """

    __tablename__ = "face_errors"

    message: Mapped[str] = mapped_column(String(200), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)


class CatalogEnrollmentFlag(CatalogEntry, Base):
    """Marcas de un registro facial para que la empresa lo revise (accesorios o posible suplantación)."""

    __tablename__ = "enrollment_flags"


class CatalogScreen(CatalogEntry, Base):
    """Pantallas de la aplicación: el frontend arma el menú y sus rutas con las que recibe del backend.

    `path` es la ruta base de la pantalla; `short_name`, el título corto en la barra superior de los
    teléfonos; `icon`, el nombre del ícono (lucide); `badge`, el contador que acompaña la opción.
    Qué rol ve cada pantalla vive en `role_screens`, y cada endpoint exige la pantalla que lo usa.
    """

    __tablename__ = "screens"

    path: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(30))
    icon: Mapped[str] = mapped_column(String(40), nullable=False)
    badge: Mapped[str | None] = mapped_column(String(30))


class RoleScreen(Base):
    """Qué pantallas tiene cada rol (permiso de la pantalla y de los endpoints que usa)."""

    __tablename__ = "role_screens"
    __table_args__ = (
        # FK hacia la pantalla (y "qué roles la tienen"): la llave primaria empieza por el rol.
        Index("ix_role_screens_screen_code", "screen_code"),
        {"schema": CATALOG},
    )

    role_code: Mapped[str] = mapped_column(ForeignKey(f"{CATALOG}.roles.code", ondelete="CASCADE"), primary_key=True)
    screen_code: Mapped[str] = mapped_column(
        ForeignKey(f"{CATALOG}.screens.code", ondelete="CASCADE"), primary_key=True
    )
