"""Cobranza de cada empresa (esquema `billing`): su plan, cuántos empleados activos tuvo cada día (la
base del prorrateo), los cargos con su detalle por mes, los pagos que registra el ADMIN (con su
comprobante) y cómo se aplicó cada pago a los cargos.

- Dinero en NUMERIC (Decimal en Python), nunca float: cada importe se redondea a los decimales de su moneda al
  cerrarlo (`billing_rules.money`). Precios SIN IVA; cada cargo suma el IVA a la tasa de la empresa.
- Cada empresa se cobra en la moneda de su plan (`catalog.currencies`: MXN, USD, EUR) y cada cargo y cada pago
  guardan la suya: una empresa tiene UNA moneda (fija desde su primer cargo o pago) y nunca se suman ni se
  convierten monedas distintas.
- Quién hizo algo (`*_by`) se guarda como el correo literal de la cuenta, sin llave foránea: es un dato
  de auditoría financiera que debe sobrevivir aunque la cuenta del ADMIN se borre.
- Los cargos y los pagos no se borran nunca (FK `RESTRICT` hacia la empresa): una empresa con
  movimientos no se puede eliminar, solo desactivar o suspender. Un cargo o un pago equivocado se
  ANULA con su motivo (`VOID`) y queda en el historial.
- `paid` del cargo y `applied` del pago son la suma de sus `payment_allocations`: se actualizan juntos
  en la misma transacción, con la empresa bloqueada (`FOR UPDATE` de su plan), para leer saldos sin
  sumar las aplicaciones en cada listado.

Las reglas puras (cortes, prorrateo, descuentos, IVA, reparto de pagos, vencimientos) viven en
`app/services/billing_rules.py`.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.db_schemas import BILLING, CATALOG, TENANCY
from app.models.enums import Currency
from app.models.mixins import TimestampMixin, company_fk

#: Importes: hasta 999 999 999 999.99 (cargos de empresas muy grandes o periodos anuales).
MONEY = Numeric(14, 2)
#: Precios unitarios y descuentos de monto fijo.
PRICE = Numeric(12, 2)
#: Tasas en porcentaje (IVA 16.00).
RATE = Numeric(5, 2)


def _catalog(table: str) -> ForeignKey:
    """FK a un catálogo que nunca se borra: sin índice (§3.1.8)."""
    return ForeignKey(f"{CATALOG}.{table}.code")


def _company(ondelete: str) -> ForeignKey:
    return ForeignKey(f"{TENANCY}.companies.id", ondelete=ondelete)


class BillingPlan(TimestampMixin, Base):
    """El plan de cobro de una empresa (una fila por empresa; sin fila, la empresa no se cobra).

    Cambiarlo aplica desde el PRÓXIMO cargo: los cargos ya emitidos guardan su copia del precio y del
    IVA. `next_cut_on` es el siguiente corte (fin de mes) en que toca emitir un cargo: el mantenimiento
    busca por él qué empresas ya cortaron. `forecast_total` es el pronóstico del cargo del periodo en
    curso (con IVA), que el mantenimiento refresca a diario y el servicio al cambiar el plan: el resumen
    de la plataforma lo suma sin calcular cada empresa. `grace_until`: tras reactivar a mano una
    empresa con adeudo, hasta ese día no vuelve a suspenderse sola.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price"),
        CheckConstraint("interval_months BETWEEN 1 AND 12", name="interval_months"),
        CheckConstraint("trial_days BETWEEN 0 AND 365", name="trial_days"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 100", name="tax_rate"),
        CheckConstraint("grace_days BETWEEN 0 AND 90", name="grace_days"),
        # El descuento va completo (tipo, valor y recurrencia) o no va.
        CheckConstraint(
            "(discount_type IS NULL) = (discount_value IS NULL)"
            " AND (discount_type IS NULL) = (discount_recurrence IS NULL)",
            name="discount_complete",
        ),
        CheckConstraint("discount_value IS NULL OR discount_value > 0", name="discount_value"),
        CheckConstraint("discount_periods IS NULL OR discount_periods BETWEEN 1 AND 120", name="discount_periods"),
        # Generación de cargos: los planes cuyo corte ya pasó (el mantenimiento los toma por lotes).
        Index("ix_plans_next_cut_on", "next_cut_on"),
        {"schema": BILLING},
    )

    company_id: Mapped[int] = mapped_column(_company("CASCADE"), primary_key=True)
    pricing_mode: Mapped[str] = mapped_column(String(20), _catalog("pricing_modes"), nullable=False)
    #: Precio sin IVA por empleado activo (PER_USER) o de la empresa (FLAT), por `price_period`.
    unit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    price_period: Mapped[str] = mapped_column(String(20), _catalog("price_periods"), nullable=False)
    #: Un cargo cada N meses (1 mensual, 3 trimestral, 12 anual).
    interval_months: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Días de demo sin cobro desde `starts_on`.
    trial_days: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"), nullable=False)
    #: Último día de la demo (`starts_on + trial_days - 1`; vacío sin demo). Lo escribe el servicio junto
    #: con los dos anteriores: el resumen de la plataforma cuenta las empresas en demo sin aritmética de
    #: fechas en SQL (distinta en cada motor).
    trial_ends_on: Mapped[date | None] = mapped_column(Date)
    discount_type: Mapped[str | None] = mapped_column(String(20), _catalog("discount_types"))
    discount_value: Mapped[Decimal | None] = mapped_column(PRICE)
    discount_recurrence: Mapped[str | None] = mapped_column(String(20), _catalog("discount_recurrences"))
    #: N de "primeros N cargos" o "cada N cargos".
    discount_periods: Mapped[int | None] = mapped_column(SmallInteger)
    #: IVA en porcentaje (16.00 por omisión; 0 permitido).
    tax_rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal("16"), server_default=text("16"), nullable=False)
    #: Días después del vencimiento antes de suspender sola a la empresa.
    grace_days: Mapped[int] = mapped_column(SmallInteger, default=10, server_default=text("10"), nullable=False)
    #: Moneda de TODO el cobro de la empresa (catalog.currencies). Se cambia libremente mientras la empresa no
    #: tenga cargos ni pagos; con el primero queda fija (422 `CURRENCY_LOCKED`, `BillingService.save_plan`).
    currency: Mapped[str] = mapped_column(
        String(3), _catalog("currencies"), default=Currency.MXN, server_default=Currency.MXN.value, nullable=False
    )
    next_cut_on: Mapped[date] = mapped_column(Date, nullable=False)
    grace_until: Mapped[date | None] = mapped_column(Date)
    forecast_total: Mapped[Decimal | None] = mapped_column(MONEY)
    forecast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Correo de quien lo guardó por última vez.
    updated_by: Mapped[str | None] = mapped_column(String(255))


