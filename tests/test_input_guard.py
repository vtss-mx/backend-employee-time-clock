"""Texto que la API no acepta (`app/core/input_guard.py`, regla 21 de la raíz): la regla, sus tres lugares y la última
barrera en el motor de la BD. La matriz de todas las rutas está en `tests/test_sql_injection.py`."""

import io

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import insert, select, text, update
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.database import Base, SessionLocal
from app.core.input_guard import (
    INVALID_TEXT,
    InvalidCharactersError,
    _is_json,
    _pairs_field,
    _unstorable,
    ensure_valid_text,
    invalid_field,
)
from app.models import User
from tests.conftest import COMPANY_EMAIL


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("normal\tcon tabulador\ny saltos\r\n", None),
        ("a\x00b", ""),
        ("\x0b", ""),
        ("\ud800", ""),
        ("😀 par sustituto ya unido", None),
        ({"nombre": "ok", "notas": ["bien", {"detalle": "mal\x1f"}]}, "notas.1.detalle"),
        ({"llave\x00": "valor"}, ""),
        ({"anidado": {"llave\x00": "valor"}}, "anidado"),
        ((1, 2.5, None, True, b"\x00 bytes no son texto"), None),
        (["x", ("y", "z\x07")], "1.1"),
        (42, None),
    ],
)
def test_the_rule_finds_the_first_invalid_text_at_any_depth(value, field):
    assert invalid_field(value) == field


def test_the_rule_keeps_tab_and_line_breaks_and_rejects_the_rest_of_c0():
    allowed = {"\t", "\n", "\r"}
    for code in range(0x20):
        assert (INVALID_TEXT.search(chr(code)) is None) == (chr(code) in allowed)
    assert INVALID_TEXT.search("\x7f ñ ü € 中") is None  # DEL y cualquier letra o símbolo: se aceptan


def test_ensure_valid_text_raises_the_422_with_its_field():
    ensure_valid_text({"ok": ["sí"]})
    with pytest.raises(InvalidCharactersError) as raised:
        ensure_valid_text({"campo": "a\x00"})
    assert (raised.value.status_code, raised.value.code, raised.value.field) == (422, "INVALID_CHARACTERS", "campo")
    with pytest.raises(InvalidCharactersError) as raised:
        ensure_valid_text("\x00")
    assert raised.value.field is None
    assert len(InvalidCharactersError("x" * 500).field or "") == 120  # nombre de un cliente: acotado


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (None, False),
        ("", False),
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("application/vnd.api+json", True),
        ("text/plain", False),
        ("multipart/form-data; boundary=x", False),
    ],
)
def test_the_body_is_read_as_json_exactly_when_fastapi_reads_it(content_type, expected):
    assert _is_json(content_type) is expected


def test_a_json_route_with_another_content_type_or_without_body_is_left_to_its_validation(client, company_headers):
    as_text = client.post(
        "/api/departments", content=b'{"name": "a\\u0000"}', headers={**company_headers, "Content-Type": "text/plain"}
    )
    assert as_text.status_code == 422 and as_text.json()["code"] == "VALIDATION_ERROR"
    empty = client.post(
        "/api/departments", content=b"", headers={**company_headers, "Content-Type": "application/json"}
    )
    assert empty.status_code == 422 and empty.json()["code"] == "VALIDATION_ERROR"


def test_query_names_and_uploaded_file_names_are_checked_too(client, company_headers):
    by_name = client.get("/api/departments", params={"a\x00": "1"}, headers=company_headers)
    assert by_name.status_code == 422 and by_name.json()["errors"][0]["field"] is None  # el nombre no se repite
    files = [("images", UploadFile(io.BytesIO(b"x"), filename=name)) for name in ("bien.jpg", None, "mal\x00.jpg")]
    assert _pairs_field(files) == "images" and _pairs_field(files[:2]) is None


def test_the_message_is_in_the_language_of_the_request(client, company_headers):
    spanish = client.get("/api/departments", params={"search": "\x00"}, headers=company_headers).json()
    english = client.get(
        "/api/departments", params={"search": "\x00"}, headers={**company_headers, "Accept-Language": "en-US"}
    ).json()
    assert spanish["message"] == "El texto tiene caracteres que no se permiten. Quítalos e intenta de nuevo."
    assert english["message"] == "The text has characters that aren't allowed. Remove them and try again."
    assert spanish["errors"][0]["field"] == english["errors"][0]["field"] == "search"


# ------------------------------------------------------------------------------------- la última barrera (el motor)


@pytest.mark.parametrize(
    "parameters",
    [
        {"v": "a\x00b"},
        {"v": "ñandú\ud800"},
        [{"v": "bien"}, {"v": "\x00"}],
        ("tupla", "\x00"),
        {"v": {"json": ["a", {"b": "\x00"}]}},
        {"v": {"llave\x00": 1}},
        {"v": Jsonb({"json": "\x00"})},
    ],
)
def test_the_engine_refuses_what_the_database_cannot_store(parameters):
    assert _unstorable(parameters)


@pytest.mark.parametrize(
    "parameters",
    [{"v": "texto normal"}, {"v": "ñandú 😀 \x1b"}, {"v": 7, "w": None, "x": b"\x00"}, {"v": Jsonb({"a": "b"})}, {}],
)
def test_the_engine_lets_through_everything_else(parameters):
    assert not _unstorable(parameters)


def test_a_statement_with_nul_is_never_sent_and_answers_422():
    """Un camino que no pasó por la regla de la petición (un JSON dentro de un formulario, un proceso nuevo): la base
    nunca recibe el NUL (psycopg respondería un `DataError`, un 500) y la sentencia anterior queda intacta."""
    with SessionLocal() as db:
        with pytest.raises(InvalidCharactersError):
            db.execute(text("SELECT :v"), {"v": "a\x00"})
        db.rollback()
        with pytest.raises(InvalidCharactersError):
            db.execute(update(User).where(User.email == COMPANY_EMAIL).values(preferences={"nota": "\x00"}))
        db.rollback()
        assert db.scalar(select(User.preferences).where(User.email == COMPANY_EMAIL)) == {}
        assert db.execute(text("SELECT :v"), {"v": "sin problema"}).scalar() == "sin problema"
    with SessionLocal() as db, pytest.raises(InvalidCharactersError):  # SQL del controlador, sin compilar
        db.connection().exec_driver_sql(
            "SELECT 1 WHERE 'a' <> %(v)s" if _postgres(db) else "SELECT 1 WHERE 'a' <> ?",
            {"v": "\x00"} if _postgres(db) else ("\x00",),
        )
    with SessionLocal() as db, pytest.raises(InvalidCharactersError):  # un lote: cada fila que va a enviarse
        rows = [{"code": f"NUL_{n}", "name": "bien" if n else "mal\x00"} for n in range(3)]
        db.execute(insert(Base.metadata.tables["catalog.error_severities"]), rows)


def _postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"
