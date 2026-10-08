"""El cliente REAL de Google Cloud Storage (`GcsStorage`) contra un servidor falso de su API JSON: sin red ni
credenciales reales, pero pasando por el mismo código del cliente oficial (subida con MD5, verificación de
la descarga, reintentos de lo idempotente, traducción de cada falla a un código propio). También cómo se
arma el almacenamiento según la configuración (apagado sin llave, sin spam de errores)."""

import argparse
import base64
import hashlib
import io
import json
import logging
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.oauth2 import service_account
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.response import HTTPResponse

from app import cli
from app.core import object_storage
from app.core.config import settings
from app.core.object_storage import (
    SCOPE,
    DisabledStorage,
    GcsStorage,
    ObjectExists,
    ObjectNotFound,
    StorageError,
    StorageNotConfigured,
    StorageUnavailable,
    content_md5,
    get_storage,
    load_storage,
    service_account_credentials,
    use_storage,
)
from app.services import storage_lifecycle

BUCKET = "employee-time-clock-fb8ba.firebasestorage.app"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "employee-time-clock-fb8ba",
    "private_key_id": "llave-de-pruebas",
    "private_key": _KEY.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode(),
    "client_email": "time-clock-storage@employee-time-clock-fb8ba.iam.gserviceaccount.com",
    "client_id": "1",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


