"""Tolerancia a fallas: cada prueba provoca una falla real y verifica que el sistema la soporte
(responde con su código y el sobre de siempre, sigue atendiendo lo demás y no pierde datos)."""

import json
from typing import Annotated

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core import crypto
from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.exceptions import ServiceUnavailableError, register_exception_handlers
from app.middleware.security import SecurityMiddleware
from app.models import FaceEmbedding
from app.services import billing_jobs, catalog_service, maintenance_service, storage_jobs
from app.services.face_gallery import FaceGalleryCache, face_galleries
from tests.conftest import approved_employee, create_employee, login, submit_enrollment, turn_files
from tests.test_validators import approved, identify_face, validator_headers

COOKIE = settings.REFRESH_COOKIE_NAME


@pytest.fixture(autouse=True)
def _fresh_gallery():
    face_galleries.clear()
    yield


def _verify(client, headers, *, turn: str = "turn:{person}"):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"c{i}.jpg", b"face:juan", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "juan", image=turn)
    data = {"challenge_id": challenge["challenge_id"]}
    return client.post("/api/verification/face", data=data, files=files, headers=headers)


def _corrupt_sample(employee_id: int) -> None:
    with SessionLocal() as db:
        row = db.query(FaceEmbedding).filter_by(employee_id=employee_id).order_by(FaceEmbedding.id).first()
        row.embedding_encrypted = b"dato-danado"
        db.commit()


# ---------------------------------------------------------------- datos cifrados


def test_unreadable_samples_are_skipped_not_fatal(client, company_headers):
    juan = approved(client, company_headers, "juan", number="EMP-001")
    approved(client, company_headers, "ana", number="EMP-002")
    _corrupt_sample(juan["id"])  # una de sus 3 muestras quedó ilegible
    headers = validator_headers(client, company_headers, mode="FACE")
    found = identify_face(client, headers, "juan").json()["data"]  # la galería se arma sin ella
    assert found["verified"] is True and found["employee_id"] == juan["id"]
    employee = login(client, "juan@empresa.com", "Empleado123")
    assert _verify(client, employee).json()["data"]["verified"] is True  # 1:1 con las muestras legibles


def test_previous_encryption_keys_still_read_old_data(monkeypatch):
    old = Fernet.generate_key()
    stored = Fernet(old).encrypt(b"vector")
    assert crypto.try_decrypt(stored) is None  # con solo la llave nueva no se lee
    monkeypatch.setattr(settings, "DATA_ENCRYPTION_PREVIOUS_KEYS", old.decode())
    monkeypatch.setattr(
        crypto, "_fernet", crypto.MultiFernet([Fernet(k.encode()) for k in settings.data_encryption_keys])
    )
    assert crypto.decrypt_bytes(stored) == b"vector"
    assert crypto.decrypt_bytes(crypto.encrypt_bytes(b"nuevo")) == b"nuevo"
    with pytest.raises(ValueError):
        crypto.decrypt_bytes(b"basura")


# ---------------------------------------------------------------- sesión


