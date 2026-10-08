"""Textos de los catálogos sin préstamos del inglés en español («nunca se mezclan idiomas»)

Decisión del dueño del producto (2026-10-06): «mucho ojo con el idioma… no mezcles el spanish con el english» (regla 16
de la raíz, `backend-employee-time-clock/AGENTS.md` §11.4). El revisor estricto de la app
(`webapp-employee-time-clock/src/i18n/backendLanguage.test.ts`) encontró palabras en inglés dentro de textos en español
de los catálogos de la BD: «anti-spoofing», «morphing», «pHash y embedding», «token», «app» y referencias a nombres
internos («(phrase)», «catalog.verification_reasons»). Se escriben en español («detección de suplantación», «rostro
combinado», «huella visual y vector facial», «sesión» o «credencial», «aplicación»); el inglés solo pierde las
referencias internas. No cambian códigos, `{marcadores}` ni el largo de ninguna columna (≤ 300).

El español va en la columna de cada catálogo y el inglés en `catalog.translations` (§11.3). Cada texto lleva aquí su
versión anterior y la nueva (no se leen del seed), así que `downgrade` deja exactamente lo que había. Sin SQL escrita:
las sentencias son expresiones de SQLAlchemy y los textos y códigos viajan como parámetros (regla 21 de la raíz).

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-06 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "catalog"
LOCALE = "en-US"

#: (catálogo, código, columna) → ((español anterior, español nuevo), (inglés anterior, inglés nuevo)). Un idioma que no
#: cambia lleva el mismo texto en los dos lados (no se toca su fila).
TEXTS: dict[tuple[str, str, str], tuple[tuple[str, str], tuple[str, str]]] = {
    ('enrollment_flags', 'SPOOF', 'description'): (
        ('El anti-spoofing sugiere que las capturas podrían ser de una foto o una pantalla.', 'La detección de suplantación sugiere que las capturas podrían ser de una foto o una pantalla.'),
        ('Anti-spoofing suggests the captures could be of a photo or a screen.', 'Anti-spoofing suggests the captures could be of a photo or a screen.'),
    ),
    ('face_errors', 'ACCESSORIES_DETECTED', 'description'): (
        ('{accessories} se arma con las frases (phrase) del catálogo de accesorios.', '{accessories} se arma con las frases del catálogo de accesorios.'),
        ('{accessories} is built from the phrases (phrase) in the accessories catalog.', '{accessories} is built from the phrases in the accessories catalog.'),
    ),
    ('face_errors', 'LIVENESS_FAILED', 'description'): (
        ('En el registro facial (en la identificación se usa catalog.verification_reasons).', 'En el registro facial (en la identificación se usan los motivos de verificación).'),
        ('During face enrollment (identification uses catalog.verification_reasons).', 'During face enrollment (identification uses the verification reasons).'),
    ),
    ('face_errors', 'LIVENESS_MISMATCH', 'description'): (
        ('En el registro facial (en la identificación se usa catalog.verification_reasons).', 'En el registro facial (en la identificación se usan los motivos de verificación).'),
        ('During face enrollment (identification uses catalog.verification_reasons).', 'During face enrollment (identification uses the verification reasons).'),
    ),
    ('fraud_kinds', 'MORPH', 'name'): (
        ('Rostro combinado (morphing)', 'Rostro combinado'),
        ('Blended face (morphing)', 'Blended face (morphing)'),
    ),
    ('policy_presets', 'HIGH', 'description'): (
        ('Más exigente: tres movimientos en 45 s, destello obligatorio, anti-spoofing Alto, reenvío perceptual obligatorio, un paso más en un dispositivo nuevo y cortes 25/50/75. El protocolo de captura y la presencia de los validadores, en Solo medir.', 'Más exigente: tres movimientos en 45 s, destello obligatorio, detección de suplantación Alta, reenvío perceptual obligatorio, un paso más en un dispositivo nuevo y cortes 25/50/75. El protocolo de captura y la presencia de los validadores, en Solo medir.'),
        ('Stricter: three movements in 45 s, required flash, High anti-spoofing, required perceptual resubmission check, one more step on a new device, and cutoffs 25/50/75. The capture protocol and validator presence stay in Measure only.', 'Stricter: three movements in 45 s, required flash, High anti-spoofing, required perceptual resubmission check, one more step on a new device, and cutoffs 25/50/75. The capture protocol and validator presence stay in Measure only.'),
    ),
    ('policy_presets', 'MAXIMUM', 'description'): (
        ('Para fraude confirmado: tres movimientos en 30 s, anti-spoofing Máximo, reglas duras obligatorias, la empresa aprueba cada dispositivo y el riesgo alto se niega. Exige destello dictado y ráfaga (si faltan, un paso más), firma y ubicación de validadores y código de sitio. Más reintentos con poca luz.', 'Para fraude confirmado: tres movimientos en 30 s, detección de suplantación Máxima, reglas duras obligatorias, la empresa aprueba cada equipo y el riesgo alto se niega. Exige destello dictado y ráfaga (o un paso más), firma y ubicación de validadores y código de sitio. Más reintentos con poca luz.'),
        ('For confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, the company approves each device, and high risk is denied. Requires the paced flash and burst (if missing, one more step), validator signing and location, and site codes. More retries in low light.', 'For confirmed fraud: three movements in 30 s, Maximum anti-spoofing, required hard rules, the company approves each device, and high risk is denied. Requires the paced flash and burst (if missing, one more step), validator signing and location, and site codes. More retries in low light.'),
    ),
    ('policy_presets', 'STANDARD', 'description'): (
        ('Para la mayoría: dos movimientos en 60 s, anti-spoofing Estándar y cortes de riesgo 30/60/80. El destello (hasta calibrarlo), el dispositivo del empleado, el protocolo de captura y la presencia de los validadores, en Solo medir.', 'Para la mayoría: dos movimientos en 60 s, detección de suplantación Estándar y cortes de riesgo 30/60/80. El destello (hasta calibrarlo), el dispositivo del empleado, el protocolo de captura y la presencia de los validadores, en Solo medir.'),
        ("For most companies: two movements in 60 s, Standard anti-spoofing, and risk cutoffs 30/60/80. The flash (until calibrated), the employee's device, the capture protocol, and validator presence stay in Measure only.", "For most companies: two movements in 60 s, Standard anti-spoofing, and risk cutoffs 30/60/80. The flash (until calibrated), the employee's device, the capture protocol, and validator presence stay in Measure only."),
    ),
    ('risk_signals', 'REPLAY_PERCEPTUAL', 'description'): (
        ('Una captura casi idéntica (pHash y embedding) a otra de un intento anterior del mismo empleado. Regla dura al exigirla.', 'Una captura casi idéntica (por su huella visual y su vector facial) a otra de un intento anterior del mismo empleado. Regla dura al exigirla.'),
        ('A capture nearly identical (pHash and embedding) to one from a previous attempt by the same employee. A hard rule when required.', 'A capture nearly identical (pHash and embedding) to one from a previous attempt by the same employee. A hard rule when required.'),
    ),
    ('risk_signals', 'SPOOF_PROB_LOW', 'description'): (
        ('El anti-spoofing pasó, pero la probabilidad de rostro real quedó cerca de su umbral.', 'La detección de suplantación pasó, pero la probabilidad de rostro real quedó cerca de su umbral.'),
        ('Anti-spoofing passed, but the real-face probability was close to its threshold.', 'Anti-spoofing passed, but the real-face probability was close to its threshold.'),
    ),
    ('risk_signals', 'VALIDATOR_KEY_MISMATCH', 'description'): (
        ('Firmó la identificación un dispositivo distinto al que inició sesión: típico de un token copiado a otro equipo. También pasa si el navegador borró su llave.', 'Firmó la identificación un dispositivo distinto al que inició sesión: típico de una sesión copiada a otro equipo. También pasa si el navegador borró su llave.'),
        ('A device other than the one that signed in signed the identification: typical of a token copied to another device. It also happens if the browser cleared its key.', 'A device other than the one that signed in signed the identification: typical of a token copied to another device. It also happens if the browser cleared its key.'),
    ),
    ('risk_signals', 'VALIDATOR_LOCATION_MISSING', 'description'): (
        ('El validador requiere ubicación y la identificación no la trajo: se retiró el permiso o la petición no salió de la app.', 'El validador requiere ubicación y la identificación no la trajo: se retiró el permiso o la petición no salió de la aplicación.'),
        ("The validator requires location and the identification didn't include it: the permission was revoked or the request didn't come from the app.", "The validator requires location and the identification didn't include it: the permission was revoked or the request didn't come from the app."),
    ),
    ('risk_signals', 'VALIDATOR_OUT_OF_ZONE', 'description'): (
        ('La identificación se hizo lejos del lugar donde opera el validador: una tableta que salió del sitio o un token usado en otro lado.', 'La identificación se hizo lejos del lugar donde opera el validador: una tableta que salió del sitio o una sesión usada en otro lado.'),
        ('The identification happened far from where the validator operates: a tablet taken off-site or a token used elsewhere.', 'The identification happened far from where the validator operates: a tablet taken off-site or a token used elsewhere.'),
    ),
    ('risk_signals', 'VALIDATOR_SIGNATURE_INVALID', 'description'): (
        ('La firma de la identificación no corresponde a su reto ni a su contenido. Solo una app alterada o un ataque la produce. Regla dura al exigirla.', 'La firma de la identificación no corresponde a su reto ni a su contenido. Solo una aplicación alterada o un ataque la produce. Regla dura al exigirla.'),
        ("The identification's signature doesn't match its challenge or its content. Only a tampered app or an attack produces it. Hard rule when required.", "The identification's signature doesn't match its challenge or its content. Only a tampered app or an attack produces it. Hard rule when required."),
    ),
    ('risk_signals', 'VALIDATOR_UNSIGNED', 'description'): (
        ('La identificación del validador no trajo la firma de su dispositivo o su reto ya había vencido. La app siempre firma; un programa con un token copiado, no.', 'La identificación del validador no trajo la firma de su dispositivo o su reto ya había vencido. La aplicación siempre firma; un programa con una sesión copiada, no.'),
        ("The validator's identification didn't include its device signature, or its challenge had expired. The app always signs; a program using a copied token doesn't.", "The validator's identification didn't include its device signature, or its challenge had expired. The app always signs; a program using a copied token doesn't."),
    ),
    ('session_revocation_reasons', 'REFRESH_REUSE_DETECTED', 'description'): (
        ('Se presentó un token de renovación ya usado: se asume robo y se cierra la sesión.', 'Se presentó una credencial de renovación ya usada: se asume robo y se cierra la sesión.'),
        ('An already used refresh token was presented: theft is assumed and the session is closed.', 'An already used refresh token was presented: theft is assumed and the session is closed.'),
    ),
    ('verification_reasons', 'SPOOF_DETECTED', 'description'): (
        ('El anti-spoofing detectó una foto impresa, una pantalla o un video.', 'La detección de suplantación encontró una foto impresa, una pantalla o un video.'),
        ('Anti-spoofing detected a printed photo, a screen, or a video.', 'Anti-spoofing detected a printed photo, a screen, or a video.'),
    ),
}

_TRANSLATIONS = sa.table(
    "translations",
    sa.column("catalog"),
    sa.column("code"),
    sa.column("locale"),
    sa.column("field"),
    sa.column("text"),
    schema=CATALOG,
)


def _apply(new: bool) -> None:
    """Cada texto en español (columna del catálogo) y en inglés (`catalog.translations`); una sentencia por texto."""
    pick = 1 if new else 0
    for (catalog, code, column), (spanish, english) in TEXTS.items():
        if spanish[0] != spanish[1]:
            table = sa.table(catalog, sa.column("code"), sa.column(column), schema=CATALOG)
            op.execute(sa.update(table).where(table.c.code == code).values({column: spanish[pick]}))
        if english[0] != english[1]:
            op.execute(
                sa.update(_TRANSLATIONS)
                .where(
                    _TRANSLATIONS.c.catalog == catalog,
                    _TRANSLATIONS.c.code == code,
                    _TRANSLATIONS.c.locale == LOCALE,
                    _TRANSLATIONS.c.field == column,
                )
                .values(text=english[pick])
            )


def upgrade() -> None:
    _apply(new=True)


def downgrade() -> None:
    _apply(new=False)
