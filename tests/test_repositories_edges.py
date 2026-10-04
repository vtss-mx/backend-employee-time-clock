"""Consultas de los repositorios en sus casos límite: páginas vacías, filtros opcionales y agregados
de una empresa sin datos (una consulta por página, nunca una por elemento)."""

from app.core.database import SessionLocal
from app.models import EnrollmentStatus
from app.repositories.enrollment_repository import FaceEnrollmentRepository
from app.repositories.user_repository import UserRepository
from tests.conftest import COMPANY_EMAIL, approved_employee, create_employee, login, submit_enrollment

LEARNING = "/api/employees/face/learning"


def test_an_empty_department_list_has_no_counts_to_load(client, company_headers):
    listed = client.get("/api/departments", headers=company_headers)
    assert listed.status_code == 200
    assert (listed.json()["data"]["items"], listed.json()["data"]["total"]) == ([], 0)


def test_employees_are_filtered_by_their_status(client, company_headers):
    create_employee(client, company_headers)
    gone = create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com").json()["data"]
    client.patch(f"/api/employees/{gone['id']}/status", json={"active": False}, headers=company_headers)

    def numbers(active: str) -> list[str]:
        page = client.get("/api/employees", params={"active": active}, headers=company_headers).json()["data"]
        return [e["employee_number"] for e in page["items"]]

    assert numbers("false") == ["EMP-002"]
    assert numbers("true") == ["EMP-001"]


def test_enrollments_without_a_status_filter_come_most_recent_first(client, company_headers):
    """Sin filtro de estado (todo el historial): el más reciente primero, con el desempate por id."""
    approved_employee(client, company_headers)
    create_employee(client, company_headers, number="EMP-002", email="ana@empresa.com")
    assert submit_enrollment(client, login(client, "ana@empresa.com", "Empleado123")).status_code == 201
    with SessionLocal() as db:
        company_id = UserRepository(db).get_by_email(COMPANY_EMAIL).company_id
        items, total = FaceEnrollmentRepository(db, company_id).search(status=None, offset=0, limit=10)
    assert total == 2
    assert [e.status for e in items] == [EnrollmentStatus.PENDING, EnrollmentStatus.APPROVED]


def test_the_learning_summary_of_a_company_without_samples_and_with_approved_faces(client, company_headers):
    empty = client.get(LEARNING, headers=company_headers).json()["data"]
    assert (empty["approved_employees"], empty["learned_samples"], empty["last_learned_at"]) == (0, 0, None)

    approved_employee(client, company_headers)
    summary = client.get(LEARNING, headers=company_headers).json()["data"]
    assert summary["approved_employees"] == 1
    # Solo el registro aprobado (el ancla): aún no aprende ni decidió identificaciones.
    assert (summary["employees_learning"], summary["learned_samples"], summary["identifications"]) == (0, 0, 0)
