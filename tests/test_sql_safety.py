"""SQL sin inyección (regla 21 del `AGENTS.md` raíz), vigilado en el CÓDIGO: una regresión no llega a revisarse.

Lee el AST de todo `app/` y `alembic/` y falla si:

1. **Texto de SQL armado con datos**: `text()`, `literal_column()`, `DDL()`, `exec_driver_sql()`, `op.execute()` (o un
   `.execute()` con un texto) reciben algo que no sea constante, o una f-string / `%` / `.format` / `.join` /
   concatenación arma SQL (empieza con `SELECT`, `INSERT`, `ALTER`, `GRANT`...) con algo que no sea:
   - otra constante (también un nombre del módulo o importado de `app`, un elemento de una tupla constante que se
     recorre con `for`, o una variable local que solo recibe cosas así);
   - `sql_identifier(...)` / `sql_literal(...)` (`app/core/sql_safety.py`) o `int(...)`.
   Los valores van SIEMPRE como parámetros (`:nombre`). Lo que no se puede demostrar así va en `ALLOWED` (código) o
   `FROZEN_MIGRATIONS` (migraciones ya aplicadas, que no se editan), cada una con su motivo y su cuenta exacta: una
   construcción nueva en esa función o archivo cambia la cuenta y la prueba falla; una entrada que sobra, también.
2. **`LIKE` fuera del ayudante**: `like`/`ilike`/`contains`/`startswith`/`endswith`/`regexp_match` o `autoescape=`/
   `escape=` en `app/` fuera de `app/repositories/search.py` (`contains_text`: el término literal, con `ESCAPE`).
3. **Columna dinámica**: `getattr(Modelo, <no constante>)` en repositorios y modelos (o `getattr(func, ...)` en
   cualquier lado) fuera de `DYNAMIC_ATTRIBUTES` (cada una de una lista cerrada, con su motivo).
4. **Funciones de la base** (`alembic/sql/*.sql`): toda `SECURITY DEFINER` fija `search_path = pg_catalog, pg_temp`
   (sin eso, un objeto con el mismo nombre en otro esquema la secuestra) y todo `EXECUTE` dinámico es `format(...)`
   (`%I`, `%L`) o un texto constante con `USING`, nunca concatenado. En PostgreSQL además se revisa la base viva:
   ninguna función `SECURITY DEFINER` sin su `search_path`, ninguna ejecutable por `PUBLIC` ni por la API.
"""

import ast
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.core.db_schemas import ALL_SCHEMAS
from app.core.sql_safety import sql_identifier, sql_literal

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "alembic" / "versions"
#: El único lugar con `LIKE` (ver la regla 2).
SEARCH_MODULE = "app/repositories/search.py"
#: Llamadas que reciben TEXTO de SQL en su primer argumento.
SQL_SINKS = frozenset({"text", "literal_column", "DDL", "exec_driver_sql"})
#: Lo que se puede interpolar en una SQL armada: los ayudantes de `app/core/sql_safety.py` y un entero.
SAFE_CALLS = frozenset({"sql_identifier", "sql_literal", "int"})
#: Envolturas de una colección constante (`frozenset({...})`, `tuple(...)`...).
COLLECTIONS = frozenset({"frozenset", "tuple", "list", "set", "sorted"})
#: Métodos que producen TEXTO (lo demás que recibe un `.execute()` es una sentencia de SQLAlchemy, no texto).
TEXT_METHODS = frozenset({"format", "join", "replace", "read_text"})
#: Un texto que es SQL (palabras de una sentencia, en mayúsculas como se escribe la SQL en este código).
SQL_TEXT = re.compile(
    r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM|ALTER|CREATE|DROP|GRANT|REVOKE|TRUNCATE|COMMENT ON|EXECUTE|VACUUM)\b"
)
#: Operadores de patrón de SQLAlchemy (solo `contains_text` los usa).
LIKE_METHODS = frozenset({"like", "ilike", "notlike", "notilike", "not_like", "not_ilike", "regexp_match"})
#: Además, en repositorios y modelos (donde no hay cadenas de Python que los usen).
LIKE_METHODS_IN_SQL_LAYERS = frozenset({"contains", "startswith", "endswith", "icontains", "istartswith", "iendswith"})

