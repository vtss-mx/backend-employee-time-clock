"""Operación de la base: roles de mínimo privilegio (`app/core/db_roles.py`), respaldos (`app/services/db_backup.py`),
particiones del mantenimiento (`maintenance_service`, `partition_repository`), su línea de comandos y las revisiones
del arranque (rol que se salta la seguridad por fila).

PostgreSQL de verdad: `tests/test_tenant_isolation.py` (roles y política con el usuario de la API) y la prueba de
respaldo y restauración `scripts/db_restore_check.sh`. Aquí, cada regla con conexiones y procesos simulados.
"""

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app import cli
from app import main as app_main
from app.core import database
from app.core.config import Settings, settings
from app.core.crypto import decrypt_bytes, encrypt_bytes
from app.core.db_roles import PROVISION_LOCK_KEY, RolePlan, provision, role_name, scram_verifier
from app.core.object_storage import StorageUnavailable, use_storage
from app.core.partitions import PARTITIONED
from app.core.row_security import PLATFORM, SCOPE_KEY
from app.repositories import partition_repository
from app.services import db_backup, maintenance_service
from tests.storage_support import FakeStorage, swap_object

# ---------------------------------------------------------------- roles


def test_scram_verifier_is_what_postgresql_stores():
    salt = b"0123456789abcdef"
    verifier = scram_verifier("secreto", salt=salt, iterations=4096)
    method, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    iterations, salt_b64 = params.split(":")
    stored, server = (base64.b64decode(k) for k in keys.split(":"))
    salted = hashlib.pbkdf2_hmac("sha256", b"secreto", salt, 4096)
    assert (method, int(iterations), base64.b64decode(salt_b64)) == ("SCRAM-SHA-256", 4096, salt)
    assert stored == hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    assert server == hmac.new(salted, b"Server Key", "sha256").digest()
    assert scram_verifier("secreto") != scram_verifier("secreto")  # sal nueva cada vez


def test_role_names_are_never_quoted_sql():
    assert role_name("timeclock_app") == "timeclock_app"
    for bad in ("Timeclock", "app; DROP TABLE x", "a-b", ""):
        with pytest.raises(ValueError, match="Nombre inválido"):
            role_name(bad)


def test_the_plan_needs_the_api_password(monkeypatch):
    monkeypatch.setattr(settings, "DB_APP_PASSWORD", "")
    with pytest.raises(ValueError, match="DB_APP_PASSWORD"):
        RolePlan.from_settings()
    monkeypatch.setattr(settings, "DB_APP_PASSWORD", "x")
    monkeypatch.setattr(settings, "DB_READONLY_PASSWORD", "")
    assert RolePlan.from_settings().readonly is None
    monkeypatch.setattr(settings, "DB_READONLY_PASSWORD", "y")
    plan = RolePlan.from_settings()
    assert (plan.app, plan.platform, plan.readonly) == (
        settings.DB_APP_USER,
        settings.DB_PLATFORM_ROLE,
        settings.DB_READONLY_USER,
    )


class _Result:
    def __init__(self, value=None, rows=()):
        self.value, self.rows = value, rows

    def scalar(self):
        return self.value

    def scalar_one(self):
        return self.value

    def __iter__(self):
        return iter(self.rows)


class _Owner:
    """Conexión del dueño simulada: responde las consultas del aprovisionamiento y anota cada sentencia."""

    def __init__(
        self, *, exists=False, version=160004, preload="pg_stat_statements", extension_fails=False, functions=True
    ):
        self.exists, self.version, self.preload, self.extension_fails = exists, version, preload, extension_fails
        self.functions = functions
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        query = str(statement)
        if "pg_advisory_xact_lock" in query:
            self.sql.append(f"lock {params['key']}")
            return _Result()
        if "pg_roles" in query:
            return _Result(self.exists)
        if "server_version_num" in query:
            return _Result(str(self.version))
        if "current_database" in query:
            return _Result("timeclock")
        if "relkind" in query:
            return _Result(rows=[("employees",)] if params["schema"] == "workforce" else [("roles",)])
        if "to_regprocedure" in query:
            return _Result(self.functions)
        return _Result(self.preload)

    def exec_driver_sql(self, statement):
        if "pg_stat_statements" in statement and self.extension_fails:
            raise DBAPIError(statement, {}, PermissionError("permiso denegado"))
        self.sql.append(statement)

    @contextmanager
    def begin_nested(self):
        yield


