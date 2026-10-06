"""Almacenamiento de objetos: el bucket privado de Google Cloud Storage de la plataforma.

Decisión del dueño del producto: ninguna imagen ni archivo se guarda en la base de datos; van CIFRADOS
al bucket de Firebase Storage (un bucket de GCS) y la BD solo guarda la referencia. Aquí vive solo el
acceso al bucket; qué se guarda, cómo se nombra y cuándo lo decide `app/services/image_storage.py` (los
objetos llegan aquí ya cifrados: el bucket nunca ve una imagen legible).

Una interfaz pequeña (`ObjectStorage`) con dos implementaciones:

- `GcsStorage`: cliente oficial `google-cloud-storage` con la llave de una cuenta de servicio. Toda
  llamada tiene tiempo límite (`GCS_TIMEOUT_SECONDS`) y un tiempo total con reintentos
  (`GCS_REQUEST_SECONDS` dentro de una petición, `GCS_RETRY_SECONDS` en segundo plano). Solo se reintenta
  lo idempotente: la subida lleva la condición "solo si no existe" (`if_generation_match=0`) a un nombre
  fijo (repetirla no reemplaza nada: el objeto que ya está se verifica), y consultar, leer y borrar
  (borrar lo que ya no está es el mismo resultado). La subida envía su MD5 y el bucket rechaza una
  subida dañada; la descarga se verifica contra su MD5.
- `DisabledStorage`: sin bucket o sin llave. Quien necesita el bucket recibe `StorageNotConfigured`.

Ningún otro módulo importa el cliente de Google: sus excepciones (red, tiempo límite, permisos, cuota,
respuesta dañada) se traducen a `StorageError` con la original encadenada, para que una falla del
bucket nunca tumbe el servicio ni deje una petición colgada.
"""

import base64
import contextlib
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY
from google.oauth2 import service_account

from app.core.config import settings
from app.core.observability import observed

logger = logging.getLogger(__name__)

#: Permiso que pide la llave: leer y escribir objetos (nada de administrar el bucket ni sus permisos).
SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
#: Tipo de todos los objetos: son bytes cifrados, nunca una imagen que un navegador pueda mostrar.
ENCRYPTED_CONTENT_TYPE = "application/octet-stream"


class StorageError(Exception):
    """Falla del almacenamiento de objetos (el mensaje nunca lleva el contenido)."""


class StorageNotConfigured(StorageError):
    """No hay bucket o llave configurados."""


class StorageUnavailable(StorageError):
    """El bucket no respondió bien (red, tiempo límite, permisos, cuota): se reintenta después."""


class ObjectNotFound(StorageError):
    """El objeto no existe en el bucket."""


class ObjectExists(StorageError):
    """Ya hay un objeto con ese nombre (la subida es "solo si no existe"): quien sube lo verifica."""


@dataclass(frozen=True)
class ObjectInfo:
    """Lo que el bucket dice de un objeto: basta para verificar una subida sin descargarla."""

    name: str
    size: int
    #: MD5 en base64, como lo reporta GCS (None en objetos compuestos).
    md5: str | None
    metadata: dict[str, str]


def content_md5(data: bytes) -> str:
    """MD5 en base64 (el formato de GCS): integridad de lo subido, no seguridad."""
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


class ObjectStorage(Protocol):
    """Lo único que la plataforma necesita de un almacenamiento de objetos. `interactive`: la llamada
    ocurre dentro de una petición (su tiempo total es `GCS_REQUEST_SECONDS`, no el de segundo plano)."""

    @property
    def configured(self) -> bool: ...

    def describe(self) -> dict[str, str | None]:
        """Para el ADMIN: dónde guarda (o por qué está apagado). Nunca la llave."""
        ...

    def put(self, name: str, data: bytes, metadata: Mapping[str, str], *, interactive: bool = False) -> ObjectInfo:
        """Crea el objeto `name` SOLO si no existe (`ObjectExists` si ya está) y devuelve lo que el bucket
        guardó (tamaño, MD5 y metadatos) para verificarlo."""
        ...

    def stat(self, name: str, *, interactive: bool = False) -> ObjectInfo | None:
        """Tamaño, MD5 y metadatos del objeto; None si no existe."""
        ...

    def get(self, name: str, *, interactive: bool = False) -> bytes:
        """El contenido del objeto (verificado contra su MD5). `ObjectNotFound` si no existe."""
        ...

    def delete(self, name: str, *, interactive: bool = False) -> None:
        """Borra el objeto; si ya no estaba, no es una falla (el resultado es el mismo)."""
        ...

    def list_names(self, prefix: str, *, limit: int) -> list[str]:
        """Nombres de los objetos bajo `prefix`, a lo más `limit` (nunca una lista sin tope). Solo en segundo plano y
        en herramientas de operación (respaldos): ninguna petición recorre el bucket."""
        ...


