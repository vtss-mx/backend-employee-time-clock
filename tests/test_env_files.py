"""Toda la configuración base vive en `.env`, completa y a la vista (decisión del dueño del producto).

`scripts/generate_secrets.py` arma el `.env` completo leyendo `app/core/config.py` (sin importarlo) más las
variables de los scripts de arranque (`RUNTIME`). Estas pruebas son el guardián:

- la salida del generador tiene cada campo de `Settings` y cada variable de los scripts una sola vez (nada
  desconocido), con el valor por defecto del código y documentada (corren en CI, sin `.env`);
- el `.env` local (si existe) tiene exactamente esas variables: un campo nuevo sin su línea en `.env`, o una
  línea que nadie lee, falla aquí. Los mensajes solo nombran variables, nunca muestran un valor.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from app.core.config import ENV_FILE, Settings

ROOT = Path(__file__).resolve().parent.parent
#: Scripts de arranque que leen variables del `.env` además de la API (pgbackrest.sh: los servicios db y pitr, con las
#: mismas variables PITR_* que el monitor del servicio backup).
SCRIPTS = (ROOT / "entrypoint.sh", ROOT / "pgbouncer" / "entrypoint.sh", ROOT / "postgres" / "pgbackrest.sh")
#: Lo que `Settings` deriva al validar (no se compara con el valor por defecto).
DERIVED = {"API_WORKERS", "DATABASE_URL", "DATABASE_DIRECT_URL"}
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_secrets", ROOT / "scripts" / "generate_secrets.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = _load_generator()


def _pairs(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for line in text.splitlines() if (m := _LINE.match(line))]


def _expected_names() -> set[str]:
    return set(Settings.model_fields) | {name for _, name, _, _ in generator.RUNTIME}


def _script_defaults() -> dict[str, str]:
    """`${VAR:-valor}` de los scripts de arranque (el mismo nombre debe tener el mismo valor en todos)."""
    found: dict[str, str] = {}
    for script in SCRIPTS:
        for name, value in re.findall(r"\$\{([A-Z][A-Z0-9_]*):-([^}]*)\}", script.read_text(encoding="utf-8")):
            assert found.setdefault(name, value) == value, f"{name}: dos valores por defecto distintos en los scripts"
    return found


def _docker_format_problems(lines: list[str]) -> list[str]:
    """Comillas o comentarios al final de la línea: `docker run --env-file`, Kubernetes y systemd los tomarían
    como parte del valor."""
    inline = [line for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=.*\s#", line)]
    quoted = [line for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=[\"']", line)]
    return [line.split("=")[0] for line in inline + quoted]


@pytest.fixture(scope="module")
def generated() -> str:
    return generator.render()


def test_the_generator_writes_every_setting_and_script_variable_once(generated):
    names = [name for name, _ in _pairs(generated)]
    assert len(names) == len(set(names)), "Variables repetidas en la salida del generador"
    expected = _expected_names()
    assert sorted(expected - set(names)) == [], "Faltan en el generador"
    assert sorted(set(names) - expected) == [], "El generador escribe variables que nadie lee"
    assert _docker_format_problems(generated.splitlines()) == []


def test_generated_values_are_the_code_defaults_and_new_secrets(generated):
    """Lo generado es exactamente lo que el código usaría sin `.env`, y con llaves válidas."""
    values = dict(_pairs(generated))
    configured = Settings(_env_file=None, **{name: values[name] for name in Settings.model_fields})
    for name, field in Settings.model_fields.items():
        if name in generator.SECRETS:
            assert values[name], f"{name}: secreto vacío"
        elif name in DERIVED:
            assert values[name] == generator._format(field.default), name
        else:
            expected = "" if field.default is None else field.default  # vacío = sin valor (p. ej. FIRST_ADMIN_*)
            assert getattr(configured, name) == expected, name
    again = dict(_pairs(generator.render()))
    assert all(values[name] != again[name] for name in generator.SECRETS), "Los secretos deben ser nuevos cada vez"


def test_script_variables_have_the_same_default_everywhere(generated):
    """El valor de `${VAR:-valor}` en los scripts es el del generador (y el de `Settings` si también la lee)."""
    values = dict(_pairs(generated))
    defaults = _script_defaults()
    for name, default in defaults.items():
        if name in generator.TOPOLOGY:
            assert name not in values, f"{name}: la fija docker compose, no va en .env"
        else:
            assert values.get(name) == default, f"{name}: el script usa {default!r}"
    runtime = {name for _, name, _, _ in generator.RUNTIME}
    assert sorted(runtime - set(defaults)) == [], "Variables de RUNTIME que ningún script lee"
    assert sorted(generator.TOPOLOGY - set(defaults)) == [], "Variables de TOPOLOGY que ningún script lee"


def test_every_setting_says_what_it_does():
    """Cada campo de config.py lleva su comentario (arriba o al final de la línea) o es parte de un grupo de
    líneas seguidas cuyo primer campo lo tiene: el `.env` se explica solo."""
    undocumented: list[str] = []
    previous = None
    for variable in generator.settings_variables():
        grouped = previous is not None and previous.section == variable.section and not variable.gap
        if not variable.notes and not (grouped and previous.name not in undocumented):
            undocumented.append(variable.name)
        previous = variable
    assert undocumented == []


def test_the_generator_reads_every_field_form():
    """Valores literales, `Field(default=...)` con límites, `Literal`, listas, opcionales y secretos."""
    source = """