def _plan(readonly: str | None = "timeclock_readonly") -> RolePlan:
    return RolePlan(
        app="timeclock_app",
        app_password="secreto-app",
        platform="timeclock_platform",
        readonly=readonly,
        readonly_password="secreto-lectura",
        statement_timeout_ms=15000,
        lock_timeout_ms=10000,
        idle_in_transaction_ms=120000,
    )


def test_provisioning_creates_least_privilege_roles_and_grants():
    conn = _Owner()
    provision(conn, _plan())
    assert conn.sql[0] == f"lock {PROVISION_LOCK_KEY}"  # primero el candado: réplicas que aprovisionan a la vez
    sql = "\n".join(conn.sql)
    assert "CREATE ROLE timeclock_platform" in sql and "CREATE ROLE timeclock_app" in sql
    assert (
        "ALTER ROLE timeclock_platform WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS" in sql
    )
    assert "ALTER ROLE timeclock_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS" in sql
    assert "secreto-app" not in sql and "PASSWORD 'SCRAM-SHA-256$4096:" in sql  # nunca la contraseña en claro
    assert "GRANT timeclock_platform TO timeclock_app WITH INHERIT FALSE, SET TRUE" in sql
    assert "ALTER ROLE timeclock_app SET statement_timeout = 15000" in sql
    assert "ALTER ROLE timeclock_app SET idle_in_transaction_session_timeout = 120000" in sql
    assert "ALTER ROLE timeclock_readonly SET default_transaction_read_only = on" in sql
    assert "REVOKE ALL ON DATABASE timeclock FROM PUBLIC" in sql
    writers = "timeclock_app, timeclock_platform"
    assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON workforce.employees TO {writers}" in sql
    assert f"GRANT SELECT ON catalog.roles TO {writers}" in sql  # los catálogos solo se leen
    assert "GRANT SELECT ON workforce.employees TO timeclock_readonly" in sql
    assert "GRANT EXECUTE ON FUNCTION ops.ensure_partitions(text, date, integer, date) TO timeclock_platform" in sql
    assert "GRANT EXECUTE ON FUNCTION ops.top_statements(text, integer, integer) TO timeclock_platform" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public" in sql
    assert "DEFAULT PRIVILEGES" not in sql  # las particiones nunca reciben permisos


def test_provisioning_is_idempotent_and_degrades_without_extensions(caplog):
    # Sin la función de particiones (base aún sin migrar): no hay a quién darle permiso de ejecutarla.
    conn = _Owner(exists=True, version=150008, preload="", extension_fails=True, functions=False)
    provision(conn, _plan(readonly=None))
    sql = "\n".join(conn.sql)
    assert "CREATE ROLE" not in sql and "GRANT timeclock_platform TO timeclock_app\n" in sql + "\n"
    assert "ensure_partitions" not in sql
    assert "timeclock_readonly" not in sql and "pg_stat_statements" not in sql
    assert "no carga pg_stat_statements" in caplog.text
    failing = _Owner(extension_fails=True)
    provision(failing, _plan())
    assert "No se pudo crear pg_stat_statements" in caplog.text