#: SQL armada que el AST no puede demostrar segura: (archivo, función) → (cuántas, motivo).
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("app/core/db_roles.py", "_ensure_role"): (
        1,
        "`attributes`: las palabras clave constantes que le pasa `provision` (LOGIN, NOSUPERUSER, BYPASSRLS...); el "
        "nombre va con `sql_identifier` y el verificador SCRAM con `sql_literal`",
    ),
    ("app/core/row_security.py", "_create_policy"): (
        1,
        "las sentencias de `policy_ddl` (identificadores con `sql_identifier`, comentario con `sql_literal`)",
    ),
    ("app/core/db_sql.py", "register_sql_files"): (
        1,
        "el contenido de `alembic/sql/*.sql` (archivos del repositorio, revisados por la regla 4 de esta prueba)",
    ),
}

#: Migraciones ya aplicadas (no se editan, `backend-employee-time-clock/AGENTS.md` §3): su SQL armada se revisó una por
#: una en la auditoría de inyección (2026-10-05). Todas corren con el DUEÑO en el despliegue, sin ninguna entrada de una
#: persona: lo que interpolan son nombres de esquemas, tablas, columnas, índices y restricciones, y códigos de
#: catálogo, de las constantes del propio módulo. Archivo → (cuántas, motivo).
_NAMES = "nombres de esquema, tabla, columna, índice o restricción de las constantes del módulo"
_SPECS = "atributos de las especificaciones constantes del módulo (tabla, llave, índice, restricción, definición)"
_HELPER = "ayudantes del módulo que reciben SQL o archivos `alembic/sql` constantes (`_run_sql`, `_scalar`)"
_CODES = "códigos de catálogo de las constantes del módulo, entre comillas (`_in(NEW_SIGNALS)`, `codes`)"
_TEXTS = "catálogo y columna de las tuplas constantes del módulo (`PREVIOUS_TEXTS`...); el texto va como `:text`"
FROZEN_MIGRATIONS: dict[str, tuple[int, str]] = {
    "0011_policy_min_confidence.py": (2, "constantes numéricas de la calibración del módulo (`A, B = -13.3, 38.0`)"),
    "0017_domain_schemas.py": (5, f"{_NAMES} (`TABLES_BY_SCHEMA`) y el `search_path` constante"),
    "0020_catalogs.py": (1, "el `search_path` constante del módulo (`SEARCH_PATH`)"),
    "0045_calendar.py": (2, "`column` de las llamadas del propio módulo a `_user_index` (nombres fijos)"),
    "0047_performance_audit.py": (4, f"{_SPECS} (`Ix`)"),
    "0054_tenant_keys.py": (10, f"{_SPECS} (`Key`, `INDEXES`) y {_HELPER}"),
    "0055_partitions.py": (33, f"{_SPECS} (`Spec`), {_HELPER} y la fecha de corte que calcula la base"),
    "0060_currencies.py": (2, f"{_NAMES} (`MOVEMENTS`, `_fk`)"),
    "0062_identity_antifraud.py": (5, f"{_CODES}, {_HELPER} y {_NAMES}"),
    "0063_performance_observability.py": (1, _HELPER),
    "0064_catalog_translations.py": (1, "los catálogos del archivo de semillas del repositorio (`catalogs.*.json`)"),
    "0065_antifraud_device_network.py": (7, f"{_CODES} y {_TEXTS}"),
    "0066_antifraud_capture_protocol.py": (5, f"{_CODES} y {_TEXTS}"),
    "0067_validator_limits.py": (1, _TEXTS),
    "0068_soft_delete.py": (10, f"{_SPECS} (`Ix`, `Uq`) y las columnas de la constante `REASON` (con parámetros)"),
    "0069_catalog_copy_style.py": (1, _TEXTS),
}