class DisabledStorage:
    """Sin bucket configurado: guardar o leer una imagen no es posible (503 STORAGE_UNAVAILABLE)."""

    configured = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def describe(self) -> dict[str, str | None]:
        return {"backend": "disabled", "bucket": None, "reason": self.reason}

    def _unavailable(self) -> StorageNotConfigured:
        return StorageNotConfigured(f"almacenamiento de imágenes no configurado: {self.reason}")

    def put(self, name: str, data: bytes, metadata: Mapping[str, str], *, interactive: bool = False) -> ObjectInfo:
        raise self._unavailable()

    def stat(self, name: str, *, interactive: bool = False) -> ObjectInfo | None:
        raise self._unavailable()

    def get(self, name: str, *, interactive: bool = False) -> bytes:
        raise self._unavailable()

    def delete(self, name: str, *, interactive: bool = False) -> None:
        raise self._unavailable()

    def list_names(self, prefix: str, *, limit: int) -> list[str]:
        raise self._unavailable()


class GcsStorage:
    """El bucket privado de la plataforma en Google Cloud Storage (Firebase Storage)."""

    configured = True

    def __init__(
        self,
        bucket: str,
        credentials: service_account.Credentials,
        *,
        timeout: float,
        request_seconds: float,
        retry_seconds: float,
        http: Any = None,
    ) -> None:
        """`http`: sesión HTTP propia (solo pruebas: un servidor falso); sin ella el cliente usa la
        llave. Crear el cliente no hace ninguna llamada de red."""
        client = storage.Client(project=credentials.project_id, credentials=credentials, _http=http)
        self.bucket_name = bucket
        self._bucket = client.bucket(bucket)
        self._timeout = timeout
        # Reintentos con espera creciente hasta su tiempo total (0 en segundo plano = un solo intento).
        self._request_retry = DEFAULT_RETRY.with_timeout(request_seconds)
        self._background_retry = DEFAULT_RETRY.with_timeout(retry_seconds) if retry_seconds else None

    def describe(self) -> dict[str, str | None]:
        return {"backend": "gcs", "bucket": self.bucket_name, "reason": None}

    @observed("storage.put")
    def put(self, name: str, data: bytes, metadata: Mapping[str, str], *, interactive: bool = False) -> ObjectInfo:
        blob = self._bucket.blob(name)
        blob.metadata = dict(metadata)
        translate_errors(
            lambda: blob.upload_from_string(
                data,
                content_type=ENCRYPTED_CONTENT_TYPE,
                # Solo si no existe: un reintento (o una subida repetida) nunca reemplaza un objeto.
                if_generation_match=0,
                timeout=self._timeout,
                retry=self._retry(interactive),
                checksum="md5",
            )
        )
        return self._info(name, blob)

    @observed("storage.stat")
    def stat(self, name: str, *, interactive: bool = False) -> ObjectInfo | None:
        blob = self._bucket.blob(name)
        try:
            translate_errors(lambda: blob.reload(timeout=self._timeout, retry=self._retry(interactive)))
        except ObjectNotFound:
            return None
        return self._info(name, blob)

    @observed("storage.get")
    def get(self, name: str, *, interactive: bool = False) -> bytes:
        blob = self._bucket.blob(name)
        retry = self._retry(interactive)
        return translate_errors(lambda: blob.download_as_bytes(timeout=self._timeout, retry=retry, checksum="md5"))

    @observed("storage.delete")
    def delete(self, name: str, *, interactive: bool = False) -> None:
        blob = self._bucket.blob(name)
        with contextlib.suppress(ObjectNotFound):
            translate_errors(lambda: blob.delete(timeout=self._timeout, retry=self._retry(interactive)))

    @observed("storage.list")
    def list_names(self, prefix: str, *, limit: int) -> list[str]:
        blobs = self._bucket.list_blobs(
            prefix=prefix, max_results=limit, timeout=self._timeout, retry=self._retry(interactive=False)
        )
        return translate_errors(lambda: [blob.name for blob in blobs])

    def _retry(self, interactive: bool) -> Any:
        return self._request_retry if interactive else self._background_retry

    @staticmethod
    def _info(name: str, blob: Any) -> ObjectInfo:
        return ObjectInfo(name=name, size=int(blob.size or 0), md5=blob.md5_hash, metadata=dict(blob.metadata or {}))


