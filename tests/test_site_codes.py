"""Antifraude 2b, decisión D9: el código de sitio (opcional por sitio), sus kioscos y la entrada y la salida con él.

Por omisión se mide («Solo medir»: sin código o con uno que no vale, una señal del motor de riesgo); obligatorio, se
rechaza con su código ANTES de usar el reto de la prueba de vida; apagado, no se pide."""

import base64
import logging
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import AttendanceEvent, SiteKiosk, WorkSite
from app.services import kiosk_service, site_codes
from app.services.device_service import issue_nonce
from tests.conftest import device_proof, turn_files
from tests.test_attendance import clock, recorded, worker  # noqa: F401 (fixtures)
from tests.test_attendance_calendar import remote_worker
from tests.test_policy import set_policy
from tests.test_risk_engine import last_reasons
from tests.test_shifts import POINT, SITES, address, create_site

TABLET = ec.generate_private_key(ec.SECP256R1())
TABLET_UA = "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/604.1"


def enable_code(client, headers, site: dict, enabled: bool | None = True) -> dict:
    body = {"name": site["name"], "address": address(*POINT), "radius_m": site["radius_m"], "presence_code": enabled}
    response = client.put(f"{SITES}/{site['id']}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def site_row(site_id: int) -> WorkSite:
    with SessionLocal() as db:
        site = db.get(WorkSite, site_id)
        assert site is not None
        return site


def code_at(site_id: int, moment: datetime, shift_windows: int = 0) -> str:
    """El código del sitio en un instante (o `shift_windows` periodos antes o después)."""
    secret = site_codes.secret_of(site_row(site_id))
    assert secret is not None
    return site_codes.code_for(secret, site_codes.window_at(moment.timestamp()) + shift_windows)


def punch(client, headers, action: str, code: str | None = None, *, at=POINT):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    return send(client, headers, action, challenge, code, at=at)


def send(client, headers, action: str, challenge: dict, code: str | None = None, *, at=POINT):
    files = [("images", (f"c{i}.jpg", b"face:ana", "image/jpeg")) for i in range(2)]
    files += turn_files(challenge, "ana")
    data = {"challenge_id": challenge["challenge_id"], "latitude": at[0], "longitude": at[1], "accuracy": 12}
    if code is not None:
        data["site_code"] = code
    return client.post(f"/api/me/attendance/{action}", data=data, files=files, headers=headers)


def events() -> list[AttendanceEvent]:
    with SessionLocal() as db:
        return list(db.scalars(select(AttendanceEvent).order_by(AttendanceEvent.id)))


def today(client, headers) -> dict:
    return client.get("/api/me/attendance/today", headers=headers).json()["data"]


# ---------------------------------------------------------------- el código del sitio (COMPANY)


def test_a_site_turns_its_code_on_and_off_and_keeps_its_secret(client, company_headers):
    site = create_site(client, company_headers)
    assert site["presence_code"] is False and site["kiosks"] == 0 and site_row(site["id"]).presence_secret is None
    on = enable_code(client, company_headers, site)
    secret = site_row(site["id"]).presence_secret
    assert on["presence_code"] is True and secret and len(secret) > 100  # cifrado (texto Fernet), nunca en claro
    assert enable_code(client, company_headers, site, None)["presence_code"] is True  # nulo: no cambia
    assert enable_code(client, company_headers, site, False)["presence_code"] is False
    assert enable_code(client, company_headers, site, True)["presence_code"] is True
    assert site_row(site["id"]).presence_secret == secret  # volver a activarlo no cambia los kioscos ya vinculados
    created = client.post(
        SITES,
        json={"name": "Planta Sur", "address": address(*POINT), "radius_m": 100, "presence_code": True},
        headers=company_headers,
    ).json()["data"]
    assert created["presence_code"] is True and site_row(created["id"]).presence_secret


def test_the_code_rotates_like_totp():
    secret = b"s" * 32
    codes = {site_codes.code_for(secret, window) for window in range(50)}
    assert len(codes) > 45 and all(len(code) == 6 and code.isdigit() for code in codes)
    site = WorkSite(id=7)
    now = 1_000_000.0
    current = site_codes.current(site, secret, now)
    assert current.qr == f"TC-SITE:7:{current.code}" and 1 <= current.expires_in <= settings.SITE_CODE_PERIOD_SECONDS


# ---------------------------------------------------------------- kioscos (COMPANY)


def kiosks_url(site: dict) -> str:
    return f"{SITES}/{site['id']}/kiosks"


def new_kiosk(client, headers, site: dict, name: str = "Recepción") -> dict:
    response = client.post(kiosks_url(site), json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_the_company_manages_the_kiosks_of_a_site(client, company_headers):
    site = enable_code(client, company_headers, create_site(client, company_headers))
    created = new_kiosk(client, company_headers, site)
    code = created["pairing_code"]
    assert len(code) == 11 and code[5] == "-" and created["kiosk"]["paired"] is False
    assert created["kiosk"]["pairing_expires_at"] == created["pairing_expires_at"]
    with SessionLocal() as db:  # solo el hash del código de vinculación
        row = db.scalars(select(SiteKiosk)).one()
        assert row.pairing_hash == kiosk_service.pairing_hash(code) and code not in str(row.__dict__)
    listed = client.get(kiosks_url(site), headers=company_headers).json()
    assert listed["data"]["total"] == 1 and listed["message"] == "1 kiosco"
    assert client.get(SITES, headers=company_headers).json()["data"]["items"][0]["kiosks"] == 1
    kiosk_id = created["kiosk"]["id"]
    renewed = client.post(f"{kiosks_url(site)}/{kiosk_id}/pairing", headers=company_headers).json()["data"]
    assert renewed["pairing_code"] != code
    assert client.delete(f"{kiosks_url(site)}/{kiosk_id}", headers=company_headers).json()["code"] == "KIOSK_DELETED"
    again = client.delete(f"{kiosks_url(site)}/{kiosk_id}", headers=company_headers)
    assert again.status_code == 409 and again.json()["code"] == "ALREADY_DELETED"
    assert client.get(kiosks_url(site), headers=company_headers).json()["data"]["total"] == 0
    trash = client.get(f"{kiosks_url(site)}?deleted=true", headers=company_headers).json()["data"]
    assert trash["total"] == 1 and trash["items"][0]["deleted_by"]
    restored = client.post(f"{kiosks_url(site)}/{kiosk_id}/restore", headers=company_headers)
    assert restored.status_code == 200 and restored.json()["data"]["deleted_at"] is None
    not_deleted = client.post(f"{kiosks_url(site)}/{kiosk_id}/restore", headers=company_headers)
    assert not_deleted.status_code == 409 and not_deleted.json()["code"] == "NOT_DELETED"


def test_a_kiosk_belongs_to_its_site(client, company_headers):
    site = create_site(client, company_headers)
    other = create_site(client, company_headers, name="Planta Sur")
    kiosk_id = new_kiosk(client, company_headers, site)["kiosk"]["id"]
    for url in (f"{kiosks_url(other)}/{kiosk_id}/pairing", f"{kiosks_url(other)}/{kiosk_id}/restore"):
        response = client.post(url, headers=company_headers)
        assert response.status_code == 404 and response.json()["code"] == "KIOSK_NOT_FOUND", url
    missing = client.get(f"{SITES}/9999/kiosks", headers=company_headers)
    assert missing.status_code == 404 and missing.json()["code"] == "SITE_NOT_FOUND"
    blank = client.post(kiosks_url(site), json={"name": " "}, headers=company_headers)
    assert blank.status_code == 422


# ---------------------------------------------------------------- la tableta del kiosco (pública)


def public_key(key: ec.EllipticCurvePrivateKey = TABLET) -> str:
    return device_proof("x", key)["public_key"]


def pair(client, code: str, key: ec.EllipticCurvePrivateKey = TABLET, **extra):
    body = {"pairing_code": code, "public_key": public_key(key), **extra}
    return client.post("/api/kiosk/pair", json=body, headers={"User-Agent": TABLET_UA})


def signed(kiosk_id: int, nonce: str, key: ec.EllipticCurvePrivateKey = TABLET) -> dict:
    proof = device_proof(kiosk_service.signed_message(nonce, kiosk_id), key)
    return {"kiosk_id": kiosk_id, "nonce": nonce, "signature": proof["signature"]}


def fetch(client, kiosk_id: int, nonce: str | None = None, key=TABLET, **headers):
    body = signed(kiosk_id, nonce, key) if nonce else {"kiosk_id": kiosk_id}
    return client.post("/api/kiosk/code", json=body, headers=headers)


@pytest.fixture
def kiosk(client, company_headers) -> dict:
    """Un sitio con su código y su kiosco vinculado a la tableta de pruebas."""
    site = enable_code(client, company_headers, create_site(client, company_headers))
    created = new_kiosk(client, company_headers, site)
    paired = pair(client, created["pairing_code"].lower().replace("-", " "))  # se escribe como se pueda
    assert paired.status_code == 200, paired.text
    return {
        "site": site,
        "id": created["kiosk"]["id"],
        "session": paired.json()["data"],
        "code": created["pairing_code"],
    }


def test_a_tablet_pairs_once_and_shows_the_code(client, company_headers, kiosk):
    session = kiosk["session"]
    assert (session["site_name"], session["kiosk_id"]) == ("Planta Norte", kiosk["id"])
    again = pair(client, kiosk["code"])
    assert again.status_code == 422 and again.json()["code"] == "KIOSK_PAIRING_INVALID"  # un solo uso
    listed = client.get(kiosks_url(kiosk["site"]), headers=company_headers).json()["data"]["items"][0]
    assert (
        listed["paired"] is True and listed["device_name"] == "iPad · Safari" and listed["pairing_expires_at"] is None
    )
    shown = fetch(client, kiosk["id"], session["device_nonce"])
    assert shown.status_code == 200, shown.text
    data = shown.json()["data"]
    assert data["code"] == code_at(kiosk["site"]["id"], datetime.now(UTC)) and data["qr"].endswith(data["code"])
    assert (
        data["period_seconds"] == settings.SITE_CODE_PERIOD_SECONDS and data["device_nonce"] != session["device_nonce"]
    )
    assert data["company_name"]


def test_a_tablet_without_a_challenge_signs_the_one_it_receives(client, company_headers, kiosk):
    asked = fetch(client, kiosk["id"], **company_headers)  # una sesión de la app no le da nada
    assert asked.status_code == 403 and asked.json()["code"] == "KIOSK_PROOF_REQUIRED"
    nonce = asked.json()["errors"][0]["details"]["nonce"]
    assert fetch(client, kiosk["id"], nonce).status_code == 200
    forged = fetch(client, kiosk["id"], nonce + "x")
    assert forged.status_code == 403 and forged.json()["code"] == "KIOSK_PROOF_INVALID"
    unsigned = client.post("/api/kiosk/code", json={"kiosk_id": kiosk["id"], "nonce": nonce})
    assert unsigned.json()["code"] == "KIOSK_PROOF_INVALID"
    other_key = fetch(client, kiosk["id"], nonce, ec.generate_private_key(ec.SECP256R1()))
    assert other_key.status_code == 403 and other_key.json()["code"] == "KIOSK_PROOF_INVALID"  # copiar el id no sirve
    user_nonce = fetch(client, kiosk["id"], issue_nonce(kiosk["id"]))  # el reto de una CUENTA con el mismo número
    assert user_nonce.json()["code"] == "KIOSK_PROOF_INVALID"


def test_an_expired_challenge_asks_for_a_new_one(client, kiosk, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("app.services.device_service.DEVICE_NONCE_SECONDS", -10)
        expired = fetch(client, kiosk["id"]).json()["errors"][0]["details"]["nonce"]
    response = fetch(client, kiosk["id"], expired)
    assert response.status_code == 403 and response.json()["code"] == "KIOSK_PROOF_REQUIRED"


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"public_key": None, "key_hash": None}, 404, "KIOSK_NOT_FOUND"),
        ({"deleted_at": datetime(2026, 1, 1, tzinfo=UTC)}, 404, "KIOSK_NOT_FOUND"),
    ],
)
def test_an_unpaired_or_deleted_kiosk_must_pair_again(client, kiosk, change, status, code):
    nonce = kiosk["session"]["device_nonce"]
    with SessionLocal() as db:
        db.execute(update(SiteKiosk).values(**change))
        db.commit()
    response = fetch(client, kiosk["id"], nonce)
    assert response.status_code == status and response.json()["code"] == code
    assert "vincul" in response.json()["message"]
    unknown = fetch(client, 9999, issue_nonce(9999, purpose=kiosk_service.KIOSK_PURPOSE))
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    "change",
    [{"presence_code": False}, {"active": False}, {"deleted_at": datetime(2026, 1, 1, tzinfo=UTC)}],
)
def test_a_site_without_its_code_turns_the_kiosk_off(client, kiosk, change):
    with SessionLocal() as db:
        db.execute(update(WorkSite).values(**change))
        db.commit()
    response = fetch(client, kiosk["id"], kiosk["session"]["device_nonce"])
    assert response.status_code == 409 and response.json()["code"] == "SITE_CODE_DISABLED"