def test_refresh_retry_after_a_lost_response_gets_the_same_token(client):
    login(client, "admin@empresa.com", "Admin1234")
    first = client.cookies.get(COOKIE)
    assert client.post("/api/auth/refresh").status_code == 200
    second = client.cookies.get(COOKIE)
    # La respuesta se perdió: el teléfono reintenta con el token anterior (dentro de la gracia).
    client.cookies.set(COOKIE, first, path="/api/auth")
    retry = client.post("/api/auth/refresh")
    assert retry.status_code == 200 and retry.cookies.get(COOKIE) == second  # el mismo, idempotente
    client.cookies.clear()
    client.cookies.set(COOKIE, second, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 200  # y con él la sesión sigue sana


def test_remembering_the_account_never_breaks_a_login(client, monkeypatch):
    from app.services.remembered_account_service import RememberedAccountService

    def broken(*_args, **_kwargs):
        raise OperationalError("INSERT", {}, Exception("BD ocupada"))

    monkeypatch.setattr(RememberedAccountService, "remember", broken)
    response = client.post(
        "/api/auth/login", json={"email": "admin@empresa.com", "password": "Admin1234", "remember": True}
    )
    assert response.status_code == 200 and response.json()["code"] == "LOGIN_SUCCESS"


# ---------------------------------------------------------------- reconocimiento facial


def test_unreadable_turn_capture_is_a_retryable_liveness_failure(client, company_headers):
    headers = approved_employee(client, company_headers)
    response = _verify(client, headers, turn="broken-turn:{person}")
    assert response.status_code == 200
    assert response.json()["data"]["verified"] is False  # no es un 500: se pide repetir la captura


def test_learning_failure_never_turns_a_success_into_an_error(client, company_headers, monkeypatch):
    from app.services.face_learning import FaceLearning

    def broken(*_args, **_kwargs):
        raise RuntimeError("falla inesperada al evaluar")

    monkeypatch.setattr(FaceLearning, "_candidate", broken)
    headers = approved_employee(client, company_headers)
    assert _verify(client, headers).json()["data"]["verified"] is True


def test_face_engine_loading_fails_fast_instead_of_blocking():
    from app.facial_recognition import FaceEngineUnavailable, _PipelineHolder

    holder = _PipelineHolder()
    holder._lock.acquire()  # otro hilo está cargando los modelos
    try:
        with pytest.raises(FaceEngineUnavailable):
            holder.pool()
    finally:
        holder._lock.release()


def test_waiting_for_a_face_worker_releases_the_database_connection():
    from app.dependencies import get_pipeline

    class Session:
        commits = 0

        def commit(self):
            Session.commits += 1

    dependency = get_pipeline(Session())  # type: ignore[arg-type]
    with pytest.raises(ServiceUnavailableError):  # sin modelos en las pruebas: 503, DESPUÉS de soltar la conexión
        next(dependency)
    assert Session.commits == 1


def test_gallery_cache_is_bounded_by_memory():
    import numpy as np

    from app.services.face_gallery import Gallery

    cache = FaceGalleryCache(max_companies=100, max_bytes=3000)
    gallery = Gallery((1, 1, ""), np.arange(2), np.arange(2), np.ones((2, 128), dtype=np.float32))  # ~1 KB
    for company in range(5):
        cache._store((company, "m"), gallery)
    kept = list(cache._items)
    assert kept[-1] == (4, "m") and sum(g.nbytes for g in cache._items.values()) <= 3000 and len(kept) < 5
    huge = Gallery((1, 1, ""), np.arange(9), np.arange(9), np.ones((9, 512), dtype=np.float32))  # > límite
    cache._store((9, "m"), huge)
    assert list(cache._items) == [(9, "m")]  # se pasa sola del límite, pero la recién usada se queda


# ---------------------------------------------------------------- base de datos y mantenimiento


def test_one_failing_purge_does_not_stop_the_others(monkeypatch):
    real = maintenance_service.delete_batch
    calls: list[str] = []

    def flaky(db, key, condition, limit, **options):
        calls.append((key[0] if isinstance(key, tuple) else key).class_.__name__)  # llave simple o compuesta
        if len(calls) == 1:
            raise OperationalError("DELETE", {}, Exception("statement_timeout"))
        return real(db, key, condition, limit, **options)

    monkeypatch.setattr(maintenance_service, "delete_batch", flaky)
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    purges = (*maintenance_service.PURGES, *maintenance_service.SOFT_DELETE_PURGES)
    # Cada tabla depurada se intentó (dos depuraciones pueden ser de la misma tabla: los documentos de la empresa).
    assert set(calls) == {(p.key[0] if isinstance(p.key, tuple) else p.key).class_.__name__ for p in purges}
    extra = {
        "jornadas sin salida",
        "umbrales recalibrados",
        maintenance_service.PERF_ROLLUPS,
        *(name for name, _ in billing_jobs.TASKS),
        storage_jobs.DELETED,
        # Antifraude: la evidencia vencida y los casos decididos viejos (en lotes, con su bucket).
        "evidencia de casos de fraude",
        "casos de fraude decididos",
        # Particiones mensuales: solo en PostgreSQL.
        *(
            (maintenance_service.PARTITIONS_CREATED, maintenance_service.PARTITIONS_DROPPED)
            if engine.dialect.name == "postgresql"
            else ()
        ),
    }
    purges = (*maintenance_service.PURGES, *maintenance_service.SOFT_DELETE_PURGES)
    assert set(removed) == {purge.name for purge in purges} | extra


def test_catalogs_survive_a_database_blip(monkeypatch):
    first = catalog_service.get_catalogs()

    def down(_db):
        raise OperationalError("SELECT", {}, Exception("conexión rechazada"))

    monkeypatch.setattr(catalog_service, "load_catalogs", down)
    monkeypatch.setattr(settings, "CATALOG_CACHE_SECONDS", 0)
    assert catalog_service.get_catalogs() is first  # se sirven los anteriores


def _app_raising(error: Exception) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:
        raise error

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("sqlstate", "status", "code"),
    [("40P01", 409, "CONCURRENT_UPDATE"), ("57014", 503, "DATABASE_TIMEOUT"), (None, 503, "DATABASE_UNAVAILABLE")],
)
def test_database_errors_get_their_own_stable_code(sqlstate, status, code):
    class Origin(Exception):
        pass

    origin = Origin("pg")
    origin.sqlstate = sqlstate  # type: ignore[attr-defined]
    response = _app_raising(OperationalError("SELECT 1", {}, origin)).get("/boom")
    body = response.json()
    assert response.status_code == status and body["code"] == code and body["success"] is False
    assert set(body) >= {"statusCode", "message", "data", "errors", "traceId", "timestamp"}


