"""Lo que se borra DE VERDAD al eliminar a una persona: la excepción del borrado lógico (regla 20 de la raíz).

Decisión del dueño del producto (LFPDPPP: los datos biométricos son datos personales sensibles; regla 13 de la raíz):
eliminar a un empleado, a un validador o las cuentas de una empresa manda su ficha a «Eliminados» (borrado lógico),
pero sus datos biométricos y sus fotos se borran para siempre en ese momento —los objetos del bucket y sus referencias
en la base— y restaurarlo NO los regresa: registra su rostro de nuevo.

- Empleado (`erase_employee_biometrics`): sus plantillas faciales, sus registros faciales (con las fotos de referencia),
  la foto inicial de un registro a medias (su borrador y su plantilla), las huellas perceptuales de sus capturas (llevan
  su embedding) y los fotogramas de evidencia de sus casos de fraude.
  Su estado facial vuelve a "sin registro" con el motivo que verá si lo restauran.
- Empleado (`erase_employee_documents`): sus documentos de identidad del onboarding —archivo cifrado en el bucket y los
  DATOS que el OCR extrajo— se borran también DE VERDAD con la persona (decisión del dueño del producto, 2026-10-07:
  una identificación oficial lleva la foto y los datos de la persona, no es el expediente de una empresa).
  Sus filas, las
  vigentes y las que el propio empleado ya había eliminado de su lista; sus objetos, a la cola del bucket.
  Restaurarlo NO
  los regresa: el empleado los vuelve a subir.
- Cuenta (`erase_account`, `erase_company_accounts`): su foto de perfil (todos los tamaños) y la cuenta recordada en
  los dispositivos.

Todo en la MISMA transacción del borrado lógico: los objetos del bucket pasan a la cola de borrado
(`ops.storage_deletions`) y el mantenimiento (`storage_jobs`) los borra; si el bucket está caído, la cola espera a la
siguiente vuelta (nunca se quedan para siempre) y eliminar responde igual (nada de esto toca la red dentro de la
petición). Lo aprendido del rostro ("Olvidar lo aprendido") y "Quitar foto" siguen siendo borrados reales aparte.
"""

from sqlalchemy.orm import Session

from app.i18n import stored
from app.models import Employee, FaceStatus, User, UserAvatar
from app.repositories.avatar_repository import AvatarRepository
from app.repositories.employee_document_repository import EmployeeDocumentRepository
from app.repositories.face_repository import FaceEmbeddingRepository
from app.repositories.fraud_repository import FraudCaseRepository
from app.repositories.remembered_account_repository import RememberedAccountRepository
from app.repositories.user_repository import UserRepository
from app.services.image_storage import (
    release_company_evidence,
    release_employee_documents,
    release_employee_images,
    release_user_avatars,
)

#: Lo que ve el empleado al registrar su rostro si lo restauran (se traduce al leerse: `app/i18n/stored.py`).
FACE_ERASED = stored("FACE_ERASED_ON_DELETE")


def erase_employee_biometrics(db: Session, employee: Employee) -> None:
    """Los datos biométricos y las fotos del registro facial del empleado, para siempre (sus objetos a la cola)."""
    release_employee_images(db, employee.company_id, employee.id)
    FaceEmbeddingRepository(db).erase_employee(employee.company_id, employee.id)
    employee.face_status = FaceStatus.NOT_ENROLLED
    employee.face_rejection_reason = FACE_ERASED


def erase_employee_documents(db: Session, employee: Employee) -> None:
    """Los documentos de identidad del empleado (archivo cifrado + datos extraídos), para siempre (regla 13, LFPDPPP:
    una identificación oficial lleva la foto y los datos de la persona). Primero sus objetos del bucket a la cola de
    borrado (también los de los que él mismo ya había eliminado) y después sus filas, en la MISMA transacción del
    borrado lógico del empleado; restaurarlo NO los regresa (como la biometría). Nunca quedan ni una fila huérfana ni un
    objeto sin dueño."""
    release_employee_documents(db, employee.company_id, employee.id)
    EmployeeDocumentRepository(db, employee.company_id).erase_employee(employee.id)


def erase_account(db: Session, user: User) -> None:
    """La foto de perfil de una cuenta que se elimina y su cuenta recordada en los dispositivos."""
    release_user_avatars(db, UserAvatar.user_id == user.id)
    AvatarRepository(db).remove(user.id)
    RememberedAccountRepository(db).forget_account(user.id)
    user.avatar_version = None


def erase_company_accounts(db: Session, company_id: int) -> None:
    """Las fotos de perfil de las cuentas de una empresa que se elimina (administradores y validadores), su cuenta
    recordada y la evidencia de sus casos de fraude (fotogramas de rostros), en unas cuantas sentencias."""
    accounts = AvatarRepository.of_company_accounts(company_id)
    release_user_avatars(db, accounts)
    AvatarRepository(db).remove_matching(accounts)
    RememberedAccountRepository(db).forget_company_accounts(company_id)
    UserRepository(db).clear_company_avatars(company_id)
    release_company_evidence(db, company_id)
    FraudCaseRepository(db).delete_company_evidence(company_id)
