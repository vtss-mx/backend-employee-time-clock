"""Preferencias de la interfaz de cada usuario (viven en la BD y lo siguen en cualquier dispositivo)."""

from sqlalchemy.orm import Session

from app.models import User
from app.schemas.user import UserPreferences, UserPreferencesUpdate


def update_preferences(db: Session, user: User, changes: UserPreferencesUpdate) -> UserPreferences:
    """Cambio parcial: solo se modifican las preferencias enviadas."""
    current = UserPreferences.model_validate(user.preferences or {})
    updated = current.model_copy(update=changes.model_dump(exclude_unset=True, exclude_none=True))
    user.preferences = updated.model_dump()  # nuevo dict: SQLAlchemy detecta el cambio del JSON
    db.commit()
    return updated
