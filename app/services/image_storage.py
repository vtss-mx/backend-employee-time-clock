"""Imágenes y archivos de la plataforma: CIFRADOS en el bucket privado, nunca en la base de datos.

Decisión del dueño del producto: "por ninguna razón se guardan imágenes en nuestra base de datos". Todo
proceso que recibe una imagen o un archivo (hoy: la foto de referencia del registro facial y la foto inicial de su
borrador, el comprobante de un pago, la foto de perfil de una persona, la evidencia de un caso de fraude, los documentos
de una empresa y los videos de la verificación por voz) la cifra con la llave de la plataforma
(`DATA_ENCRYPTION_KEY`) y la sube a `gs://GCS_BUCKET/<entorno>/companies/<empresa>/...` (lo de una empresa)
o `.../people/users/<cuenta>/...` (lo de la persona) DURANTE la petición; la BD solo guarda la referencia
(nombre del objeto, tipo, tamaño, SHA-256 del objeto cifrado y cuándo se subió). El bucket es privado y
nadie descarga de él directamente: solo el backend lee, con los permisos de cada pantalla, y entrega el
contenido por su API (nunca URLs públicas ni firmadas).

- **Guardar** (`store`): primero se inserta la fila (para tener su id, parte del nombre), después se sube
  con la condición "solo si no existe" y se verifica lo que reporta el bucket (tamaño, MD5, SHA-256), y
  la referencia se confirma junto con la fila. Si el bucket no está configurado o no responde, la
  petición entera falla con 503 `STORAGE_UNAVAILABLE` (reintentable): nada queda a medias. Si la
  transacción no llega a confirmarse, el objeto se borra en ese momento (o, si el bucket tampoco
  responde, queda en la cola de borrado del mantenimiento): un objeto nunca se queda sin dueño.
- **Leer** (`read`): se descarga con tiempo límite, se verifica su SHA-256 y se descifra en memoria.
- **Dejar de conservar** (`forget`, `release`): el objeto pasa a la cola de borrado en la misma
  transacción (registro rechazado, empleado borrado) y el mantenimiento lo borra del bucket.

Ningún byte de una imagen vive en la BD: las de antes de esta decisión se llevaron al bucket y su
almacenamiento legado se borró (migración `0053`). Un tipo de imagen nuevo es UNA entrada en
`STORED_IMAGES` (tabla, columnas de la referencia y ruta), nunca un camino aparte.
"""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, event
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import settings
from app.core.crypto import encrypt_bytes, try_decrypt
from app.core.database import SessionLocal
from app.core.exceptions import ServiceUnavailableError
from app.core.object_storage import (
    ObjectExists,
    ObjectInfo,
    ObjectStorage,
    StorageError,
    content_md5,
    get_storage,
)
from app.models import (
    CompanyDocument,
    EmployeeDocument,
    EnrollmentVoiceAnswer,
    FaceEnrollment,
    FaceEnrollmentDraft,
    FraudEvidence,
    Payment,
    UserAvatar,
)
from app.repositories.storage_repository import ImageRef, StorageRepository, StoredImage

logger = logging.getLogger(__name__)

#: Objetos subidos en la transacción en curso de una sesión (se borran si no se confirma).
_UPLOADED = "image_storage.uploaded"
#: Extensión del objeto según el tipo de contenido (el objeto en sí siempre es `application/octet-stream`).
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
    # Documentos de la empresa (`app/services/document_files.py`)
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/xml": "xml",
    # Clips de la verificación por voz y video del registro facial (`app/speech/audio.py`: el formato se reconoce por
    # su contenido; WebM en Chrome y Firefox, MP4 en Safari).
    "video/webm": "webm",
    "video/mp4": "mp4",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
}


class VerificationFailed(StorageError):
    """Lo que reporta el bucket no es exactamente lo que se subió (o lo que se guardó)."""


class ImageUnreadable(StorageError):
    """La imagen existe pero no se puede descifrar (dañada o de una llave que ya no se tiene)."""


def extension(content_type: str | None) -> str:
    return _EXTENSIONS.get(content_type or "", "bin")