def test_chunked_uploads_cannot_exceed_the_size_limit():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/upload")
    async def upload(request: Request) -> dict:
        return {"size": len(await request.body())}

    @app.post("/form")
    async def form(name: Annotated[str, Form()]) -> dict:
        return {"name": name}

    @app.post("/json")
    async def as_json(payload: dict[str, str]) -> dict:
        return payload

    client = TestClient(SecurityMiddleware(app, max_body=10), raise_server_exceptions=False)

    def chunks():  # sin Content-Length: por partes
        return (part for part in (b"12345", b"67890", b"abcde"))

    # Igual si el cuerpo lo lee el código o FastAPI (formulario o JSON): 413 en español, no un 400.
    for path, content_type in (("/upload", None), ("/form", "application/x-www-form-urlencoded"), ("/json", None)):
        headers = {"Content-Type": content_type} if content_type else {"Content-Type": "application/json"}
        response = client.post(path, content=chunks(), headers=headers)
        body = response.json()
        assert response.status_code == 413 and body["code"] == "PAYLOAD_TOO_LARGE", path
        assert "excede el tamaño máximo" in body["message"]
    assert client.post("/upload", content=b"pequeno").json() == {"size": 7}
    # Un cuerpo ilegible tampoco responde en inglés.
    broken = client.post("/form", content=b"x", headers={"Content-Type": "multipart/form-data"})
    assert broken.status_code == 400 and broken.json()["message"] == "No se pudo leer el contenido de la solicitud"


def test_concurrent_manager_retries_are_idempotent(client, company_headers, monkeypatch):
    from app.repositories.department_repository import DepartmentRepository

    department = client.post("/api/departments", json={"name": "Producción"}, headers=company_headers).json()["data"]
    person = create_employee(client, company_headers).json()["data"]
    url = f"/api/departments/{department['id']}/managers"
    assert client.post(url, json={"employee_id": person["id"]}, headers=company_headers).status_code == 200
    # El reintento simultáneo no vio la fila (aún sin confirmar) e intenta insertarla otra vez.
    monkeypatch.setattr(DepartmentRepository, "is_manager", lambda *_: False)
    again = client.post(url, json={"employee_id": person["id"]}, headers=company_headers)
    assert again.status_code == 200 and [m["employee_id"] for m in again.json()["data"]["managers"]] == [person["id"]]


# ---------------------------------------------------------------- canal en vivo


def test_live_validation_survives_a_database_blip(client, company_headers, monkeypatch):
    from app.routers import realtime

    token = company_headers["Authorization"].removeprefix("Bearer ")

    def down(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("BD reiniciándose"))

    with client.websocket_connect("/api/ws/validation") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert ws.receive_json()["code"] == "WS_AUTHENTICATED"
        monkeypatch.setattr(realtime, "_validate", down)
        ws.send_text(json.dumps({"type": "validate", "field": "email", "value": "x@y.com", "id": "a1"}))
        failed = ws.receive_json()
        assert failed["code"] == "DATABASE_UNAVAILABLE" and failed["statusCode"] == 503
        ws.send_text(json.dumps({"type": "ping"}))  # el canal sigue abierto
        assert ws.receive_json()["code"] == "PONG"


# ---------------------------------------------------------------- consumo y cobranza


def test_metering_never_breaks_a_request(client, company_headers, monkeypatch, caplog):
    """Si el medidor de consumo fallara, la respuesta sale igual y la falla queda registrada."""
    from app.services.usage_meter import usage_meter

    def broken(**_kwargs):
        raise RuntimeError("medidor roto")

    monkeypatch.setattr(usage_meter, "record", broken)
    response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200
    assert "No se pudo medir el consumo de GET /api/users/me" in caplog.text


