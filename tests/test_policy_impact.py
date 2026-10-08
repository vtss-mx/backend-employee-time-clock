"""Cada configuración de la Política de verificación de identidad REALMENTE tiene impacto (decisión del dueño del
producto, 2026-10-08: «asegúrate que cualquier configuración que se haga en Política de verificación de identidad
realmente tenga impacto»).

Guardianes estructurales que fallan solos cuando alguien agrega una columna muerta (que se guarda pero nada la usa) o
que el ADMIN no puede configurar ni volver a leer, más pruebas de comportamiento de punta a punta de los controles que
cambiaron en este lote (los movimientos de la prueba de vida y el destello dictado): cambiar el valor cambia el reto.

Tres garantías estructurales encadenadas (ninguna basta sola, juntas no dejan pasar una configuración sin efecto):
1. **Llega al motor**: cada columna de la política viaja a `PolicySnapshot` (lo que se lee en cada operación facial).
   Una columna que no esté en el snapshot se guardaría y nadie la miraría.
2. **El ADMIN la configura y la vuelve a leer**: cada columna se envía por `VerificationPolicyUpdate` (o el preset) y
   se observa en `AdminPolicyRead`. Una que no se pueda enviar es un control que no se puede «hacer».
3. **El gobierno conoce su dirección de seguridad**: cada columna está clasificada (`policy_rules`: endender/apagar,
   subir/bajar, catálogo con orden) o declarada NEUTRAL a propósito. Así la regla de dos personas y el historial tratan
   TODA columna; una nueva sin clasificar truena aquí (no se puede «rodear» agregando una columna suelta).
"""

from sqlalchemy import inspect

from app.core.config import settings
from app.models import VerificationPolicy
from app.schemas.policy import AdminPolicyRead, VerificationPolicyRead, VerificationPolicyUpdate
from app.services.policy_rules import (
    ORDERED,
    SAFER_WHEN_HIGHER,
    SAFER_WHEN_LOWER,
    SAFER_WHEN_OFF,
    SAFER_WHEN_ON,
)
from app.services.policy_service import POLICY_COLUMNS
from tests.conftest import approved_employee, create_employee, enrollment_challenge, login
from tests.test_policy import admin_policy, set_policy

#: Columnas de la fila que NO son configuración de la empresa (identidad, auditoría del cambio).
METADATA = {"id", "company_id", "updated_at", "updated_by_id"}

#: Columnas sin dirección de seguridad (se aplican al momento, la regla de dos personas no las frena): ayudas de uso o
#: apuntes, nunca un candado. `adaptive_learning` y `fraud_evidence` son decisiones de operación; `preset` es el nombre
#: del último nivel aplicado; `voice_guidance_enabled`/`voice_profile` son la guía por audio (la dicta el navegador).
NEUTRAL = {"adaptive_learning", "fraud_evidence", "preset", "voice_guidance_enabled", "voice_profile"}
#: La configuración por señal se clasifica por entrada (`risk_signals.<código>.<modo|puntos>`), no como columna suelta.
PER_ENTRY = {"risk_signals"}
#: El preset no se envía por el update genérico: tiene su propia ruta (`POST .../preset`).
CONFIGURED_BY_PRESET_ENDPOINT = {"preset"}


def _config_columns() -> set[str]:
    """Las columnas que SÍ son configuración (todo lo de la fila menos identidad y auditoría)."""
    return {c.key for c in inspect(VerificationPolicy).columns} - METADATA


# ----------------------------------------------------------------- guardianes estructurales


def test_every_policy_column_reaches_the_snapshot_the_engine_reads():
    """1. Cada columna de configuración viaja a `PolicySnapshot` (y al revés): una columna que no llegue al snapshot se
    guardaría sin que ninguna operación facial la mirara (configuración muerta)."""
    assert _config_columns() == set(POLICY_COLUMNS)


def test_every_policy_column_is_configurable_by_the_admin_and_observable():
    """2. El ADMIN puede ENVIAR cada columna (por el update o por el preset) y VOLVER A LEERLA (AdminPolicyRead): un
    control que no se puede enviar o que no se puede leer de vuelta no se puede «hacer»."""
    settable = set(VerificationPolicyUpdate.model_fields) | CONFIGURED_BY_PRESET_ENDPOINT
    missing_in_update = set(POLICY_COLUMNS) - settable
    assert not missing_in_update, f"columnas que el ADMIN no puede configurar: {missing_in_update}"
    missing_in_read = set(POLICY_COLUMNS) - set(AdminPolicyRead.model_fields)
    assert not missing_in_read, f"columnas que el ADMIN no vuelve a leer: {missing_in_read}"


