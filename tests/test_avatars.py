"""Foto de perfil por la API: cada persona sube, cambia o quita la SUYA; se guarda cifrada en el bucket (nunca en la
BD) con una ruta versionada; solo la ve quien puede (la propia, su empresa, la plataforma sin fotos de empleados);
caché del navegador por versión (ETag, 304); y toda falla responde con su código sin dejar nada a medias."""

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.crypto import decrypt_bytes
from app.core.database import SessionLocal
from app.core.object_storage import DisabledStorage, use_storage
from app.models import StorageDeletion, User, UserAvatar
from app.repositories.avatar_repository import AvatarRepository
from app.routers.users import avatar_cache_control
from app.services import storage_jobs
from app.services.avatar_service import matches
from tests.avatar_support import decoded, fetch, me, noise, photo, upload
from tests.conftest import create_company, create_employee, login
from tests.test_envelope import assert_envelope
from tests.test_validators import PASSWORD, create_validator, validator_headers


def _employee(client, company_headers, email="ana@empresa.com", number="EMP-010") -> tuple[dict, dict]:
    """(datos del empleado, headers de su sesión)."""
    created = create_employee(client, company_headers, number=number, email=email)
    assert created.status_code == 201, created.text
    return created.json()["data"], login(client, email, "Empleado123")


def _rows(user_id: int) -> list[UserAvatar]:
    with SessionLocal() as db:
        return list(db.scalars(select(UserAvatar).where(UserAvatar.user_id == user_id).order_by(UserAvatar.size_px)))


def _queued() -> set[str]:
    with SessionLocal() as db:
        return set(db.scalars(select(StorageDeletion.object_name)))


def _objects(bucket, user_id: int) -> set[str]:
    return {name for name in bucket.objects if f"/people/users/{user_id}/" in name}


# ---------------------------------------------------------------- subir, ver, cambiar y quitar


@pytest.mark.parametrize("role", ["COMPANY", "EMPLOYEE", "VALIDATOR", "ADMIN"])
def test_every_role_uploads_its_own_photo(client, company_headers, admin_headers, bucket, role):
    headers = {
        "COMPANY": lambda: company_headers,
        "EMPLOYEE": lambda: _employee(client, company_headers)[1],
        "VALIDATOR": lambda: validator_headers(client, company_headers),
        "ADMIN": lambda: admin_headers,
    }[role]()
    user = me(client, headers)
    assert user["avatar"] is None
    response = upload(client, headers)
    assert response.status_code == 200 and response.json()["code"] == "AVATAR_UPDATED"
    saved = response.json()["data"]
    assert saved["avatar"] == f"/users/{user['id']}/avatar?v={saved['version']}" == me(client, headers)["avatar"]
    for side in (96, 512):
        image = fetch(client, headers, saved["avatar"], side)
        assert image.status_code == 200 and image.json()["data"]["content_type"] == "image/webp"
        assert decoded(image).size == (side, side)
    # En el bucket: cifrado, solo con ids, sin empresa (es de la persona); en la BD solo la referencia.
    names = _objects(bucket, user["id"])
    assert names == {f"test/people/users/{user['id']}/avatar/{saved['version']}/{side}.webp.enc" for side in (96, 512)}
    for name in names:
        payload, metadata = bucket.objects[name]
        assert payload[:4] != b"RIFF" and decrypt_bytes(payload)[:4] == b"RIFF"
        assert metadata["kind"] == "user-avatar" and "company_id" not in metadata
    rows = _rows(user["id"])
    assert [row.size_px for row in rows] == [96, 512]
    assert all(row.sha256 == hashlib.sha256(bucket.objects[row.object_name][0]).hexdigest() for row in rows)