def test_measuring_performance_never_breaks_a_request(client, company_headers, monkeypatch, caplog):
    """Si los observadores de rendimiento fallaran, la respuesta sale igual y la falla queda registrada."""
    from app.core.perf_meter import perf_meter

    def broken(*_args, **_kwargs):
        raise RuntimeError("medidor de rendimiento roto")

    monkeypatch.setattr(perf_meter, "record", broken)
    response = client.get("/api/users/me", headers=company_headers)
    assert response.status_code == 200
    assert "No se pudo medir el rendimiento de GET /api/users/me" in caplog.text


def test_performance_survives_a_database_outage(monkeypatch):
    """BD caída al guardar el rendimiento y las peticiones lentas: todo se conserva y se guarda cuando vuelve."""
    from app.core.perf_meter import PerfMeter, SlowRequestLog
    from app.models import PerfMinute, SlowRequestAlert
    from app.services import perf_store

    meter, slow = PerfMeter(100), SlowRequestLog(10)
    meter.record("HTTP", "GET /api/x", 1500)
    slow.record("GET /api/x", 1500, trace_id="t", status=200, threshold_ms=1000, sample=None)

    def down(*_args):
        raise OperationalError("INSERT", {}, Exception("BD caída"))

    real = perf_store.platform_session
    monkeypatch.setattr(perf_store, "platform_session", down)
    assert perf_store.flush(meter, slow) == 0 and meter.pending() == 1 and slow.pending() == 1
    monkeypatch.setattr(perf_store, "platform_session", real)
    assert perf_store.flush(meter, slow) == 2
    with SessionLocal() as db:
        assert db.query(PerfMinute).one().count == 1 and db.query(SlowRequestAlert).one().count == 1


def test_a_failing_performance_rollup_does_not_stop_maintenance(monkeypatch, caplog):
    """El resumen del rendimiento falla solo (BD con bloqueo): el resto del mantenimiento sigue."""
    from app.services import perf_rollup

    def locked(*_args):
        raise OperationalError("INSERT", {}, Exception("lock timeout"))

    monkeypatch.setattr(perf_rollup, "run", locked)
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    assert removed[maintenance_service.PERF_ROLLUPS] == 0 and "fotos de almacenamiento" in removed
    assert "Falló el resumen del rendimiento" in caplog.text


def test_usage_survives_a_database_outage(monkeypatch):
    """BD caída al guardar el consumo: los contadores se conservan y se guardan cuando vuelve."""
    from datetime import date

    from app.models import UsageDaily
    from app.services import usage_meter as meter_module

    meter = meter_module.UsageMeter(100)
    meter.record(
        company_id=3,
        user_id=4,
        route="GET /api/x",
        bytes_in=1,
        bytes_out=2,
        duration_ms=3,
        status=200,
        day=date(2026, 11, 1),
    )
    real = meter_module.UsageRepository.add_counts

    def down(*_args):
        raise OperationalError("INSERT", {}, Exception("BD caída"))

    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", down)
    assert meter.flush() == 0 and meter.pending() == 3
    monkeypatch.setattr(meter_module.UsageRepository, "add_counts", real)
    assert meter.flush() == 3
    with SessionLocal() as db:
        assert db.query(UsageDaily).one().requests == 1


def test_a_billing_task_failing_does_not_stop_maintenance(monkeypatch):
    """Una tarea de cobranza que falla (BD con bloqueo) no detiene a las demás ni a la depuración."""

    def locked(*_args):
        raise OperationalError("UPDATE", {}, Exception("lock timeout"))

    monkeypatch.setattr(billing_jobs, "TASKS", (("bloqueada", locked), *billing_jobs.TASKS))
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    assert removed["bloqueada"] == 0 and "fotos de almacenamiento" in removed


# ---------------------------------------------------------------- bucket de imágenes


def test_a_bucket_outage_never_stops_maintenance(bucket):
    """El bucket no responde al borrar: la vuelta del mantenimiento termina (depuración y cobranza
    incluidas), la falla queda anotada para el ADMIN y el objeto sigue en la cola para la siguiente."""
    from datetime import UTC, datetime

    from app.models import StorageDeletion, StorageStatus

    with SessionLocal() as db:
        db.add(StorageDeletion(object_name="test/sobrante.enc", requested_at=datetime.now(UTC)))
        db.commit()
    bucket.down = {"delete"}
    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
        assert removed[storage_jobs.DELETED] == 0 and "fotos de almacenamiento" in removed
        status = db.get(StorageStatus, storage_jobs.DELETE_TASK)
        assert status is not None and "StorageUnavailable" in (status.last_error or "")
        assert db.get(StorageDeletion, "test/sobrante.enc") is not None
    bucket.down = set()
    with SessionLocal() as db:
        assert maintenance_service.purge_expired(db)[storage_jobs.DELETED] == 1