def test_an_unreadable_secret_is_a_failure_of_the_server(client, kiosk):
    with SessionLocal() as db:
        db.execute(update(WorkSite).values(presence_secret="no-es-fernet"))
        db.commit()
    response = fetch(client, kiosk["id"], kiosk["session"]["device_nonce"])
    assert response.status_code == 503 and response.json()["code"] == "SITE_CODE_UNAVAILABLE"
    assert response.headers["Retry-After"] == str(settings.SITE_CODE_PERIOD_SECONDS)


def test_the_last_time_a_kiosk_was_seen_is_written_every_so_often(client, kiosk):
    long_ago = datetime.now(UTC) - timedelta(seconds=settings.SITE_KIOSK_SEEN_SECONDS + 5)
    with SessionLocal() as db:
        db.execute(update(SiteKiosk).values(last_seen_at=long_ago))
        db.commit()
    nonce = fetch(client, kiosk["id"], kiosk["session"]["device_nonce"]).json()["data"]["device_nonce"]
    with SessionLocal() as db:
        seen = db.scalars(select(SiteKiosk.last_seen_at)).one()
    assert seen is not None and seen.replace(tzinfo=UTC) > long_ago
    assert fetch(client, kiosk["id"], nonce).status_code == 200  # dentro de la ventana: sin escribir


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"pairing_code": "AAAAA-AAAAA"}, "KIOSK_PAIRING_INVALID"),
        ({"public_key": base64.b64encode(b"x" * 60).decode()}, "DEVICE_KEY_INVALID"),
    ],
)
def test_a_wrong_pairing_is_rejected(client, company_headers, body, code):
    site = create_site(client, company_headers)
    created = new_kiosk(client, company_headers, site)
    payload = {"pairing_code": created["pairing_code"], "public_key": public_key(), **body}
    response = client.post("/api/kiosk/pair", json=payload)
    assert response.status_code == 422 and response.json()["code"] == code


