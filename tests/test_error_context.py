"""Contexto literal de un error: todo tal cual salvo secretos y archivos, copiado mientras pasa."""

import json
import logging

from app.core.error_context import CAPTURE_LIMIT, BodyCapture, headers_of, parse_body, query_of, redact
from app.core.request_context import RequestInfo, request_info_var
from app.middleware.request_id import _Exchange
from app.services.error_reporter import _log_context

BOUNDARY = "----limite123"


def _multipart(*parts: str) -> bytes:
    body = "".join(f"--{BOUNDARY}\r\n{part}\r\n" for part in parts) + f"--{BOUNDARY}--\r\n"
    return body.encode()


FIELD = 'Content-Disposition: form-data; name="challenge_id"\r\n\r\nabc123'
SECRET = 'Content-Disposition: form-data; name="password"\r\n\r\nMiClave123'
PHOTO = (
    'Content-Disposition: form-data; name="images"; filename="f0.jpg"\r\nContent-Type: image/jpeg\r\n\r\n' + "x" * 300
)


def _capture(body: bytes, content_type: str, *, chunk: int | None = None) -> BodyCapture:
    capture = BodyCapture(content_type)
    step = chunk or len(body) or 1
    for start in range(0, len(body), step):
        capture.feed(body[start : start + step])
    return capture


def test_secrets_are_hidden_at_any_level_and_the_rest_is_literal():
    value = {"email": "a@b.com", "password": "x", "nested": [{"api_key": "k", "name": "Ana"}], "Token": "t"}
    assert redact(value) == {
        "email": "a@b.com",
        "password": "[oculto]",
        "nested": [{"api_key": "[oculto]", "name": "Ana"}],
        "Token": "[oculto]",
    }
    assert query_of(b"") is None
    assert query_of(b"a=1&b=2&b=3&b=4&refresh_token=z") == {"a": "1", "b": ["2", "3", "4"], "refresh_token": "[oculto]"}
    headers = [
        (b"user-agent", b"iPhone"),
        (b"authorization", b"Bearer x"),
        (b"cookie", b"tc_refresh=y"),
        (b"content-type", b"x"),
    ]
    assert headers_of(headers) == {"user-agent": "iPhone", "content-type": "x"}


def test_bodies_are_read_by_their_type():
    assert parse_body("application/json", b'{"password": "x", "n": 1}') == {"password": "[oculto]", "n": 1}
    assert parse_body("application/json", b"{roto") == "{roto"
    assert parse_body("application/x-www-form-urlencoded", b"email=a%40b.com&password=x") == {
        "email": "a@b.com",
        "password": "[oculto]",
    }
    assert parse_body("text/plain", "hola ñ".encode()) == "hola ñ"
    assert parse_body("", b"sin tipo") == "sin tipo"
    assert parse_body("application/octet-stream", b"\x00\x01") == "[2 bytes de application/octet-stream]"
    assert parse_body("application/json", b"") is None


def test_a_large_body_is_kept_up_to_the_limit():
    # Las partes que llegan después del tope solo se cuentan: la copia no crece.
    capture = _capture(b"x" * (CAPTURE_LIMIT + 2_000), "application/json", chunk=1_000)
    assert capture.truncated and capture.total == CAPTURE_LIMIT + 2_000 and len(capture.value()) == CAPTURE_LIMIT
    assert not _capture(b"", "multipart/form-data").truncated  # sin boundary: se trata como cuerpo normal


def test_only_the_request_body_is_copied_not_a_disconnect():
    exchange = _Exchange({"type": "http", "headers": [(b"content-type", b"application/json")]})
    exchange.received({"type": "http.request", "body": b'{"a": 1}', "more_body": True})
    exchange.received({"type": "http.disconnect"})  # la persona cerró la conexión a medias
    assert exchange.request.total == 8 and exchange.request.value() == {"a": 1}


def test_multipart_is_summarized_even_when_it_arrives_byte_by_byte():
    body = _multipart(FIELD, SECRET, PHOTO)
    for chunk in (None, 1, 7):
        capture = _capture(b"preambulo\r\n" + body, f'multipart/form-data; boundary="{BOUNDARY}"', chunk=chunk)
        assert capture.value() == [
            {"name": "challenge_id", "value": "abc123"},
            {"name": "password", "value": "[oculto]"},
            {"name": "images", "filename": "f0.jpg", "content_type": "image/jpeg", "size": 300},
        ], chunk
        assert not capture.truncated


def test_a_cut_or_malformed_multipart_keeps_what_was_seen():
    body = _multipart(FIELD, PHOTO)
    cut = _capture(body[:-60], f"multipart/form-data; boundary={BOUNDARY}", chunk=50)  # se cortó a mitad del archivo
    parts = cut.value()
    assert parts[0] == {"name": "challenge_id", "value": "abc123"} and parts[1]["filename"] == "f0.jpg"
    nameless = _capture(
        _multipart("Content-Type: text/plain\r\n\r\nsin nombre"), f"multipart/form-data; boundary={BOUNDARY}"
    )
    assert nameless.value() == [{"name": "", "value": "sin nombre"}]
    endless = _capture(
        f"--{BOUNDARY}\r\n".encode() + b"x" * 9_000, f"multipart/form-data; boundary={BOUNDARY}", chunk=3_000
    )
    assert endless.value() == []  # encabezados imposibles: se deja de resumir
    nothing = _capture(b"sin delimitadores" * 10, f"multipart/form-data; boundary={BOUNDARY}", chunk=8)
    assert nothing.value() == []


def test_log_errors_inside_a_request_say_which_and_whose():
    record = logging.LogRecord("app.x", logging.ERROR, "x.py", 1, "falló", None, None, func="tarea")
    outside = _log_context(record)
    assert outside == {"logger": "app.x", "thread": record.threadName, "function": "tarea"}
    token = request_info_var.set(
        RequestInfo(user_id=3, user_email="a@b.com", user_role="COMPANY", company_id=9, method="GET", path="/api/x")
    )
    try:
        inside = _log_context(record)
    finally:
        request_info_var.reset(token)
    assert inside["request"] == {"method": "GET", "path": "/api/x"} and inside["company_id"] == 9
    assert inside["user"] == {"id": 3, "email": "a@b.com", "role": "COMPANY"}
    anonymous = request_info_var.set(RequestInfo(method="POST", path="/api/auth/login"))
    try:
        assert _log_context(record)["user"] is None
    finally:
        request_info_var.reset(anonymous)
    assert json.dumps(inside)  # se guarda como JSON