def test_deleting_a_person_with_the_bucket_down_still_erases_their_biometrics(client, company_headers, bucket):
    """Borrado lógico (regla 20) con el bucket caído: eliminar a la persona responde igual (nada de red en la
    petición), sus datos biométricos salen de la base en ese momento y sus fotos quedan en la cola de borrado: el
    mantenimiento las borra en cuanto el bucket responde (nunca se quedan para siempre)."""
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from app.models import FaceEnrollment, StorageDeletion

    data = approved(client, company_headers, "ana", number="EMP-001")
    with SessionLocal() as db:
        photo = db.scalar(select(FaceEnrollment.photo_object))
    assert photo in bucket.objects
    bucket.down = {"put", "get", "delete"}
    assert client.delete(f"/api/employees/{data['id']}", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FaceEmbedding)) == 0
        assert db.scalar(select(func.count()).select_from(FaceEnrollment)) == 0
        assert db.get(StorageDeletion, photo) is not None
        assert storage_jobs.run(db, datetime.now(UTC))[storage_jobs.DELETED] == 0  # sigue caído: espera en la cola
    assert photo in bucket.objects
    bucket.down = set()
    with SessionLocal() as db:
        assert storage_jobs.run(db, datetime.now(UTC))[storage_jobs.DELETED] >= 1
        assert db.get(StorageDeletion, photo) is None
    assert photo not in bucket.objects


def test_company_documents_with_the_bucket_down(client, company_headers, bucket):
    """Documentos de la empresa (migración 0075) con el bucket caído: subir responde 503 STORAGE_UNAVAILABLE
    reintentable sin fila ni objeto (nunca a la base ni a disco), descargar también responde 503, y eliminar o
    restaurar siguen funcionando (no tocan la red: el archivo se queda hasta que la depuración lo saca)."""
    from sqlalchemy import func, select

    from app.core.soft_delete import with_deleted
    from app.models import CompanyDocument
    from tests.test_company_documents import OWN, uploaded

    document = uploaded(client, company_headers)
    bucket.down = {"put", "get", "delete", "stat"}
    down = client.post(
        OWN,
        files={"file": ("acta.pdf", b"%PDF-1.7\n", "application/pdf")},
        data={"type": "INCORPORATION"},
        headers=company_headers,
    )
    assert down.status_code == 503 and down.json()["code"] == "STORAGE_UNAVAILABLE" and down.headers["Retry-After"]
    read = client.get(f"{OWN}/{document['id']}/file", headers=company_headers)
    assert read.status_code == 503 and read.json()["code"] == "STORAGE_UNAVAILABLE"
    assert client.delete(f"{OWN}/{document['id']}", headers=company_headers).status_code == 200
    assert client.post(f"{OWN}/{document['id']}/restore", headers=company_headers).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(with_deleted(select(func.count()).select_from(CompanyDocument))) == 1
    assert len(bucket.objects) == 1


def test_a_bucket_outage_fails_cleanly_and_never_breaks_a_review(client, company_headers, bucket):
    """Sin bucket no se guarda una imagen en la BD (nunca): registrar el rostro responde 503
    STORAGE_UNAVAILABLE reintentable con el sobre de siempre y sin nada a medias. Revisar un registro con
    el bucket caído muestra el registro sin la foto (no un 500)."""
    from app.models import FaceEnrollment

    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    bucket.down = {"put"}
    failed = submit_enrollment(client, headers)
    assert failed.status_code == 503 and failed.json()["code"] == "STORAGE_UNAVAILABLE"
    assert failed.headers["Retry-After"] and failed.json()["errors"][0]["code"] == "STORAGE_UNAVAILABLE"
    with SessionLocal() as db:
        assert db.query(FaceEnrollment).count() == 0
    bucket.down = {"get"}
    enrollment_id = submit_enrollment(client, headers).json()["data"]["enrollment_id"]
    detail = client.get(f"/api/enrollments/{enrollment_id}", headers=company_headers)
    assert detail.status_code == 200 and detail.json()["data"]["photo"] is None