def test_cli_provisions_with_the_owner_connection(monkeypatch, capsys):
    monkeypatch.setattr(settings, "DB_APP_PASSWORD", "")
    assert cli._db_roles(argparse.Namespace()) == 0
    assert "Sin roles que aprovisionar" in capsys.readouterr().out
    monkeypatch.setattr(settings, "DB_APP_PASSWORD", "x")
    done = []

    class _Engine:
        @contextmanager
        def begin(self):
            yield "conexión"

        def dispose(self):
            done.append("dispose")

    monkeypatch.setattr(cli, "provision", lambda conn, plan: done.append((conn, plan.app)))
    assert cli._db_roles(argparse.Namespace(), direct=_Engine()) == 0
    assert done == [("conexión", settings.DB_APP_USER), "dispose"] and "Roles al día" in capsys.readouterr().out
    monkeypatch.setattr(cli, "build_engine", lambda url: _Engine())
    assert cli._db_roles(argparse.Namespace()) == 0


# ---------------------------------------------------------------- respaldos


@pytest.fixture
def backups(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "DATABASE_DIRECT_URL", "postgresql+psycopg://dueño:clave@db:5432/timeclock")
    calls: list[list[str]] = []

    def run(command, *, env, **_):
        calls.append(command)
        if command[0] == "pg_dump":
            assert env["PGPASSWORD"] == "clave" and "clave" not in " ".join(command)  # la contraseña nunca en argv
            target = command[command.index("--file") + 1]
            with open(target, "wb") as handle:
                handle.write(b"PGDMP" + os.urandom(3 * 1024 * 1024))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(db_backup.subprocess, "run", run)
    return SimpleNamespace(folder=tmp_path / "backups", calls=calls)


@pytest.fixture
def uploading(backups, monkeypatch):
    """Copia al bucket encendida (falso, en memoria) con partes de 1 MB: cada respaldo de prueba son 4 partes."""
    storage = FakeStorage()
    use_storage(storage)
    monkeypatch.setattr(settings, "BACKUP_UPLOAD", True)
    monkeypatch.setattr(settings, "BACKUP_CHUNK_MB", 1)
    return storage


class _FallsMidway(FakeStorage):
    """Un bucket que se cae a la mitad de una subida (a partir de la subida número `fail_from`)."""

    def __init__(self, fail_from: int) -> None:
        super().__init__()
        self.fail_from, self.puts = fail_from, 0

    def put(self, name, data, metadata, *, interactive=False):
        self.puts += 1
        if self.puts >= self.fail_from:
            raise StorageUnavailable("el bucket se cayó a la mitad (simulado)")
        return super().put(name, data, metadata, interactive=interactive)


def _manifest(backup: db_backup.Backup) -> dict:
    return json.loads(backup.path.with_name(backup.path.stem + db_backup.MANIFEST_SUFFIX).read_text())