#: `getattr` con un nombre que no es constante: (archivo, función) → (cuántas, motivo).
DYNAMIC_ATTRIBUTES: dict[tuple[str, str], tuple[int, str]] = {
    ("app/repositories/employee_repository.py", "EmployeeRepository.unique_exists"): (
        1,
        "`field` es `UniqueField` = Literal['employee_number', 'rfc', 'curp', 'nss']",
    ),
    ("app/repositories/performance_repository.py", "_bucket_bound"): (
        2,
        "las columnas fijas del histograma (`COLUMNS` de `app/core/histogram.py`)",
    ),
    ("app/repositories/performance_repository.py", "sums"): (
        1,
        "recorre la constante `SUMMED` (contadores y columnas del histograma)",
    ),
    ("app/repositories/risk_repository.py", "RiskSignalStatRepository.bump"): (
        1,
        "`column` sale de `STAT_COLUMNS` de `fraud_case_service` ('confirmed' o 'false_positive'), nunca del cliente",
    ),
    ("app/repositories/storage_repository.py", "StorageRepository.record"): (
        1,
        "las llaves del diccionario que arma el propio método (`values`: columnas fijas de la tarea)",
    ),
}


# ----------------------------------------------------------------------------------------------- análisis del AST


@dataclass
class Module:
    """Un archivo con lo que se necesita para resolver nombres: sus constantes y de dónde importa cada nombre."""

    path: Path
    tree: ast.Module
    root: Path = ROOT
    constants: dict[str, ast.expr] = field(default_factory=dict)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)

    @property
    def relative(self) -> str:
        return self.path.relative_to(self.root).as_posix()


