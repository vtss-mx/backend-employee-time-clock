"""Listados paginados: mismo contrato (items, total, page, size), 10 por página por omisión y un
máximo de 50 (lo que ofrece el paginador de la webapp)."""

import pytest

from tests.conftest import approved_employee, create_company, qr_content
from tests.test_validators import URL as VALIDATORS
from tests.test_validators import create_validator, validator_headers

COMPANY_LISTS = ["/api/employees", "/api/validators", "/api/enrollments"]


def _check_limits(client, headers, url: str) -> None:
    body = client.get(url, headers=headers).json()["data"]
    assert (body["page"], body["size"]) == (1, 10) and {"items", "total"} <= body.keys()
    assert client.get(url, params={"size": 50}, headers=headers).status_code == 200
    for params in ({"size": 51}, {"size": 0}, {"page": 0}):
        assert client.get(url, params=params, headers=headers).status_code == 422, params


@pytest.mark.parametrize("url", COMPANY_LISTS)
def test_company_lists_share_defaults_and_limits(client, company_headers, url):
    _check_limits(client, company_headers, url)


def test_admin_companies_list_shares_defaults_and_limits(client, admin_headers):
    _check_limits(client, admin_headers, "/api/admin/companies")


def test_validators_are_paginated_by_name(client, company_headers):
    for i, name in enumerate(["Comedor", "Acceso norte", "Bodega"]):
        assert create_validator(client, company_headers, email=f"v{i}@empresa.com", name=name).status_code == 201

    first = client.get(VALIDATORS, params={"size": 2}, headers=company_headers).json()["data"]
    assert [v["name"] for v in first["items"]] == ["Acceso norte", "Bodega"]
    assert (first["total"], first["page"], first["size"]) == (3, 1, 2)
    second = client.get(VALIDATORS, params={"size": 2, "page": 2}, headers=company_headers).json()["data"]
    assert [v["name"] for v in second["items"]] == ["Comedor"]
    beyond = client.get(VALIDATORS, params={"size": 2, "page": 3}, headers=company_headers).json()["data"]
    assert beyond["items"] == [] and beyond["total"] == 3


def test_verification_history_is_paginated_newest_first(client, company_headers):
    headers = approved_employee(client, company_headers)
    employee_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    checkpoint = validator_headers(client, company_headers, mode="QR")
    for _ in range(3):  # cada identificación con un QR nuevo (un QR sirve una sola vez)
        body = {"qr_content": qr_content(employee_id)}
        assert client.post("/api/checkpoint/identify/qr", json=body, headers=checkpoint).status_code == 200

    url = f"/api/employees/{employee_id}/verifications"
    everything = client.get(url, params={"size": 50}, headers=company_headers).json()["data"]
    logs = everything["items"]
    assert everything["total"] == len(logs) >= 3
    assert [log["created_at"] for log in logs] == sorted((log["created_at"] for log in logs), reverse=True)

    first = client.get(url, params={"size": 2}, headers=company_headers).json()["data"]
    second = client.get(url, params={"size": 2, "page": 2}, headers=company_headers).json()["data"]
    assert first["items"] + second["items"] == logs[:4]
    assert client.get("/api/employees/999999/verifications", headers=company_headers).status_code == 404


def test_company_admins_are_paginated(client, admin_headers):
    company = create_company(client, admin_headers).json()["data"]
    url = f"/api/admin/companies/{company['id']}/admins"
    for i in range(2):
        added = client.post(
            url, json={"admin_email": f"rh{i}@pan.com", "admin_password": "Recursos123"}, headers=admin_headers
        )
        assert added.status_code == 201, added.text
    first = client.get(url, params={"size": 2}, headers=admin_headers).json()["data"]
    assert first["total"] == 3 and [a["email"] for a in first["items"]] == ["admin@panificadora.com", "rh0@pan.com"]
    second = client.get(url, params={"size": 2, "page": 2}, headers=admin_headers).json()["data"]
    assert [a["email"] for a in second["items"]] == ["rh1@pan.com"]
    assert client.get("/api/admin/companies/999999/admins", headers=admin_headers).status_code == 404
    _check_limits(client, admin_headers, url)