def test_an_expired_pairing_code_is_rejected(client, company_headers):
    site = create_site(client, company_headers)
    created = new_kiosk(client, company_headers, site)
    with SessionLocal() as db:
        db.execute(update(SiteKiosk).values(pairing_expires_at=datetime.now(UTC) - timedelta(minutes=1)))
        db.commit()
    response = pair(client, created["pairing_code"], name="Tableta de la entrada")
    assert response.status_code == 422 and response.json()["code"] == "KIOSK_PAIRING_INVALID"


def test_a_tablet_named_by_its_app_or_unknown_keeps_that_name(client, company_headers):
    site = create_site(client, company_headers)
    named = pair(client, new_kiosk(client, company_headers, site)["pairing_code"], name="Entrada norte")
    assert named.status_code == 200
    unknown = client.post(
        "/api/kiosk/pair",
        json={
            "pairing_code": new_kiosk(client, company_headers, site, "Otra")["pairing_code"],
            "public_key": public_key(),
        },
        headers={"User-Agent": "Kiosco/1.0"},
    )
    assert unknown.status_code == 200
    names = {
        k["name"]: k["device_name"]
        for k in client.get(kiosks_url(site), headers=company_headers).json()["data"]["items"]
    }
    assert names == {"Recepción": "Entrada norte", "Otra": "Computadora"}


