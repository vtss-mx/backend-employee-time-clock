"""Pasos previos de toda captura facial, para cualquier rol: el reto de prueba de vida y la revisión
previa de una imagen (borrosa, oscura, sin el rostro completo o con cubrebocas, antes de enviar; los lentes se
permiten: decisión del dueño, 2026-10-07).

Quién opera la cámara decide qué reglas aplican (una sola implementación para el router):
- EMPLOYEE: su propio rostro, con su excepción de prenda de cabeza.
- COMPANY: el empleado presente (registro asistido o verificación); puede omitir la prenda de cabeza.
- VALIDATOR: aún no sabe quién es (la prenda de cabeza se decide al identificarlo) y su reto
  depende de que su modo use el rostro.
"""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.facial_recognition import FaceAnalysis, FacePipeline, FacePolicy
from app.facial_recognition.pipeline import Accessory, accessory_consensus
from app.models import Screen, User, UserRole
from app.schemas.face import FaceCheckResponse
from app.schemas.verification import ChallengePurpose, FaceChallengeResponse
from app.services.checkpoint_service import CheckpointService
from app.services.employee_access import active_employee
from app.services.face_service import analyze_frames
from app.services.identity_core import issue_challenge
from app.services.policy_service import PolicyService

#: Quienes capturan rostros: el empleado (registro y verificación), el validador (punto de control) y la empresa
#: (registro y verificación en persona). El ADMIN de la plataforma no ve rostros. Las rutas de captura (`/face/*`) y el
#: destello dictado por el canal en vivo (`routers/realtime.py`) se abren con estas pantallas.
FACE_CAPTURE_SCREENS = (
    Screen.EMPLOYEE_ENROLL,
    Screen.EMPLOYEE_VERIFY,
    Screen.VALIDATOR_CHECKPOINT,
    Screen.COMPANY_EMPLOYEES,
)


class FaceCaptureService:
    def __init__(self, db: Session, user: User, company_id: int) -> None:
        self.db = db
        self.user = user
        self.company_id = company_id

    def challenge(self, purpose: ChallengePurpose = "VERIFICATION") -> FaceChallengeResponse:
        """Reto de uso único para quien opera la cámara (registro facial y verificación). El del empleado lleva además
        el reto que firma la llave de su dispositivo (decisión D2), si su empresa los vincula. `purpose="ENROLLMENT"`
        (el empleado que se registra o la empresa con el empleado presente): los cuatro movimientos del registro
        (decisión del dueño, 2026-10-07); un validador nunca registra rostros, así que su reto es siempre el de su
        modo."""
        if self.user.role == UserRole.VALIDATOR:
            return CheckpointService(self.db, self.user).issue_challenge()
        employee = self.user.role == UserRole.EMPLOYEE
        if employee:
            active_employee(self.user)
        policy = PolicyService(self.db, self.company_id).current()
        return issue_challenge(
            self.db, self.user.id, policy, self.company_id, device=employee, enrollment=purpose == "ENROLLMENT"
        )

    def frontal_policy(self, *, allow_headwear: bool) -> FacePolicy:
        """Las reglas de la empresa para quien se captura, con los accesorios SIEMPRE medidos (`report_accessories`:
        una a tres fotos, ≈ 40 ms cada una) para las insignias de la app (decisión del dueño, 2026-10-07)."""
        policy = PolicyService(self.db, self.company_id).current().face_policy(self.user.employee)
        if self.user.role == UserRole.VALIDATOR or (self.user.role == UserRole.COMPANY and allow_headwear):
            policy = replace(policy, block_headwear=False)
        return replace(policy, report_accessories=True)

    @staticmethod
    def analyze(
        pipeline: FacePipeline, images: list[bytes], policy: FacePolicy
    ) -> tuple[list[FaceAnalysis], tuple[Accessory, ...]]:
        """Calidad, pose y accesorios de 1 a 3 capturas (sin comparar identidad ni registrar intentos), en el orden de
        las imágenes, y los accesorios detectados por consenso (bloqueados o no). Solo CPU: quien llama cierra antes
        su transacción. La suplantación se decide en el envío definitivo (verificación) o la revisa COMPANY
        (registro); un accesorio bloqueado responde 422 ACCESSORIES_DETECTED."""
        analyses, _ = analyze_frames(pipeline, images, policy=policy, check_spoof=False)
        return analyses, accessory_consensus([a.accessories_detected for a in analyses])

    @staticmethod
    def report(analysis: FaceAnalysis, detected: tuple[Accessory, ...]) -> FaceCheckResponse:
        """Lo que la app necesita de una captura aceptada: sus números y los accesorios para las insignias."""
        return FaceCheckResponse(
            detection_score=round(analysis.detection_score, 4),
            quality_score=analysis.quality_score,
            yaw_ratio=analysis.pose.yaw_ratio if analysis.pose else None,
            accessories=[a.value for a in detected],
        )

    def precheck(self, pipeline: FacePipeline, images: list[bytes], *, allow_headwear: bool) -> FaceCheckResponse:
        """La validación previa (`POST /face/check`): la captura de menor calidad decide los números y los accesorios
        detectados viajan aunque la empresa no los bloquee (la insignia es el único aviso)."""
        analyses, detected = self.analyze(pipeline, images, self.frontal_policy(allow_headwear=allow_headwear))
        return self.report(min(analyses, key=lambda a: a.quality_score), detected)
