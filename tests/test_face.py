"""Auto-registro facial, validación por COMPANY, accesorios, prueba de vida y verificación."""

from tests.conftest import (
    approved_employee,
    create_company,
    create_employee,
    login,
    submit_enrollment,
    turn_files,
)


def _verify(client, headers, *, frontal=(b"face:juan", b"face:juan"), turn_person="juan", wrong_turn=False):
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    assert challenge["liveness_required"] is True
    files = [("images", (f"c{i}.jpg", f, "image/jpeg")) for i, f in enumerate(frontal)]
    files += turn_files(challenge, turn_person, wrong=wrong_turn)
    return client.post(
        "/api/verification/face", data={"challenge_id": challenge["challenge_id"]}, files=files, headers=headers
    )


# ---------------- Flujo de registro y validación ----------------


def test_company_creates_employee_without_face(client, company_headers):
    body = create_employee(client, company_headers).json()["data"]
    assert body["face_status"] == "NOT_ENROLLED" and body["has_face"] is False


def test_full_enrollment_review_flow(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "NOT_ENROLLED"

    # No puede verificarse antes de registrar/aprobar.
    assert _verify(client, headers).json()["code"] == "FACE_NOT_APPROVED"

    submitted = submit_enrollment(client, headers)
    assert submitted.status_code == 201
    assert submitted.json()["data"]["face_status"] == "PENDING_REVIEW"
    assert submit_enrollment(client, headers).json()["code"] == "ENROLLMENT_PENDING"

    # Pendiente: sigue sin poder verificarse ni generar su QR.
    assert _verify(client, headers).status_code == 403
    assert client.post("/api/users/me/qr", headers=headers).status_code == 403

    pending = client.get("/api/enrollments", headers=company_headers).json()["data"]
    assert pending["total"] == 1
    enrollment_id = pending["items"][0]["id"]
    detail = client.get(f"/api/enrollments/{enrollment_id}", headers=company_headers).json()["data"]
    assert detail["photo"].startswith("data:image/") and detail["liveness_passed"] is True
    assert detail["full_name"] == "Juan Pérez"

    approved = client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers).json()["data"]
    assert approved["status"] == "APPROVED" and approved["reviewed_by"] == "admin@empresa.com"
    assert client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers).status_code == 409

    assert _verify(client, headers).json()["data"]["verified"] is True