class Settings:
    model_config = {}

    # --- Grupo ---
    # Qué hace.
    A: int = Field(default=3, gt=0, lt=10)  # en segundos
    B: Literal["x", "y"] = "x"
    C: list[str] = ["a", "b"]

    D: str | None = None
    E: float = Field(default=1.5, le=2.0)
    F: int = Field(default=2, ge=1)
    G: float = Field(default=1.0, gt=0)
    DATA_ENCRYPTION_KEY: str

    def method(self) -> None:
        pass
"""
    found = {v.name: v for v in generator.settings_variables(source)}
    notes = ("Qué hace.", "en segundos")
    assert found["A"] == generator.Variable("Grupo", "A", "3", notes, "Rango: 0 < valor < 10.", gap=True)
    assert (found["B"].notes, found["B"].gap) == ((), False)  # comparte el comentario de A
    assert (found["B"].value, found["B"].hint) == ("x", "Valores: x | y.")
    assert (found["C"].value, found["C"].hint) == ("a,b", "Lista separada por comas.")
    assert (found["D"].value, found["D"].hint, found["D"].gap) == ("", "Opcional: vacío = sin valor.", True)
    assert [found[name].hint for name in "EFG"] == ["Rango: valor ≤ 2.0.", "Rango: valor ≥ 1.", "Rango: valor > 0."]
    assert len(generator.banner("x" * 200)) > 120 >= len(generator.banner("Un grupo con un nombre bastante largo"))
    assert found["DATA_ENCRYPTION_KEY"].value == ""
    with pytest.raises(ValueError, match="Z"):
        generator.settings_variables("class Settings:\n    Z: str\n")
    with pytest.raises(ValueError, match="W"):
        generator.settings_variables("class Settings:\n    W: list[str] = Field(default_factory=list)\n")


@pytest.mark.skipif(not ENV_FILE.is_file(), reason="Sin backend/.env en este entorno (CI)")
def test_the_project_env_file_has_every_variable_and_nothing_else():
    """`backend/.env` está completo: cada variable que lee la API o un script de arranque, una sola vez.
    Un campo nuevo sin su línea aquí, o una línea que nadie lee, falla (solo se nombran las variables)."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    names = [name for name, _ in _pairs("\n".join(lines))]
    assert sorted({n for n in names if names.count(n) > 1}) == [], "Variables repetidas en .env"
    expected = _expected_names()
    assert sorted(expected - set(names)) == [], "Faltan en .env (agrégalas con su valor por defecto)"
    assert sorted(set(names) - expected) == [], "Variables en .env que nadie lee"
    assert _docker_format_problems(lines) == []
