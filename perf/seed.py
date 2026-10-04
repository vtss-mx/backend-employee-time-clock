"""Siembra la base de las PRUEBAS DE CARGA: N empleados en la empresa de prueba.

Solo corre dentro del entorno aislado de perf/run.sh (PERF_ENVIRONMENT=1): jamás en una base real.
La contraseña se calcula una vez (Argon2 es lento a propósito) y se reutiliza.
"""

import os
import sys
from datetime import date

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.passwords import hash_password
from app.models import Company, Department, Employee, User, UserRole


def main(count: int) -> None:
    if os.environ.get("PERF_ENVIRONMENT") != "1":
        sys.exit("seed.py solo corre en el entorno de pruebas de carga (PERF_ENVIRONMENT=1)")
    with SessionLocal() as db:
        company = db.scalars(select(Company).order_by(Company.id)).first()
        if company is None:
            sys.exit("No existe la empresa de prueba (FIRST_COMPANY_EMAIL)")
        departments = [Department(company_id=company.id, name=f"Área {n}") for n in range(1, 11)]
        db.add_all(departments)
        db.flush()
        start = db.scalar(select(func.count()).select_from(Employee).where(Employee.company_id == company.id)) or 0
        secret = hash_password("Empleado123")
        for i in range(start, count):
            user = User(
                email=f"empleado{i}@carga-timeclock.com",
                password_hash=secret,
                role=UserRole.EMPLOYEE,
                phone=f"+5266{i:08d}",
            )
            db.add(user)
            db.flush()
            db.add(
                Employee(
                    user_id=user.id,
                    company_id=company.id,
                    employee_number=f"PERF-{i:05d}",
                    first_name=f"Nombre{i}",
                    last_name=f"Apellido{i % 97}",
                    birth_date=date(1990, 1, 1),
                    department_id=departments[i % len(departments)].id,
                )
            )
        db.commit()
        print(f"Empleados de prueba: {count} · departamentos: {len(departments)}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500)