def test_the_photo_is_cached_by_version_and_revalidated_with_its_etag(client, company_headers, bucket):
    url = upload(client, company_headers).json()["data"]["avatar"]
    first = fetch(client, company_headers, url, 512)
    version = first.json()["data"]["version"]
    assert first.headers["ETag"] == f'"{version}-512"' and "Authorization" in first.headers["Vary"]
    assert first.headers["Cache-Control"] == f"private, max-age={settings.AVATAR_CACHE_SECONDS}, immutable"
    reads = len([call for call in bucket.calls if call[0] == "get"])
    again = fetch(client, {**company_headers, "If-None-Match": first.headers["ETag"]}, url, 512)
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == first.headers["ETag"]
    assert len([call for call in bucket.calls if call[0] == "get"]) == reads  # 304 sin leer el bucket
    # Sin versión (o con una vieja) se entrega la vigente, pero el navegador la vuelve a validar.
    user_id = me(client, company_headers)["id"]
    stale = client.get(f"/api/users/{user_id}/avatar", headers=company_headers)
    assert stale.status_code == 200 and stale.json()["data"]["size_px"] == 96
    assert stale.headers["Cache-Control"] == "private, no-cache"


def test_a_new_photo_replaces_the_old_one_and_the_old_objects_leave_the_bucket(client, company_headers, bucket):
    user_id = me(client, company_headers)["id"]
    first = upload(client, company_headers).json()["data"]
    old = _objects(bucket, user_id)
    second = upload(client, company_headers, photo(600, 900)).json()["data"]
    assert second["version"] != first["version"] and me(client, company_headers)["avatar"] == second["avatar"]
    assert {row.version for row in _rows(user_id)} == {second["version"]}
    assert _queued() == old  # la foto anterior va a la cola de borrado en la misma transacción
    with SessionLocal() as db:
        assert storage_jobs.run(db, datetime.now(UTC))[storage_jobs.DELETED] == 2
    assert _objects(bucket, user_id) == {name.replace(first["version"], second["version"]) for name in old}
    # La URL vieja ya entrega la foto nueva sin fijarla en la caché (la app ya pide la nueva).
    stale = fetch(client, company_headers, first["avatar"])
    assert stale.json()["data"]["version"] == second["version"] and "no-cache" in stale.headers["Cache-Control"]


def test_removing_the_photo_is_idempotent_and_leaves_the_initials(client, company_headers, bucket):
    user_id = me(client, company_headers)["id"]
    url = upload(client, company_headers).json()["data"]["avatar"]
    names = _objects(bucket, user_id)
    removed = client.delete("/api/users/me/avatar", headers=company_headers)
    assert removed.status_code == 200 and removed.json()["code"] == "AVATAR_REMOVED"
    assert removed.json()["data"] == {"avatar": None, "version": None}
    assert me(client, company_headers)["avatar"] is None and _rows(user_id) == [] and _queued() == names
    assert_envelope(fetch(client, company_headers, url), 404, "AVATAR_NOT_FOUND")
    assert client.delete("/api/users/me/avatar", headers=company_headers).status_code == 200  # otra vez: nada


# ---------------------------------------------------------------- quién ve a quién


def test_the_company_sees_its_active_people_and_nobody_else_does(client, company_headers, admin_headers):
    employee, ana = _employee(client, company_headers)
    _, luis = _employee(client, company_headers, email="luis@empresa.com", number="EMP-011")
    validator = validator_headers(client, company_headers)
    urls = {}
    for name, headers in (("ana", ana), ("validator", validator), ("company", company_headers)):
        urls[name] = upload(client, headers).json()["data"]["avatar"]
    # La empresa ve a su empleada (también en el listado y el detalle) y a su validador; el validador, a ambas.
    for viewer in (company_headers, validator):
        assert fetch(client, viewer, urls["ana"]).status_code == 200
    assert fetch(client, company_headers, urls["validator"]).status_code == 200
    listed = client.get("/api/employees", headers=company_headers).json()["data"]["items"]
    assert {item["user_id"]: item["avatar"] for item in listed}[employee["user_id"]] == urls["ana"]
    detail = client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]
    assert detail["avatar"] == urls["ana"]
    # Un empleado solo ve la suya; el ADMIN ve cuentas que no son de empleados (regla 13: nunca fotos de empleados).
    for viewer, url in ((luis, urls["ana"]), (luis, urls["company"]), (ana, urls["validator"])):
        assert_envelope(fetch(client, viewer, url), 404, "AVATAR_NOT_FOUND")
    assert fetch(client, admin_headers, urls["company"]).status_code == 200
    assert fetch(client, admin_headers, urls["validator"]).status_code == 200
    assert_envelope(fetch(client, admin_headers, urls["ana"]), 404, "AVATAR_NOT_FOUND")
    # Inactiva: la empresa deja de ver su foto (en la lista tampoco viaja).
    client.patch(f"/api/employees/{employee['id']}/status", json={"active": False}, headers=company_headers)
    assert fetch(client, company_headers, urls["ana"]).status_code == 404
    detail = client.get(f"/api/employees/{employee['id']}", headers=company_headers).json()["data"]
    assert detail["avatar"] is None


