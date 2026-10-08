"""Genera el `.env` COMPLETO del backend: todas las variables con su valor por defecto y secretos nuevos.

    python3 scripts/generate_secrets.py > .env && chmod 600 .env      # desde backend-employee-time-clock/

Decisión del dueño del producto: toda la configuración base vive en el `.env` de cada proyecto, a la vista, con su
valor y un comentario que dice qué hace y qué valores acepta. Para no repetir nada, la fuente es
`app/core/config.py` (campos, valores por defecto, límites y comentarios), que este script lee como TEXTO sin
importarlo: en una instalación nueva el `.env` todavía no existe (la redirección `>` lo vacía antes de que el script
corra) y `Settings()` fallaría sin las llaves. Así basta la biblioteca estándar y `cryptography`, también dentro de la
imagen (`perf/run.sh` y `perf/scale/run.sh` lo ejecutan ahí). Se agregan las variables que leen los scripts de
arranque y que no son de `Settings` (`RUNTIME`).

`tests/test_env_files.py` lo verifica: cada campo de `Settings` y cada variable de los scripts sale una sola vez (y
nada desconocido), con el valor del código y documentada; y el `.env` local tiene exactamente esas variables.
"""

import ast
import base64
import io
import re
import secrets
import textwrap
import tokenize
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

CONFIG_FILE = Path(__file__).resolve().parents[1] / "app" / "core" / "config.py"
#: Encabezado de un grupo en config.py: `# --- Base de datos ---`.
_SECTION = re.compile(r"#\s*---\s*(.+?)\s*---\s*$")
#: Límites de `Field(...)` que se anotan como rango válido.
_LIMITS = ("ge", "gt", "le", "lt")

_STARTUP = "Arranque del contenedor de la API (entrypoint.sh)"
_PGBOUNCER = "PgBouncer (pgbouncer/entrypoint.sh)"
#: Variables que no son de `Settings`: las leen los scripts de arranque (no la API). Su valor es el mismo
#: `${VAR:-valor}` del script (lo verifica tests/test_env_files.py). (sección, nombre, valor, comentario).
RUNTIME: tuple[tuple[str, str, str, str], ...] = (
    (_STARTUP, "PORT", "8000", "Puerto de uvicorn dentro del contenedor (el gateway llega por la red interna)."),
    (
        _STARTUP,
        "UVICORN_LIMIT_CONCURRENCY",
        "4000",
        "Tope duro de conexiones por proceso (último recurso: la admisión responde antes 503). Rango: 1 o más.",
    ),
    (_STARTUP, "UVICORN_BACKLOG", "4096", "Conexiones en espera de aceptarse en el socket. Rango: 1 o más."),
    (
        _STARTUP,
        "UVICORN_WS_MAX_SIZE",
        "65536",
        "Tamaño máximo de un mensaje WebSocket en el servidor (bytes); el canal además aplica WS_MAX_MESSAGE_BYTES.",
    ),
    (
        _STARTUP,
        "UVICORN_TIMEOUT_KEEP_ALIVE",
        "5",
        "Segundos que se conserva una conexión HTTP inactiva. Debe ser MAYOR que el keepalive_timeout del gateway "
        "hacia la API (NGINX_UPSTREAM_KEEPALIVE_TIMEOUT del `.env` de la raíz, 4 s): así quien cierra primero es el "
        "proxy, nunca uvicorn a media reutilización. Rango: 1 o más.",
    ),
    (
        _STARTUP,
        "SHUTDOWN_GRACE_SECONDS",
        "20",
        "Apagado ordenado: segundos que se espera a las peticiones en curso tras el drenado "
        "(SHUTDOWN_DRAIN_SECONDS). La suma debe ser menor que stop_grace_period de docker compose (45 s).",
    ),
    (_STARTUP, "MIGRATION_RETRIES", "20", "Intentos (cada 3 s) de aplicar las migraciones mientras la base arranca."),
    (
        _PGBOUNCER,
        "PGBOUNCER_MAX_CLIENT_CONN",
        "2000",
        "Conexiones de cliente que acepta: réplicas x API_WORKERS x (DB_POOL_SIZE + DB_MAX_OVERFLOW) debe caber.",
    ),
    (_PGBOUNCER, "PGBOUNCER_POOL_SIZE", "40", "Conexiones reales a PostgreSQL por base y usuario."),
    (_PGBOUNCER, "PGBOUNCER_MIN_POOL_SIZE", "5", "Conexiones reales que se mantienen abiertas aunque no haya carga."),
    (
        _PGBOUNCER,
        "PGBOUNCER_RESERVE_POOL_SIZE",
        "10",
        "Conexiones extra para quien lleva esperando más de PGBOUNCER_RESERVE_POOL_TIMEOUT.",
    ),
    (_PGBOUNCER, "PGBOUNCER_RESERVE_POOL_TIMEOUT", "3", "Segundos de espera antes de usar la reserva."),
    (
        _PGBOUNCER,
        "PGBOUNCER_MAX_DB_CONNECTIONS",
        "60",
        "Tope total de conexiones hacia PostgreSQL: debe caber en POSTGRES_MAX_CONNECTIONS (`.env` de la raíz, 150) "
        "dejando lugar a las directas (migraciones, respaldos, DBeaver). También reparte el work_mem de PostgreSQL.",
    ),
    (
        _PGBOUNCER,
        "PGBOUNCER_QUERY_WAIT_TIMEOUT",
        "10",
        "Segundos que una consulta espera una conexión libre antes de fallar.",
    ),
    (_PGBOUNCER, "PGBOUNCER_CLIENT_LOGIN_TIMEOUT", "15", "Segundos para que un cliente termine de autenticarse."),
    (
        _PGBOUNCER,
        "PGBOUNCER_IDLE_TRANSACTION_TIMEOUT",
        "1800",
        "Segundos que se tolera una transacción abierta sin actividad (una vuelta del mantenimiento sostiene la "
        "suya mientras trabaja).",
    ),
    (_PGBOUNCER, "PGBOUNCER_SERVER_IDLE_TIMEOUT", "600", "Segundos que una conexión real ociosa sigue abierta."),
    (
        _PGBOUNCER,
        "PGBOUNCER_SERVER_LIFETIME",
        "3600",
        "Vida máxima de una conexión real (segundos): al cumplirla se recicla en cuanto queda libre.",
    ),
)
#: Variables de los scripts de arranque que fija docker compose según la topología (ganan sobre el `.env`): no van
#: en el archivo. Fuera de compose valen el valor por defecto del script.
TOPOLOGY = frozenset({"RUN_MIGRATIONS", "FORWARDED_ALLOW_IPS", "PGBOUNCER_SERVER_HOST", "PGBOUNCER_SERVER_PORT"})
#: Campos de `Settings` que se generan en cada ejecución (nunca un valor fijo).
SECRETS = (
    "JWT_PRIVATE_KEY",
    "DATA_ENCRYPTION_KEY",
    "POSTGRES_PASSWORD",
    "DB_APP_PASSWORD",
    "DB_READONLY_PASSWORD",
    "PITR_CIPHER_PASS",
    "REDIS_PASSWORD",
    "RATE_LIMIT_HASH_SALT",
)