def test_a_backup_is_dumped_verified_and_described(backups):
    backup = db_backup.backup_now(datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
    assert backup.path.name == "timeclock-20261005T030000Z.dump" and backup.objects == []
    assert [c[0] for c in backups.calls] == ["pg_dump", "pg_restore"]
    assert "--format=custom" in backups.calls[0] and backups.calls[1][:2] == ["pg_restore", "--list"]
    assert backup.sha256 == hashlib.sha256(backup.path.read_bytes()).hexdigest()
    manifest = _manifest(backup)
    assert manifest["sha256"] == backup.sha256 and manifest["objects"] == [] and manifest["upload"] == "off"
    assert not list(backups.folder.glob("*.partial"))


def test_the_offsite_copy_is_encrypted_in_parts(uploading):
    backup = db_backup.backup_now(datetime(2026, 10, 5, tzinfo=UTC))
    parts = [name for name in backup.objects if "part-" in name]
    assert len(parts) == 4 and backup.objects[-1].endswith(db_backup.MANIFEST_OBJECT)
    assert all(name.startswith("test/backups/timeclock-") for name in backup.objects)
    assert backup.upload == "done" and _manifest(backup)["upload"] == "done"
    # Nunca legible: cada objeto es un token cifrado (Fernet en base64, sin los bytes del respaldo) y las partes,
    # descifradas en orden, son el respaldo exacto; el manifiesto cifrado dice el SHA-256 de cada parte y del total.
    stored = [uploading.objects[name][0] for name in backup.objects]
    assert all(data.startswith(b"gAAAAA") and not data.startswith(b"PGDMP") for data in stored)
    plain = [decrypt_bytes(data) for data in stored[:-1]]
    assert b"".join(plain) == backup.path.read_bytes()
    remote = json.loads(decrypt_bytes(stored[-1]))
    assert remote["sha256"] == backup.sha256 and remote["parts"] == parts
    assert remote["part_sha256"] == [hashlib.sha256(chunk).hexdigest() for chunk in plain]
    assert [uploading.objects[name][1]["sha256"] for name in parts] == remote["part_sha256"]


def test_a_copy_cut_midway_is_resumed_without_repeating_or_overwriting(backups, monkeypatch, caplog):
    storage = _FallsMidway(fail_from=3)  # suben 2 partes y el bucket se cae
    use_storage(storage)
    monkeypatch.setattr(settings, "BACKUP_UPLOAD", True)
    monkeypatch.setattr(settings, "BACKUP_CHUNK_MB", 1)
    backup = db_backup.backup_now(datetime(2026, 10, 5, tzinfo=UTC))  # el respaldo NO falla por el bucket
    assert backup.upload == "pending" and len(backup.objects) == 2 and backup.path.exists()
    assert (
        _manifest(backup)["objects"] == backup.objects
    )  # lo que alcanzó a subir queda anotado (la depuración lo borra)
    assert "No se pudo subir la copia cifrada" in caplog.text
    first = storage.objects[backup.objects[0]][0]
    storage.fail_from = 10**9  # el bucket vuelve
    assert db_backup.retry_uploads() == 1
    (again,) = db_backup.local_backups()
    assert again.upload == "done" and len(again.objects) == 5
    assert storage.objects[backup.objects[0]][0] == first  # lo que ya estaba se reconoció: ni se repitió ni se pisó
    assert db_backup.retry_uploads() == 0  # nada pendiente


def test_a_foreign_object_with_the_same_name_is_never_overwritten(uploading, caplog):
    name = "test/backups/timeclock-20261005T000000Z/part-00001.enc"
    uploading.objects[name] = (b"de otro respaldo", {"sha256": "otra cosa"})
    backup = db_backup.backup_now(datetime(2026, 10, 5, tzinfo=UTC))
    assert backup.upload == "pending" and uploading.objects[name][0] == b"de otro respaldo"
    assert "ya hay otro objeto llamado" in caplog.text


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError("pg_dump"), "no está instalado pg_dump"),
        (subprocess.TimeoutExpired("pg_dump", 1), "pasó de"),
        (subprocess.CompletedProcess(["pg_dump"], 1, "", "conexión rechazada"), "conexión rechazada"),
    ],
    ids=["sin-pg_dump", "tiempo-limite", "falla"],
)
def test_a_failed_backup_leaves_nothing_half_done(backups, monkeypatch, failure, message):
    def run(command, **_):
        with open(command[command.index("--file") + 1], "wb") as handle:
            handle.write(b"a medias")
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(db_backup.subprocess, "run", run)
    with pytest.raises(db_backup.BackupError, match=message):
        db_backup.backup_now()
    assert list(backups.folder.iterdir()) == []


