"""Regla de ciclo de vida del bucket para las copias cifradas de los respaldos (README "Respaldos y restauración").

Decisión del dueño (2026-10-06): las copias `pg_dump` cifradas (`<GCS_PREFIX>/backups/`, `app/services/db_backup.py`)
no se acumulan en el bucket. El servicio `backup` ya borra las vencidas con sus manifiestos locales, pero eso depende
de la carpeta local: si el volumen se pierde (lo que pasó el 2026-10-05), nadie las borraría. La regla del bucket es la
red de seguridad: Google borra sola cada copia con más de `BACKUP_RETENTION_DAYS + BACKUP_LIFECYCLE_MARGIN_DAYS` días.

- **Idempotente**: la regla administrada es la de borrar con `matchesPrefix` exactamente `["<prefijo>/backups/"]`; se
  agrega si falta, se corrige si su edad cambió y nunca se toca ninguna otra (otras carpetas, otros entornos, reglas
  puestas a mano). Aplicarla dos veces no cambia nada.
- **Sin aplicar por omisión**: `python -m app.cli storage lifecycle` solo muestra lo actual y lo que quedaría (también
  como JSON para `gcloud storage buckets update --lifecycle-file`); `--apply` la escribe con
  `if_metageneration_match` (si alguien cambió el bucket entre leer y escribir, falla en lugar de pisarlo).
- **Nunca sobre `<GCS_PREFIX>/pitr/`**: el repositorio de pgBackRest tiene su propia retención por tiempo
  (`PITR_RETENTION_DAYS`). Borrar por edad dejaría huérfanos los diferenciales y el WAL de un respaldo completo que
  pgBackRest aún conserva (si los respaldos base se atrasan, el único completo es justo el más viejo).
- **Permiso**: cambiar el ciclo de vida es administrar el bucket (`storage.buckets.update`), no solo sus objetos: la
  llave pide `devstorage.full_control` y la cuenta de servicio necesita ese permiso (p. ej. *Storage Admin* sobre el
  bucket). Sin él, `--apply` responde con el motivo y la salida trae el JSON para aplicarlo con `gcloud`.
"""

import json
from dataclasses import dataclass
from typing import Any

from google.api_core.retry import Retry
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY

from app.core.config import settings
from app.core.object_storage import ObjectExists, StorageError, service_account_credentials, translate_errors

#: Administrar el bucket (su ciclo de vida); el almacenamiento de cada proceso solo lee y escribe objetos.
ADMIN_SCOPE = "https://www.googleapis.com/auth/devstorage.full_control"


def backups_prefix() -> str:
    """Carpeta de las copias cifradas de `pg_dump` en el bucket (la misma de `db_backup`)."""
    return f"{settings.storage_prefix}/backups/"


def managed_rule() -> dict[str, Any]:
    """La regla que administra este módulo: borrar las copias cifradas vencidas (más un margen)."""
    age = settings.BACKUP_RETENTION_DAYS + settings.BACKUP_LIFECYCLE_MARGIN_DAYS
    return {"action": {"type": "Delete"}, "condition": {"age": age, "matchesPrefix": [backups_prefix()]}}


def _is_managed(rule: dict[str, Any]) -> bool:
    condition = rule.get("condition") or {}
    return rule.get("action", {}).get("type") == "Delete" and condition.get("matchesPrefix") == [backups_prefix()]


@dataclass(frozen=True)
class LifecyclePlan:
    """Las reglas que tiene el bucket y las que quedarían (las ajenas, intactas, más la administrada)."""

    bucket: str
    current: list[dict[str, Any]]
    desired: list[dict[str, Any]]

    @property
    def changed(self) -> bool:
        return self.current != self.desired

    def gcloud_file(self) -> str:
        """El ciclo de vida completo en el formato de `gcloud storage buckets update --lifecycle-file` (reemplaza
        todas las reglas: por eso incluye las ajenas)."""
        return json.dumps({"rule": self.desired}, indent=2, ensure_ascii=False)


def plan_for(bucket: str, current: list[dict[str, Any]]) -> LifecyclePlan:
    """Lo que quedaría: las reglas ajenas en su orden y, al final, la administrada (una sola, con la edad vigente)."""
    current = [dict(rule) for rule in current]
    return LifecyclePlan(bucket, current, [rule for rule in current if not _is_managed(rule)] + [managed_rule()])


def open_bucket(http: Any = None) -> storage.Bucket:
    """El bucket con permiso de administrarlo (sin llamadas de red). `http`: sesión propia (solo pruebas)."""
    credentials = service_account_credentials(ADMIN_SCOPE)
    client = storage.Client(project=credentials.project_id, credentials=credentials, _http=http)
    return client.bucket(settings.GCS_BUCKET)


def lifecycle(*, apply: bool, bucket: storage.Bucket | None = None) -> LifecyclePlan:
    """Lee las reglas del bucket y, con `apply`, deja la administrada (solo si cambia algo). Cada llamada con su
    tiempo límite; leer se reintenta (idempotente) y escribir va condicionado a la versión leída. Las fallas del
    bucket salen como `StorageError` (sin permiso: `StorageUnavailable` con el 403)."""
    target = bucket or open_bucket()
    retry: Retry | None = DEFAULT_RETRY.with_timeout(settings.GCS_RETRY_SECONDS) if settings.GCS_RETRY_SECONDS else None
    translate_errors(lambda: target.reload(timeout=settings.GCS_TIMEOUT_SECONDS, retry=retry))
    plan = plan_for(target.name, list(target.lifecycle_rules))
    if apply and plan.changed:
        target.lifecycle_rules = plan.desired
        generation = target.metageneration
        try:
            translate_errors(
                lambda: target.patch(timeout=settings.GCS_TIMEOUT_SECONDS, if_metageneration_match=generation)
            )
        except ObjectExists as exc:  # 412: alguien cambió el bucket después de leerlo (nada se pisó)
            raise StorageError("el bucket cambió mientras se aplicaba la regla: vuelve a correr el comando") from exc
    return plan