def test_every_policy_column_has_a_declared_security_direction():
    """3. El gobierno (regla de dos personas e historial) conoce TODA columna: está clasificada como endurece/relaja o
    declarada NEUTRAL a propósito. Una columna nueva sin clasificar truena aquí."""
    classified = SAFER_WHEN_ON | SAFER_WHEN_OFF | SAFER_WHEN_HIGHER | SAFER_WHEN_LOWER | set(ORDERED)
    assert classified & NEUTRAL == set()  # ninguna está en dos cubetas a la vez
    assert set(POLICY_COLUMNS) == classified | NEUTRAL | PER_ENTRY


# ----------------------------------------------------------------- impacto real (de punta a punta)


def test_the_enabled_head_movements_drive_the_enrollment_challenge(client, company_headers):
    """Los interruptores de movimientos cambian el reto del registro: por omisión solo los giros; al encender mirar
    arriba y abajo, el registro pide los cuatro (decisión del dueño, 2026-10-08)."""
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    assert set(enrollment_challenge(client, headers)["actions"]) == {"TURN_RIGHT", "TURN_LEFT"}
    set_policy(client, company_headers, enable_look_up=True, enable_look_down=True)
    assert set(enrollment_challenge(client, headers)["actions"]) == {"TURN_RIGHT", "TURN_LEFT", "LOOK_UP", "LOOK_DOWN"}


def test_disabling_a_movement_removes_it_from_the_repertoire(client, company_headers):
    """Apagar un movimiento lo saca del repertorio: con los giros a la izquierda y los cabeceos activos (y el giro a la
    derecha apagado), ningún reto vuelve a pedir girar a la derecha."""
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    set_policy(client, company_headers, enable_turn_right=False, enable_look_up=True, enable_look_down=True)
    seen: set[str] = set()
    for _ in range(5):
        seen |= set(enrollment_challenge(client, headers)["actions"])
    assert "TURN_RIGHT" not in seen and {"TURN_LEFT", "LOOK_UP", "LOOK_DOWN"} <= seen


def test_the_verification_repertoire_follows_the_enabled_movements(client, company_headers):
    """La verificación elige de los movimientos activos más «acercarse»: con los cuatro activos, un reto largo puede
    pedir mirar arriba o abajo; por omisión (solo giros) nunca los pide."""
    set_policy(client, company_headers, enable_look_up=True, enable_look_down=True, liveness_steps=3)
    headers = approved_employee(client, company_headers)
    seen: set[str] = set()
    for _ in range(20):
        seen |= set(client.post("/api/face/challenge", headers=headers).json()["data"]["actions"])
    assert seen & {"LOOK_UP", "LOOK_DOWN"}  # con los cabeceos activos, aparecen


def test_at_least_two_movements_must_stay_enabled(client, company_headers):
    """La prueba de vida necesita al menos dos movimientos de dónde elegir (para no repetir el mismo seguido): dejar
    menos responde 422 LIVENESS_MOVES_MIN antes de guardar nada."""
    url, admin = admin_policy(client, company_headers)
    response = client.put(url, json={"enable_turn_right": False, "enable_turn_left": False}, headers=admin)
    assert response.status_code == 422 and response.json()["code"] == "LIVENESS_MOVES_MIN"


def test_the_flash_paced_switch_activates_and_deactivates_the_dictated_flash(client, company_headers):
    """Destello dictado por el servidor (decisión del dueño, 2026-10-08): un interruptor del ADMIN, apagado por
    omisión; encenderlo activa el mecanismo (los colores no viajan con el reto, los dicta el canal en vivo) y apagarlo
    lo quita. El estado del switch activa/desactiva la funcionalidad."""
    headers = approved_employee(client, company_headers)
    assert client.post("/api/face/challenge", headers=headers).json()["data"]["flash_pace"] is None
    set_policy(client, company_headers, flash_paced=True)
    paced = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert paced["flash"] == [] and paced["flash_pace"]["token"]
    assert paced["flash_pace"]["total"] == settings.FACE_FLASH_COLORS
    set_policy(client, company_headers, flash_paced=False)
    assert client.post("/api/face/challenge", headers=headers).json()["data"]["flash_pace"] is None


def test_the_company_facing_read_exposes_the_audio_guidance_hints(client, company_headers):
    """La guía por audio es del navegador (regla 13): su impacto es que la empresa y su personal RECIBEN si está
    encendida y con qué voz, para que la app la dicte. Debe viajar en la lectura de la empresa."""
    assert {"voice_guidance_enabled", "voice_profile"} <= set(VerificationPolicyRead.model_fields)
    set_policy(client, company_headers, voice_guidance_enabled=True, voice_profile="MALE_CALM")
    policy = client.get("/api/settings/verification", headers=company_headers).json()["data"]
    assert policy["voice_guidance_enabled"] is True and policy["voice_profile"] == "MALE_CALM"
