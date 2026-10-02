"""Comandos de administración.

python -m app.cli create-admin --email admin@plataforma.com
python -m app.cli create-company --email admin@empresa.com
python -m app.cli create-company --email admin@empresa.com --password 'Admin1234'
"""

import argparse
import getpass
import sys

from app.core.database import SessionLocal
from app.services.bootstrap import UserFactory, create_admin_user, create_company_user


def _create_admin(args: argparse.Namespace) -> int:
    return _create(args, "ADMIN", create_admin_user)


def _create_company(args: argparse.Namespace) -> int:
    return _create(args, "COMPANY", create_company_user)


def _create(args: argparse.Namespace, label: str, create: UserFactory) -> int:
    password = args.password or getpass.getpass("Contraseña: ")
    if not args.password and password != getpass.getpass("Confirmar contraseña: "):
        print("Las contraseñas no coinciden", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        try:
            user = create(db, args.email.lower(), password)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    print(f"Usuario {label} creado: id={user.id} email={user.email}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Crear un administrador de la plataforma (ADMIN)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", help="Si se omite se solicita de forma interactiva")
    admin.set_defaults(func=_create_admin)
    create = sub.add_parser("create-company", help="Crear un administrador de la primera empresa (COMPANY)")
    create.add_argument("--email", required=True)
    create.add_argument("--password", help="Si se omite se solicita de forma interactiva")
    create.set_defaults(func=_create_company)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