@cache
def module_at(path: Path, root: Path = ROOT) -> Module:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module = Module(path, tree, root)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            module.constants[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            module.constants[node.target.id] = node.value
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            for alias in node.names:
                module.imports[alias.asname or alias.name] = (node.module, alias.name)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            module.parents[child] = parent
    return module


def module_named(name: str) -> Module | None:
    """El módulo `app.x.y` (o paquete) por su nombre, si existe."""
    base = ROOT.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return module_at(candidate)
    return None


def scopes(module: Module, node: ast.AST) -> list[ast.AST]:
    """Las funciones que encierran al nodo, de la más cercana a la más lejana."""
    found = []
    current = module.parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            found.append(current)
        current = module.parents.get(current)
    return found


def qualname(module: Module, node: ast.AST) -> str:
    """`Clase.función` (o `<module>`) donde está el nodo."""
    names = []
    current = module.parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(current.name)
        current = module.parents.get(current)
    return ".".join(reversed(names)) or "<module>"


class Resolver:
    """¿Una expresión es texto CONSTANTE (o armado solo con los ayudantes seguros)? Sigue nombres locales, del módulo,
    importados de `app` y variables de un `for` sobre una colección constante."""

    def __init__(self, module: Module) -> None:
        self.module = module
        self.visiting: set[tuple[str, int]] = set()

    def safe(self, node: ast.AST, at: ast.AST) -> bool:
        match node:
            case ast.Constant():
                return isinstance(node.value, str | int | float | bool) or node.value is None
            case ast.JoinedStr():
                return all(self.safe(v.value, at) for v in node.values if isinstance(v, ast.FormattedValue))
            case ast.BinOp(op=ast.Add()):
                return self.safe(node.left, at) and self.safe(node.right, at)
            case ast.IfExp():
                return self.safe(node.body, at) and self.safe(node.orelse, at)
            case ast.Tuple() | ast.List() | ast.Set():
                return all(self.safe(element, at) for element in node.elts)
            case ast.Call(func=ast.Name(id=name)) if name in SAFE_CALLS:
                return True
            case ast.Call(func=ast.Name(id=name), args=[inner]) if name in COLLECTIONS:
                return self.safe(inner, at)
            case ast.Call(func=ast.Attribute(attr="replace", value=inner), args=[ast.Constant(), ast.Constant()]):
                return self.safe(inner, at)  # `.replace("%", "%%")` de un texto seguro
            case ast.Call(func=ast.Attribute(attr="read_text", value=path)):
                return self.sql_file(path)
            case ast.Name(id=name):
                return self.name(name, at)
        return False

    def sql_file(self, path: ast.AST) -> bool:
        """Un archivo `.sql` FIJO del repositorio (`SQL_DIR / "0070_comments.sql"`): lo revisa la regla 4."""
        if isinstance(path, ast.Name):
            value, owner = self._constant(path.id)
            return value is not None and Resolver(owner).sql_file(value)
        return (
            isinstance(path, ast.BinOp)
            and isinstance(path.op, ast.Div)
            and isinstance(path.right, ast.Constant)
            and str(path.right.value).endswith(".sql")
        )

    def textual(self, node: ast.AST, at: ast.AST, *, unknown: bool) -> bool:
        """¿La expresión es TEXTO de SQL (y no una sentencia de SQLAlchemy, que lleva sus valores como parámetros)?
        `unknown`: qué suponer de lo que no se puede saber (un parámetro): texto en `op.execute` (Alembic ejecuta
        cadenas); sentencia en un `.execute()` de SQLAlchemy 2 (rechaza cadenas: `ObjectNotExecutableError`)."""
        match node:
            case ast.Constant():
                return isinstance(node.value, str)
            case ast.JoinedStr() | ast.BinOp():
                return True
            case ast.Call():
                return call_name(node) in TEXT_METHODS
            case ast.Name(id=name):
                values = [value for scope in scopes(self.module, at) for value in self._assigned(scope, name)]
                if not values and name in self.module.constants:
                    values = [self.module.constants[name]]
                if not values:
                    return unknown
                return any(self.textual(value, value, unknown=unknown) for value in values)
        return unknown and not isinstance(node, ast.Starred)

    @staticmethod
    def _assigned(scope: ast.AST, name: str) -> Iterator[ast.expr]:
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                yield node.value

    def name(self, name: str, at: ast.AST) -> bool:
        key = (name, id(at))
        if key in self.visiting:
            return False
        self.visiting.add(key)
        try:
            return self._name(name, at)
        finally:
            self.visiting.discard(key)

    def _name(self, name: str, at: ast.AST) -> bool:
        for scope in scopes(self.module, at):
            local = list(self._local(scope, name))
            if local:
                return all(local)
            if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) and name in {
                a.arg for a in [*scope.args.args, *scope.args.kwonlyargs, *scope.args.posonlyargs]
            }:
                return False  # parámetro de la función: puede venir de cualquier lado
        if name in self.module.constants:
            return self.safe(self.module.constants[name], self.module.constants[name])
        if name in self.module.imports:
            origin, original = self.module.imports[name]
            other = module_named(origin)
            if other is not None and original in other.constants:
                return Resolver(other).safe(other.constants[original], other.constants[original])
        return False

    def _local(self, scope: ast.AST, name: str) -> Iterator[bool]:
        """Cada asignación del nombre dentro de la función (o el `for` que lo recorre): ¿es segura?"""
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
                    yield self.safe(node.value, node)
            elif isinstance(node, ast.For | ast.comprehension):
                position = self._position(node.target, name)
                if position is not None:
                    yield self._element(node.iter, position, node)

    @staticmethod
    def _position(target: ast.AST, name: str) -> int | None:
        """-1 si el `for` asigna el nombre entero; su posición si desempaca una tupla; None si no lo asigna."""
        if isinstance(target, ast.Name):
            return -1 if target.id == name else None
        if isinstance(target, ast.Tuple):
            for index, element in enumerate(target.elts):
                if isinstance(element, ast.Name) and element.id == name:
                    return index
        return None

    def _element(self, iterable: ast.AST, position: int, at: ast.AST) -> bool:
        """¿Cada elemento (o su parte `position`) de la colección constante que recorre el `for` es seguro?"""
        collection = self._collection(iterable, at)
        if collection is None:
            return False
        if position < 0:
            return all(self.safe(element, at) for element in collection)
        return all(
            isinstance(element, ast.Tuple) and len(element.elts) > position and self.safe(element.elts[position], at)
            for element in collection
        )

    def _collection(self, node: ast.AST, at: ast.AST) -> list[ast.expr] | None:
        match node:
            case ast.Tuple() | ast.List() | ast.Set():
                return list(node.elts)
            case ast.Call(func=ast.Name(id=name), args=[inner]) if name in COLLECTIONS:
                return self._collection(inner, at)
            case ast.Name(id=name):
                value, owner = self._constant(name)
                return None if value is None else Resolver(owner)._collection(value, value)
        return None

    def _constant(self, name: str) -> tuple[ast.expr | None, Module]:
        if name in self.module.constants:
            return self.module.constants[name], self.module
        if name in self.module.imports:
            origin, original = self.module.imports[name]
            other = module_named(origin)
            if other is not None and original in other.constants:
                return other.constants[original], other
        return None, self.module