def test_a_bucket_outage_never_breaks_the_profile_nor_leaves_half_a_photo(client, company_headers, bucket):
    """Foto de perfil con el bucket caído: subirla responde 503 STORAGE_UNAVAILABLE reintentable y nada cambia (ni
    filas ni objetos ni la versión de la cuenta); leerla responde 503 (la app muestra las iniciales) y el perfil, el
    menú y los listados siguen funcionando con la ruta de la foto."""
    from tests.avatar_support import fetch, upload

    url = upload(client, company_headers).json()["data"]["avatar"]
    objects = set(bucket.objects)
    bucket.down = {"put", "get"}
    failed = upload(client, company_headers)
    assert (
        failed.status_code == 503 and failed.json()["code"] == "STORAGE_UNAVAILABLE" and failed.headers["Retry-After"]
    )
    assert set(bucket.objects) == objects
    read = fetch(client, company_headers, url)
    assert read.status_code == 503 and read.json()["code"] == "STORAGE_UNAVAILABLE"
    profile = client.get("/api/users/me", headers=company_headers)
    assert profile.status_code == 200 and profile.json()["data"]["avatar"] == url
    assert client.get("/api/employees", headers=company_headers).status_code == 200


# ---------------------------------------------------------------- protocolo de captura (antifraude 2a)


def test_the_capture_protocol_degrades_instead_of_failing(client, company_headers, caplog):
    """Sin canal en vivo la app usa el destello de siempre (una señal medida, no un rechazo); un comprobante sellado
    con una llave rotada sigue abriendo; y una falla del motor al medir la ráfaga deja la ráfaga sin medir (registrada
    para el ADMIN), nunca tumba el intento."""
    from tests.conftest import burst_files
    from tests.test_capture_protocol import verify_with

    headers = approved_employee(client, company_headers)
    unpaced, _ = verify_with(client, headers, paced=False)
    assert unpaced.json()["data"]["verified"] is True
    with caplog.at_level("ERROR"):
        crashed, _ = verify_with(client, headers, burst=burst_files(kind="burst-crash"))
    assert crashed.status_code == 200 and crashed.json()["data"]["verified"] is True
    assert "El motor falló al analizar la ráfaga" in caplog.text


def test_a_parallel_burst_that_breaks_or_hangs_never_breaks_the_attempt(client, company_headers, monkeypatch, caplog):
    """La ráfaga en otro worker (decisión del dueño, 2026-10-06): si su hilo falla de forma inesperada o no termina a
    tiempo (`FACE_BURST_WAIT_SECONDS`), el intento sigue sin medirla (registrado) y el worker de repuesto regresa."""
    import app.facial_recognition as face_module
    from app.facial_recognition.worker_pool import WorkerPool
    from app.services import capture_protocol
    from tests.conftest import FakePipeline
    from tests.test_capture_protocol import verify_with

    holder = face_module._PipelineHolder()
    holder._pool = WorkerPool(lambda _: FakePipeline(), size=2, max_waiting=2, wait_timeout=1)
    monkeypatch.setattr(face_module, "_holder", holder)
    headers = approved_employee(client, company_headers)

    def broken(*_: object) -> None:
        raise RuntimeError("hilo roto")

    monkeypatch.setattr(capture_protocol, "measure_burst", broken)
    with caplog.at_level("ERROR"):
        answer, _ = verify_with(client, headers)
    assert answer.status_code == 200 and answer.json()["data"]["verified"] is True
    assert "La ráfaga medida en paralelo no terminó" in caplog.text
    holder.spare_threads(2).shutdown(wait=True)
    assert holder._pool.stats().busy == 0


def test_a_burst_engine_failure_leaves_no_consensus_and_the_attempt_continues(client, company_headers):
    """El consenso de identidad sale de la ráfaga (decisión del dueño, 2026-10-06): si el motor falla al medirla, no se
    juzga (solo endurece: nunca rechaza por no poder medir) y el intento sigue con la decisión de siempre."""
    from tests.conftest import burst_files
    from tests.test_capture_protocol import metric, verify_with

    headers = approved_employee(client, company_headers)
    answer, _ = verify_with(client, headers, burst=burst_files("pedro", kind="burst-crash"))
    assert answer.status_code == 200 and answer.json()["data"]["verified"] is True
    assert metric().burst_consensus is None