HEADER = """\
# =====================================================================================
#  Employee Time Clock API — ÚNICO archivo de configuración del backend
#  Archivo REAL con secretos: no lo compartas ni lo subas al repositorio (permisos 600).
#  Lo leen: la API (app/core/config.py), entrypoint.sh, PgBouncer (pgbouncer/entrypoint.sh), PostgreSQL
#  y docker compose. Tiene TODAS las variables con su valor (el del código si nadie lo cambió).
#  Instalación nueva: python3 scripts/generate_secrets.py > .env  (secretos nuevos en cada ejecución).
#  Una variable nueva va a app/core/config.py (o al script que la lee) Y a este archivo en el mismo cambio:
#  tests/test_env_files.py falla si falta alguna o sobra una que nadie lee.
#  Formato docker: sin comillas ni comentarios al final de la línea. Vacío = sin valor.
#  docker compose fija lo que depende de la topología y gana sobre este archivo: POSTGRES_HOST/PORT,
#  DB_POOLER, POSTGRES_DIRECT_HOST/PORT, DATABASE_URL, RUN_MIGRATIONS, FORWARDED_ALLOW_IPS y
#  PGBOUNCER_SERVER_HOST/PORT. Lo que solo lee docker compose va en el .env de la raíz.
# ====================================================================================="""


@dataclass(frozen=True)
class Variable:
    """Una línea `NOMBRE=valor` del `.env`, con su grupo y sus comentarios."""

    section: str
    name: str
    value: str
    #: Comentarios escritos junto al campo en config.py (qué hace).
    notes: tuple[str, ...] = ()
    #: Valores válidos, deducidos del tipo y de los límites de `Field(...)`.
    hint: str = ""
    #: Línea en blanco antes (agrupa como en config.py).
    gap: bool = False


def _inline_comments(source: str) -> dict[int, str]:
    """Comentario al final de una línea de código, por número de línea (tokenize: un `#` dentro de un texto no
    cuenta)."""
    found: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and token.line[: token.start[1]].strip():
            found[token.start[0]] = token.string.lstrip("#").strip()
    return found


def _leading(lines: list[str], section: str) -> tuple[str, tuple[str, ...], bool]:
    """Grupo, comentarios pegados al campo y si hay línea en blanco antes, a partir de las líneas que lo preceden."""
    notes: list[str] = []
    gap = False
    for raw in lines:
        line = raw.strip()
        if header := _SECTION.match(line):
            section, notes = header.group(1), []
        elif line.startswith("#"):
            notes.append(line.lstrip("#").strip())
        elif not line:
            notes, gap = [], True
    return section, tuple(notes), gap


def _default(name: str, node: ast.expr | None) -> tuple[object, dict[str, object]]:
    """Valor por defecto y límites de un campo: `= valor` o `= Field(default=valor, ge=..., le=...)`."""
    if node is None:
        if name not in SECRETS:
            raise ValueError(f"{name}: campo sin valor por defecto que no es un secreto generado")
        return "", {}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Field":
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if "default" not in keywords:
            raise ValueError(f"{name}: Field(...) sin `default=` literal")
        limits = {key: ast.literal_eval(keywords[key]) for key in _LIMITS if key in keywords}
        return ast.literal_eval(keywords["default"]), limits
    return ast.literal_eval(node), {}