def test_a_new_pairing_code_turns_the_old_tablet_off(client, company_headers, kiosk):
    renewed = client.post(f"{kiosks_url(kiosk['site'])}/{kiosk['id']}/pairing", headers=company_headers)
    assert renewed.status_code == 200
    response = fetch(client, kiosk["id"], kiosk["session"]["device_nonce"])
    assert response.status_code == 404 and response.json()["code"] == "KIOSK_NOT_FOUND"


# ---------------------------------------------------------------- la entrada y la salida con el código


@pytest.fixture
def coded(client, company_headers, worker):  # noqa: F811
    """Ana con su turno en la Planta Norte, que activó su código."""
    enable_code(client, company_headers, worker["site"])
    return worker


def test_today_tells_the_app_to_ask_for_the_code(client, company_headers, coded, clock):  # noqa: F811
    clock(coded["day"], "07:50")
    assert today(client, coded["headers"])["site_code"] is True
    set_policy(client, company_headers, site_codes="OFF")
    assert today(client, coded["headers"])["site_code"] is False
    enable_code(client, company_headers, coded["site"], False)
    set_policy(client, company_headers, site_codes="OBSERVE")
    assert today(client, coded["headers"])["site_code"] is False


def test_the_current_code_confirms_presence(client, coded, clock):  # noqa: F811
    moment = clock(coded["day"], "07:50")
    site_id = coded["site"]["id"]
    recorded(punch(client, coded["headers"], "check-in", code_at(site_id, moment)))
    assert not {code for code in last_reasons() if code.startswith("SITE_CODE")}
    [event] = events()
    assert event.presence_window == site_codes.window_at(moment.timestamp())  # el periodo, nunca el código
    clock(coded["day"], "12:00")
    recorded(punch(client, coded["headers"], "break-start"))  # los descansos no lo llevan
    assert not {code for code in last_reasons() if code.startswith("SITE_CODE")}
    exit_moment = clock(coded["day"], "15:59")
    recorded(punch(client, coded["headers"], "check-out", f"TC-SITE:{site_id}:{code_at(site_id, exit_moment)}"))
    assert events()[-1].presence_window == site_codes.window_at(exit_moment.timestamp())  # también el QR