def test_another_company_never_sees_the_photo_of_a_person_of_this_one(client, company_headers, admin_headers):
    _, ana = _employee(client, company_headers)
    url = upload(client, ana).json()["data"]["avatar"]
    assert create_company(client, admin_headers, max_validators=1).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    assert create_validator(client, other, email="caseta@panificadora.com").status_code == 201
    other_validator = login(client, "caseta@panificadora.com", PASSWORD)
    for viewer in (other, other_validator):
        assert_envelope(fetch(client, viewer, url), 404, "AVATAR_NOT_FOUND")  # igual que si no tuviera foto


def test_the_same_person_in_two_companies_has_one_photo_seen_by_both(client, company_headers, admin_headers):
    employee, ana = _employee(client, company_headers)
    url = upload(client, ana).json()["data"]["avatar"]
    assert create_company(client, admin_headers).status_code == 201
    other = login(client, "admin@panificadora.com", "Empresa1234")
    linked = create_employee(client, other, number="PN-001", email="ana@empresa.com", phone=employee["phone"])
    assert linked.status_code == 201 and linked.json()["data"]["avatar"] == url
    assert fetch(client, other, url).status_code == 200


# ---------------------------------------------------------------- errores con su código


def test_invalid_uploads_answer_with_their_code(client, company_headers, monkeypatch):
    assert_envelope(upload(client, company_headers, b"no soy una foto"), 422, "AVATAR_INVALID")
    body = assert_envelope(upload(client, company_headers, crop={"crop_x": 0, "crop_y": 0}), 422, "AVATAR_CROP_INVALID")
    assert body["errors"][0]["field"] == "crop"
    outside = {"crop_x": 400, "crop_y": 0, "crop_size": 500}
    assert_envelope(upload(client, company_headers, crop=outside), 422, "AVATAR_CROP_INVALID")
    chosen = upload(client, company_headers, crop={"crop_x": 100, "crop_y": 50, "crop_size": 400})
    assert chosen.status_code == 200
    bad_size = client.get(f"/api{chosen.json()['data']['avatar']}&size=100", headers=company_headers)
    assert_envelope(bad_size, 422, "AVATAR_SIZE_INVALID")
    monkeypatch.setattr(settings, "AVATAR_MAX_MB", 0.01)
    body = assert_envelope(upload(client, company_headers, noise(300, 300)), 413, "AVATAR_TOO_LARGE")
    assert "0.01 MB" in body["message"]
    assert me(client, company_headers)["avatar"] == chosen.json()["data"]["avatar"]  # nada cambió