def _format(value: object) -> str:
    """El valor como lo escribe el `.env`: true/false, listas separadas por comas, vacío = sin valor."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _choices(annotation: ast.expr) -> list[str]:
    """Opciones de un `Literal["a", "b"]` (vacío si el tipo no es un Literal)."""
    if not (isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name)):
        return []
    if annotation.value.id != "Literal":
        return []
    options = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    return [str(ast.literal_eval(option)) for option in options]


def _hint(annotation: ast.expr, default: object, limits: dict[str, object]) -> str:
    """Qué valores acepta: opciones, true/false, lista, opcional o el rango de `Field(ge/gt/le/lt)`."""
    if choices := _choices(annotation):
        return "Valores: " + " | ".join(choices) + "."
    if isinstance(default, bool):
        return "Valores: true | false."
    if isinstance(default, list):
        return "Lista separada por comas."
    if default is None:
        return "Opcional: vacío = sin valor."
    if not limits:
        return ""
    high = f"≤ {limits['le']}" if "le" in limits else f"< {limits['lt']}" if "lt" in limits else ""
    if not high:  # solo mínimo: "valor ≥ 1" se lee mejor que "1 ≤ valor"
        return f"Rango: valor ≥ {limits['ge']}." if "ge" in limits else f"Rango: valor > {limits['gt']}."
    low = f"{limits['ge']} ≤ " if "ge" in limits else f"{limits['gt']} < " if "gt" in limits else ""
    return f"Rango: {low}valor {high}."


def settings_variables(source: str | None = None) -> list[Variable]:
    """Los campos de `Settings` en el orden de config.py, con su valor por defecto, rango y comentarios."""
    source = CONFIG_FILE.read_text(encoding="utf-8") if source is None else source
    lines = source.splitlines()
    inline = _inline_comments(source)
    classes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    settings = next(node for node in classes if node.name == "Settings")
    found: list[Variable] = []
    section, start = "General", settings.lineno
    for statement in settings.body:
        section, notes, gap = _leading(lines[start : statement.lineno - 1], section)
        end = statement.end_lineno or statement.lineno
        start = end
        if not (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)):
            continue
        name = statement.target.id
        default, limits = _default(name, statement.value)
        notes += tuple(inline[line] for line in range(statement.lineno, end + 1) if line in inline)
        hint = _hint(statement.annotation, default, limits)
        found.append(Variable(section, name, _format(default), notes, hint, gap))
    return found


def runtime_variables() -> list[Variable]:
    """Las variables de los scripts de arranque (`RUNTIME`), con su comentario en renglones cortos."""
    return [Variable(section, name, value, tuple(textwrap.wrap(note, 108))) for section, name, value, note in RUNTIME]


def banner(title: str) -> str:
    """Encabezado de un grupo del `.env` (a lo más 120 caracteres)."""
    line = f"# ======================== {title} "
    return line + "=" * max(3, min(24, 120 - len(line)))


def generated_secrets() -> dict[str, str]:
    """Secretos nuevos: llave ES256 (PEM en base64), llave Fernet, las contraseñas de la base (el dueño, el usuario
    de la API y el de solo lectura: cada uno la suya), la llave del repositorio de pgBackRest (PITR, propia: no se
    deriva de DATA_ENCRYPTION_KEY para que rotar una no deje ilegible el otro), la contraseña de Redis y la sal de los
    límites de peticiones."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return {
        "JWT_PRIVATE_KEY": base64.b64encode(pem).decode(),
        "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "DB_APP_PASSWORD": secrets.token_urlsafe(24),
        "DB_READONLY_PASSWORD": secrets.token_urlsafe(24),
        "PITR_CIPHER_PASS": secrets.token_urlsafe(48),
        # La contraseña de Redis (la leen la API y el servicio redis de docker compose) y la sal de las huellas de los
        # límites de peticiones (token_urlsafe(32) son 43 caracteres: caben en la llave de BLAKE2, de hasta 64 bytes).
        "REDIS_PASSWORD": secrets.token_urlsafe(32),
        "RATE_LIMIT_HASH_SALT": secrets.token_urlsafe(32),
    }


def render() -> str:
    """El `.env` completo: encabezado, cada grupo de config.py y las variables de los scripts de arranque."""
    values = generated_secrets()
    out = [HEADER]
    section = ""
    for variable in [*settings_variables(), *runtime_variables()]:
        if variable.section != section:
            section = variable.section
            out += ["", banner(section)]
        elif variable.gap or variable.notes:
            out.append("")
        out += [f"# {line}" for line in (*variable.notes, variable.hint) if line]
        out.append(f"{variable.name}={values.get(variable.name, variable.value)}")
    return "\n".join(out) + "\n"


def main() -> None:
    print(render(), end="")


if __name__ == "__main__":
    main()