class HeadcountDay(Base):
    """Empleados y validadores activos de una empresa en un día del negocio: la base del prorrateo (PER_USER).

    Cuenta quien estuvo activo en ALGÚN momento de ese día (entrar o salir a mitad del día paga el
    día). La escribe el mantenimiento al cerrar el día, a partir de `workforce.employee_status_events` y
    `workforce.validator_status_events` (una sentencia para todas las empresas; repetible: recalcular un día da
    lo mismo), y la lee la emisión de cada cargo (por la llave primaria: a lo más los días de un periodo). Se
    cobran los dos: un validador activo cuenta como un empleado (decisión del dueño del producto); van en columnas
    separadas para mostrar el desglose.
    """

    __tablename__ = "headcount_days"
    __table_args__ = (
        CheckConstraint("active_employees >= 0", name="active_employees"),
        CheckConstraint("active_validators >= 0", name="active_validators"),
        {"schema": BILLING},
    )

    company_id: Mapped[int] = mapped_column(_company("CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    active_employees: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Validadores activos ese día (0 en los días cerrados antes de que se cobraran, migración 0067).
    active_validators: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)


class Charge(Base):
    """Un cargo emitido al corte (fin de mes) de un periodo de `interval_months` meses.

    Guarda la copia del plan con que se calculó (modalidad, precio, periodo del precio, IVA): cambiar el
    plan nunca altera lo emitido. `sequence` numera los cargos de la empresa (1 = el primero): de él
    depende si aplica un descuento de "primeros N" o "cada N". Vence el día en que se emite
    (`due_on` = `issued_on`); la empresa se suspende sola si sigue abierto después de sus días de gracia.
    """

    __tablename__ = "charges"
    __table_args__ = (
        # Un cargo por corte (la emisión es idempotente) y una numeración sin huecos repetidos. El
        # primero también sirve el listado de la empresa (por corte, el más reciente primero).
        UniqueConstraint("company_id", "cut_on", name="uq_charges_company_cut"),
        UniqueConstraint("company_id", "sequence", name="uq_charges_company_sequence"),
        # Destino de las FK compuestas de sus líneas y aplicaciones: el cargo y su empresa van juntos.
        UniqueConstraint("id", "company_id", name="uq_charges_id_company"),
        CheckConstraint("period_start <= period_end AND period_end = cut_on", name="period"),
        CheckConstraint("subtotal >= 0 AND discount >= 0 AND discount <= subtotal", name="discount"),
        CheckConstraint("tax >= 0 AND total >= 0", name="total"),
        CheckConstraint("paid >= 0 AND paid <= total", name="paid"),
        CheckConstraint("(status = 'VOID') = (voided_at IS NOT NULL)", name="void"),
        CheckConstraint("validator_units >= 0 AND validator_units <= units", name="validator_units"),
        # Vencidos de toda la plataforma (suspensión automática y resumen): solo los abiertos.
        Index(
            "ix_charges_open_due_on",
            "due_on",
            "company_id",
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
        # Resumen de la plataforma: lo emitido en el mes y el último corte.
        Index("ix_charges_issued_on", "issued_on"),
        Index("ix_charges_cut_on", "cut_on"),
        {"schema": BILLING},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(_company("RESTRICT"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    cut_on: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    billable_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Días-persona (PER_USER: cada empleado y cada validador activos, día por día) o días cobrables (FLAT).
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    #: De esos días-persona, cuántos fueron de validadores (el desglose que se muestra; no cambia el importe). None:
    #: monto fijo o un cargo emitido antes de guardarlo (migración 0072).
    validator_units: Mapped[int | None] = mapped_column(Integer)
    pricing_mode: Mapped[str] = mapped_column(String(20), _catalog("pricing_modes"), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    price_period: Mapped[str] = mapped_column(String(20), _catalog("price_periods"), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    #: Moneda del cargo (la del plan al emitirse): el historial se explica solo. El valor por omisión existe solo
    #: para el despliegue gradual (las réplicas anteriores únicamente conocen MXN); el código siempre lo escribe.
    currency: Mapped[str] = mapped_column(
        String(3), _catalog("currencies"), server_default=Currency.MXN.value, nullable=False
    )
    subtotal: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    discount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default=text("0"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), _catalog("charge_statuses"), default="OPEN", server_default="OPEN", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(255))
    void_reason: Mapped[str | None] = mapped_column(String(300))


class ChargeLine(Base):
    """Una línea del cargo por mes del periodo: días cobrables, unidades (días-persona o días), cuántas de ellas fueron
    de validadores e importe."""

    __tablename__ = "charge_lines"
    __table_args__ = (
        CheckConstraint("days >= 0 AND units >= 0 AND amount >= 0", name="amounts"),
        CheckConstraint("validator_units >= 0 AND validator_units <= units", name="validator_units"),
        company_fk("charge_lines", "charge_id", f"{BILLING}.charges"),
        {"schema": BILLING},
    )

    charge_id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(nullable=False)
    #: Primer día del mes.
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    #: De `units`, los días-persona de validadores (None: monto fijo o línea anterior a la migración 0072).
    validator_units: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class Payment(Base):
    """Un pago que el ADMIN registró y confirmó a mano (sin pasarela de pagos).

    Se aplica a los cargos abiertos del más antiguo al más nuevo; lo que sobra queda a favor
    (`amount - applied`) y se aplica solo al siguiente cargo. Del comprobante (opcional) se guardan aquí
    nombre, tipo, tamaño y la referencia de su objeto CIFRADO en el bucket: sus bytes nunca van a la BD.
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("applied >= 0 AND applied <= amount", name="applied"),
        CheckConstraint("(status = 'VOID') = (voided_at IS NOT NULL)", name="void"),
        # Pagos de la empresa (el más reciente primero) y su saldo a favor.
        Index("ix_payments_company_paid_on", "company_id", "paid_on", "id"),
        # Resumen de la plataforma: lo cobrado en el mes.
        Index("ix_payments_paid_on", "paid_on"),
        # Saldo a favor: los pagos confirmados que aún tienen algo sin aplicar (pocos) para aplicarlos al
        # siguiente cargo y sumarlos en el resumen.
        # Destino de la FK compuesta de sus aplicaciones: el pago y su empresa van juntos.
        UniqueConstraint("id", "company_id", name="uq_payments_id_company"),
        Index(
            "ix_payments_unapplied",
            "company_id",
            postgresql_where=text("status = 'CONFIRMED' AND applied < amount"),
            sqlite_where=text("status = 'CONFIRMED' AND applied < amount"),
        ),
        {"schema": BILLING},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(_company("RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Moneda del pago: la misma de los cargos que paga (la de la cuenta). Valor por omisión solo para el
    #: despliegue gradual, como en `Charge.currency`.
    currency: Mapped[str] = mapped_column(
        String(3), _catalog("currencies"), server_default=Currency.MXN.value, nullable=False
    )
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(20), _catalog("payment_methods"), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(
        String(20), _catalog("payment_statuses"), default="CONFIRMED", server_default="CONFIRMED", nullable=False
    )
    applied: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), server_default=text("0"), nullable=False)
    receipt_name: Mapped[str | None] = mapped_column(String(200))
    receipt_type: Mapped[str | None] = mapped_column(String(100))
    receipt_size: Mapped[int | None] = mapped_column(Integer)
    #: El comprobante vive CIFRADO en el bucket (`image_storage.PAYMENT_RECEIPTS`); aquí solo su referencia:
    #: nombre del objeto, SHA-256 del objeto cifrado y cuándo se subió (verificado).
    receipt_object: Mapped[str | None] = mapped_column(String(300))
    receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(255))
    void_reason: Mapped[str | None] = mapped_column(String(300))


class PaymentAllocation(Base):
    """Cuánto de un pago se aplicó a un cargo. Anular el pago (o el cargo) borra sus aplicaciones y
    devuelve el dinero a su origen: el cargo vuelve a quedar por pagar o el pago queda a favor.

    El pago y el cargo son de la MISMA empresa (dos FK compuestas): la base nunca aplica el dinero de una
    empresa a la deuda de otra."""

    __tablename__ = "payment_allocations"
    __table_args__ = (
        PrimaryKeyConstraint("payment_id", "charge_id", name="pk_payment_allocations"),
        CheckConstraint("amount > 0", name="amount"),
        # Pagos aplicados a un cargo (detalle del cargo y anularlo); también la FK hacia el cargo.
        Index("ix_payment_allocations_charge_id", "charge_id"),
        company_fk("payment_allocations", "payment_id", f"{BILLING}.payments"),
        company_fk("payment_allocations", "charge_id", f"{BILLING}.charges"),
        {"schema": BILLING},
    )

    payment_id: Mapped[int] = mapped_column()
    charge_id: Mapped[int] = mapped_column()
    company_id: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
