"""Pasos previos de toda captura facial, para cualquier rol: el reto de prueba de vida y la revisión
previa de una imagen ("quítate los lentes" antes de enviar).

Quién opera la cámara decide qué reglas aplican (una sola implementación para el router):
- EMPLOYEE: su propio rostro, con su excepción de prenda de cabeza.
- COMPANY: el empleado presente (registro asistido o verificación); puede omitir la prenda de cabeza.
- VALIDATOR: aún no sabe quién es (la prenda de cabeza se decide al identificarlo) y su reto
  depende de que su modo use el rostro.
"""

from dataclasses import replace

from sqlalchemy.orm import Session

from app.facial_recognition import FacePipeline
from app.models import Screen, User, UserRole
from app.schemas.face import FaceCheckResponse
from app.schemas.verification import FaceChallengeResponse
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

    def challenge(self) -> FaceChallengeResponse:
        """Reto de uso único para quien opera la cámara (registro facial y verificación). El del empleado lleva además
        el reto que firma la llave de su dispositivo (decisión D2), si su empresa los vincula."""
        if self.user.role == UserRole.VALIDATOR:
            return CheckpointService(self.db, self.user).issue_challenge()
        employee = self.user.role == UserRole.EMPLOYEE
        if employee:
            active_employee(self.user)
        policy = PolicyService(self.db, self.company_id).current()
        return issue_challenge(self.db, self.user.id, policy, self.company_id, device=employee)

    def precheck(self, pipeline: FacePipeline, images: list[bytes], *, allow_headwear: bool) -> FaceCheckResponse:
        """Calidad, pose y accesorios de 1 a 3 capturas (sin comparar identidad ni registrar intentos).
        La suplantación se decide en el envío definitivo (verificación) o la revisa COMPANY (registro)."""
        policy = PolicyService(self.db, self.company_id).current().face_policy(self.user.employee)
        if self.user.role == UserRole.VALIDATOR or (self.user.role == UserRole.COMPANY and allow_headwear):
            policy = replace(policy, block_headwear=False)
        analyses, _ = analyze_frames(pipeline, images, policy=policy, check_spoof=False)
        analysis = min(analyses, key=lambda a: a.quality_score)
        return FaceCheckResponse(
            detection_score=round(analysis.detection_score, 4),
            quality_score=analysis.quality_score,
            yaw_ratio=analysis.pose.yaw_ratio if analysis.pose else None,
        )
