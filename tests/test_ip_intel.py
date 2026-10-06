"""Base local de IP (DB-IP Lite, decisión D8): país, red y nube de una IP sin consultas ni servicios externos, tolerante
a fallas (sin archivo, dañado o con una IP privada no se mide nada) y al día cuando otro proceso reemplaza el archivo;
y su actualización mensual (descarga con tiempo límite, suma SHA-1 publicada, tipo correcto y reemplazo atómico)."""

import fcntl
import hashlib
import io
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

from app import cli
from app.core.config import settings
from app.core.ip_intel import IpInfo, ip_intel, is_hosting
from app.services import ip_database, maintenance_service
from app.services.maintenance_service import MaintenanceScheduler
from tests.mmdb_support import COUNTRIES, MX_HOME, NETWORKS, US_CLOUD, build, gzipped, install, write_mmdb

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
COUNTRY = build("DBIP-Country-Lite", COUNTRIES)
ASN = build("DBIP-ASN-Lite (compat=GeoLite2-ASN)", NETWORKS)


# ---------------------------------------------------------------- lectura


def test_country_network_and_cloud_of_an_ip():
    install()
    assert ip_intel.lookup(MX_HOME) == IpInfo("MX", 22884, "Proveedor de Casa", False)
    assert ip_intel.lookup(US_CLOUD) == IpInfo("US", 16509, "Amazon.com, Inc.", True)
    assert ip_intel.lookup("8.8.8.8") is None  # en ninguna de las dos bases


def test_each_database_works_alone():
    install(asn=False)
    assert ip_intel.lookup(US_CLOUD) == IpInfo("US", None, None, False)
    install(country=False)
    assert ip_intel.lookup(US_CLOUD) == IpInfo(None, 16509, "Amazon.com, Inc.", True)


def test_private_invalid_or_ipv6_in_an_ipv4_database_are_not_measured():
    install()
    for ip in (None, "", "testclient", "10.0.0.1", "127.0.0.1", "192.168.1.1", "2806:2f0::1"):
        assert ip_intel.lookup(ip) is None, ip


def test_without_files_nothing_is_measured():
    assert ip_intel.lookup(MX_HOME) is None


def test_odd_records_are_read_as_unknown():
    write_mmdb(settings.ip_country_db, "DBIP-Country-Lite", {"187.188.0.0/16": ["no es un mapa"]})
    write_mmdb(settings.ip_asn_db, "DBIP-ASN-Lite", {"187.188.0.0/16": {"autonomous_system_number": "x"}})
    ip_intel.reload()
    assert ip_intel.lookup(MX_HOME) == IpInfo(None, None, None, False)


def test_a_damaged_file_is_logged_once_and_ignored(caplog):
    settings.ip_country_db.parent.mkdir(parents=True, exist_ok=True)
    settings.ip_country_db.write_bytes(b"esto no es una base MMDB")
    ip_intel.reload()
    with caplog.at_level(logging.ERROR, logger="app.core.ip_intel"):
        assert ip_intel.lookup(MX_HOME) is None
        assert ip_intel.lookup(MX_HOME) is None  # ya revisado: no se vuelve a intentar en cada consulta
    assert len([r for r in caplog.records if r.name == "app.core.ip_intel"]) == 1


def test_a_file_replaced_by_another_process_is_reopened(monkeypatch):
    install()
    assert ip_intel.lookup(MX_HOME).country == "MX"
    replaced = settings.ip_country_db.with_suffix(".nuevo")
    write_mmdb(replaced, "DBIP-Country-Lite", {"187.188.0.0/16": {"country": {"iso_code": "gt"}}})
    os.replace(replaced, settings.ip_country_db)  # como lo hace la actualización (reemplazo atómico)
    assert ip_intel.lookup(MX_HOME).country == "MX"  # dentro de IP_DB_RELOAD_CHECK_SECONDS sigue la que tenía
    monkeypatch.setattr(settings, "IP_DB_RELOAD_CHECK_SECONDS", 0)
    assert ip_intel.lookup(MX_HOME).country == "GT"
    settings.ip_country_db.unlink()
    assert ip_intel.lookup(MX_HOME).country is None  # el archivo se fue: solo queda la red


def test_hosting_by_network_number_or_by_name():
    assert is_hosting(16509, None) and is_hosting(None, "Some VPS Hosting S.A.")
    assert not is_hosting(None, None) and not is_hosting(22884, "Total Play Telecomunicaciones")


