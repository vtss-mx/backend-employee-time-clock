"""API pública de verificación facial: la aplicación móvil de una empresa (SDK de Android, iOS y sus envoltorios)
verifica (1:1) o identifica (1:N) a sus empleados con la llave de la API (permiso `VERIFICATION`, migración 0084).

Contrato: `docs/sdk/contrato-verificacion.md` (raíz). Nada de un motor aparte: el 1:1 es
`VerificationService.verify_employee_face` y el 1:N es `face_identification.GallerySearch`, con TODAS las cerraduras
de `identity_core` (reto de uso único, prueba de vida, toma única, cámara real, motor de riesgo, bloqueo por intentos y
bitácora con sus métricas y casos de fraude). Lo propio de la API:

- **Quién opera la cámara** es un DISPOSITIVO de la llave (`ApiDevice`: la llave de la API + la huella de la llave
  pública P-256 del teléfono). Su prueba es OBLIGATORIA: firma `"{device_nonce}.{acción}.{SHA-256 de la primera
  captura}"` (el esquema de la firma por petición de los validadores, `request_signing`) con el reto HMAC sin estado
  ligado a la llave de la API y a la del dispositivo (`device_service.issue_api_nonce`). Se revisa ANTES de reservar un
  worker facial y de consumir el reto: sin ella, 403 `DEVICE_PROOF_REQUIRED` / `DEVICE_PROOF_INVALID` y nada cuenta
  como intento.
- **Límites**: por llave en estas rutas (`RATE_LIMIT_API_VERIFICATION_PER_MINUTE`, en la autenticación) y por
  dispositivo (`RATE_LIMIT_API_VERIFICATION_DEVICE_PER_MINUTE`, aquí). El bloqueo por intentos: el 1:1 por empleado con
  los fallos de todos los canales; el 1:N por dispositivo, con los intentos sospechosos (`attempt_guard`).
- **El resultado de un intento es SIEMPRE un 200 con su decisión** (`ApiVerificationResult`): permitir, "en revisión",
  un paso más (con el reto nuevo, en lugar del 422 `STEP_UP_REQUIRED` de la aplicación web) o negar con su motivo (un
  intento sospechoso, que ya quedó en la bitácora, en lugar de su 422). Lo que no llegó a ser un intento (calidad de la
  imagen, reto vencido, empleado que no existe) sigue siendo su error 4xx.
- La bitácora lo registra con el método `API_FACE` y la huella del dispositivo (`verification_logs.device_hash`); la
  respuesta nunca lleva fotos (la API de integración no las recibe).
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, UnprocessableError
from app.facial_recognition import FacePipeline
from app.facial_recognition.image_utils import PROCESSING_MAX_SIDE
from app.middleware.rate_limit import enforce
from app.models import Employee, VerificationLog, VerificationMethod
from app.repositories.employee_repository import EmployeeRepository
from app.schemas.auth import DeviceLocation
from app.schemas.verification import FaceChallengeResponse, VerificationResult
from app.schemas.verification_api import (
    ApiChallenge,
    ApiChallengeIn,
    ApiDecision,
    ApiEmployeeRef,
    ApiVerificationResult,
    CaptureSpec,
)
from app.services.api_key_service import ApiClient
from app.services.attempt_guard import ensure_unlocked
from app.services.client_evidence import ClientEvidence, DeviceProofInput
from app.services.device_service import NonceState, api_nonce_state, key_hash, public_key_is_valid, signature_is_valid
from app.services.face_identification import GallerySearch
from app.services.face_service import SECURITY_REASONS, SuspiciousCapture
from app.services.identity_core import (
    MAX_FRONTAL_FRAMES,
    IdentityLog,
    StepUpRequired,
    ensure_frame_count,
    ensure_verification_location,
    issue_challenge,
)
from app.services.liveness_service import ApiDevice, LivenessResponse
from app.services.policy_service import PolicyService
from app.services.request_signing import (
    MAX_KEY_CHARS,
    MAX_NONCE_CHARS,
    MAX_SIGNATURE_CHARS,
    digest_of,
    signed_message,
)
from app.services.verification_service import VerificationService, ensure_verifiable

#: Acciones que firma el dispositivo (la segunda parte del mensaje firmado).
ACTION_VERIFY = "verify"
ACTION_IDENTIFY = "identify"
#: Las frontales que se recomiendan (dos: el consenso entre capturas sin pesar de más).
FRONTAL_RECOMMENDED = 2
#: Lo que los SDK mandan (el servidor también acepta PNG y WEBP, como en la aplicación web).
CAPTURE_FORMATS = ["image/jpeg"]
METHOD = VerificationMethod.API_FACE


@dataclass(frozen=True)
class ApiTake:
    """Lo que llegó con un intento y ya se revisó antes del motor: el dispositivo probado y las frontales leídas."""

    device: ApiDevice
    frontal: list[bytes]


@dataclass(frozen=True)
class EmployeeReference:
    """A quién se verifica: su id o su número de empleado (exactamente uno)."""

    employee_id: int | None = None
    employee_number: str | None = None


def device_hash_of(public_key: str) -> str:
    """La huella de una llave pública P-256 (SPKI, base64) o 422 `DEVICE_KEY_INVALID` si no lo es."""
    if len(public_key) > MAX_KEY_CHARS or not public_key_is_valid(public_key):
        raise UnprocessableError(code="DEVICE_KEY_INVALID", key="API_DEVICE_KEY_INVALID", field="device_key")
    return key_hash(public_key)


def limit_device(device: ApiDevice) -> None:
    """El límite por dispositivo de la llave (429 `RATE_LIMITED`): antes de cualquier trabajo del intento."""
    limit = settings.RATE_LIMIT_API_VERIFICATION_DEVICE_PER_MINUTE
    enforce(f"api-verify-device:{device.key_id}:{device.device_hash}", limit)


def prove_device(client: ApiClient, proof: DeviceProofInput, action: str, first_frontal: bytes) -> ApiDevice:
    """El dispositivo que firma el intento (obligatorio en la API): su llave, el reto que el servidor le dio a ESA llave
    con ESA llave de la API (vigente) y la firma de `"{reto}.{acción}.{SHA-256 de la primera frontal}"` (liga las
    capturas al dispositivo). 403 sin la prueba o con una que no verifica; el reto de la prueba de vida no se toca."""
    public_key, nonce, signature = proof.public_key, proof.nonce, proof.signature
    if not (public_key and nonce and signature):
        raise PermissionDeniedError(code="DEVICE_PROOF_REQUIRED", key="API_DEVICE_PROOF_REQUIRED")
    if len(public_key) > MAX_KEY_CHARS or not public_key_is_valid(public_key):
        raise PermissionDeniedError(code="DEVICE_PROOF_INVALID", key="API_DEVICE_PROOF_INVALID")
    device = ApiDevice(client.key_id, key_hash(public_key))
    limit_device(device)
    message = signed_message(nonce, action, digest_of(first_frontal))
    state = api_nonce_state(client.key_id, device.device_hash, nonce) if len(nonce) <= MAX_NONCE_CHARS else None
    fresh = state == NonceState.VALID
    if not fresh or len(signature) > MAX_SIGNATURE_CHARS or not signature_is_valid(public_key, message, signature):
        raise PermissionDeniedError(code="DEVICE_PROOF_INVALID", key="API_DEVICE_PROOF_INVALID")
    return device


def api_challenge(challenge: FaceChallengeResponse) -> ApiChallenge:
    """El reto de siempre más cómo capturar (todo de la configuración: una sola fuente para el servidor y los SDK)."""
    capture = CaptureSpec(
        frontal_min=1,
        frontal_max=MAX_FRONTAL_FRAMES,
        frontal_recommended=FRONTAL_RECOMMENDED,
        min_side_px=settings.MIN_IMAGE_DIMENSION,
        max_side_px=settings.MAX_IMAGE_DIMENSION,
        recommended_long_side_px=PROCESSING_MAX_SIDE,
        max_image_bytes=settings.max_image_bytes,
        formats=CAPTURE_FORMATS,
        min_response_seconds=round(settings.FACE_CHALLENGE_MIN_SECONDS * len(challenge.actions), 3),
    )
    return ApiChallenge(**challenge.model_dump(), capture=capture)


class ApiVerificationService:
    """La verificación de la aplicación móvil de UNA empresa (la de la llave)."""

    def __init__(self, db: Session, client: ApiClient, *, ip: str | None, user_agent: str | None) -> None:
        self.db = db
        self.client = client
        self.company_id = client.company_id
        self.ip = ip
        self.user_agent = user_agent
        self.log = IdentityLog(db, ip=ip, user_agent=user_agent)
        #: El último intento registrado por el 1:N (el 1:1 lo lleva su `VerificationService`).
        self.last_log: VerificationLog | None = None

    def challenge(self, data: ApiChallengeIn) -> ApiChallenge:
        """El reto de un intento para ESTE dispositivo (reemplaza el anterior del mismo dispositivo): movimientos de la
        política de la empresa, ráfaga si la usa, sin destello y con el reto que firma su llave."""
        device = ApiDevice(self.client.key_id, device_hash_of(data.device_key))
        limit_device(device)
        policy = PolicyService(self.db, self.company_id).current()
        return api_challenge(issue_challenge(self.db, device, policy, self.company_id))

    def verify(
        self,
        take: ApiTake,
        reference: EmployeeReference,
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse,
        camera_label: str | None,
        client: ClientEvidence,
        location: DeviceLocation | None = None,
    ) -> ApiVerificationResult:
        """1:1: ¿es este empleado? Activo y con su registro aprobado (409 si no), contra sus muestras. `location`: dónde
        se hizo (mapa de «Verificaciones»); en ENFORCE, sin ubicación válida se rechaza antes del motor."""
        employee = self._employee(reference)
        ensure_verifiable(employee)
        verifier = VerificationService(self.db, ip=self.ip, user_agent=self.user_agent)
        try:
            result = verifier.verify_employee_face(
                employee,
                take.device,
                take.frontal,
                pipeline,
                liveness=liveness,
                camera_label=camera_label,
                client=client,
                method=METHOD,
                location=location,
                enforce_location=True,
            )
        except StepUpRequired as exc:
            return _step_up(exc, verifier.last_log)
        except SuspiciousCapture as exc:
            return _denied(exc, verifier.last_log)
        return _outcome(result, verifier.last_log)

    def identify(
        self,
        take: ApiTake,
        pipeline: FacePipeline,
        *,
        liveness: LivenessResponse,
        camera_label: str | None,
        client: ClientEvidence,
        location: DeviceLocation | None = None,
    ) -> ApiVerificationResult:
        """1:N: ¿quién de los empleados con registro aprobado de la empresa es? El dispositivo con demasiados intentos
        sospechosos seguidos se bloquea (429 `FACE_LOCKED`). `location`: dónde se hizo (mapa); en ENFORCE, sin ubicación
        válida se rechaza antes del motor."""
        ensure_frame_count(take.frontal)
        policy = PolicyService(self.db, self.company_id).current()
        self.log.location = location  # dónde se hizo (mapa de «Verificaciones»)
        ensure_verification_location(policy, location)
        ensure_unlocked(self.db, policy, device_hash=take.device.device_hash, reasons=SECURITY_REASONS)

        def record(
            employee: Employee | None,
            method: VerificationMethod,
            success: bool,
            score: float | None,
            reason: str | None,
        ) -> None:
            self.last_log = self.log.record(
                company_id=self.company_id,
                employee_id=employee.id if employee else None,
                actor_id=None,
                method=method,
                success=success,
                score=score,
                reason=reason,
                device_hash=take.device.device_hash,
            )

        search = GallerySearch(self.db, self.company_id, policy, actor=take.device, record=record)
        try:
            captured = search.capture(pipeline, take.frontal, liveness, camera_label, client, method=METHOD)
            if isinstance(captured, VerificationResult):
                return _outcome(captured, self.last_log)
            result, _ = search.search(pipeline, captured, method=METHOD)
        except StepUpRequired as exc:
            return _step_up(exc, self.last_log)
        except SuspiciousCapture as exc:
            return _denied(exc, self.last_log)
        return _outcome(result, self.last_log)

    def _employee(self, reference: EmployeeReference) -> Employee:
        """El empleado de la empresa de la llave por su id o su número (exactamente uno: 422); de otra empresa o en
        «Eliminados», 404 como el que no existe."""
        by_id, by_number = reference.employee_id, reference.employee_number
        if (by_id is None) == (by_number is None):
            raise UnprocessableError(code="EMPLOYEE_REFERENCE_INVALID", key="API_EMPLOYEE_REFERENCE_INVALID")
        employees = EmployeeRepository(self.db, self.company_id)
        # El número se guarda en mayúsculas y sin espacios alrededor (`normalize_employee_number`): se busca igual.
        number = str(by_number).strip().upper()
        employee = employees.get_by_id(by_id) if by_id is not None else employees.get_by_number(number)
        if employee is None:
            raise NotFoundError(code="EMPLOYEE_NOT_FOUND")
        return employee


def _outcome(result: VerificationResult, log: VerificationLog | None) -> ApiVerificationResult:
    """Un intento que terminó (coincida o no): permitir, "en revisión" o negar con el motivo de la bitácora."""
    decision: ApiDecision = ("REVIEW" if result.review else "ALLOW") if result.verified else "DENY"
    employee = (
        ApiEmployeeRef(id=result.employee_id, employee_number=result.employee_number, name=result.name or "")
        if result.verified and result.employee_id is not None
        else None
    )
    return ApiVerificationResult(
        matched=result.verified,
        decision=decision,
        reason=None if result.verified or log is None else log.reason,
        confidence=result.confidence,
        employee=employee,
        attempt_id=log.id if log is not None else None,
        verified_at=result.verified_at,
        message=result.text,
    )


def _step_up(exc: StepUpRequired, log: VerificationLog | None) -> ApiVerificationResult:
    """El motor de riesgo pide un paso más: el reto nuevo va en el resultado (no cuenta para el bloqueo)."""
    return ApiVerificationResult(
        matched=False,
        decision="STEP_UP",
        reason=exc.code,
        attempt_id=log.id if log is not None else None,
        challenge=api_challenge(exc.challenge),
        message=exc.text,
    )


def _denied(exc: SuspiciousCapture, log: VerificationLog | None) -> ApiVerificationResult:
    """Un intento sospechoso o de riesgo crítico (ya en la bitácora, cuenta para el bloqueo): negar con su motivo."""
    return ApiVerificationResult(
        matched=False,
        decision="DENY",
        reason=exc.code,
        attempt_id=log.id if log is not None else None,
        message=exc.text,
    )