class FakeGcs(BaseAdapter):
    """Lo que el cliente oficial usa de la API JSON de Cloud Storage: subir (multipart), consultar,
    descargar y borrar un objeto. `fail` responde ese estado las siguientes `fail_times` veces."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.fail = 503
        self.fail_times = 0
        self.down = False
        self.corrupt_download = False
        self.lose_responses = 0
        self.requests: list[tuple[str, str]] = []
        #: Ciclo de vida del bucket y su versión de metadatos (la escritura va condicionada a ella).
        self.rules: list[dict] = []
        self.metageneration = 1
        self.patches = 0
        self.race = False
        #: Estado con que responden las llamadas al BUCKET (no a sus objetos): p. ej. 403 sin permiso de administrarlo.
        self.bucket_status: int | None = None

    def send(self, request, **_kwargs):
        if self.down:
            raise requests.ConnectionError("sin red (simulado)")
        path = unquote(urlsplit(request.url).path)
        match = re.search(r"/b/[^/]+/o/(.+)$", path)
        if "/o" not in path:  # metadatos del bucket (su ciclo de vida, o telemetría del propio cliente)
            return self._bucket(request)
        if request.method == "GET" and path.endswith("/o"):  # listar con prefijo y tope
            query = dict(parse_qsl(urlsplit(request.url).query))
            names = sorted(name for name in self.objects if name.startswith(query.get("prefix", "")))
            items = [_resource(name, *self.objects[name]) for name in names[: int(query.get("maxResults", 1000))]]
            return _response(request, 200, {"kind": "storage#objects", "items": items})
        self.requests.append((request.method, match.group(1) if match else path))
        if self.fail_times:
            self.fail_times -= 1
            return _response(request, self.fail, {"error": {"code": self.fail, "message": "falla simulada"}})
        if request.method == "POST":
            return self._upload(request)
        name = match.group(1) if match else ""
        if name not in self.objects:
            return _response(request, 404, {"error": {"code": 404, "message": "No such object"}})
        data, metadata = self.objects[name]
        if request.method == "DELETE":
            del self.objects[name]
            return _response(request, 204, b"")
        if "/download/" in path:
            served = data + b"!" if self.corrupt_download else data
            return _response(request, 200, served, {"x-goog-hash": f"md5={_md5(data)}", "x-goog-generation": "1"})
        return _response(request, 200, _resource(name, data, metadata))

    def _upload(self, request):
        boundary = re.search(r'boundary="([^"]+)"', request.headers["content-type"].decode()).group(1)
        parts = [part for part in request.body.split(b"--" + boundary.encode()) if part.strip(b"-\r\n")]
        meta, data = (part.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n") for part in parts)
        resource = json.loads(meta)
        if resource["md5Hash"] != _md5(data):  # GCS rechaza una subida que llegó dañada
            return _response(request, 400, {"error": {"code": 400, "message": "MD5 distinto"}})
        only_new = "ifGenerationMatch=0" in urlsplit(request.url).query
        if only_new and resource["name"] in self.objects:  # "solo si no existe": 412
            return _response(request, 412, {"error": {"code": 412, "message": "Precondition Failed"}})
        self.objects[resource["name"]] = (data, resource.get("metadata") or {})
        if self.lose_responses:  # se guardó, pero la respuesta no llegó: el cliente reintenta
            self.lose_responses -= 1
            return _response(request, 503, {"error": {"code": 503, "message": "respuesta perdida"}})
        return _response(request, 200, _resource(resource["name"], data, resource.get("metadata") or {}))

    def _bucket(self, request):
        if self.bucket_status:
            status = self.bucket_status
            return _response(request, status, {"error": {"code": status, "message": "falla simulada"}})
        if request.method == "PATCH":
            expected = dict(parse_qsl(urlsplit(request.url).query)).get("ifMetagenerationMatch")
            if self.race or expected != str(self.metageneration):  # alguien lo cambió después de leerlo
                return _response(request, 412, {"error": {"code": 412, "message": "Precondition Failed"}})
            self.rules = json.loads(request.body)["lifecycle"]["rule"]
            self.metageneration += 1
            self.patches += 1
        body = {"name": BUCKET, "location": "US", "metageneration": str(self.metageneration)}
        return _response(request, 200, body | ({"lifecycle": {"rule": self.rules}} if self.rules else {}))

    def close(self) -> None:
        return None


def _resource(name: str, data: bytes, metadata: dict[str, str]) -> dict:
    return {"name": name, "bucket": BUCKET, "size": str(len(data)), "md5Hash": _md5(data), "metadata": metadata}


def _response(request, status: int, body: dict | bytes, headers: dict | None = None):
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    kind = {"content-type": "application/octet-stream" if isinstance(body, bytes) else "application/json"}
    raw = HTTPResponse(
        body=io.BytesIO(content), headers={**kind, **(headers or {})}, status=status, preload_content=False
    )
    return HTTPAdapter().build_response(request, raw)


@pytest.fixture
def gcs():
    """`GcsStorage` real con su sesión HTTP apuntando al servidor falso (reintentos sin espera)."""
    server = FakeGcs()
    session = requests.Session()
    session.is_mtls = False  # lo consulta el cliente para elegir el dominio de la API
    session.mount("https://", server)
    credentials = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT, scopes=[SCOPE])
    storage = GcsStorage(BUCKET, credentials, timeout=5, request_seconds=0.5, retry_seconds=5, http=session)
    storage._request_retry = storage._request_retry.with_delay(initial=0.01, maximum=0.02)
    storage._background_retry = storage._background_retry.with_delay(initial=0.01, maximum=0.02)
    return storage, server


def test_upload_verify_download_and_delete_through_the_official_client(gcs):
    storage, server = gcs
    name = "test/companies/1/employees/2/face-enrollments/3.jpg.enc"
    uploaded = storage.put(name, b"bytes-cifrados", {"kind": "face-enrollment", "sha256": "abc"}, interactive=True)
    assert server.objects[name] == (b"bytes-cifrados", {"kind": "face-enrollment", "sha256": "abc"})
    assert (uploaded.size, uploaded.md5) == (14, content_md5(b"bytes-cifrados"))  # lo que reportó el bucket
    with pytest.raises(ObjectExists):  # "solo si no existe": nunca reemplaza un objeto
        storage.put(name, b"otro", {})
    info = storage.stat(name)
    assert info is not None and (info.size, info.md5) == (14, content_md5(b"bytes-cifrados"))
    assert info.metadata == {"kind": "face-enrollment", "sha256": "abc"}
    assert storage.get(name, interactive=True) == b"bytes-cifrados"
    storage.delete(name)
    assert name not in server.objects
    storage.delete(name)  # ya no estaba: mismo resultado, sin falla
    assert storage.stat(name) is None
    with pytest.raises(ObjectNotFound):
        storage.get(name)
    assert storage.describe() == {"backend": "gcs", "bucket": BUCKET, "reason": None}


def test_idempotent_calls_are_retried_and_failures_get_their_own_type(gcs):
    storage, server = gcs
    server.fail_times = 1  # un 503 pasajero: la subida ("solo si no existe", nombre fijo) se repite sola
    storage.put("test/a.enc", b"x", {})
    assert server.objects["test/a.enc"][0] == b"x"
    assert [method for method, _ in server.requests] == ["POST", "POST"]

    server.lose_responses = 1  # se guardó pero la respuesta se perdió: el reintento encuentra el objeto (412)
    with pytest.raises(ObjectExists):
        storage.put("test/b.enc", b"y", {}, interactive=True)
    assert server.objects["test/b.enc"][0] == b"y"  # quien sube lo verifica (image_storage.upload)

    server.fail_times = 1_000  # el bucket no se recupera: dentro de una petición el tiempo total está acotado
    with pytest.raises(StorageUnavailable, match="503"):
        storage.get("test/a.enc", interactive=True)
    server.fail_times = 0

    server.fail, server.fail_times = 403, 1  # sin permiso: no se reintenta y se dice por qué
    with pytest.raises(StorageUnavailable, match="Forbidden"):
        storage.stat("test/a.enc")

    server.corrupt_download = True  # la descarga no coincide con su MD5: nunca se entrega dañada
    with pytest.raises(StorageUnavailable):
        storage.get("test/a.enc", interactive=True)

    server.down = True  # sin red: se traduce también
    with pytest.raises(StorageUnavailable, match="sin red"):
        storage.get("test/a.enc", interactive=True)


def test_without_retries_a_single_attempt_is_made():
    credentials = service_account.Credentials.from_service_account_info(SERVICE_ACCOUNT, scopes=[SCOPE])
    storage = GcsStorage(BUCKET, credentials, timeout=1, request_seconds=1, retry_seconds=0)
    assert storage._retry(interactive=False) is None and storage._retry(interactive=True) is not None


# ---------------------------------------------------------------- según la configuración


def _configure(monkeypatch, tmp_path, content: bytes | None) -> None:
    path = tmp_path / "gcs.json"
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setattr(settings, "GCS_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "GCS_CREDENTIALS_FILE", str(path))


def test_without_a_bucket_or_key_storage_is_off_with_a_single_warning(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.object_storage"):
        disabled = load_storage()
    assert isinstance(disabled, DisabledStorage) and disabled.configured is False
    assert "faltan GCS_BUCKET" in caplog.text and not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert disabled.describe() == {"backend": "disabled", "bucket": None, "reason": str(disabled.reason)}
    for call in (
        lambda: disabled.put("x", b"", {}),
        lambda: disabled.stat("x"),
        lambda: disabled.get("x"),
        lambda: disabled.delete("x"),
        lambda: disabled.list_names("x", limit=1),
    ):
        with pytest.raises(StorageNotConfigured):
            call()

    _configure(monkeypatch, tmp_path, None)  # la ruta del .env apunta a un archivo que no existe
    assert "no se pudo leer la llave" in load_storage().describe()["reason"]
    _configure(monkeypatch, tmp_path, b"")  # docker compose monta /dev/null mientras no hay llave
    assert "aún no está montada" in load_storage().describe()["reason"]


def test_an_invalid_key_is_a_recorded_configuration_error(monkeypatch, tmp_path, caplog):
    for content in (b"no es json", b"[]", json.dumps({"type": "service_account"}).encode()):
        _configure(monkeypatch, tmp_path, content)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="app.core.object_storage"):
            storage = load_storage()
        assert storage.describe()["reason"] == "la llave de la cuenta de servicio no es válida"
        assert "no es una cuenta de servicio válida" in caplog.text


def test_a_valid_key_turns_the_bucket_on(monkeypatch, tmp_path, caplog):
    _configure(monkeypatch, tmp_path, json.dumps(SERVICE_ACCOUNT).encode())
    with caplog.at_level(logging.INFO, logger="app.core.object_storage"):
        storage = load_storage()
    assert isinstance(storage, GcsStorage) and storage.configured is True
    assert f"gs://{BUCKET}/test/" in caplog.text

    def broken(*_args, **_kwargs):
        raise RuntimeError("cliente sin crear")

    monkeypatch.setattr(object_storage, "GcsStorage", broken)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="app.core.object_storage"):
        assert load_storage().configured is False  # la API arranca igual
    assert "No se pudo crear el cliente" in caplog.text


def test_the_process_builds_its_storage_once():
    use_storage(None)
    first = get_storage()
    assert get_storage() is first
    replacement = DisabledStorage("prueba")
    use_storage(replacement)
    assert get_storage() is replacement
    use_storage(None)
    assert get_storage() is not replacement


# ---------------------------------------------------------------- listar y ciclo de vida del bucket


def test_listing_is_bounded_and_its_failures_translated(gcs):
    storage, server = gcs
    for name in ("test/backups/a/part-00001.enc", "test/backups/a/manifest.json.enc", "test/otra/x.enc"):
        storage.put(name, b"x", {})
    assert storage.list_names("test/backups/", limit=10) == [
        "test/backups/a/manifest.json.enc",
        "test/backups/a/part-00001.enc",
    ]
    assert storage.list_names("test/backups/", limit=1) == ["test/backups/a/manifest.json.enc"]
    server.down = True
    with pytest.raises(StorageUnavailable, match="sin red"):
        storage.list_names("test/", limit=10)


@pytest.fixture
def admin_bucket(monkeypatch, tmp_path):
    """El bucket con permiso de administrarlo (llave de pruebas) contra el servidor falso."""
    server = FakeGcs()
    session = requests.Session()
    session.is_mtls = False
    session.mount("https://", server)
    _configure(monkeypatch, tmp_path, json.dumps(SERVICE_ACCOUNT).encode())
    monkeypatch.setattr(settings, "GCS_RETRY_SECONDS", 0.0)  # sin reintentos: cada falla se ve en seguida
    return storage_lifecycle.open_bucket(http=session), server


def test_the_lifecycle_rule_is_previewed_applied_once_and_never_touches_other_rules(admin_bucket):
    bucket, server = admin_bucket
    foreign = {"action": {"type": "Delete"}, "condition": {"age": 3, "matchesPrefix": ["production/tmp/"]}}
    stale = {"action": {"type": "Delete"}, "condition": {"age": 5, "matchesPrefix": ["test/backups/"]}}
    server.rules = [foreign, stale]
    managed = {"action": {"type": "Delete"}, "condition": {"age": 21, "matchesPrefix": ["test/backups/"]}}
    preview = storage_lifecycle.lifecycle(apply=False, bucket=bucket)  # 14 días + 7 de margen
    assert preview.changed and preview.desired == [foreign, managed] and server.patches == 0
    assert json.loads(preview.gcloud_file()) == {"rule": [foreign, managed]}
    applied = storage_lifecycle.lifecycle(apply=True, bucket=bucket)
    assert server.rules == applied.desired == [foreign, managed] and server.patches == 1
    again = storage_lifecycle.lifecycle(apply=True, bucket=bucket)
    assert not again.changed and server.patches == 1  # idempotente: nada que escribir


def test_the_lifecycle_rule_never_overwrites_a_concurrent_change_nor_hides_a_failure(admin_bucket):
    bucket, server = admin_bucket
    server.race = True  # alguien cambió el bucket entre leerlo y escribirlo
    with pytest.raises(StorageError, match="el bucket cambió"):
        storage_lifecycle.lifecycle(apply=True, bucket=bucket)
    assert server.rules == []
    server.race, server.bucket_status = False, 403  # la cuenta de servicio no puede administrar el bucket
    with pytest.raises(StorageUnavailable, match="Forbidden"):
        storage_lifecycle.lifecycle(apply=True, bucket=bucket)


def test_the_lifecycle_needs_the_bucket_and_asks_for_admin_scope(monkeypatch, tmp_path):
    with pytest.raises(StorageNotConfigured, match="faltan GCS_BUCKET"):
        storage_lifecycle.lifecycle(apply=False)
    _configure(monkeypatch, tmp_path, json.dumps(SERVICE_ACCOUNT).encode())
    assert service_account_credentials(storage_lifecycle.ADMIN_SCOPE).scopes == [storage_lifecycle.ADMIN_SCOPE]
    assert storage_lifecycle.open_bucket().name == BUCKET  # sin llamadas de red


def test_cli_storage_lifecycle(monkeypatch, capsys):
    current = [{"action": {"type": "Delete"}, "condition": {"age": 3, "matchesPrefix": ["otra/"]}}]
    plan = storage_lifecycle.plan_for(BUCKET, current)
    monkeypatch.setattr(storage_lifecycle, "lifecycle", lambda *, apply: plan)
    assert cli._storage_lifecycle(argparse.Namespace(apply=False)) == 0
    out = capsys.readouterr().out
    assert "con más de 21 días" in out and "Sin aplicar (vista previa)" in out and '"otra/"' in out
    assert cli._storage_lifecycle(argparse.Namespace(apply=True)) == 0 and "Aplicada." in capsys.readouterr().out
    done = storage_lifecycle.plan_for(BUCKET, plan.desired)
    monkeypatch.setattr(storage_lifecycle, "lifecycle", lambda *, apply: done)
    assert cli._storage_lifecycle(argparse.Namespace(apply=True)) == 0 and "Sin cambios" in capsys.readouterr().out

    def denied(*, apply):
        raise StorageUnavailable("Forbidden: 403")

    monkeypatch.setattr(storage_lifecycle, "lifecycle", denied)
    assert cli._storage_lifecycle(argparse.Namespace(apply=True)) == 1 and "Forbidden" in capsys.readouterr().err