def test_rejection_requires_new_enrollment(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    enrollment_id = submit_enrollment(client, headers).json()["data"]["enrollment_id"]

    assert (
        client.post(
            f"/api/enrollments/{enrollment_id}/reject", json={"reason": ""}, headers=company_headers
        ).status_code
        == 422
    )
    rejected = client.post(
        f"/api/enrollments/{enrollment_id}/reject",
        json={"reason": "La foto no corresponde al empleado"},
        headers=company_headers,
    ).json()["data"]
    assert rejected["status"] == "REJECTED" and rejected["photo"] is None

    me = client.get("/api/users/me", headers=headers).json()["data"]["employee"]
    assert me["face_status"] == "REJECTED"
    assert me["face_rejection_reason"] == "La foto no corresponde al empleado"
    assert _verify(client, headers).status_code == 403
    # Puede volver a registrarse.
    assert submit_enrollment(client, headers).status_code == 201


def test_employee_cannot_review(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    enrollment_id = submit_enrollment(client, headers).json()["data"]["enrollment_id"]
    assert client.get("/api/enrollments", headers=headers).status_code == 403
    assert client.post(f"/api/enrollments/{enrollment_id}/approve", headers=headers).status_code == 403


def test_company_can_reset_face(client, company_headers):
    headers = approved_employee(client, company_headers)
    emp_id = client.get("/api/users/me", headers=headers).json()["data"]["employee"]["id"]
    reset = client.post(f"/api/employees/{emp_id}/face/reset", headers=company_headers).json()["data"]
    assert reset["face_status"] == "NOT_ENROLLED" and reset["has_face"] is False
    assert _verify(client, headers).status_code == 403


def test_company_requests_reverification_from_all_employees(client, admin_headers, company_headers):
    """Toda la empresa a la vez: quien tenía registro (aprobado, en validación o rechazado) lo pierde
    y lo vuelve a hacer; quien aún no se registraba no cambia. Las demás empresas no se tocan."""
    juan = approved_employee(client, company_headers)  # aprobado
    create_employee(client, company_headers, number="EMP-2", email="ana@empresa.com")
    ana = login(client, "ana@empresa.com", "Empleado123")
    assert submit_enrollment(client, ana, frontal=(b"face:ana",) * 3, turn_person="ana").status_code == 201
    create_employee(client, company_headers, number="EMP-3", email="luis@empresa.com")  # sin registro
    create_company(client, admin_headers)
    other_headers = login(client, "admin@panificadora.com", "Empresa1234")
    approved_employee(client, other_headers, number="PAN-1", email="eva@panificadora.com")

    url = "/api/employees/face/reset"
    assert client.post(url, headers=juan).status_code == 403  # solo la empresa
    done = client.post(url, json={"reason": "Cambiamos las cámaras del acceso"}, headers=company_headers)
    assert done.status_code == 200 and done.json()["code"] == "IDENTITY_REVERIFY_REQUESTED_ALL"
    assert done.json()["data"]["employees"] == 2

    people = client.get("/api/employees", headers=company_headers).json()["data"]["items"]
    by_number = {p["employee_number"]: p for p in people}
    assert {p["face_status"] for p in people} == {"NOT_ENROLLED"} and not any(p["has_face"] for p in people)
    assert by_number["EMP-001"]["face_rejection_reason"] == "Cambiamos las cámaras del acceso"
    assert by_number["EMP-3"]["face_rejection_reason"] is None  # no tenía registro: no cambió
    pending = client.get("/api/enrollments", params={"status": "PENDING"}, headers=company_headers)
    assert pending.json()["data"]["total"] == 0  # lo que esperaba validación se rechazó
    assert _verify(client, juan).status_code == 403  # debe registrarse de nuevo

    other = client.get("/api/employees", headers=other_headers).json()["data"]["items"]
    assert [p["face_status"] for p in other] == ["APPROVED"] and other[0]["face_samples"] == 3


# ---------------- Validaciones del registro ----------------


def test_enrollment_rejects_glasses_inconsistency_and_bad_liveness(client, company_headers):
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    response = submit_enrollment(client, headers, frontal=(b"glasses:juan", b"face:juan", b"glasses:juan"))
    assert response.status_code == 422 and response.json()["code"] == "ACCESSORIES_DETECTED"
    assert response.json()["message"] == "Quítate los lentes para continuar"
    response = submit_enrollment(client, headers, frontal=(b"face:juan", b"noface"))
    assert response.json()["code"] == "NO_FACE" and response.json()["message"].startswith("Foto 2:")
    response = submit_enrollment(client, headers, frontal=(b"face:juan", b"face:otra"))
    assert response.json()["code"] == "ENROLL_INCONSISTENT"
    response = submit_enrollment(client, headers, turn_person="otra")
    assert response.json()["code"] == "LIVENESS_MISMATCH"
    assert client.get("/api/users/me", headers=headers).json()["data"]["employee"]["face_status"] == "NOT_ENROLLED"


def test_headwear_exemption(client, company_headers):
    create_employee(client, company_headers, headwear_exempt=True)
    headers = login(client, "juan@empresa.com", "Empleado123")
    assert submit_enrollment(client, headers, frontal=(b"hat:juan",)).status_code == 201


# ---------------- Verificación ----------------


def test_face_verification_rejects_other_person(client, company_headers):
    headers = approved_employee(client, company_headers)
    response = _verify(client, headers, frontal=(b"face:intruso", b"face:intruso"), turn_person="intruso")
    assert response.json()["data"]["verified"] is False and response.json()["message"] == "Rostro no reconocido"
    assert _verify(client, headers, frontal=(b"face:juan", b"face:intruso")).json()["data"]["verified"] is False


def test_face_verification_accessories_block(client, company_headers):
    headers = approved_employee(client, company_headers)
    response = _verify(client, headers, frontal=(b"glasses:juan",))
    assert response.status_code == 422 and response.json()["message"] == "Quítate los lentes para continuar"
    check = client.post("/api/face/check", files={"image": ("c.jpg", b"hat:juan", "image/jpeg")}, headers=headers)
    assert check.status_code == 422 and check.json()["errors"][0]["details"]["accessories"] == ["HEADWEAR"]


def test_liveness_wrong_direction_swapped_face_and_replay(client, company_headers):
    headers = approved_employee(client, company_headers)
    assert "movimiento solicitado" in _verify(client, headers, wrong_turn=True).json()["message"]
    assert _verify(client, headers, turn_person="otra-persona").json()["data"]["verified"] is False

    files = [("images", ("c.jpg", b"face:juan", "image/jpeg"))]
    assert client.post("/api/verification/face", files=files, headers=headers).json()["code"] == "LIVENESS_REQUIRED"
    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files += turn_files(challenge)
    data = {"challenge_id": challenge["challenge_id"]}
    assert (
        client.post("/api/verification/face", data=data, files=files, headers=headers).json()["data"]["verified"]
        is True
    )
    replay = client.post("/api/verification/face", data=data, files=files, headers=headers)
    assert replay.json()["code"] == "CHALLENGE_INVALID"


def test_employee_generates_dynamic_qr_only_after_approval(client, company_headers):
    from tests.conftest import create_employee, login, submit_enrollment

    assert create_employee(client, company_headers).status_code == 201
    headers = login(client, "juan@empresa.com", "Empleado123")
    denied = client.post("/api/users/me/qr", headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "FACE_NOT_APPROVED"

    enrollment_id = submit_enrollment(client, headers).json()["data"]["enrollment_id"]
    assert client.post("/api/users/me/qr", headers=headers).status_code == 403  # en validación
    client.post(f"/api/enrollments/{enrollment_id}/approve", headers=company_headers)

    response = client.post("/api/users/me/qr", headers=headers)
    assert response.status_code == 201 and response.json()["code"] == "MY_QR"
    qr = response.json()["data"]
    assert qr["content"].startswith("TCQR2:") and qr["employee_number"] == "EMP-001"
    assert qr["lifetime_seconds"] == 30 and "image_base64" not in qr  # el teléfono dibuja el código
    # COMPANY no genera QR de empleados (ni los ve: son del teléfono del empleado).
    assert client.post("/api/users/me/qr", headers=company_headers).status_code == 403


# ---------------- Consenso de accesorios (robustez ante falsos positivos) ----------------


def test_isolated_accessory_false_positive_does_not_block(client, company_headers):
    """Un falso positivo en 1 de 3 capturas no bloquea; en la mayoría sí."""
    headers = approved_employee(client, company_headers)
    assert _verify(client, headers, frontal=(b"face:juan", b"mask:juan", b"face:juan")).json()["data"]["verified"]
    blocked = _verify(client, headers, frontal=(b"mask:juan", b"mask:juan", b"face:juan"))
    assert blocked.status_code == 422 and blocked.json()["errors"][0]["details"] == {"accessories": ["MASK"]}

    files = [
        ("images", (f"c{i}.jpg", img, "image/jpeg")) for i, img in enumerate([b"face:juan", b"mask:juan", b"face:juan"])
    ]
    assert client.post("/api/face/check", files=files, headers=headers).status_code == 200
    files = [("images", (f"c{i}.jpg", b"mask:juan", "image/jpeg")) for i in range(2)]
    assert client.post("/api/face/check", files=files, headers=headers).json()["code"] == "ACCESSORIES_DETECTED"
    assert client.post("/api/face/check", headers=headers).json()["code"] == "IMAGE_REQUIRED"


def test_enrollment_accessory_review_flags_for_admin(client, company_headers):
    """Si el empleado no usa el accesorio detectado, envía a revisión y el admin lo ve marcado."""
    create_employee(client, company_headers)
    headers = login(client, "juan@empresa.com", "Empleado123")
    frontal = (b"mask:juan", b"mask:juan", b"face:juan")
    assert submit_enrollment(client, headers, frontal=frontal).json()["code"] == "ACCESSORIES_DETECTED"

    challenge = client.post("/api/face/challenge", headers=headers).json()["data"]
    files = [("images", (f"f{i}.jpg", f, "image/jpeg")) for i, f in enumerate(frontal)]
    files += turn_files(challenge)
    data = {"challenge_id": challenge["challenge_id"], "accessory_review": "true"}
    submitted = client.post("/api/enrollment/face", data=data, files=files, headers=headers)
    assert submitted.status_code == 201, submitted.text
    enrollment_id = submitted.json()["data"]["enrollment_id"]
    detail = client.get(f"/api/enrollments/{enrollment_id}", headers=company_headers).json()["data"]
    assert detail["flagged_accessories"] == ["MASK"]
