"""Identificador fiscal de la empresa de cualquier país (migración 0074; decisión del dueño del producto, 2026-10-06).

País fiscal + tipo (`catalog.tax_id_types`) + número normalizado:

- cada tipo valida su formato con la regla del catálogo; el RFC conserva su validación completa y los que tienen un
  dígito verificador conocido (CUIT, RUT, NIT, RUC, CNPJ, SIREN, SIRET) lo revisan;
- opcional: sin número no se guardan ni país ni tipo; con número y sin ellos, los de omisión;
- único entre las empresas vigentes por (país, tipo, número): 409 `COMPANY_TAX_ID_TAKEN` en `tax_id`, también al
  restaurar (`RESTORE_CONFLICT`);
- la validación en vivo (`company_tax_id`, «país:tipo» en `related`; `company_rfc` se queda para la versión anterior);
- la API de integración entrega los tres datos y conserva `rfc` (obsoleto);
- los RFC que ya existían pasan a ser su identificador (la sentencia de la migración) y la base impide los tres datos a
  medias o repetidos.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal, engine
from app.core.soft_delete import WITH_DELETED
from app.i18n import LocalizedValueError, use_locale
from app.models import CatalogTaxIdType, Company
from app.models.catalog_seed import load_catalog_seed
from app.models.company import TaxId
from app.schemas.tax_ids import (
    CHECK_DIGITS,
    OTHER_TYPE,
    RFC_TYPE,
    is_blank_tax_id,
    main_tax_id_type,
    normalize_tax_id,
    resolve_tax_id,
    tax_id_name,
    tax_id_type,
)
from app.services.catalog_service import clear_catalog_cache
from tests.conftest import create_company, login
from tests.test_api_keys import call, new_key

COMPANIES = "/api/admin/companies"
EN = {"Accept-Language": "en-US"}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0074_company_tax_id.py"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def tax(country: str | None, type_code: str | None, number: str | None) -> dict:
    return {"tax_country": country, "tax_id_type": type_code, "tax_id": number}


def ein(number: str = "12-3456789") -> dict:
    return tax("US", "US_EIN", number)


def stored(company_id: int) -> tuple[str | None, str | None, str | None, str | None]:
    """(país, tipo, número, rfc) como quedaron en la base."""
    with SessionLocal() as db:
        company = db.get(Company, company_id, execution_options=WITH_DELETED)
        assert company is not None
        return company.tax_country, company.tax_id_type, company.tax_id, company.rfc


def triple(data: dict) -> tuple:
    return data["tax_country"], data["tax_id_type"], data["tax_id"]


# ---------------------------------------------------------------- el catálogo


def test_the_catalog_has_every_type_the_code_names_and_each_example_passes_its_own_rule():
    rows = {row["code"]: row for row in load_catalog_seed()["tax_id_types"]}
    required = {"MX_RFC", "US_EIN", "ES_NIF", "CO_NIT", "AR_CUIT", "CL_RUT", "PE_RUC", "BR_CNPJ", "CA_BN", "GB_VAT"}
    assert required | {"GB_CRN", "DE_VAT", "FR_SIREN", "FR_SIRET", OTHER_TYPE} <= set(rows)
    assert {RFC_TYPE, OTHER_TYPE, *CHECK_DIGITS} <= set(rows)
    countries = {row["code"] for row in load_catalog_seed()["countries"] if row["active"]}
    for code, row in rows.items():
        assert (row["country_code"] is None) == (code == OTHER_TYPE) and row["country_code"] in countries | {None}
        assert row["min_length"] <= len(row["example"]) <= row["max_length"] <= 30, code
        assert normalize_tax_id(code, row["example"]) == row["example"], code
    assert (rows[OTHER_TYPE]["min_length"], rows[OTHER_TYPE]["max_length"]) == (3, 30)


def test_the_catalogs_endpoint_sends_each_type_with_its_rule_in_both_languages(client, admin_headers):
    spanish = client.get("/api/catalogs", headers=admin_headers).json()["data"]["tax_id_types"]
    english = client.get("/api/catalogs", headers={**admin_headers, **EN}).json()["data"]["tax_id_types"]
    rfc = next(row for row in spanish if row["code"] == RFC_TYPE)
    assert (rfc["short_name"], rfc["country_code"], rfc["example"]) == ("RFC", "MX", "PNO120315AB1")
    assert rfc["pattern"] and (rfc["min_length"], rfc["max_length"]) == (12, 13)
    other = {row["code"]: row for row in english}[OTHER_TYPE]
    assert (other["short_name"], other["name"], other["description"]) == (
        "Tax ID",
        "Other tax ID",
        "3 to 30 letters or digits",
    )
    assert [row["code"] for row in spanish] == [row["code"] for row in english]  # el mismo orden en los dos


# ---------------------------------------------------------------- formato y dígito verificador de cada tipo


@pytest.mark.parametrize(
    ("type_code", "raw", "normalized"),
    [
        ("MX_RFC", "pno-120315-ab1", "PNO120315AB1"),
        ("MX_RFC", "PEXJ.900510.AB1", "PEXJ900510AB1"),
        ("US_EIN", "12-3456789", "123456789"),
        ("CA_BN", "123456789", "123456789"),
        ("CA_BN", "123456789 rt 0001", "123456789RT0001"),
        ("ES_NIF", "b-12345678", "B12345678"),
        ("ES_NIF", "12345678Z", "12345678Z"),
        ("ES_NIF", "X1234567L", "X1234567L"),
        ("CO_NIT", "800.197.268-4", "8001972684"),
        ("CO_NIT", "800197262-0", "8001972620"),
        ("CO_NIT", "800197266-1", "8001972661"),
        ("AR_CUIT", "33-69345023-9", "33693450239"),
        ("AR_CUIT", "30-50001091-2", "30500010912"),
        ("CL_RUT", "60.803.000-k", "60803000K"),
        ("CL_RUT", "10.000.004-0", "100000040"),
        ("CL_RUT", "12.345.678-5", "123456785"),
        ("PE_RUC", "20131312955", "20131312955"),
        ("PE_RUC", "20131312980", "20131312980"),
        ("PE_RUC", "20131313081", "20131313081"),
        ("BR_CNPJ", "11.222.333/0001-81", "11222333000181"),
        ("BR_CNPJ", "12.abc.345/01de-35", "12ABC34501DE35"),  # CNPJ alfanumérico (Receita Federal, 2026)
        ("GB_VAT", "GB 123 4567 89", "GB123456789"),
        ("GB_CRN", "sc123456", "SC123456"),
        ("DE_VAT", "DE 123 456 789", "DE123456789"),
        ("FR_SIREN", "732 829 320", "732829320"),
        ("FR_SIRET", "732 829 320 00074", "73282932000074"),
        ("FR_SIRET", "356 000 000 49837", "35600000049837"),  # La Poste: suma de dígitos múltiplo de 5
        ("OTHER", "abc-123/9", "ABC1239"),
    ],
)
def test_each_type_normalizes_a_valid_number(type_code, raw, normalized):
    assert normalize_tax_id(type_code, raw) == normalized


@pytest.mark.parametrize(
    ("type_code", "raw", "message"),
    [
        ("MX_RFC", "XAXX010101000", "genérico"),
        ("MX_RFC", "ABC991332XX1", "fecha"),
        ("MX_RFC", "PNO12", "12 caracteres (persona moral) o 13"),
        ("US_EIN", "12345678", "El número de EIN debe tener 9 caracteres"),
        ("US_EIN", "12345678A", "Formato de EIN no válido (p. ej. 123456789)"),
        ("CA_BN", "1234", "El número de BN debe tener de 9 a 15 caracteres"),
        ("CA_BN", "123456789RT", "Formato de BN no válido"),
        ("ES_NIF", "I12345678", "Formato de NIF no válido"),
        ("CO_NIT", "8001972685", "El dígito verificador de NIT no coincide. Revisa el número."),
        ("AR_CUIT", "33693450238", "El dígito verificador de CUIT no coincide"),
        ("AR_CUIT", "20000000010", "El dígito verificador de CUIT no coincide"),  # su dígito sería 10: no se asigna
        ("AR_CUIT", "11693450239", "Formato de CUIT no válido"),
        ("CL_RUT", "60803000-1", "El dígito verificador de RUT no coincide"),
        ("PE_RUC", "20131312956", "El dígito verificador de RUC no coincide"),
        ("PE_RUC", "30131312955", "Formato de RUC no válido"),
        ("BR_CNPJ", "11222333000182", "El dígito verificador de CNPJ no coincide"),
        ("GB_VAT", "GB12345678A", "Formato de VAT no válido (p. ej. GB123456789)"),
        ("GB_CRN", "1234567", "El número de CRN debe tener 8 caracteres"),
        ("DE_VAT", "DE12345678", "El número de USt-IdNr. debe tener 11 caracteres"),
        ("FR_SIREN", "732829321", "El dígito verificador de SIREN no coincide"),
        ("FR_SIRET", "73282932000075", "El dígito verificador de SIRET no coincide"),
        ("FR_SIRET", "35600000049838", "El dígito verificador de SIRET no coincide"),
        ("OTHER", "AB", "El número de ID fiscal debe tener de 3 a 30 caracteres"),
        ("OTHER", "ABC*123", "Formato de ID fiscal no válido"),
    ],
)
def test_each_type_rejects_what_breaks_its_rule(type_code, raw, message):
    with pytest.raises(LocalizedValueError) as error:
        normalize_tax_id(type_code, raw)
    assert message in str(error.value)


def test_the_rules_speak_english():
    def message(call) -> str:
        """El mensaje se arma al leerse: en el idioma vigente."""
        with use_locale("en-US"), pytest.raises(LocalizedValueError) as error:
            call()
        with use_locale("en-US"):
            return str(error.value)

    assert message(lambda: normalize_tax_id("AR_CUIT", "33693450238")) == (
        "The CUIT check digit doesn't match. Check the number."
    )
    assert message(lambda: normalize_tax_id("US_EIN", "1234")) == "The EIN must have 9 characters"
    assert message(lambda: normalize_tax_id("CA_BN", "1234")) == "The BN must have 9 to 15 characters"
    assert message(lambda: normalize_tax_id("BR_CNPJ", "1122233300018A")) == (
        "The CNPJ format isn't valid (e.g., 11222333000181)"
    )
    assert message(lambda: resolve_tax_id("MX", "US_EIN", "123456789")) == "EIN isn't a tax ID used in Mexico"


def test_country_and_type_defaults_and_their_errors():
    assert resolve_tax_id(None, None, "pno120315ab1") == TaxId("MX", "MX_RFC", "PNO120315AB1")
    assert resolve_tax_id("us", None, "12-3456789") == TaxId("US", "US_EIN", "123456789")
    assert resolve_tax_id(None, "us_ein", "123456789") == TaxId("US", "US_EIN", "123456789")  # el país del tipo
    assert resolve_tax_id(None, OTHER_TYPE, "abc123") == TaxId("MX", OTHER_TYPE, "ABC123")
    assert resolve_tax_id("JP", None, "T1234567890123") == TaxId("JP", OTHER_TYPE, "T1234567890123")
    assert resolve_tax_id("PE", OTHER_TYPE, "abc123") == TaxId("PE", OTHER_TYPE, "ABC123")
    for args, message in [
        (("ZZ", None, "123"), "Elige un país de la lista"),
        (("US", "NOPE", "123"), "Elige un tipo de identificador de la lista"),
        (("US", "MX_RFC", "PNO120315AB1"), "RFC no es un identificador fiscal de Estados Unidos"),
    ]:
        with pytest.raises(LocalizedValueError) as error:
            resolve_tax_id(*args)
        assert str(error.value) == message
    with pytest.raises(LocalizedValueError) as unknown:  # un tipo que el catálogo ya no tiene
        normalize_tax_id("NOPE", "123")
    assert str(unknown.value) == "Elige un tipo de identificador de la lista"
    assert tax_id_name("US_EIN") == "EIN" and tax_id_name("GONE") == "GONE"
    assert is_blank_tax_id(None) and is_blank_tax_id(" - . / ") and not is_blank_tax_id("A")


def test_an_inactive_type_is_no_longer_offered_nor_accepted():
    with SessionLocal() as db:
        db.execute(update(CatalogTaxIdType).where(CatalogTaxIdType.code == "US_EIN").values(active=False))
        db.commit()
    clear_catalog_cache()
    assert main_tax_id_type("US") == OTHER_TYPE
    with pytest.raises(LocalizedValueError):
        tax_id_type("US_EIN", "US")


# ---------------------------------------------------------------- alta y edición (consola del ADMIN)


def test_a_company_from_any_country_is_registered_with_its_tax_id(client, admin_headers):
    created = create_company(client, admin_headers, **ein())
    assert created.status_code == 201, created.text
    company = created.json()["data"]
    # `rfc` (obsoleto) solo lleva el número si es un RFC; la columna anterior, igual.
    assert triple(company) == ("US", "US_EIN", "123456789") and company["rfc"] is None
    assert stored(company["id"]) == ("US", "US_EIN", "123456789", None)
    detail = client.get(f"{COMPANIES}/{company['id']}", headers=admin_headers).json()["data"]
    assert triple(detail) == ("US", "US_EIN", "123456789")
    listed = client.get(COMPANIES, params={"search": "123456789"}, headers=admin_headers).json()["data"]["items"]
    assert [(item["id"], item["tax_id"]) for item in listed] == [(company["id"], "123456789")]

    rfc = create_company(client, admin_headers, admin_email="b@mx.com", **tax("MX", "MX_RFC", "pno-120315-ab1"))
    data = rfc.json()["data"]
    assert triple(data) == ("MX", "MX_RFC", "PNO120315AB1") and data["rfc"] == "PNO120315AB1"
    assert stored(data["id"]) == ("MX", "MX_RFC", "PNO120315AB1", "PNO120315AB1")
    # Con el número y sin país ni tipo: México y su RFC (lo mismo que propone la aplicación web).
    bare = create_company(client, admin_headers, admin_email="c@mx.com", **tax(None, None, "ACM010101AB2"))
    assert triple(bare.json()["data"]) == ("MX", "MX_RFC", "ACM010101AB2")


def test_without_a_number_the_country_and_type_are_ignored(client, admin_headers):
    for position, blank in enumerate((None, "", " - ")):
        email = f"x{position}@x.com"
        created = create_company(client, admin_headers, admin_email=email, **tax("US", "US_EIN", blank))
        assert created.status_code == 201, created.text
        assert triple(created.json()["data"]) == (None, None, None)
        assert stored(created.json()["data"]["id"]) == (None, None, None, None)


def test_the_tax_id_is_unique_per_country_type_and_number_among_live_companies(client, admin_headers):
    first = create_company(client, admin_headers, **ein())
    assert first.status_code == 201
    taken = create_company(client, admin_headers, admin_email="b@b.com", **ein("123456789"))
    assert taken.status_code == 409 and taken.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    assert taken.json()["errors"][0]["field"] == "tax_id"
    assert taken.json()["message"] == "Ya existe una empresa con ese identificador fiscal"
    english = create_company(client, {**admin_headers, **EN}, admin_email="b@b.com", **ein())
    assert english.json()["message"] == "A company with that tax ID already exists"
    # El mismo número con otro tipo, o «Otro» en otro país, es otro identificador.
    siren = create_company(client, admin_headers, admin_email="c@c.com", **tax("FR", "FR_SIREN", "732829320"))
    same_number = create_company(client, admin_headers, admin_email="d@d.com", **ein("732829320"))
    other_mx = create_company(client, admin_headers, admin_email="e@e.com", **tax("MX", "OTHER", "abc123"))
    other_pe = create_company(client, admin_headers, admin_email="f@f.com", **tax("PE", "OTHER", "ABC123"))
    assert {r.status_code for r in (siren, same_number, other_mx, other_pe)} == {201}
    repeated = create_company(client, admin_headers, admin_email="g@g.com", **tax("PE", "OTHER", "abc-123"))
    assert repeated.status_code == 409 and repeated.json()["code"] == "COMPANY_TAX_ID_TAKEN"
    # El de una empresa en «Eliminados» se puede volver a usar (regla 20).
    assert client.delete(f"{COMPANIES}/{first.json()['data']['id']}", headers=admin_headers).status_code == 200
    assert create_company(client, admin_headers, admin_email="h@h.com", **ein()).status_code == 201


def test_each_error_lands_on_its_own_field(client, admin_headers):
    def errors(**body) -> dict[str, str]:
        response = create_company(client, admin_headers, admin_email="z@z.com", **body)
        assert response.status_code == 422, response.text
        return {e["field"]: e["message"] for e in response.json()["errors"]}

    # Un país o un tipo inválido tiene su error; el número no se mide contra otra regla.
    assert errors(**tax("ZZ", "US_EIN", "1")) == {"tax_country": "Elige un país de la lista"}
    assert errors(**tax("US", "NOPE", "1")) == {"tax_id_type": "Elige un tipo de identificador de la lista"}
    assert errors(**tax("US", "MX_RFC", "PNO120315AB1")) == {
        "tax_id_type": "RFC no es un identificador fiscal de Estados Unidos"
    }
    assert errors(**ein("1234")) == {"tax_id": "El número de EIN debe tener 9 caracteres"}
    assert errors(**tax(None, "AR_CUIT", "33693450238")) == {
        "tax_id": "El dígito verificador de CUIT no coincide. Revisa el número."
    }
    # Un país inválido sin número también se rechaza (es un dato mal escrito, aunque luego se ignore).
    assert errors(**tax("ZZ", None, None)) == {"tax_country": "Elige un país de la lista"}
    assert client.post(COMPANIES, json=[1, 2], headers=admin_headers).status_code == 422


def test_the_previous_rfc_field_is_still_accepted_but_never_overrides_the_tax_id(client, admin_headers):
    """`rfc` (obsoleto): la aplicación web anterior en marcha durante un despliegue lo sigue enviando."""
    legacy = create_company(client, admin_headers, rfc="pno-120315-ab1")
    assert triple(legacy.json()["data"]) == ("MX", "MX_RFC", "PNO120315AB1")
    both = create_company(client, admin_headers, admin_email="b@b.com", rfc="ACM010101AB2", **ein())
    assert triple(both.json()["data"]) == ("US", "US_EIN", "123456789") and both.json()["data"]["rfc"] is None


def test_editing_the_tax_id(client, admin_headers):
    company_id = create_company(client, admin_headers).json()["data"]["id"]
    url = f"{COMPANIES}/{company_id}"

    def edit(body: dict, status: int = 200) -> dict:
        response = client.put(url, json=body, headers=admin_headers)
        assert response.status_code == status, response.text
        return response.json()

    changed = edit(ein("98-7654321"))["data"]
    assert triple(changed) == ("US", "US_EIN", "987654321") and changed["rfc"] is None
    assert stored(company_id) == ("US", "US_EIN", "987654321", None)  # la columna anterior se vacía
    # Sin el número, el país y el tipo no cambian nada; lo demás se edita igual.
    assert triple(edit({"tax_country": "FR", "tax_id_type": "FR_SIREN", "name": "Otra"})["data"]) == (
        "US",
        "US_EIN",
        "987654321",
    )
    # Su propio identificador no choca; el de otra empresa sí.
    assert edit(ein("987654321"))["data"]["tax_id"] == "987654321"
    other = create_company(client, admin_headers, admin_email="b@b.com", **ein("111111111")).json()["data"]
    taken = edit(ein("111111111"), status=409)
    assert taken["code"] == "COMPANY_TAX_ID_TAKEN" and taken["errors"][0]["field"] == "tax_id"
    # El identificador viaja completo: el número solo toma los valores de omisión (México y su RFC), no los anteriores.
    assert edit({"tax_id": "123456789"}, status=422)["errors"][0]["field"] == "tax_id"
    back = edit(tax("MX", "MX_RFC", "PNO120315AB1"))["data"]
    assert back["rfc"] == "PNO120315AB1" and stored(company_id)[3] == "PNO120315AB1"
    for blank in (None, "  "):
        assert triple(edit({"tax_id": blank})["data"]) == (None, None, None)
        assert stored(company_id) == (None, None, None, None)
        edit(ein("222222222"))
    assert other["tax_id"] == "111111111"


def test_restoring_a_company_whose_tax_id_another_one_took(client, admin_headers):
    company_id = create_company(client, admin_headers, **ein()).json()["data"]["id"]
    assert client.delete(f"{COMPANIES}/{company_id}", headers=admin_headers).status_code == 200
    clash = create_company(client, admin_headers, admin_email="b@b.com", **ein()).json()["data"]
    refused = client.post(f"{COMPANIES}/{company_id}/restore", headers=admin_headers).json()
    assert refused["code"] == "RESTORE_CONFLICT" and refused["errors"][0]["field"] == "tax_id"
    assert refused["message"] == "No se puede restaurar: otra empresa ya tiene ese identificador fiscal (EIN 123456789)"
    english = client.post(f"{COMPANIES}/{company_id}/restore", headers={**admin_headers, **EN}).json()
    assert english["message"] == "Can't restore: another company already has that tax ID (EIN 123456789)"
    assert client.put(f"{COMPANIES}/{clash['id']}", json=ein("999999999"), headers=admin_headers).status_code == 200
    restored = client.post(f"{COMPANIES}/{company_id}/restore", headers=admin_headers)
    assert restored.status_code == 200 and triple(restored.json()["data"]) == ("US", "US_EIN", "123456789")


def test_search_by_tax_id_in_every_company_list(client, admin_headers):
    company_id = create_company(client, admin_headers, **tax("BR", "BR_CNPJ", "11.222.333/0001-81")).json()["data"]
    found = client.get(COMPANIES, params={"search": "11222333"}, headers=admin_headers).json()["data"]["items"]
    assert [c["id"] for c in found] == [company_id["id"]]
    billing = client.get("/api/admin/billing/companies", params={"search": "0001"}, headers=admin_headers)
    assert [row["company_id"] for row in billing.json()["data"]["items"]] == [company_id["id"]]


# ---------------------------------------------------------------- validación en vivo


def test_live_validation_of_the_tax_id(client, admin_headers, company_headers):
    company_id = create_company(client, admin_headers, **ein()).json()["data"]["id"]

    def check(value: str, related: str | None = None, field: str = "company_tax_id", **extra) -> dict:
        params = {"field": field, "value": value, "related": related, **extra}
        return client.get("/api/validation", params=params, headers=admin_headers).json()["data"]

    for blank in ("", "  ", "-./"):
        result = check(blank, "US:US_EIN")
        assert (result["code"], result["valid"], result["available"]) == ("EMPTY", True, True)
    taken = check("12-3456789", "US:US_EIN")
    assert (taken["code"], taken["normalized"], taken["message"]) == (
        "TAKEN",
        "123456789",
        "Ya existe una empresa con ese identificador fiscal",
    )
    free = check("12-3456789", "US:US_EIN", exclude_id=company_id)
    assert (free["code"], free["message"]) == ("AVAILABLE", "Identificador fiscal disponible")
    assert check("123456789", "FR:FR_SIREN")["code"] == "INVALID_FORMAT"  # Luhn
    assert check("732829320", "FR:FR_SIREN")["code"] == "AVAILABLE"  # otro tipo: otro identificador
    invalid = check("33693450238", "AR:AR_CUIT")
    assert (invalid["code"], invalid["message"]) == (
        "INVALID_FORMAT",
        "El dígito verificador de CUIT no coincide. Revisa el número.",
    )
    assert check("1", "ZZ:")["message"] == "Elige un país de la lista"
    assert check("PNO120315AB1")["code"] == "AVAILABLE"  # sin «país:tipo»: México y su RFC
    assert check("PNO120315AB1", ":MX_RFC")["code"] == "AVAILABLE"
    english = client.get(
        "/api/validation",
        params={"field": "company_tax_id", "value": "12-3456789", "related": "US:US_EIN"},
        headers={**admin_headers, **EN},
    ).json()["data"]
    assert english["message"] == "A company with that tax ID already exists"
    # El campo anterior (solo RFC) ignora «país:tipo»: la aplicación web anterior no lo manda.
    assert check("123456789", "US:US_EIN", field="company_rfc")["code"] == "INVALID_FORMAT"
    assert check("", field="company_rfc")["code"] == "EMPTY"
    # Solo el ADMIN de la plataforma (la pantalla de empresas).
    denied = client.get("/api/validation", params={"field": "company_tax_id", "value": "1"}, headers=company_headers)
    assert denied.status_code == 403 and denied.json()["code"] == "FIELD_NOT_ALLOWED"


def test_live_validation_by_the_realtime_channel(client, admin_headers):
    create_company(client, admin_headers, **ein())
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect("/api/ws/validation") as socket:
        socket.send_json({"type": "auth", "token": token})
        assert socket.receive_json()["code"] == "WS_AUTHENTICATED"
        message = {"type": "validate", "id": "req-tax-0001", "field": "company_tax_id", "value": "12-3456789"}
        socket.send_json({**message, "related": "US:US_EIN"})
        assert socket.receive_json()["code"] == "TAKEN"
        socket.send_json({**message, "related": "FR:FR_SIREN"})
        assert socket.receive_json()["code"] == "INVALID_FORMAT"


# ---------------------------------------------------------------- API de integración


def test_the_integration_api_delivers_the_tax_id_and_keeps_rfc(client, admin_headers):
    body = {"api_enabled": True, **ein()}
    company_id = create_company(client, admin_headers, **body).json()["data"]["id"]
    headers = login(client, "admin@panificadora.com", "Empresa1234")
    data = call(client, new_key(client, headers)["secret"], "/company").json()["data"]
    assert triple(data) == ("US", "US_EIN", "123456789") and data["rfc"] is None
    client.put(f"{COMPANIES}/{company_id}", json=tax("MX", "MX_RFC", "PNO120315AB1"), headers=admin_headers)
    data = call(client, new_key(client, headers, name="Otra")["secret"], "/company").json()["data"]
    assert triple(data) == ("MX", "MX_RFC", "PNO120315AB1") and data["rfc"] == "PNO120315AB1"


def test_the_integration_api_delivers_a_missing_tax_id_as_null(client, company_headers):
    data = call(client, new_key(client, company_headers)["secret"], "/company").json()["data"]
    assert triple(data) == (None, None, None) and data["rfc"] is None


# ---------------------------------------------------------------- la base: migración, restricción y único


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0074", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_rfcs_become_their_tax_id():
    """La sentencia de la migración: cada RFC (también de una eliminada) pasa a ser su identificador; lo demás no se
    toca. En SQLite, sin el esquema (`schema_translate_map` no traduce un texto)."""
    statement = _migration().BACKFILL
    if engine.dialect.name == "sqlite":
        statement = statement.replace("tenancy.", "")
    with SessionLocal() as db:
        legacy = Company(name="Con RFC", rfc="PNO120315AB1", active=True)
        gone = Company(name="Eliminada", rfc="ACM010101AB2", active=True, deleted_at=NOW, deleted_by="admin@x.com")
        bare = Company(name="Sin RFC", active=True)
        kept = Company(name="Ya migrada", active=True)
        kept.set_tax(TaxId("US", "US_EIN", "123456789"))
        db.add_all([legacy, gone, bare, kept])
        db.commit()
        ids = [legacy.id, gone.id, bare.id, kept.id]
        db.execute(text(statement))
        db.commit()
    assert [stored(i) for i in ids] == [
        ("MX", "MX_RFC", "PNO120315AB1", "PNO120315AB1"),
        ("MX", "MX_RFC", "ACM010101AB2", "ACM010101AB2"),
        (None, None, None, None),
        ("US", "US_EIN", "123456789", None),
    ]


def test_the_database_refuses_a_partial_or_repeated_tax_id():
    with SessionLocal() as db:
        db.add(Company(name="A medias", tax_id="123456789", active=True))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        for name in ("Uno", "Dos"):
            company = Company(name=name, active=True)
            company.set_tax(TaxId("US", "US_EIN", "123456789"))
            db.add(company)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        live, deleted = Company(name="Vigente", active=True), Company(name="Eliminada", active=True)
        for company in (live, deleted):
            company.set_tax(TaxId("US", "US_EIN", "123456789"))
        deleted.deleted_at = NOW
        db.add_all([deleted, live])
        db.commit()  # una en «Eliminados» no bloquea su número
        assert len(db.scalars(select(Company.id).where(Company.tax_id == "123456789")).all()) == 1
