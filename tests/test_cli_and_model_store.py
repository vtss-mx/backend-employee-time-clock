"""Comandos de administración (python -m app.cli) y almacén de modelos ONNX (descarga verificada)."""

import hashlib
import re
import sys

import pytest

from app import cli
from app.facial_recognition import model_store
from app.facial_recognition.model_store import ModelFile, ensure_models


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["python -m app.cli", *argv])
    return cli.main()


# ---------------------------------------------------------------- CLI


def test_create_admin_and_company_from_the_command_line(monkeypatch, capsys):
    assert _run(monkeypatch, "create-admin", "--email", "Nuevo@Plataforma.com", "--password", "Plataforma1234") == 0
    assert "Usuario ADMIN creado" in capsys.readouterr().out
    assert _run(monkeypatch, "create-company", "--email", "otra@empresa.com", "--password", "Empresa1234") == 0
    assert "Usuario COMPANY creado" in capsys.readouterr().out
    # El mismo correo otra vez: error claro y código de salida 1 (no un stack trace).
    assert _run(monkeypatch, "create-admin", "--email", "nuevo@plataforma.com", "--password", "Plataforma1234") == 1
    assert "Error:" in capsys.readouterr().err


def test_interactive_password_must_be_typed_twice(monkeypatch, capsys):
    answers = iter(["Plataforma1234", "Distinta1234"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    assert _run(monkeypatch, "create-admin", "--email", "a@plataforma.com") == 1
    assert "no coinciden" in capsys.readouterr().err
    same = iter(["Plataforma1234", "Plataforma1234"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(same))
    assert _run(monkeypatch, "create-admin", "--email", "a@plataforma.com") == 0


def test_purge_from_the_command_line(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_once", lambda: None)
    assert _run(monkeypatch, "purge") == 0
    assert "Otra instancia" in capsys.readouterr().out
    monkeypatch.setattr(cli, "run_once", lambda: {"sesiones": 2, "códigos QR": 0})
    assert _run(monkeypatch, "purge") == 0
    assert "Depurado: sesiones=2, códigos QR=0" in capsys.readouterr().out


# ---------------------------------------------------------------- almacén de modelos


def _model(content: bytes, *, url: str | None = "https://modelos.example/m.onnx") -> ModelFile:
    return ModelFile(filename="m.onnx", url=url, sha256=hashlib.sha256(content).hexdigest())


class _Download:
    """Respuesta simulada de urllib (sin red)."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        chunk, self.content = (self.content, b"") if size < 0 else (self.content[:size], self.content[size:])
        return chunk


def test_present_and_verified_models_are_not_downloaded(tmp_path, monkeypatch):
    model = _model(b"pesos")
    (tmp_path / "m.onnx").write_bytes(b"pesos")
    monkeypatch.setattr(model_store.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("no debía descargar"))
    assert ensure_models(tmp_path, models=(model,)) == {"m.onnx": tmp_path / "m.onnx"}


def test_missing_model_without_download_fails_with_instructions(tmp_path):
    with pytest.raises(RuntimeError, match=re.escape("python -m app.facial_recognition.model_store")):
        ensure_models(tmp_path, download=False, models=(_model(b"pesos"),))


def test_downloads_with_a_timeout_and_verifies_the_hash(tmp_path, monkeypatch):
    seen: dict = {}

    def urlopen(url, timeout):
        seen.update(url=url, timeout=timeout)
        return _Download(b"pesos")

    monkeypatch.setattr(model_store.urllib.request, "urlopen", urlopen)
    paths = ensure_models(tmp_path, models=(_model(b"pesos"),))
    assert paths["m.onnx"].read_bytes() == b"pesos" and seen["timeout"] == model_store.DOWNLOAD_TIMEOUT_SECONDS


def test_a_tampered_download_is_rejected_and_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store.urllib.request, "urlopen", lambda *_a, **_k: _Download(b"alterado"))
    with pytest.raises(RuntimeError, match="Hash inválido"):
        ensure_models(tmp_path, models=(_model(b"pesos"),))
    assert list(tmp_path.iterdir()) == []  # ni el archivo final ni el temporal


def test_bundled_models_are_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(model_store, "BUNDLED_DIR", tmp_path)
    bundled = _model(b"incluido", url=None)
    with pytest.raises(RuntimeError, match="ausente o alterado"):
        ensure_models(tmp_path / "otro", models=(bundled,))
    (tmp_path / "m.onnx").write_bytes(b"incluido")
    assert ensure_models(tmp_path / "otro", models=(bundled,)) == {"m.onnx": tmp_path / "m.onnx"}