def test_expired_backups_leave_the_folder_and_the_bucket(uploading, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_RETENTION_DAYS", 7)
    now = datetime.now(UTC)
    old = db_backup.backup_now(now - timedelta(days=10))
    recent = db_backup.backup_now(now - timedelta(days=1))
    assert db_backup.prune(now) == 1
    assert not old.path.exists() and recent.path.exists()
    assert not any(name in uploading.objects for name in old.objects) and all(
        n in uploading.objects for n in recent.objects
    )


def test_unreadable_or_old_manifests_never_stop_the_others(backups, caplog):
    backups.folder.mkdir(parents=True)
    (backups.folder / "roto.manifest.json").write_text("{no es json")
    for stem, objects in (("viejo", ["test/backups/viejo/x"]), ("viejo2", None)):
        legacy = {"path": str(backups.folder / f"{stem}.dump"), "size": 1, "sha256": "x"}
        legacy |= {"created_at": "2026-01-01T00:00:00+00:00"} | ({"objects": objects} if objects else {})
        (backups.folder / f"{stem}.manifest.json").write_text(json.dumps(legacy))
    found = {backup.path.name: backup.upload for backup in db_backup.local_backups()}
    assert found == {"viejo.dump": "done", "viejo2.dump": "off"}  # antes de las copias pendientes: con objetos, arriba
    assert "Manifiesto de respaldo ilegible (se omite): roto.manifest.json" in caplog.text
    assert db_backup.prune(datetime(2026, 10, 5, tzinfo=UTC)) == 2  # los vencidos se borran igual


def test_a_copy_comes_back_from_the_bucket_decrypted_and_verified(uploading, backups, tmp_path):
    older = db_backup.backup_now(datetime(2026, 10, 4, tzinfo=UTC))
    newer = db_backup.backup_now(datetime(2026, 10, 5, tzinfo=UTC))
    original = {b.path.stem: b.path.read_bytes() for b in (older, newer)}
    uploading.objects["test/backups/timeclock-20261006T000000Z/part-00001.enc"] = (b"incompleta", {})
    for path in backups.folder.iterdir():  # el desastre: la carpeta local ya no existe
        path.unlink()
    assert db_backup.remote_backups() == [older.path.stem, newer.path.stem]  # solo las completas (con manifiesto)
    restored = db_backup.fetch(None, tmp_path / "restore")  # sin nombre: la más reciente
    assert restored.read_bytes() == original[newer.path.stem] and restored.name == newer.path.name
    assert backups.calls[-1][:2] == ["pg_restore", "--list"] and backups.calls[-1][2].endswith(".partial")
    assert db_backup.fetch(older.path.stem, tmp_path / "restore").read_bytes() == original[older.path.stem]
    assert not list((tmp_path / "restore").glob("*.partial"))


def _replace(storage: FakeStorage, name: str, payload: bytes) -> None:
    storage.objects[name] = (payload, storage.objects[name][1])


def _manifest_object(storage: FakeStorage, stem: str) -> str:
    return f"test/backups/{stem}/{db_backup.MANIFEST_OBJECT}"


@pytest.mark.parametrize(
    "case",
    ["sin-copias", "sin-esa-copia", "otra-llave", "parte-cambiada", "manifiesto-viejo-y-mal", "indice-roto"],
)
def test_a_copy_that_cannot_be_verified_never_reaches_the_folder(uploading, backups, monkeypatch, tmp_path, case):
    target = tmp_path / "restore"
    if case == "sin-copias":
        with pytest.raises(db_backup.BackupError, match="no hay copias completas"):
            db_backup.fetch(None, target)
        return
    backup = db_backup.backup_now(datetime(2026, 10, 5, tzinfo=UTC))
    parts = [name for name in backup.objects if "part-" in name]
    expected = {
        "sin-esa-copia": "no existe la copia",
        "otra-llave": "no se pudo descifrar",
        "parte-cambiada": "SHA-256 no es el del manifiesto",
        "manifiesto-viejo-y-mal": "no coincide con su manifiesto",
        "indice-roto": "índice dañado",
    }[case]
    name = backup.path.stem
    if case == "sin-esa-copia":
        name = "timeclock-19990101T000000Z"
    elif case == "otra-llave":
        swap_object(uploading, parts[1])
    elif case == "parte-cambiada":
        _replace(uploading, parts[1], encrypt_bytes(b"otro contenido"))
    elif case == "manifiesto-viejo-y-mal":  # sin SHA-256 por parte (formato anterior) y con un tamaño que no es
        legacy = {"file": backup.path.name, "size": 1, "sha256": backup.sha256, "parts": parts}
        _replace(uploading, _manifest_object(uploading, name), encrypt_bytes(json.dumps(legacy).encode()))
    else:
        broken = subprocess.CompletedProcess(["pg_restore"], 1, "", "índice dañado")
        monkeypatch.setattr(db_backup.subprocess, "run", lambda command, **_: broken)
    with pytest.raises(db_backup.BackupError, match=expected):
        db_backup.fetch(name, target)
    assert not target.exists() or list(target.iterdir()) == []


def test_the_backup_service_never_dies_and_waits_before_retrying(backups, monkeypatch, caplog):
    results = iter([RuntimeError("la base no responde"), None])
    attempts = []

    def attempt():
        attempts.append("respaldo")
        if failure := next(results):
            raise failure

    slept = []
    moments = iter([0.0, 10.0, 2000.0])
    monkeypatch.setattr(db_backup, "backup_now", attempt)
    monkeypatch.setattr(db_backup, "prune", lambda: 0)
    db_backup.run_forever(0.5, rounds=3, sleep=slept.append, clock=lambda: next(moments))
    # Falló a los 0 s: a los 10 s no se reintenta (BACKUP_RETRY_MINUTES = 15); a los 2000 s sí.
    assert attempts == ["respaldo", "respaldo"] and slept == [1800.0] * 3 and "Falló el respaldo" in caplog.text


def test_restarting_the_service_does_not_back_up_more_than_its_interval(backups, monkeypatch):
    db_backup.backup_now(datetime.now(UTC) - timedelta(hours=1))
    calls = []
    monkeypatch.setattr(db_backup, "backup_now", lambda: calls.append("respaldo"))
    db_backup.run_forever(24, rounds=1, sleep=lambda _: None)
    assert calls == []  # el último es de hace 1 h: toca dentro de 23 h
    db_backup.run_forever(0, rounds=1, sleep=lambda _: None)
    assert calls == []  # 0 = nunca solo
    db_backup.run_forever(0.5, rounds=1, sleep=lambda _: None)
    assert calls == ["respaldo"]


def test_pending_copies_are_retried_without_hammering_the_bucket(backups, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_UPLOAD", True)
    retries, checks, slept = [], [], []
    monkeypatch.setattr(db_backup, "retry_uploads", lambda: retries.append("reintento"))
    monkeypatch.setattr(db_backup, "_due", lambda *_: False)
    moments = iter([0.0, 60.0, 1000.0])
    db_backup.run_forever(
        24, rounds=3, sleep=slept.append, clock=lambda: next(moments), monitor=lambda: checks.append(1)
    )
    assert retries == ["reintento", "reintento"]  # a los 0 y a los 1000 s (cada 15 min), no a los 60 s
    assert checks == [1, 1, 1] and slept == [float(settings.PITR_CHECK_SECONDS)] * 3  # el monitor, en cada vuelta
    monkeypatch.setattr(settings, "BACKUP_UPLOAD", False)
    assert db_backup._tick(0, monitored=False) == settings.BACKUP_RETRY_MINUTES * 60  # sin nada más, igual da vueltas
    assert db_backup._tick(24, monitored=False) == 24 * 3600


def test_cli_backup(monkeypatch, capsys, tmp_path):
    finished = db_backup.Backup(tmp_path / "x.dump", 10, "abc", datetime.now(UTC), upload="pending")
    monkeypatch.setattr(db_backup, "backup_now", lambda: finished)
    monkeypatch.setattr(db_backup, "prune", lambda: 2)
    assert cli._db_backup(argparse.Namespace(loop=False)) == 0
    assert "sha256 abc" in (out := capsys.readouterr().out) and "copia al bucket: pending" in out

    def broken():
        raise db_backup.BackupError("pg_dump: sin conexión")

    monkeypatch.setattr(db_backup, "backup_now", broken)
    assert cli._db_backup(argparse.Namespace(loop=False)) == 1
    monkeypatch.setattr(settings, "BACKUP_INTERVAL_HOURS", 0)
    assert cli._db_backup(argparse.Namespace(loop=True)) == 0
    assert "no tiene nada que hacer" in capsys.readouterr().out


class _Flusher:
    def __init__(self, interval):
        self.interval, self.events = interval, []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


@pytest.mark.parametrize("pitr", [False, True], ids=["sin-pitr", "con-pitr"])
def test_the_backup_service_reports_its_failures_and_stops_cleanly(monkeypatch, pitr):
    flushers, handlers, loops = [], {}, []
    monkeypatch.setattr(cli, "ErrorReportFlusher", lambda interval: flushers.append(_Flusher(interval)) or flushers[-1])
    monkeypatch.setattr(cli, "install_log_handler", lambda: handlers.setdefault("log", True))
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))
    monkeypatch.setattr(settings, "ERROR_REPORT_FLUSH_SECONDS", 2.0)
    monkeypatch.setattr(settings, "BACKUP_INTERVAL_HOURS", 6)
    monkeypatch.setattr(settings, "PITR_ENABLED", pitr)

    def loop(interval, *, monitor):
        loops.append((interval, monitor))
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # docker stop en plena vuelta

    monkeypatch.setattr(db_backup, "run_forever", loop)
    with pytest.raises(SystemExit):
        cli._db_backup(argparse.Namespace(loop=True))
    ((interval, monitor),) = loops
    assert interval == 6 and handlers["log"] is True and flushers[0].events == ["start", "stop"]  # guardó lo pendiente
    assert (monitor is not None) is pitr
    monkeypatch.setattr(settings, "ERROR_REPORT_FLUSH_SECONDS", 0)
    monkeypatch.setattr(db_backup, "run_forever", lambda interval, *, monitor: None)
    assert cli._db_backup(argparse.Namespace(loop=True)) == 0  # sin hilo de errores (pruebas)