def test_a_spare_worker_that_fails_during_an_enrollment_never_breaks_it(client, company_headers, monkeypatch, caplog):
    """El registro de 36 fotos reparte su análisis con los workers de repuesto libres (decisión del dueño, 2026-10-06):
    si uno falla, el de la petición analiza su parte; el registro sale igual y la falla queda registrada."""
    import app.facial_recognition as face_module
    from app.dependencies import get_pipeline
    from app.facial_recognition.worker_pool import WorkerPool
    from app.main import app
    from tests.conftest import FakePipeline
    from tests.test_enrollment_selection import Busy, enroll, take

    class Broken(FakePipeline):
        def analyze_frontal(self, image_bytes, **kwargs):
            raise RuntimeError("worker dañado")

    holder = face_module._PipelineHolder()
    holder._pool = WorkerPool(lambda _: Broken(), size=2, max_waiting=2, wait_timeout=1)
    monkeypatch.setattr(face_module, "_holder", holder)
    app.dependency_overrides[get_pipeline] = Busy  # el worker de la petición, más lento: los repuestos sí toman fotos
    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    with caplog.at_level("ERROR"):
        response = enroll(client, headers, take())
    assert response.status_code == 201, response.text
    assert "Un worker de repuesto no terminó" in caplog.text
    holder.spare_threads(2).shutdown(wait=True)
    assert holder._pool.stats().busy == 0


# ---------------------------------------------------------------- presencia (antifraude 2b)


def test_a_kiosk_that_goes_offline_never_leaves_anyone_without_checking_in(client, company_headers, monkeypatch):
    """La tableta del sitio se apagó o perdió la red: sin código, el registro sigue (en «Solo medir» queda la señal);
    obligatorio, la respuesta es un 403 claro que dice qué hacer, nunca un 500 ni la pantalla colgada."""
    from datetime import timedelta

    from app.core.clock import business_today
    from tests.test_attendance import recorded, server_clock
    from tests.test_policy import set_policy
    from tests.test_risk_engine import last_reasons
    from tests.test_shifts import assign, create_shift, create_site, employee_with_face
    from tests.test_site_codes import enable_code, punch

    employee_id, headers = employee_with_face(client, company_headers)
    site = enable_code(client, company_headers, create_site(client, company_headers))
    shift = create_shift(client, company_headers, sites=[site["id"]])
    assert assign(client, company_headers, employee_id, shift["id"], business_today()).status_code == 201
    set_to = server_clock(monkeypatch)
    set_to(business_today() + timedelta(days=7), "07:50")
    recorded(punch(client, headers, "check-in"))
    assert "SITE_CODE_MISSING" in last_reasons()
    set_policy(client, company_headers, site_codes="ENFORCE")
    blocked = punch(client, headers, "check-out")
    assert blocked.status_code == 403 and blocked.json()["code"] == "SITE_CODE_REQUIRED"
    assert "escanea el código" in blocked.json()["message"]


def test_clock_skew_between_replicas_is_tolerated():
    """El código del siguiente periodo (una réplica adelantada) y del anterior (el tiempo de escribirlo) valen; dos
    periodos atrás, ya no."""
    from app.models import AttendanceAction, SignalMode, WorkSite
    from app.services import site_codes

    site = WorkSite(id=3, presence_code=True, presence_secret=site_codes.new_secret())
    secret = site_codes.secret_of(site)
    assert secret is not None
    now = 1_800_000_000.0
    window = site_codes.window_at(now)
    for offset, valid in ((1, True), (0, True), (-1, True), (-2, False)):
        code = site_codes.code_for(secret, window + offset)
        checked = site_codes.check(
            site, AttendanceAction.CHECK_IN, code, previous=None, mode=SignalMode.OBSERVE, now=now
        )
        assert (checked.window == window + offset) is valid, offset
        assert bool(checked.hits) is not valid, offset