@pytest.mark.parametrize(
    ("make", "code", "value"),
    [
        (lambda site_id, moment: None, "SITE_CODE_MISSING", None),
        (lambda site_id, moment: "123", "SITE_CODE_INVALID", 1.0),
        (lambda site_id, moment: code_at(site_id, moment, -3), "SITE_CODE_INVALID", 1.0),
        (lambda site_id, moment: f"TC-SITE:{site_id + 1}:{code_at(site_id, moment)}", "SITE_CODE_INVALID", 3.0),
    ],
)
def test_without_the_current_code_it_is_only_a_signal(client, coded, clock, make, code, value):  # noqa: F811
    moment = clock(coded["day"], "07:50")
    session = recorded(punch(client, coded["headers"], "check-in", make(coded["site"]["id"], moment)))
    assert session["review_status"] is None  # solo se mide
    reason = last_reasons()[code]
    assert (reason["mode"], reason["value"], reason["kind"]) == ("OBSERVE", value, "LOCATION")
    assert events()[-1].presence_window is None


def test_a_code_used_twice_in_its_window_is_measured_as_reused(client, coded, clock):  # noqa: F811
    moment = clock(coded["day"], "07:50")
    code = code_at(coded["site"]["id"], moment)
    recorded(punch(client, coded["headers"], "check-in", code))
    recorded(punch(client, coded["headers"], "check-out", code))  # el mismo periodo: ya se usó
    assert last_reasons()["SITE_CODE_INVALID"]["value"] == 2.0
    assert events()[-1].presence_window is None


