"""Mensajes de personal y empresas en español de España (es-ES): solo lo que cambia respecto de es-MX (vocabulario de
España; `docs/i18n/glosario.md` §3). Lo demás se reutiliza de `es_mx` (regla 6 de la raíz: sin duplicación)."""

from app.i18n.messages.base import Messages
from app.i18n.messages.es_mx import people as es_mx

#: Solo las llaves cuyo texto cambia en España (un plural que cambia va completo, con todas sus formas).
OVERRIDES: Messages = {
    "ACCOUNT_LINKABLE_EMAIL": (
        "Esta persona ya tiene cuenta en la plataforma. Se añadirá a tu empresa con su contraseña actual."
    ),
    "ACCOUNT_PHONE_MISSING": (
        "La cuenta de esta persona no tiene teléfono. Su empresa actual debe añadirlo antes de vincularla."
    ),
    "ADDRESS_NEIGHBORHOOD_REQUIRED": "El barrio es obligatorio",
    "ADDRESS_NEIGHBORHOOD_TOO_SHORT": "El barrio es muy corto",
    "ADDRESS_STATE_REQUIRED": "La provincia es obligatoria",
    "ADDRESS_STATE_TOO_SHORT": "La provincia es muy corta",
    "COMPANY_RFC_LENGTH": "El RFC debe tener 12 caracteres (persona jurídica) o 13 (persona física)",
    "DEPARTMENT_MANAGER_ADDED": "Responsable añadido",
    "EMPLOYEE_LIMIT_REACHED": (
        "Tu empresa ha alcanzado su límite de {count} empleados. Contacta con el administrador de la plataforma."
    ),
    "PHONE_INVALID_FOR_CODE": "El teléfono no es válido para el prefijo +{code}",
}
MESSAGES: Messages = {**es_mx.MESSAGES, **OVERRIDES}