def test_cli_fetch_backup(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(db_backup, "remote_backups", list)
    assert cli._db_fetch_backup(argparse.Namespace(list=True, name=None, dir=str(tmp_path))) == 0
    assert "No hay copias completas" in capsys.readouterr().out
    monkeypatch.setattr(db_backup, "remote_backups", lambda: ["timeclock-a", "timeclock-b"])
    assert cli._db_fetch_backup(argparse.Namespace(list=True, name=None, dir=str(tmp_path))) == 0
    assert capsys.readouterr().out.split() == ["timeclock-a", "timeclock-b"]
    monkeypatch.setattr(db_backup, "fetch", lambda name, folder: folder / f"{name}.dump")
    assert cli._db_fetch_backup(argparse.Namespace(list=False, name="timeclock-a", dir=str(tmp_path))) == 0
    assert "timeclock-a.dump" in capsys.readouterr().out

    def down(*_):
        raise StorageUnavailable("sin red")

    monkeypatch.setattr(db_backup, "remote_backups", down)
    assert cli._db_fetch_backup(argparse.Namespace(list=True, name=None, dir=str(tmp_path))) == 1
    assert "sin red" in capsys.readouterr().err


# ---------------------------------------------------------------- particiones


class _PostgresDb:
    """Sesión de PostgreSQL simulada para el mantenimiento de particiones."""

    def __init__(self):
        self.commits = self.rollbacks = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_maintenance_creates_months_ahead_and_drops_expired_ones(monkeypatch, caplog):
    seen = {}

    def ensure(db, table, today, ahead, keep_from):
        seen[table] = keep_from
        if table == "ops.usage_users":
            raise SQLAlchemyError("candado ocupado")
        return 1, 2 if keep_from else 0

    monkeypatch.setattr(partition_repository, "ensure", ensure)
    db = _PostgresDb()
    now = datetime(2026, 10, 5, tzinfo=UTC)
    totals = maintenance_service._maintain_partitions(db, now)
    assert seen["attendance.verification_logs"] is None  # la bitácora se conserva
    assert seen["ops.error_occurrences"] == date(2026, 10, 5) - timedelta(days=settings.ERROR_OCCURRENCE_RETENTION_DAYS)
    # Todas crean su mes salvo la que falló; las que tienen retención borran 2 (la que falló, ninguna).
    kept = [spec for name, spec in PARTITIONED.items() if name != "ops.usage_users"]
    dropping = sum(2 for spec in kept if spec.retention_days)
    assert totals == {"particiones creadas": len(kept), "particiones vencidas borradas": dropping}
    assert db.rollbacks == 1 and "ops.usage_users" in caplog.text


def test_the_partition_function_is_one_statement():
    executed = []

    class _Db:
        def execute(self, statement, params):
            executed.append((str(statement), params))
            return SimpleNamespace(one=lambda: SimpleNamespace(created=2, dropped=1))

    assert partition_repository.ensure(_Db(), "ops.usage_routes", date(2026, 10, 5), 3, None) == (2, 1)
    ((statement, params),) = executed
    assert "ops.ensure_partitions" in statement and params["table"] == "ops.usage_routes" and params["ahead"] == 3


# ---------------------------------------------------------------- arranque y configuración


def test_platform_sessions_cross_companies_on_purpose():
    with database.platform_session() as db:
        assert db.info[SCOPE_KEY] == PLATFORM


class _RoleEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, bypassed: bool) -> None:
        self.bypassed = bypassed

    @contextmanager
    def connect(self):
        yield SimpleNamespace(execute=lambda statement: SimpleNamespace(scalar=lambda: self.bypassed))