@pytest.mark.parametrize("offset", [-1, 1])
def test_clock_skew_and_typing_time_are_tolerated(client, coded, clock, offset):  # noqa: F811
    moment = clock(coded["day"], "07:50")
    recorded(punch(client, coded["headers"], "check-in", code_at(coded["site"]["id"], moment, offset)))
    assert "SITE_CODE_INVALID" not in last_reasons()


@pytest.mark.parametrize(
    ("make", "status", "error"),
    [
        (lambda site_id, moment: None, 403, "SITE_CODE_REQUIRED"),
        (lambda site_id, moment: "000000x", 403, "SITE_CODE_INVALID"),
    ],
)
def test_with_required_codes_the_challenge_survives_a_rejection(
    client,
    company_headers,
    coded,
    clock,  # noqa: F811
    make,
    status,
    error,
):
    set_policy(client, company_headers, site_codes="ENFORCE")
    moment = clock(coded["day"], "07:50")
    challenge = client.post("/api/face/challenge", headers=coded["headers"]).json()["data"]
    rejected = send(client, coded["headers"], "check-in", challenge, make(coded["site"]["id"], moment))
    assert rejected.status_code == status and rejected.json()["code"] == error
    if error == "SITE_CODE_REQUIRED":
        assert "Planta Norte" in rejected.json()["message"]
        assert rejected.json()["errors"][0]["details"]["site"] == "Planta Norte"
    recorded(send(client, coded["headers"], "check-in", challenge, code_at(coded["site"]["id"], moment)))


def test_with_required_codes_a_reused_code_is_a_conflict(client, company_headers, coded, clock):  # noqa: F811
    set_policy(client, company_headers, site_codes="ENFORCE")
    moment = clock(coded["day"], "07:50")
    code = code_at(coded["site"]["id"], moment)
    recorded(punch(client, coded["headers"], "check-in", code))
    reused = punch(client, coded["headers"], "check-out", code)
    assert reused.status_code == 409 and reused.json()["code"] == "SITE_CODE_USED"


def test_a_remote_check_in_needs_no_code(client, company_headers, clock):  # noqa: F811
    remote = remote_worker(client, company_headers)
    clock(remote["day"], "07:50")
    set_policy(client, company_headers, site_codes="ENFORCE")
    recorded(punch(client, remote["headers"], "check-in", at=(19.4326, -99.1332)))  # lejos de cualquier sitio


def test_an_unreadable_secret_never_blocks_a_check_in(client, company_headers, coded, clock, caplog):  # noqa: F811
    set_policy(client, company_headers, site_codes="ENFORCE")
    with SessionLocal() as db:
        db.execute(update(WorkSite).values(presence_secret="no-es-fernet"))
        db.commit()
    clock(coded["day"], "07:50")
    with caplog.at_level(logging.ERROR, logger="app.services.site_codes"):
        recorded(punch(client, coded["headers"], "check-in"))
    assert "secreto del código del sitio" in caplog.text


def test_a_kiosk_id_out_of_range_is_a_validation_error(client):
    response = client.post("/api/kiosk/code", json={"kiosk_id": 10**12})
    assert response.status_code == 422 and response.json()["errors"][0]["field"] == "kiosk_id"
