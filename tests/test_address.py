"""Domicilio compartido (validadores y sitios de trabajo): colonia obligatoria al guardar, referencias
opcionales en varios renglones y lectura de los domicilios guardados antes de pedirlas (migración 0049),
también en la API de integración."""

import pytest

from app.core.database import SessionLocal
from app.models import Validator, WorkSite
from app.schemas.address import ADDRESS_FIELDS, Address
from tests.test_api_keys import call, new_key
from tests.test_shifts import SITES, address, create_site
from tests.test_validators import ADDRESS, URL, create_validator

NOTES = "  Entre   Juárez y Morelos \n\n  Frente a la plaza  \r\n"


def test_the_address_has_the_fields_in_the_order_they_are_captured():
    """El orden del formulario (decisión del dueño del producto) es el de la API."""
    assert ADDRESS_FIELDS == (
        "country_code",
        "state",
        "municipality",
        "city",
        "neighborhood",
        "postal_code",
        "street",
        "exterior_number",
        "interior_number",
        "reference_notes",
        "latitude",
        "longitude",
    )
    assert "neighborhood" not in Address.model_json_schema()["required"]  # nula en lo guardado antes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"neighborhood": None}, "La colonia es obligatoria"),
        ({"neighborhood": "   "}, "La colonia es obligatoria"),
        ({"neighborhood": " C "}, "La colonia es muy corta"),
        ({"neighborhood": "x" * 121}, None),
        ({"reference_notes": "x" * 301}, None),
    ],
)
def test_the_neighborhood_is_required_and_the_references_are_limited(client, company_headers, change, message):
    response = create_validator(client, company_headers, address={**ADDRESS, **change})
    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["field"] == f"address.{next(iter(change))}"
    if message:
        assert error["message"] == message


def test_a_body_without_the_neighborhood_says_what_is_missing(client, company_headers):
    """Sin la llave (un cliente anterior): el mismo mensaje en español, no un «Field required»."""
    without = {key: value for key, value in ADDRESS.items() if key != "neighborhood"}
    response = create_validator(client, company_headers, address=without)
    assert response.status_code == 422 and response.json()["message"] == "La colonia es obligatoria"
    site = client.post(SITES, json={"name": "Planta", "address": without, "radius_m": 100}, headers=company_headers)
    assert site.status_code == 422 and site.json()["errors"][0]["field"] == "address.neighborhood"


def test_neighborhood_and_references_are_normalized_and_returned(client, company_headers):
    body = {**ADDRESS, "neighborhood": "  Centro   Norte ", "reference_notes": NOTES}
    created = create_validator(client, company_headers, address=body)
    assert created.status_code == 201, created.text
    saved = created.json()["data"]["address"]
    # Cada indicación en su renglón; sin espacios de sobra ni renglones vacíos.
    assert saved["neighborhood"] == "Centro Norte"
    assert saved["reference_notes"] == "Entre Juárez y Morelos\nFrente a la plaza"
    listed = client.get(URL, headers=company_headers).json()["data"]["items"][0]["address"]
    assert listed["neighborhood"] == "Centro Norte" and listed["reference_notes"] == saved["reference_notes"]

    blank = create_validator(
        client, company_headers, email="otro@empresa.com", address={**ADDRESS, "reference_notes": " \n "}
    )
    assert blank.json()["data"]["address"]["reference_notes"] is None
    omitted = create_validator(client, company_headers, email="b@empresa.com", address=ADDRESS).json()["data"]
    assert omitted["address"]["reference_notes"] is None  # opcionales: sin la llave también


def test_a_site_keeps_its_neighborhood_and_references(client, company_headers):
    site = create_site(client, company_headers)
    assert site["address"]["neighborhood"] == "Centro" and site["address"]["reference_notes"] is None
    url = f"{SITES}/{site['id']}"
    changed = {**address(), "neighborhood": "Pitic", "reference_notes": "Junto al parque"}
    body = {"name": "Planta Norte", "address": changed, "radius_m": 100}
    updated = client.put(url, json=body, headers=company_headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["address"]["neighborhood"] == "Pitic"
    assert client.get(url, headers=company_headers).json()["data"]["address"]["reference_notes"] == "Junto al parque"


def test_an_address_saved_before_the_neighborhood_reads_fine_and_asks_for_it_when_edited(client, company_headers):
    created = create_validator(client, company_headers).json()["data"]
    site = create_site(client, company_headers)
    with SessionLocal() as db:
        for legacy in (db.get(Validator, created["id"]), db.get(WorkSite, site["id"])):
            assert legacy is not None
            legacy.neighborhood = legacy.reference_notes = None
        db.commit()
    url = f"{URL}/{created['id']}"
    read = client.get(url, headers=company_headers).json()["data"]["address"]
    assert read["neighborhood"] is None and read["reference_notes"] is None and read["street"] == "Calle Dr. Paliza"
    site_read = client.get(f"{SITES}/{site['id']}", headers=company_headers).json()["data"]["address"]
    assert site_read["neighborhood"] is None and site_read["city"] == "Hermosillo"
    # Editarlo pide la colonia (no se inventa una).
    missing = client.put(url, json={"address": {**ADDRESS, "neighborhood": None}}, headers=company_headers)
    assert missing.status_code == 422 and missing.json()["errors"][0]["field"] == "address.neighborhood"
    assert client.put(url, json={"address": ADDRESS}, headers=company_headers).status_code == 200


def test_the_integration_api_returns_the_new_fields_and_keeps_the_previous_ones(client, company_headers):
    """Compatible hacia atrás: los campos anteriores siguen igual y los nuevos se agregan (nulos si faltan)."""
    create_validator(client, company_headers, address={**ADDRESS, "reference_notes": "Puerta 2"})
    secret = new_key(client, company_headers, scopes=["VALIDATORS_READ"])["secret"]
    item = call(client, secret, "/validators").json()["data"]["items"][0]["address"]
    assert set(item) == set(ADDRESS_FIELDS)
    assert {key: item[key] for key in ADDRESS} == ADDRESS
    assert item["reference_notes"] == "Puerta 2"