def call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def built(node: ast.AST) -> bool:
    """¿Es una cadena ARMADA (f-string con valores, `%`, `+`, `.format`, `.join`)?"""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add):
        return any(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in ast.walk(node))
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "join")


def literal_text(node: ast.AST) -> str:
    return " ".join(n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def sql_argument(node: ast.Call, resolver: Resolver) -> ast.expr | None:
    """El texto de SQL que recibe la llamada, si es un sumidero (ver la regla 1): `text()` y compañía siempre; un
    `.execute()` (`op.execute`, un cursor) solo si recibe texto y no una sentencia de SQLAlchemy."""
    name = call_name(node)
    if not node.args:
        return None
    first = node.args[0]
    if name in SQL_SINKS:
        return first
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    alembic = isinstance(receiver, ast.Name) and receiver.id == "op"
    if name in ("execute", "executemany") and resolver.textual(first, node, unknown=alembic):
        return first
    return None


def sources() -> list[Path]:
    return sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "alembic").rglob("*.py")])


def unsafe_sql(paths: list[Path] | None = None, root: Path = ROOT) -> Counter[tuple[str, str]]:
    """(archivo, función) → cuántas construcciones de SQL no se pueden demostrar seguras."""
    found: Counter[tuple[str, str]] = Counter()
    for path in sources() if paths is None else paths:
        module = module_at(path, root)
        resolver = Resolver(module)
        seen: set[int] = set()
        for node in ast.walk(module.tree):
            target: ast.AST | None = None
            if isinstance(node, ast.Call):
                target = sql_argument(node, resolver)
            if target is None and built(node) and SQL_TEXT.search(literal_text(node)):
                target = node
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            if not resolver.safe(target, target):
                found[(module.relative, qualname(module, target))] += 1
    return found


def test_sql_text_is_constant_or_built_only_with_the_safe_helpers():
    found = unsafe_sql()
    code = {key: count for key, count in found.items() if not key[0].startswith("alembic/versions/")}
    expected = {key: count for key, (count, _) in ALLOWED.items()}
    assert code == expected, (
        "SQL armada con algo que no es constante ni pasa por sql_identifier/sql_literal (o una excepción de ALLOWED "
        f"que ya no existe). Encontrado: {code}"
    )
    migrations: Counter[str] = Counter()
    for (path, _), count in found.items():
        if path.startswith("alembic/versions/"):
            migrations[Path(path).name] += count
    frozen = {name: count for name, (count, _) in FROZEN_MIGRATIONS.items()}
    assert dict(migrations) == frozen, (
        "Una migración arma SQL con algo que no es constante ni pasa por sql_identifier/sql_literal: en una migración "
        f"NUEVA usa esos ayudantes; una aplicada no se edita. Encontrado: {dict(migrations)}"
    )


def test_every_exception_explains_why():
    for _, reason in [*ALLOWED.values(), *FROZEN_MIGRATIONS.values(), *DYNAMIC_ATTRIBUTES.values()]:
        assert len(reason) > 20


def _calls(prefix: str = "") -> Iterator[tuple[Module, ast.Call]]:
    for path in sorted((ROOT / "app").rglob("*.py")):
        module = module_at(path)
        if module.relative.startswith(prefix):
            for node in ast.walk(module.tree):
                if isinstance(node, ast.Call):
                    yield module, node


