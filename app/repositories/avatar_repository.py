"""Consultas de la foto de perfil (`auth.user_avatars`; la imagen vive cifrada en el bucket).

Cada consulta va por la llave primaria `(user_id, size_px)`; la de una empresa además por
`uq_employees_company_user` (`company_id, user_id`) o la llave de `auth.users`: una búsqueda por índice, sin
recorrer nada (§3.1). Quién puede ver a quién lo decide `AvatarService`; aquí solo se expresa en SQL.
"""

from sqlalchemy import ColumnElement, delete, exists, or_, select
from sqlalchemy.orm import Session

from app.models import Employee, User, UserAvatar
from app.repositories.aggregates import affected_rows


class AvatarRepository:
    """SQL de las fotos de perfil (sin reglas de negocio ni `commit`)."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def lock_person(self, user_id: int) -> None:
        """Bloquea la cuenta (`FOR UPDATE`) para reemplazar o quitar su foto: dos cambios a la vez se ordenan y
        el último gana, sin dejar tamaños de dos versiones. La transacción es corta (nada de red con ella)."""
        self.db.execute(select(User.id).where(User.id == user_id).with_for_update())

    @staticmethod
    def of_company_accounts(company_id: int) -> ColumnElement[bool]:
        """Las fotos de las cuentas de una empresa (administradores y validadores), para encolar sus objetos antes
        de borrarlas con la empresa (`ix_users_company_role` da las cuentas)."""
        return UserAvatar.user_id.in_(select(User.id).where(User.company_id == company_id))

    def remove(self, user_id: int) -> int:
        """Borra la referencia de todos los tamaños de la foto (sus objetos ya se encolaron para el bucket)."""
        return affected_rows(self.db, delete(UserAvatar).where(UserAvatar.user_id == user_id))

    def remove_matching(self, condition: ColumnElement[bool]) -> int:
        """Borra las referencias de las fotos que cumplen `condition` (p. ej. `of_company_accounts`; sus objetos ya se
        encolaron para el bucket): una sentencia."""
        return affected_rows(self.db, delete(UserAvatar).where(condition))

    def visible(
        self, user_id: int, size_px: int, *, company_id: int | None = None, platform: bool = False
    ) -> UserAvatar | None:
        """Un tamaño de la foto de `user_id` si quien pregunta puede verla, en UNA consulta (decisión del dueño,
        2026-10-06: «las empresas pueden ver las fotos de los empleados y los admin de todos»):

        - sin condiciones: la propia;
        - `company_id`: la persona es empleado de esa empresa (activo o inactivo) o una cuenta de ella (administrador
          o validador); de otra empresa no existe (404);
        - `platform` (el ADMIN): cualquier cuenta vigente.

        Lo que está en «Eliminados» nunca: la condición de lo vigente viaja sola en cada subconsulta
        (`app/core/soft_delete.py`), así que un empleo eliminado no le da la foto a esa empresa aunque la persona
        siga trabajando en otra (y la cuenta eliminada ya no tiene foto: se borró de verdad)."""
        stmt = select(UserAvatar).where(UserAvatar.user_id == user_id, UserAvatar.size_px == size_px)
        if company_id is not None:
            employee = select(Employee.id).where(Employee.company_id == company_id, Employee.user_id == user_id)
            account = select(User.id).where(User.id == user_id, User.company_id == company_id)
            stmt = stmt.where(or_(exists(employee), exists(account)))
        elif platform:
            stmt = stmt.where(exists(select(User.id).where(User.id == user_id)))
        return self.db.scalar(stmt)