def test_the_start_warns_if_the_api_bypasses_row_security(monkeypatch, caplog):
    assert database.row_security_bypassed() is False  # SQLite: no aplica
    monkeypatch.setattr(database, "engine", _RoleEngine(True))
    assert database.row_security_bypassed() is True
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(app_main, "row_security_bypassed", database.row_security_bypassed)
    app_main._check_row_security()
    assert "se salta la seguridad por fila" in caplog.text
    caplog.clear()
    monkeypatch.setattr(database, "engine", _RoleEngine(False))
    app_main._check_row_security()
    assert caplog.text == ""

    def broken():
        raise RuntimeError("sin base")

    monkeypatch.setattr(app_main, "row_security_bypassed", broken)
    app_main._check_row_security()
    assert "No se pudo revisar el rol" in caplog.text


def test_the_api_uses_its_least_privilege_user_and_migrations_the_owner():
    # DATABASE_DIRECT_URL vacío explícito: con la suite en PostgreSQL el entorno trae la de las pruebas.
    config = Settings(
        _env_file=None,
        DATABASE_URL="",
        DATABASE_DIRECT_URL="",
        POSTGRES_HOST="pgbouncer",
        POSTGRES_PORT=6432,
        POSTGRES_USER="dueno",
        POSTGRES_PASSWORD="clave-dueno",
        DB_APP_USER="timeclock_app",
        DB_APP_PASSWORD="clave-app",
        DB_PLATFORM_ROLE="timeclock_platform",
        DB_POOLER="pgbouncer",
        POSTGRES_DIRECT_HOST="db",
    )
    assert config.DATABASE_URL == "postgresql+psycopg://timeclock_app:clave-app@pgbouncer:6432/timeclock"
    assert config.DATABASE_DIRECT_URL == "postgresql+psycopg://dueno:clave-dueno@db:5432/timeclock"
    assert config.platform_role == "timeclock_platform"
    alone = Settings(
        _env_file=None,
        DATABASE_URL="",
        DATABASE_DIRECT_URL="",
        POSTGRES_PASSWORD="x",
        DB_APP_PASSWORD="y",
        POSTGRES_HOST="db",
    )
    assert alone.DATABASE_DIRECT_URL == "postgresql+psycopg://timeclock:x@db:5432/timeclock"  # migra el dueño
    owner = Settings(_env_file=None, DATABASE_URL="", POSTGRES_PASSWORD="x", DB_APP_PASSWORD="")
    assert owner.platform_role == "" and owner.DATABASE_URL.startswith("postgresql+psycopg://timeclock:x@")