def test_like_patterns_only_through_the_search_helper():
    """Un `LIKE` con lo que escribe la persona sin escapar convierte `%` y `_` en comodines: solo `contains_text`."""
    sql_layers = ("app/repositories/", "app/models/")
    offenders = []
    for module, node in _calls():
        if module.relative == SEARCH_MODULE:
            continue
        name = call_name(node)
        in_sql_layer = module.relative.startswith(sql_layers)
        pattern = name in LIKE_METHODS or (in_sql_layer and name in LIKE_METHODS_IN_SQL_LAYERS)
        if pattern or any(k.arg in ("autoescape", "escape") for k in node.keywords):
            offenders.append(f"{module.relative}:{node.lineno} {name}")
    assert offenders == [], f"Usa contains_text de {SEARCH_MODULE}: {offenders}"


def test_columns_are_never_chosen_by_name_at_run_time():
    """`getattr(Modelo, nombre)` con un nombre que llega de afuera elige la columna: solo listas cerradas (una
    constante, o un `for` sobre una tupla constante, se demuestra solo)."""
    found: Counter[tuple[str, str]] = Counter()
    for module, node in _calls():
        if call_name(node) != "getattr" or len(node.args) < 2 or Resolver(module).safe(node.args[1], node):
            continue
        receiver = node.args[0]
        on_func = isinstance(receiver, ast.Name) and receiver.id == "func"
        if on_func or module.relative.startswith(("app/repositories/", "app/models/")):
            found[(module.relative, qualname(module, node))] += 1
    assert dict(found) == {key: count for key, (count, _) in DYNAMIC_ATTRIBUTES.items()}


# ------------------------------------------------------------------------------------------- funciones de la base

_FUNCTION = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?P<name>[\w.]+)\s*\((?P<args>[^)]*)\)(?P<head>.*?)"
    r"\bAS\s+\$(?P<tag>\w*)\$(?P<body>.*?)\$(?P=tag)\$",
    re.IGNORECASE | re.DOTALL,
)
_EXECUTE = re.compile(r"\bEXECUTE\b(?!\s+ON\b)", re.IGNORECASE)
_SEARCH_PATH = re.compile(r"\bSET\s+search_path\s*=\s*pg_catalog\s*,\s*pg_temp\b", re.IGNORECASE)


def sql_functions() -> Iterator[tuple[str, str, str]]:
    for path in sorted((ROOT / "alembic" / "sql").glob("*.sql")):
        for match in _FUNCTION.finditer(path.read_text(encoding="utf-8")):
            yield f"{path.name}:{match['name']}", match["head"], match["body"]


def _constant_end(body: str, start: int) -> int:
    """Dónde termina la cadena `'...'` que empieza en `start` (las comillas dobles `''` son parte de ella)."""
    index = start + 1
    while True:
        index = body.index("'", index)
        if body.startswith("''", index):
            index += 2
            continue
        return index + 1


def dynamic_sql_problems(body: str) -> list[str]:
    """Cada `EXECUTE` dinámico es `format(...)` o una cadena constante con sus valores en `USING`."""
    problems = []
    body = re.sub(r"--[^\n]*", "", body)  # los comentarios no son SQL
    for match in _EXECUTE.finditer(body):
        rest = body[match.end() :].lstrip()
        if rest.lower().startswith("format("):
            continue
        if rest.startswith("'"):
            after = rest[_constant_end(rest, 0) :].lstrip()
            if not after.startswith("||"):
                continue
        problems.append(rest[:60])
    return problems


def test_database_functions_pin_their_search_path_and_never_concatenate_sql():
    functions = list(sql_functions())
    assert functions, "alembic/sql no tiene funciones: ¿cambió el formato de los archivos?"
    for name, head, body in functions:
        if re.search(r"\bSECURITY\s+DEFINER\b", head, re.IGNORECASE):
            assert _SEARCH_PATH.search(head), f"{name}: SECURITY DEFINER sin SET search_path = pg_catalog, pg_temp"
        assert dynamic_sql_problems(body) == [], f"{name}: EXECUTE con SQL concatenada"


def test_the_function_rules_catch_what_they_forbid():
    assert dynamic_sql_problems("EXECUTE 'DROP TABLE ' || p_table;") == ["'DROP TABLE ' || p_table;"]
    assert dynamic_sql_problems("EXECUTE p_sql;") == ["p_sql;"]
    assert dynamic_sql_problems("EXECUTE 'SELECT ''a''' USING x; EXECUTE format('DROP TABLE %I', t);") == []
    assert not _SEARCH_PATH.search("SECURITY DEFINER SET search_path = public")