# ---------------------------------------------------------------- actualización mensual


def _sha1_page(*files: bytes) -> bytes:
    rows = "".join(f"<dt>SHA1SUM</dt><dd>{hashlib.sha1(f).hexdigest()}</dd>" for f in files)
    return f"<html><dl>{rows}</dl></html>".encode()


def _url(template: str, when: datetime = NOW) -> str:
    return template.format(year=f"{when.year:04d}", month=f"{when.month:02d}")


@pytest.fixture
def downloads(monkeypatch):
    """Las descargas de DB-IP servidas en memoria: {url: bytes o excepción}; lo que no está responde 404."""
    monkeypatch.setattr(settings, "IP_DB_REFRESH_ENABLED", True)
    ip_database._failures.clear()
    served: dict[str, object] = {}
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        found = served.get(url, HTTPError(url, 404, "Not Found", None, None))  # type: ignore[arg-type]
        if isinstance(found, Exception):
            raise found
        return found  # type: ignore[return-value]

    monkeypatch.setattr(ip_database, "fetch", fetch)
    yield served, asked
    ip_database._failures.clear()


def _serve(served: dict, when: datetime = NOW, *, country: bytes = COUNTRY, asn: bytes = ASN) -> None:
    served[_url(settings.IP_DB_COUNTRY_URL, when)] = gzipped(country)
    served[_url(settings.IP_DB_ASN_URL, when)] = gzipped(asn)
    served[settings.IP_DB_COUNTRY_CHECKSUM_URL] = _sha1_page(b"csv", country)
    served[settings.IP_DB_ASN_CHECKSUM_URL] = _sha1_page(asn, b"csv")


def test_disabled_it_never_downloads(downloads, monkeypatch):
    monkeypatch.setattr(settings, "IP_DB_REFRESH_ENABLED", False)
    assert ip_database.refresh_if_due(now=NOW) == {} and downloads[1] == []


def test_it_downloads_verifies_and_swaps_both_files(downloads):
    served, asked = downloads
    _serve(served)
    assert ip_database.refresh_if_due(now=NOW) == {"country": "updated", "asn": "updated"}
    assert settings.ip_country_db.read_bytes() == COUNTRY and settings.ip_asn_db.read_bytes() == ASN
    assert ip_intel.lookup(US_CLOUD) == IpInfo("US", 16509, "Amazon.com, Inc.", True)  # el lector la vuelve a abrir
    assert not list(settings.ip_country_db.parent.glob("*.tmp"))
    asked.clear()
    assert ip_database.refresh_if_due(now=NOW + timedelta(days=1)) == {} and asked == []  # al día
    old = (NOW - timedelta(days=settings.IP_DB_REFRESH_DAYS + 1)).timestamp()
    os.utime(settings.ip_asn_db, (old, old))
    assert ip_database.refresh_if_due(now=NOW) == {"asn": "updated"}  # solo el vencido
    assert ip_database.refresh_if_due(force=True, now=NOW) == {"country": "updated", "asn": "updated"}


def test_the_previous_month_serves_until_the_new_one_is_published(downloads):
    served, asked = downloads
    first = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    _serve(served, datetime(2025, 12, 15, tzinfo=UTC))
    assert ip_database.refresh_if_due(now=first) == {"country": "updated", "asn": "updated"}
    assert _url(settings.IP_DB_COUNTRY_URL, first) in asked  # primero intentó enero


def test_failures_keep_the_previous_file_and_wait_before_retrying(downloads, caplog):
    served, _ = downloads
    install()
    before = settings.ip_country_db.read_bytes()
    os.utime(settings.ip_country_db, (0, 0))
    os.utime(settings.ip_asn_db, (0, 0))
    _serve(served, country=ASN)  # el archivo de país resultó ser de otro tipo (con su suma correcta)
    served[_url(settings.IP_DB_ASN_URL)] = HTTPError("u", 500, "Error", None, None)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR, logger="app.services.ip_database"):
        assert ip_database.refresh_if_due(now=NOW) == {"country": "failed", "asn": "failed"}
    assert settings.ip_country_db.read_bytes() == before
    assert len([r for r in caplog.records if r.name == "app.services.ip_database"]) == 2
    assert ip_database.refresh_if_due(now=NOW) == {"country": "waiting", "asn": "waiting"}  # IP_DB_RETRY_HOURS