def image_type(data: bytes) -> str:
    """El tipo de una captura de la cámara por su contenido (PNG, WebP o, si no, JPEG): las fotos del registro facial,
    su foto inicial y la evidencia de un caso. Un solo lugar (antes, una copia en cada servicio)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _face_photo_path(ref: ImageRef) -> str:
    (employee_id,) = ref.extra
    return (
        f"companies/{ref.company_id}/employees/{employee_id}/face-enrollments/"
        f"{ref.key}.{extension(ref.content_type)}.enc"
    )


def _receipt_path(ref: ImageRef) -> str:
    return f"companies/{ref.company_id}/billing/payments/{ref.key}/receipt.{extension(ref.content_type)}.enc"


#: Foto de referencia de un registro facial. Un registro rechazado no conserva su foto (`forget`).
FACE_ENROLLMENT_PHOTOS = StoredImage(
    kind="face-enrollment",
    label="STORAGE_FACE_ENROLLMENT_PHOTOS",
    model=FaceEnrollment,
    key=FaceEnrollment.id,
    company=FaceEnrollment.company_id,
    content_type=FaceEnrollment.photo_content_type,
    object_name=FaceEnrollment.photo_object,
    size=FaceEnrollment.photo_size,
    sha256=FaceEnrollment.photo_sha256,
    uploaded_at=FaceEnrollment.photo_uploaded_at,
    extra=(FaceEnrollment.employee_id,),
    path=_face_photo_path,
)


def _draft_photo_path(ref: ImageRef) -> str:
    (employee_id,) = ref.extra
    return (
        f"companies/{ref.company_id}/employees/{employee_id}/face-enrollments/drafts/"
        f"{ref.key}.{extension(ref.content_type)}.enc"
    )


#: La foto inicial del registro facial del propio empleado (paso 1 de 3; decisión del dueño, 2026-10-07): el BORRADOR
#: del registro, cifrado como las demás. Vive horas (`FACE_ENROLLMENT_DRAFT_HOURS`): sale del bucket al reemplazarlo
#: («Repetir foto»), al vencer (depuración) y al eliminar a la persona; al aceptar las capturas del paso 2, su objeto
#: pasa a ser la foto de referencia del registro (`FACE_ENROLLMENT_PHOTOS` lo referencia con el mismo nombre: no se
#: vuelve a subir). Mientras es borrador nadie la ve: solo el servidor la compara.
FACE_ENROLLMENT_DRAFT_PHOTOS = StoredImage(
    kind="face-enrollment-draft",
    label="STORAGE_FACE_ENROLLMENT_DRAFT_PHOTOS",
    model=FaceEnrollmentDraft,
    key=FaceEnrollmentDraft.id,
    company=FaceEnrollmentDraft.company_id,
    content_type=FaceEnrollmentDraft.photo_content_type,
    object_name=FaceEnrollmentDraft.photo_object,
    size=FaceEnrollmentDraft.photo_size,
    sha256=FaceEnrollmentDraft.photo_sha256,
    uploaded_at=FaceEnrollmentDraft.photo_uploaded_at,
    extra=(FaceEnrollmentDraft.employee_id,),
    path=_draft_photo_path,
)

#: Comprobante de un pago (PDF o imagen). Los pagos se anulan, nunca se borran: su comprobante se conserva.
PAYMENT_RECEIPTS = StoredImage(
    kind="payment-receipt",
    label="STORAGE_PAYMENT_RECEIPTS",
    model=Payment,
    key=Payment.id,
    company=Payment.company_id,
    content_type=Payment.receipt_type,
    object_name=Payment.receipt_object,
    size=Payment.receipt_size,
    sha256=Payment.receipt_sha256,
    uploaded_at=Payment.receipt_uploaded_at,
    path=_receipt_path,
)


def _avatar_path(ref: ImageRef) -> str:
    size_px, version = ref.extra
    return f"people/users/{ref.key}/avatar/{version}/{size_px}.{extension(ref.content_type)}.enc"


#: Foto de perfil de una PERSONA (no de una empresa: la misma cuenta trabaja en varias), una fila por tamaño.
#: Cada foto nueva lleva otra versión (otra carpeta): la anterior sale del bucket por la cola de borrado.
USER_AVATARS = StoredImage(
    kind="user-avatar",
    label="STORAGE_USER_AVATARS",
    model=UserAvatar,
    key=UserAvatar.user_id,
    company=None,
    content_type=UserAvatar.content_type,
    object_name=UserAvatar.object_name,
    size=UserAvatar.byte_size,
    sha256=UserAvatar.sha256,
    uploaded_at=UserAvatar.uploaded_at,
    extra=(UserAvatar.size_px, UserAvatar.version),
    path=_avatar_path,
)


def _evidence_path(ref: ImageRef) -> str:
    (uid,) = ref.extra
    return f"companies/{ref.company_id}/fraud-cases/{ref.key}/evidence/{uid}.{extension(ref.content_type)}.enc"


#: Fotogramas de evidencia de un caso de fraude (decisión D1 del dueño del producto): solo de intentos
#: sospechosos o de riesgo alto, los ve solo el ADMIN por la API y salen del bucket a los
#: FRAUD_EVIDENCE_RETENTION_DAYS días (`fraud_case_service.purge_evidence`). Se suben ANTES de insertar su fila
#: (la clave del objeto es el caso y una parte aleatoria): ninguna conexión de la BD espera al bucket.
FRAUD_EVIDENCE = StoredImage(
    kind="fraud-evidence",
    label="STORAGE_FRAUD_EVIDENCE",
    model=FraudEvidence,
    key=FraudEvidence.case_id,
    company=FraudEvidence.company_id,
    content_type=FraudEvidence.content_type,
    object_name=FraudEvidence.object_name,
    size=FraudEvidence.byte_size,
    sha256=FraudEvidence.sha256,
    uploaded_at=FraudEvidence.uploaded_at,
    extra=(FraudEvidence.uid,),
    path=_evidence_path,
)


def _document_path(ref: ImageRef) -> str:
    (uid,) = ref.extra
    return f"companies/{ref.company_id}/documents/{uid}.{extension(ref.content_type)}.enc"


#: Documentos de una empresa (decisión del dueño del producto, 2026-10-06: constancia de situación fiscal, acta
#: constitutiva... para facturarle). El objeto se nombra con la empresa y una clave al azar (`uid`): se sube ANTES de
#: abrir la transacción (un archivo de hasta COMPANY_DOCUMENT_MAX_MB; ninguna conexión espera al bucket) y la fila
#: nace con su referencia. Sale del bucket cuando la depuración borra la fila (borrado lógico vencido).
COMPANY_DOCUMENTS = StoredImage(
    kind="company-document",
    label="STORAGE_COMPANY_DOCUMENTS",
    model=CompanyDocument,
    key=CompanyDocument.company_id,
    company=CompanyDocument.company_id,
    content_type=CompanyDocument.content_type,
    object_name=CompanyDocument.object_name,
    size=CompanyDocument.byte_size,
    sha256=CompanyDocument.sha256,
    uploaded_at=CompanyDocument.uploaded_at,
    extra=(CompanyDocument.uid,),
    path=_document_path,
)


def _voice_clip_path(ref: ImageRef) -> str:
    employee_id, uid = ref.extra
    return (
        f"companies/{ref.company_id}/employees/{employee_id}/face-enrollments/{ref.key}/voice/"
        f"{uid}.{extension(ref.content_type)}.enc"
    )


#: Clips de video de la verificación por voz del registro facial (decisión del dueño, 2026-10-06): solo la respuesta
#: que pasó de cada pregunta, cifrada; los revisa la EMPRESA al validar el registro (nunca el ADMIN) y salen del bucket
#: a los FACE_VIDEO_RETENTION_DAYS, al rechazar el registro o al eliminar al empleado. El objeto se nombra con el
#: registro y una clave al azar (`uid`): se sube ANTES de abrir la transacción (ninguna conexión espera al bucket).
ENROLLMENT_VOICE_CLIPS = StoredImage(
    kind="enrollment-voice",
    label="STORAGE_ENROLLMENT_VOICE_CLIPS",
    model=EnrollmentVoiceAnswer,
    key=EnrollmentVoiceAnswer.enrollment_id,
    company=EnrollmentVoiceAnswer.company_id,
    content_type=EnrollmentVoiceAnswer.content_type,
    object_name=EnrollmentVoiceAnswer.object_name,
    size=EnrollmentVoiceAnswer.byte_size,
    sha256=EnrollmentVoiceAnswer.sha256,
    uploaded_at=EnrollmentVoiceAnswer.uploaded_at,
    extra=(EnrollmentVoiceAnswer.employee_id, EnrollmentVoiceAnswer.uid),
    path=_voice_clip_path,
)


def _employee_document_path(ref: ImageRef) -> str:
    (uid,) = ref.extra
    return f"companies/{ref.company_id}/employees/{ref.key}/documents/{uid}.{extension(ref.content_type)}.enc"


#: Documentos de identidad del empleado (decisión del dueño del producto, 2026-10-07: comprobante de domicilio e
#: identificación oficial del onboarding). Mismo mecanismo que los documentos de la empresa: el objeto se nombra con la
#: empresa, el empleado y una clave al azar (`uid`); se sube ANTES de abrir la transacción de su fila (ninguna conexión
#: espera al bucket). Sale del bucket cuando la depuración borra la fila (borrado lógico vencido o empleado depurado).
EMPLOYEE_DOCUMENTS = StoredImage(
    kind="employee-document",
    label="STORAGE_EMPLOYEE_DOCUMENTS",
    model=EmployeeDocument,
    key=EmployeeDocument.employee_id,
    company=EmployeeDocument.company_id,
    content_type=EmployeeDocument.content_type,
    object_name=EmployeeDocument.object_name,
    size=EmployeeDocument.byte_size,
    sha256=EmployeeDocument.sha256,
    uploaded_at=EmployeeDocument.uploaded_at,
    extra=(EmployeeDocument.uid,),
    path=_employee_document_path,
)


#: Todo lo que la plataforma guarda como imagen o archivo. Un proceso nuevo que guarde una imagen o un
#: archivo agrega aquí su entrada (regla del backend AGENTS.md §8).
STORED_IMAGES: tuple[StoredImage, ...] = (
    FACE_ENROLLMENT_PHOTOS,
    FACE_ENROLLMENT_DRAFT_PHOTOS,
    PAYMENT_RECEIPTS,
    USER_AVATARS,
    FRAUD_EVIDENCE,
    COMPANY_DOCUMENTS,
    EMPLOYEE_DOCUMENTS,
    ENROLLMENT_VOICE_CLIPS,
)


def object_name(image: StoredImage, ref: ImageRef) -> str:
    """Nombre completo del objeto: carpeta del entorno + ruta del tipo (determinista por id)."""
    return f"{settings.storage_prefix}/{image.path(ref)}"


def ref_of(image: StoredImage, row: Any) -> ImageRef:
    """Los ids de una fila ya insertada (para nombrar su objeto)."""
    return ImageRef(
        key=getattr(row, image.key.key),
        company_id=None if image.company is None else getattr(row, image.company.key),
        content_type=getattr(row, image.content_type.key),
        extra=tuple(getattr(row, column.key) for column in image.extra),
    )


def upload(storage: ObjectStorage, image: StoredImage, ref: ImageRef, payload: bytes) -> tuple[str, str]:
    """Sube `payload` (YA cifrado) "solo si no existe" y verifica lo que reporta el bucket; devuelve
    (nombre, SHA-256). Si el objeto ya estaba (un reintento cuya respuesta se perdió) y es idéntico,
    cuenta como subido; si es otro, `VerificationFailed` (nunca se reemplaza nada). Dentro de la petición:
    con su tiempo total (`GCS_REQUEST_SECONDS`)."""
    name = object_name(image, ref)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "kind": image.kind,
        "content_type": ref.content_type or "",
        "sha256": digest,
        "encryption": "fernet",
    }
    if ref.company_id is not None:  # lo de la persona (foto de perfil) no es de una empresa
        metadata["company_id"] = str(ref.company_id)
    info: ObjectInfo | None
    try:
        info = storage.put(name, payload, metadata, interactive=True)
    except ObjectExists:
        info = storage.stat(name, interactive=True)
    if not _identical(info, payload, digest):
        raise VerificationFailed(f"el objeto {name} no es idéntico a lo que se subió")
    return name, digest


def _identical(info: ObjectInfo | None, payload: bytes, digest: str) -> bool:
    """Lo que el bucket reporta (tamaño, MD5 y el SHA-256 de sus metadatos) es exactamente `payload`."""
    if info is None:
        return False
    return info.size == len(payload) and info.md5 == content_md5(payload) and info.metadata.get("sha256") == digest


def store(db: Session, image: StoredImage, row: Any, content: bytes) -> None:
    """Dentro de la petición: cifra `content`, lo sube, lo verifica y anota la referencia en `row` (ya
    insertada: con su id). Los bytes nunca tocan la BD. Si la transacción no se confirma, el objeto se
    borra (`_discard_uploads`). 503 STORAGE_UNAVAILABLE si el bucket no está configurado o falla."""
    storage = get_storage()
    ref = ref_of(image, row)
    if storage.configured:  # se anota ANTES de subir: si algo falla después (verificación, BD), se borra
        db.info.setdefault(_UPLOADED, []).append((storage, object_name(image, ref)))
    try:
        name, digest = upload(storage, image, ref, encrypt_bytes(content))
    except StorageError as exc:
        raise unavailable(exc) from exc
    values = (name, len(content), digest, datetime.now(UTC))
    for column, value in zip(image.reference_columns, values, strict=True):
        setattr(row, column.key, value)


def read(image: StoredImage, row: Any) -> bytes | None:
    """La imagen (descifrada en memoria) de la fila, del bucket (con el tiempo total de una petición,
    `GCS_REQUEST_SECONDS`, y verificada contra su SHA-256). None si la fila no tiene imagen.

    `ImageUnreadable` si no se puede descifrar; otro `StorageError` si el bucket no responde o el objeto
    no es el que se guardó (quien llama decide: degradar o responder 503 `STORAGE_UNAVAILABLE`)."""
    name: str | None = getattr(row, image.object_name.key)
    if name is None:
        return None
    payload = get_storage().get(name, interactive=True)
    if hashlib.sha256(payload).hexdigest() != getattr(row, image.sha256.key):
        raise VerificationFailed(f"el objeto {name} no es el que se guardó")
    plain = try_decrypt(payload)
    if plain is None:
        raise ImageUnreadable(f"{name} es ilegible (¿cambió DATA_ENCRYPTION_KEY?)")
    return plain


def unavailable(exc: StorageError) -> ServiceUnavailableError:
    """La respuesta cuando una imagen no se pudo guardar o leer en el bucket (reintentable). El motivo
    queda en el log del proceso; la falla (5xx) la registra el middleware en "Errores del sistema"."""
    logger.warning("Almacenamiento de imágenes no disponible: %s: %s", type(exc).__name__, exc)
    return ServiceUnavailableError(
        code="STORAGE_UNAVAILABLE",
        retry_after=10,
    )


def forget(db: Session, image: StoredImage, row: Any) -> None:
    """La fila deja de conservar su imagen (p. ej. un registro rechazado, minimización de datos): su
    objeto pasa a la cola de borrado en esta misma transacción y la fila queda sin referencia."""
    name = getattr(row, image.object_name.key)
    if name is not None:
        StorageRepository(db).enqueue(name, datetime.now(UTC))
    for column in image.reference_columns:
        setattr(row, column.key, None)


def release(db: Session, image: StoredImage, condition: ColumnElement[bool]) -> None:
    """Antes de BORRAR filas con imagen (también en cascada): encola el borrado de sus objetos en la misma
    transacción. Lo que se borra de la BD nunca se queda en la nube."""
    StorageRepository(db).enqueue_from(image.object_name, condition, datetime.now(UTC))


def release_user_avatars(db: Session, condition: ColumnElement[bool]) -> None:
    """Antes de borrar cuentas (sus fotos de perfil se van en cascada) o al reemplazar la foto: los objetos de
    las filas de `auth.user_avatars` que cumplen `condition` salen del bucket (una sentencia)."""
    release(db, USER_AVATARS, condition)


def abandon(db: Session) -> None:
    """Lo que la sesión subió y ya no se va a confirmar porque falló ANTES de abrir su transacción: se borra
    del bucket (o queda en la cola). Lo usa quien sube primero y escribe después en una transacción corta (la
    foto de perfil: ninguna conexión de la BD espera al bucket); `store` dentro de una transacción no lo
    necesita (si no se confirma, `_ended` lo descarta)."""
    uploaded = db.info.pop(_UPLOADED, None)
    if uploaded:
        _discard_uploads(uploaded)


def release_employee_images(db: Session, company_id: int, employee_id: int) -> None:
    """Antes de borrar un empleado (sus registros faciales y sus casos de fraude se van en cascada): sus fotos, los
    clips de su verificación por voz, la foto inicial de un registro a medias y la evidencia de sus casos salen del
    bucket."""
    release(
        db,
        FACE_ENROLLMENT_PHOTOS,
        and_(FaceEnrollment.company_id == company_id, FaceEnrollment.employee_id == employee_id),
    )
    release(
        db,
        ENROLLMENT_VOICE_CLIPS,
        and_(EnrollmentVoiceAnswer.company_id == company_id, EnrollmentVoiceAnswer.employee_id == employee_id),
    )
    release(
        db,
        FACE_ENROLLMENT_DRAFT_PHOTOS,
        and_(FaceEnrollmentDraft.company_id == company_id, FaceEnrollmentDraft.employee_id == employee_id),
    )
    release(db, FRAUD_EVIDENCE, and_(FraudEvidence.company_id == company_id, FraudEvidence.employee_id == employee_id))


def release_employee_documents(db: Session, company_id: int, employee_id: int) -> None:
    """Antes de borrar DE VERDAD los documentos de identidad de un empleado que se elimina (regla 13, LFPDPPP: una
    identificación lleva la foto y los datos de la persona): los objetos de TODOS sus documentos —vigentes y los que el
    propio empleado ya había eliminado de su lista— salen del bucket. La encolada es un `INSERT ... FROM SELECT`, no una
    consulta, así que ve también lo eliminado (el borrado lógico solo filtra SELECT y UPDATE): ningún archivo de un
    documento en «Eliminados» se queda en la nube."""
    release(
        db,
        EMPLOYEE_DOCUMENTS,
        and_(EmployeeDocument.company_id == company_id, EmployeeDocument.employee_id == employee_id),
    )


def release_company_evidence(db: Session, company_id: int) -> None:
    """Antes de borrar una empresa (sus casos se van en cascada): la evidencia de sus casos sale del bucket."""
    release(db, FRAUD_EVIDENCE, FraudEvidence.company_id == company_id)


# ---------------------------------------------------------------- objetos de transacciones que no se confirman


@event.listens_for(Session, "after_commit")
def _committed(session: Session) -> None:
    """Confirmada: sus objetos ya tienen fila que los referencia."""
    session.info.pop(_UPLOADED, None)


@event.listens_for(Session, "after_transaction_end")
def _ended(session: Session, transaction: SessionTransaction) -> None:
    """Terminó la transacción raíz sin confirmarse (error, rollback o cierre): sus objetos sobran."""
    if transaction.parent is None and _UPLOADED in session.info:
        _discard_uploads(session.info.pop(_UPLOADED))


def _discard_uploads(uploaded: list[tuple[ObjectStorage, str]]) -> None:
    """Borra del bucket lo que subió una transacción que no se confirmó; si el bucket tampoco responde, lo
    deja en la cola del mantenimiento. Nunca lanza: corre al cerrar una petición que ya falló."""
    for storage, name in uploaded:
        try:
            storage.delete(name, interactive=True)
        except StorageError:
            try:
                with SessionLocal() as other:
                    StorageRepository(other).enqueue(name, datetime.now(UTC))
                    other.commit()
            except Exception:  # BD y bucket caídos a la vez: el objeto (cifrado) queda sin dueño
                logger.exception("No se pudo borrar ni encolar el objeto %s de una transacción revertida", name)