def test_uploads_are_rate_limited_per_person(client, company_headers, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_AVATAR_PER_MINUTE", 1)
    assert upload(client, company_headers).status_code == 200
    assert_envelope(client.delete("/api/users/me/avatar", headers=company_headers), 429, "RATE_LIMITED")


# ---------------------------------------------------------------- el bucket o la BD fallan: nada a medias


def test_without_the_bucket_nothing_changes_and_the_profile_still_works(client, company_headers, bucket):
    url = upload(client, company_headers).json()["data"]["avatar"]
    user_id = me(client, company_headers)["id"]
    before = _rows(user_id)
    bucket.down = {"put"}
    assert_envelope(upload(client, company_headers), 503, "STORAGE_UNAVAILABLE")
    assert [row.version for row in _rows(user_id)] == [row.version for row in before]
    assert me(client, company_headers)["avatar"] == url and len(_objects(bucket, user_id)) == 2
    bucket.down = {"get"}
    assert_envelope(fetch(client, company_headers, url), 503, "STORAGE_UNAVAILABLE")
    assert client.get("/api/users/me", headers=company_headers).status_code == 200  # el perfil sigue sin la foto
    use_storage(DisabledStorage("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env"))
    assert_envelope(upload(client, company_headers), 503, "STORAGE_UNAVAILABLE")


def test_if_the_second_size_fails_the_first_one_is_deleted(client, company_headers, bucket, monkeypatch):
    user_id = me(client, company_headers)["id"]
    put = bucket.put

    def second_fails(name, data, metadata, **kwargs):
        if name.endswith("/96.webp.enc"):
            bucket.down = {"put"}
        return put(name, data, metadata, **kwargs)

    monkeypatch.setattr(bucket, "put", second_fails)
    assert_envelope(upload(client, company_headers), 503, "STORAGE_UNAVAILABLE")
    assert _objects(bucket, user_id) == set() and _rows(user_id) == [] and me(client, company_headers)["avatar"] is None


def test_if_the_database_fails_after_uploading_the_objects_are_discarded(client, company_headers, bucket, monkeypatch):
    user_id = me(client, company_headers)["id"]

    def down(*_args, **_kwargs):
        raise OperationalError("DELETE", {}, Exception("la BD no respondió"))

    monkeypatch.setattr(AvatarRepository, "remove", down)
    assert_envelope(upload(client, company_headers), 503, "DATABASE_UNAVAILABLE")
    assert _objects(bucket, user_id) == set() and me(client, company_headers)["avatar"] is None


def test_a_tampered_or_unreadable_object_is_not_served(client, company_headers, bucket):
    url = upload(client, company_headers).json()["data"]["avatar"]
    bucket.tamper = True  # el MD5 ya no se verifica al leer, pero el SHA-256 sí
    name = next(iter(_objects(bucket, me(client, company_headers)["id"])))
    bucket.objects[name] = (b"otro-contenido", bucket.objects[name][1])
    assert {fetch(client, company_headers, url, size).status_code for size in (96, 512)} == {200, 503}


# ---------------------------------------------------------------- borrar cuentas: sus fotos salen del bucket


def test_deleting_accounts_releases_their_photos(client, company_headers, admin_headers, bucket):
    employee, ana = _employee(client, company_headers)
    upload(client, ana)
    validator = validator_headers(client, company_headers)
    upload(client, validator)
    validator_id = client.get("/api/validators", headers=company_headers).json()["data"]["items"][0]["id"]
    assert client.delete(f"/api/employees/{employee['id']}", headers=company_headers).status_code == 200
    assert client.delete(f"/api/validators/{validator_id}", headers=company_headers).status_code == 200
    assert len(_queued()) == 4
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(UserAvatar)) == 0
    # Una empresa sin empleados se borra con sus cuentas (y sus fotos).
    created = create_company(client, admin_headers)
    other = login(client, "admin@panificadora.com", "Empresa1234")
    upload(client, other)
    company_id = created.json()["data"]["id"]
    assert client.delete(f"/api/admin/companies/{company_id}", headers=admin_headers).status_code == 200
    assert len(_queued()) == 6


def test_the_admin_sees_how_many_photos_are_in_the_bucket(client, company_headers, admin_headers):
    upload(client, company_headers)
    storage = client.get("/api/admin/errors/server", headers=admin_headers).json()["data"]["storage"]
    counts = {image["kind"]: image["stored"] for image in storage["images"]}
    assert counts["user-avatar"] == 2


# ---------------------------------------------------------------- reglas pequeñas


def test_if_none_match_accepts_lists_weak_tags_and_any():
    assert matches('"a-96", W/"b-96"', '"b-96"') and matches("*", '"x-512"')
    assert not matches(None, '"x-96"') and not matches('"y-96"', '"x-96"')


def test_the_browser_cache_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_CACHE_SECONDS", 0)
    assert avatar_cache_control(True) == avatar_cache_control(False) == "no-store"


def test_the_version_is_also_kept_on_the_account(client, company_headers):
    version = upload(client, company_headers).json()["data"]["version"]
    with SessionLocal() as db:
        assert db.scalar(select(User.avatar_version).where(User.email == "admin@empresa.com")) == version
    assert base64.b64decode(
        fetch(client, company_headers, me(client, company_headers)["avatar"]).json()["data"]["data"]
    )