@pytest.mark.parametrize(
    "tamper",
    [
        "checksum",  # la suma publicada no coincide
        "unpublished",  # ni este mes ni el anterior
        "not_gzip",  # no es un gzip
        "not_mmdb",  # descomprime, la suma coincide, pero no es una base
    ],
)
def test_a_bad_download_never_replaces_anything(downloads, tamper):
    served, _ = downloads
    _serve(served)
    url = _url(settings.IP_DB_COUNTRY_URL)
    if tamper == "checksum":
        served[settings.IP_DB_COUNTRY_CHECKSUM_URL] = _sha1_page(b"otra cosa")
    elif tamper == "unpublished":
        del served[url]
    elif tamper == "not_gzip":
        served[url] = b"no es gzip"
    else:
        served[url] = gzipped(b"no es una base")
        served[settings.IP_DB_COUNTRY_CHECKSUM_URL] = _sha1_page(b"no es una base")
    assert ip_database.refresh_if_due(now=NOW)["country"] == "failed"
    assert not settings.ip_country_db.exists()


def test_files_larger_than_the_limit_are_refused(downloads, monkeypatch):
    served, _ = downloads
    monkeypatch.setattr(settings, "IP_DB_MAX_MB", 1)
    _serve(served, country=COUNTRY + bytes(2 * 1024 * 1024))  # descomprimido pasa de 1 MB
    assert ip_database.refresh_if_due(now=NOW)["country"] == "failed"


def test_one_process_downloads_at_a_time(downloads):
    served, _ = downloads
    _serve(served)
    settings.ip_country_db.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.ip_country_db.with_name(f".{settings.ip_country_db.name}.lock"), "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # otro proceso lo está actualizando
        assert ip_database.refresh_if_due(now=NOW)["country"] == "busy"


def test_another_process_just_refreshed_it(downloads, monkeypatch):
    served, asked = downloads
    _serve(served)
    answers = iter([True, False, True, False])  # vencido al revisar; al día después de tomar el candado
    monkeypatch.setattr(ip_database, "_due", lambda path, now: next(answers))
    assert ip_database.refresh_if_due(now=NOW) == {"country": "fresh", "asn": "fresh"} and asked == []


def test_fetch_has_a_size_limit(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(settings, "IP_DB_MAX_MB", 1)
    monkeypatch.setattr(ip_database, "urlopen", lambda request, timeout: Response(b"x" * 10))
    assert ip_database.fetch("https://example.invalid/a") == b"x" * 10
    monkeypatch.setattr(ip_database, "urlopen", lambda request, timeout: Response(bytes(1024 * 1024 + 1)))
    with pytest.raises(ip_database.IpDatabaseError):
        ip_database.fetch("https://example.invalid/b")


def test_the_cli_and_the_maintenance_refresh_it(downloads, monkeypatch, capsys):
    served, _ = downloads
    _serve(served)
    monkeypatch.setattr(sys, "argv", ["app.cli", "ipdb", "refresh"])
    assert cli.main() == 0 and "country=updated" in capsys.readouterr().out
    assert cli.main() == 0 and "Nada que hacer" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["app.cli", "ipdb", "refresh", "--force"])
    served[settings.IP_DB_ASN_CHECKSUM_URL] = _sha1_page(b"otra")
    assert cli.main() == 1 and "asn=failed" in capsys.readouterr().out
    # El mantenimiento de cada proceso la pone al día en cada vuelta (sin la BD).
    calls: list[int] = []
    monkeypatch.setattr(ip_database, "refresh_if_due", lambda: calls.append(1) or {})
    monkeypatch.setattr(maintenance_service, "run_once", dict)
    scheduler = MaintenanceScheduler(0.01)
    scheduler.start()
    for _ in range(100):
        if calls:
            break
        scheduler._stop.wait(0.01)
    scheduler.stop()
    assert calls


def test_the_admin_sees_the_local_ip_database_in_face_security(client, admin_headers):
    """Seguridad facial dice si la base local está (y de cuándo es): sin ella, las señales de red no se miden."""
    empty = client.get("/api/admin/face-security", headers=admin_headers).json()["data"]["ip_database"]
    days = settings.IP_DB_REFRESH_DAYS
    assert empty == {"refresh_enabled": False, "refresh_days": days, "country": None, "asn": None}
    install()
    loaded = client.get("/api/admin/face-security", headers=admin_headers).json()["data"]["ip_database"]
    assert loaded["country"]["database_type"] == "DBIP-Country-Lite"
    assert loaded["asn"]["built_at"].startswith("2026-09-21")  # el build_epoch del archivo de prueba