def test_the_scanner_catches_what_it_forbids(tmp_path):
    """La prueba misma: lo inseguro se detecta y lo seguro no (para que nadie la debilite sin notarlo)."""
    source = """
from sqlalchemy import text
TABLE = "workforce.employees"
NAMES = ("a", "b")
def bad(conn, value, order):
    conn.exec_driver_sql(f"DELETE FROM x WHERE name = '{value}'")
    text("SELECT * FROM t ORDER BY " + order)
    text("SELECT %s" % value)
    sql = "SELECT * FROM t WHERE a = {}".format(value)
    conn.execute(sql)
def good(conn, value):
    conn.execute(text("SELECT * FROM t WHERE a = :a"), {"a": value})
    conn.exec_driver_sql(f"GRANT SELECT ON {TABLE} TO {sql_identifier(value)}")
    for name in NAMES:
        text(f"SELECT {name} FROM t LIMIT {int(value)}")
"""
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    assert unsafe_sql([path], tmp_path) == {("probe.py", "bad"): 5}


def test_the_safe_helpers_refuse_what_is_not_a_closed_name_or_a_code_literal():
    assert sql_identifier("ops.perf_minutes") == "ops.perf_minutes"
    assert sql_identifier("user", "timeclock_app") == '"user", timeclock_app'  # palabra reservada: entrecomillada
    for bad in ("Employees", "a; DROP TABLE x", "a.b.c", '"x"', "x y", "", "a" * 64, "emp\x00"):
        with pytest.raises(ValueError, match="Identificador SQL inválido"):
            sql_identifier(bad)
    assert sql_literal("it's") == "'it''s'"
    for bad in ("a\\b", "a\x00b"):
        with pytest.raises(ValueError, match="Literal SQL inválida"):
            sql_literal(bad)


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Solo PostgreSQL tiene funciones y roles")
def test_the_live_database_functions_and_the_api_role_have_least_privilege():
    """Base viva (la de la suite en PostgreSQL, con los roles de producción): funciones SECURITY DEFINER con su
    `search_path`, que ni `PUBLIC` ni la API ejecutan (solo el rol de la plataforma); y la API sin DDL ni privilegios
    de superusuario (la falla cerrada de la seguridad por fila la recorre `tests/test_tenant_isolation.py`)."""
    from tests.conftest import owner_engine

    app_role = settings.DB_APP_USER
    with owner_engine.connect() as conn:
        functions = conn.execute(
            text(
                "SELECT p.oid::regprocedure::text, p.prosecdef, coalesce(p.proconfig, '{}'), "
                "has_function_privilege(:app, p.oid, 'EXECUTE'), "
                "EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
                "WHERE a.grantee = 0) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(:schemas)"
            ),
            {"app": app_role, "schemas": list(ALL_SCHEMAS)},
        ).all()
        role = conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                "has_database_privilege(rolname, current_database(), 'CREATE') "
                "FROM pg_roles WHERE rolname = :app"
            ),
            {"app": app_role},
        ).one()
        creates = conn.execute(
            text(
                "SELECT n.nspname FROM pg_namespace n "
                "WHERE n.nspname = ANY(:schemas) AND has_schema_privilege(:app, n.oid, 'CREATE')"
            ),
            {"app": app_role, "schemas": [*ALL_SCHEMAS, "public"]},
        ).scalars()
        owned = conn.execute(
            text("SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = :app"),
            {"app": app_role},
        ).scalar_one()
    definers = [f for f in functions if f[1]]
    assert definers, "Sin funciones SECURITY DEFINER: ¿se crearon las de alembic/sql?"
    for name, definer, config, app_executes, public_executes in functions:
        assert not public_executes and not app_executes, f"{name}: la ejecuta PUBLIC o la API"
        if definer:
            assert "search_path=pg_catalog, pg_temp" in config, f"{name}: SECURITY DEFINER sin search_path fijo"
    assert tuple(role) == (False, False, False, False, False)
    assert list(creates) == [] and owned == 0