def translate_errors[T](action: Callable[[], T]) -> T:
    """Ejecuta una llamada al bucket traduciendo sus excepciones (la original va encadenada)."""
    try:
        return action()
    except NotFound as exc:
        raise ObjectNotFound("el objeto no existe en el bucket") from exc
    except PreconditionFailed as exc:  # la subida "solo si no existe" encontró uno con ese nombre
        raise ObjectExists("ya existe un objeto con ese nombre") from exc
    except Exception as exc:  # red, tiempo límite, permisos, cuota o respuesta dañada: no disponible
        raise StorageUnavailable(f"{type(exc).__name__}: {exc}"[:500]) from exc


class InvalidServiceAccount(StorageNotConfigured):
    """La llave existe pero no es una cuenta de servicio (falla de configuración que se registra)."""


def service_account_credentials(scope: str = SCOPE) -> service_account.Credentials:
    """La llave de la cuenta de servicio del `.env` con ese permiso (sin llamadas de red). `StorageNotConfigured` con
    el motivo si falta el bucket o la llave, no se puede leer o está vacía (docker compose monta /dev/null mientras
    no exista); `InvalidServiceAccount` si no es una cuenta de servicio. La usan el almacenamiento de cada proceso
    (objetos: leer y escribir) y la regla de ciclo de vida del bucket (administrarlo: `storage_lifecycle`)."""
    bucket, path = settings.GCS_BUCKET, settings.GCS_CREDENTIALS_FILE
    if not bucket or not path:
        raise StorageNotConfigured("faltan GCS_BUCKET o GCS_CREDENTIALS_FILE en el .env")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise StorageNotConfigured(f"no se pudo leer la llave de la cuenta de servicio ({type(exc).__name__})") from exc
    if not raw.strip():
        raise StorageNotConfigured("la llave de la cuenta de servicio aún no está montada (archivo vacío)")
    try:
        return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=[scope])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InvalidServiceAccount(type(exc).__name__) from exc


def load_storage() -> ObjectStorage:
    """La implementación que corresponde a la configuración (sin llamadas de red).

    Sin bucket o sin llave (o con el archivo vacío: docker compose monta /dev/null mientras no exista)
    queda apagado con un AVISO al arrancar (el estado esperado hasta configurarlo; cada imagen que no se
    pueda guardar sí responde 503 y queda registrada). Una llave presente pero ilegible es una falla de
    configuración: se registra como error.
    """
    try:
        credentials = service_account_credentials()
    except InvalidServiceAccount as exc:
        logger.error("La llave de GCS_CREDENTIALS_FILE no es una cuenta de servicio válida: %s", exc)
        return DisabledStorage("la llave de la cuenta de servicio no es válida")
    except StorageNotConfigured as exc:
        return _disabled(str(exc))
    try:
        instance = GcsStorage(
            settings.GCS_BUCKET,
            credentials,
            timeout=settings.GCS_TIMEOUT_SECONDS,
            request_seconds=settings.GCS_REQUEST_SECONDS,
            retry_seconds=settings.GCS_RETRY_SECONDS,
        )
    except Exception:  # el cliente no se pudo crear: la API arranca igual
        logger.exception("No se pudo crear el cliente de Google Cloud Storage")
        return DisabledStorage("no se pudo crear el cliente de Google Cloud Storage")
    logger.info(
        "Almacenamiento de imágenes: gs://%s/%s/ (objetos cifrados)", settings.GCS_BUCKET, settings.storage_prefix
    )
    return instance


def _disabled(reason: str) -> DisabledStorage:
    logger.warning(
        "Almacenamiento de imágenes apagado: %s. Registrar rostros y comprobantes responderá 503 "
        "STORAGE_UNAVAILABLE hasta configurarlo.",
        reason,
    )
    return DisabledStorage(reason)


_lock = threading.Lock()
_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    """El almacenamiento de este proceso (se arma una vez: el aviso de "apagado" sale una sola vez)."""
    global _storage
    with _lock:
        if _storage is None:
            _storage = load_storage()
        return _storage


def use_storage(instance: ObjectStorage | None) -> None:
    """Fija el almacenamiento del proceso (pruebas: uno falso); None lo vuelve a armar con la configuración."""
    global _storage
    with _lock:
        _storage = instance
