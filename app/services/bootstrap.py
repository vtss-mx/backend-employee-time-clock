"""Usuarios iniciales: administrador de la plataforma (ADMIN) y empresa inicial (COMPANY).

Se crean al arrancar desde variables de entorno (FIRST_ADMIN_*, FIRST_COMPANY_*) o con la CLI.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.passwords import hash_password
from app.models import Company, User, UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.validators import validate_password_strength
from app.services.policy_service import PolicyService

logger = logging.getLogger(__name__)
DEFAULT_COMPANY_NAME = "Mi empresa"


def _create_user(db: Session, email: str, password: str, role: UserRole, company_id: int | None) -> User:
    validate_password_strength(password)
    repo = UserRepository(db)
    if repo.email_exists(email):
        raise ValueError(f"Ya existe un usuario con el correo {email}")
    return repo.create(email=email, password_hash=hash_password(password), role=role, company_id=company_id)


def default_company(db: Session) -> Company:
    """Primera empresa de la plataforma (se crea si no hay ninguna) con su política por defecto."""
    company = db.scalar(select(Company).order_by(Company.id).limit(1))
    if company is None:
        company = Company(name=DEFAULT_COMPANY_NAME, active=True)
        db.add(company)
        db.flush()
        PolicyService(db, company.id).ensure()
    return company


def create_admin_user(db: Session, email: str, password: str) -> User:
    """Administrador de la plataforma: da de alta y administra empresas."""
    user = _create_user(db, email, password, UserRole.ADMIN, None)
    db.commit()
    return user


def create_company_user(db: Session, email: str, password: str, company_id: int | None = None) -> User:
    """Administrador de una empresa (por defecto, la primera de la plataforma)."""
    target = company_id if company_id is not None else default_company(db).id
    user = _create_user(db, email, password, UserRole.COMPANY, target)
    db.commit()
    return user


UserFactory = Callable[[Session, str, str], User]


def _ensure(db: Session, label: str, email: str | None, password: str | None, create: UserFactory) -> None:
    if not email or not password or UserRepository(db).email_exists(email):
        return
    try:
        create(db, email, password)
        logger.info("Usuario %s inicial creado: %s", label, email)
    except IntegrityError:
        # Varios procesos de la API arrancan a la vez: otro ya lo creó (el correo es único).
        db.rollback()
    except ValueError as exc:
        db.rollback()
        logger.error("No se pudo crear el usuario %s inicial: %s", label, exc)


def ensure_first_admin(db: Session) -> None:
    _ensure(db, "ADMIN", settings.FIRST_ADMIN_EMAIL, settings.FIRST_ADMIN_PASSWORD, create_admin_user)


def ensure_first_company(db: Session) -> None:
    _ensure(db, "COMPANY", settings.FIRST_COMPANY_EMAIL, settings.FIRST_COMPANY_PASSWORD, create_company_user)
