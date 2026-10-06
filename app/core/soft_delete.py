"""Borrado lógico (soft delete): lo que una persona elimina no se borra de la base al momento (regla 20 de la raíz).

Decisión del dueño del producto: «todos los delete de la aplicación deben ser softdelete». Eliminar un registro lo
MARCA (`deleted_at` y `deleted_by`) y desde ese instante desaparece de los listados, búsquedas, conteos, validaciones
de datos únicos (su número, su correo, su nombre se pueden volver a usar: los índices únicos son parciales) y búsquedas
por id; su historial (asistencia, bitácoras, cobranza) se conserva y lo sigue nombrando con la marca «Eliminado». La
papelera («Eliminados») de cada listado lo muestra y «Restaurar» lo regresa, revisando de nuevo lo que pudo cambiar
mientras tanto. Pasados `SOFT_DELETE_RETENTION_DAYS` (365: la Ley Federal del Trabajo pide conservar la asistencia
hasta un año después de terminar la relación laboral) el mantenimiento lo borra de verdad, por lotes
(`maintenance_service.SOFT_DELETE_PURGES`).

Excepción (LFPDPPP, datos personales sensibles; regla 13 de la raíz): los datos biométricos y las fotos de una persona
eliminada (plantillas faciales, fotos del registro facial, foto de perfil, evidencia de fraude) se borran DE VERDAD al
eliminarla —objetos del bucket y referencias en la base— y restaurarla no los regresa: registra su rostro de nuevo.

El mecanismo vive en UN solo lugar (aquí) y es automático, para que ninguna consulta nueva lo olvide:

- `SoftDeleteMixin` en el modelo: `deleted_at` (NULL = vigente) y `deleted_by`, el correo LITERAL de quien eliminó
  (el mismo criterio que la cobranza: sobrevive a que esa cuenta se elimine o se depure, no necesita JOIN ni el índice
  de una llave foránea en cada tabla y no cruza empresas: el ADMIN que elimina una empresa es de la plataforma).
- El evento `do_orm_execute` agrega a TODA consulta del ORM —SELECT y UPDATE masivo— la condición
  `deleted_at IS NULL` de cada tabla con borrado lógico que aparezca en ella: FROM, JOIN, subconsultas, conteos,
  `Session.get` y las colecciones (uno a muchos, p. ej. `User.employees`: un empleo eliminado no se elige al entrar).
  NO la llevan, a propósito:
  - la referencia de un registro a otro (muchos a uno: `WorkSession.employee`, `Employee.company`, `Validator.user`):
    el historial sigue nombrando a quien se eliminó;
  - la recarga de un objeto que ya se tiene (`refresh`, columnas expiradas);
  - un `DELETE`: solo la depuración borra filas de estas tablas;
  - la consulta que lo pide con `execution_options(include_deleted=True)` (`INCLUDE_DELETED`, `with_deleted`): la
    papelera, restaurar, la depuración y las búsquedas por id que resuelven referencias del historial (`by_ids`).
- Sin estado en memoria: la condición viaja dentro de cada sentencia (correcto con N réplicas, con PgBouncer en modo
  transacción y junto con la seguridad por fila) y no agrega consultas (`tests/test_performance.py`).
"""

from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import DateTime, Executable, String, event
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column, with_loader_criteria

#: Opción de ejecución que incluye lo eliminado (papelera, restaurar, depuración, referencias del historial).
INCLUDE_DELETED: Final = "include_deleted"


class SoftDeleteMixin:
    """Borrado lógico del registro (ver el módulo). `deleted_by` es el correo literal de quien lo eliminó."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(255))

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, at: datetime, by: str) -> None:
        """Lo manda a la papelera (en la unidad de trabajo de la sesión: un UPDATE por su llave primaria)."""
        self.deleted_at, self.deleted_by = at, by

    def mark_restored(self) -> None:
        self.deleted_at = self.deleted_by = None


#: La condición de lo vigente, armada una vez (la misma opción sirve a todas las sentencias y SQLAlchemy la guarda en
#: su caché de sentencias como cualquier otra). `propagate_to_loaders=False`: las referencias muchos a uno que se
#: cargan después (historial) no la llevan; las colecciones la reciben en `_exclude_deleted`.
_LIVE = with_loader_criteria(
    SoftDeleteMixin,
    lambda cls: cls.deleted_at.is_(None),
    include_aliases=True,
    propagate_to_loaders=False,
)


def with_deleted[S: Executable](statement: S) -> S:
    """La sentencia también ve lo eliminado (papelera, restaurar, referencias del historial)."""
    return statement.execution_options(**{INCLUDE_DELETED: True})


#: Las mismas opciones para `Session.get(..., execution_options=...)`.
WITH_DELETED: Final[dict[str, Any]] = {INCLUDE_DELETED: True}


def _is_reference(state: ORMExecuteState) -> bool:
    """¿Carga una referencia muchos a uno (lo que un registro del historial nombra)? Las colecciones, no. Toda carga
    de una relación trae su camino, que termina en la relación."""
    relationship: Any = cast(Any, state.loader_strategy_path)[-1]
    return not relationship.uselist


@event.listens_for(Session, "do_orm_execute")
def _exclude_deleted(state: ORMExecuteState) -> None:
    """Agrega `deleted_at IS NULL` de cada tabla con borrado lógico a las consultas y UPDATE masivos del ORM."""
    if state.is_column_load or state.execution_options.get(INCLUDE_DELETED):
        return
    if not (state.is_select or state.is_update):
        return
    if state.is_relationship_load and _is_reference(state):
        return
    state.statement = state.statement.options(_LIVE)