def test_a_missing_nonce_is_answered_with_a_new_one_without_touching_the_database(client, monkeypatch):
    """La tableta de un kiosco que se reinició (sin reto en memoria) recibe uno nuevo aunque la base no responda: el
    reto es un HMAC sin estado. Y un validador sin reto sigue identificando (en «Solo medir» es una señal)."""
    from app.repositories import kiosk_repository

    def down(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(kiosk_repository.KioskLookup, "by_id", down)
    response = client.post("/api/kiosk/code", json={"kiosk_id": 42})
    assert response.status_code == 403 and response.json()["code"] == "KIOSK_PROOF_REQUIRED"
    assert response.json()["errors"][0]["details"]["nonce"]


def test_a_validator_without_its_nonce_keeps_identifying(client, company_headers):
    from tests.test_risk_engine import last_reasons

    approved(client, company_headers, "juan", number="EMP-001")
    headers = validator_headers(client, company_headers, mode="FACE")
    found = identify_face(client, headers, "juan").json()["data"]  # sin firma ni reto: la app vieja o sin llave
    assert found["verified"] is True and found["device_nonce"]
    assert last_reasons()["VALIDATOR_UNSIGNED"]["mode"] == "OBSERVE"


# ---------------------------------------------------------------- respaldos y recuperación a un punto en el tiempo


def test_a_bucket_outage_never_loses_a_backup_and_the_copy_completes_when_it_returns(bucket, monkeypatch, tmp_path):
    """El bucket se cae durante la copia cifrada del respaldo: el respaldo local queda completo y verificado, anotado
    como pendiente, y la falla llega a "Errores del sistema" (el servicio backup conecta su log al registro). Cuando el
    bucket vuelve, el servicio completa la copia solo y sin repetir lo que ya había subido."""
    from subprocess import CompletedProcess

    from app.models import ErrorReport
    from app.services import db_backup
    from app.services.error_reporter import error_reporter, install_log_handler

    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "BACKUP_UPLOAD", True)
    monkeypatch.setattr(settings, "BACKUP_CHUNK_MB", 1)
    monkeypatch.setattr(settings, "DATABASE_DIRECT_URL", "postgresql+psycopg://dueno:clave@db:5432/timeclock")

    def run(command, **_):  # pg_dump y pg_restore simulados (los reales: scripts/db_restore_check.sh)
        if command[0] == "pg_dump":
            with open(command[command.index("--file") + 1], "wb") as handle:
                handle.write(b"PGDMP" + b"x" * (2 * 1024 * 1024))
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(db_backup.subprocess, "run", run)
    install_log_handler()
    bucket.down = {"put"}
    db_backup.run_forever(24, rounds=1, sleep=lambda _: None, clock=lambda: 0.0)
    (backup,) = db_backup.local_backups()
    assert backup.upload == "pending" and backup.path.exists() and backup.objects == []
    error_reporter.flush()
    with SessionLocal() as db:
        report = db.query(ErrorReport).filter_by(code="app.services.db_backup").one()
        assert "No se pudo subir la copia cifrada" in report.message and report.severity == "CRITICAL"
    bucket.down = set()
    db_backup.run_forever(24, rounds=1, sleep=lambda _: None, clock=lambda: 0.0)  # el servicio (re)arranca
    (backup,) = db_backup.local_backups()
    assert (
        backup.upload == "done" and len(backup.objects) == 4 and all(name in bucket.objects for name in backup.objects)
    )


def test_a_wal_backlog_reaches_the_admin_and_the_monitor_never_breaks(monkeypatch, tmp_path):
    """El bucket no responde y el WAL se acumula en el servidor de la base: el monitor del servicio backup lo avisa en
    "Errores del sistema" (lag con la última falla y WAL acumulado con el % del guardián del disco) y, cuando el
    guardián descarta WAL para proteger el disco, también eso. Si la base deja de responder, el monitor lo dice y
    sigue (nunca tumba al servicio backup)."""
    import json
    from datetime import UTC, datetime, timedelta

    from app.models import ErrorReport
    from app.services.error_reporter import error_reporter, install_log_handler
    from app.services.pitr_monitor import PitrMonitor
    from tests.test_pitr import _archive, _Connection, _Engine

    monkeypatch.setattr(settings, "PITR_STATUS_DIR", str(tmp_path))
    install_log_handler()
    now = datetime.now(UTC)
    stuck = _archive(
        checked_at=now,
        ready_segments=200,
        oldest_ready_at=now - timedelta(minutes=30),
        last_archived_at=now - timedelta(minutes=31),
        last_failed_wal="0000000100000000000000C8",
        last_failed_at=now,
    )
    row = {name: getattr(stuck, name) for name in type(stuck).__dataclass_fields__}
    epoch = int(now.timestamp())
    (tmp_path / "scheduler.json").write_text(json.dumps({"updated_at": epoch, "state": "error", "last_error": ""}))
    (tmp_path / "wal-dropped.json").write_text(json.dumps({"first": epoch, "last": epoch, "count": 64}))
    monitor = PitrMonitor(engine=_Engine(_Connection(row)))
    assert {alert.key for alert in monitor.check()} == {"archive_lag", "wal_backlog", "wal_dropped"}
    error_reporter.flush()
    with SessionLocal() as db:
        found = {report.code: report.message for report in db.query(ErrorReport)}
    assert {"app.pitr.archive_lag", "app.pitr.wal_backlog", "app.pitr.wal_dropped"} <= set(found)
    assert "39 % del guardián" in found["app.pitr.wal_backlog"] and "Última falla" in found["app.pitr.archive_lag"]
    monitor.engine = _Engine(OperationalError("SELECT", {}, Exception("server closed the connection")))
    assert [alert.key for alert in monitor.check()] == ["archive_unreadable", "wal_dropped"]
