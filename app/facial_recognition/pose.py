"""Estimación de pose de la cabeza a partir de los 5 landmarks faciales, y las acciones del reto.

yaw_ratio = desplazamiento horizontal de la nariz respecto al punto medio de los ojos,
            dividido entre la distancia entre ojos. Por geometría facial
            (nariz ≈ 2.5 cm delante del plano de los ojos, distancia interocular ≈ 6.3 cm)
            yaw_ratio ≈ 0.4 · tan(ángulo):  15° → 0.11,  27° → 0.20,  45° → 0.40.
            Signo en la imagen original (sin espejo): positivo = la nariz apunta hacia la
            derecha de la imagen = la persona gira hacia SU izquierda.

pitch_ratio = posición vertical de la nariz entre la línea de los ojos (0) y la boca (1). Al mirar
            hacia arriba la punta de la nariz (delante del plano de ojos y boca) sube en la imagen y el
            valor BAJA; al mirar hacia abajo, sube. Unos 15° cambian el valor ≈ 0.09.

Una fotografía impresa o una pantalla girada o inclinada se deforma en perspectiva, pero sus puntos
permanecen coplanares: el yaw_ratio se mantiene cercano a 0 y el pitch_ratio casi no cambia. Por eso
los movimientos de cabeza sirven como prueba de vida.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

from app.facial_recognition.engine import FaceLandmarks


class LivenessAction(StrEnum):
    """Movimientos que puede pedir el reto (catalog.liveness_actions), elegidos al azar en cada intento:
    un video grabado o generado de antemano no conoce la secuencia."""

    TURN_LEFT = "TURN_LEFT"  # hacia la izquierda de la persona
    TURN_RIGHT = "TURN_RIGHT"
    LOOK_UP = "LOOK_UP"
    LOOK_DOWN = "LOOK_DOWN"
    MOVE_CLOSER = "MOVE_CLOSER"


@dataclass(frozen=True)
class HeadPose:
    yaw_ratio: float
    roll_degrees: float
    pitch_ratio: float  # posición vertical de la nariz entre ojos (0) y boca (1)


@dataclass(frozen=True)
class StepTarget:
    """Lo que debe mostrar la captura de un paso del reto, medido contra las frontales de la misma toma
    (la persona "en reposo"): así no importa la forma de cada rostro ni la altura de la cámara."""

    #: Giro mínimo (yaw_ratio) hacia el lado pedido.
    min_yaw_ratio: float
    #: Cambio mínimo del pitch_ratio al mirar hacia arriba o hacia abajo.
    min_pitch_delta: float
    #: Al acercarse, cuántas veces debe crecer el ancho del rostro.
    min_closer_scale: float
    #: Pitch y ancho del rostro (px) de las capturas frontales.
    baseline_pitch: float
    baseline_width: float

    def required(self, action: LivenessAction) -> float:
        """El mínimo que debe alcanzar `step_measure` para la acción."""
        if action in (LivenessAction.TURN_LEFT, LivenessAction.TURN_RIGHT):
            return self.min_yaw_ratio
        if action == LivenessAction.MOVE_CLOSER:
            return self.min_closer_scale
        return self.min_pitch_delta


def step_measure(action: LivenessAction, pose: HeadPose, face_width: float, target: StepTarget) -> float:
    """Cuánto se movió la persona en el sentido de la acción (mayor = más claro).

    Giros: yaw_ratio hacia el lado pedido. Mirar arriba/abajo: cambio del pitch respecto a las
    frontales. Acercarse: ancho del rostro entre el de las frontales.
    """
    if action == LivenessAction.TURN_LEFT:
        return pose.yaw_ratio
    if action == LivenessAction.TURN_RIGHT:
        return -pose.yaw_ratio
    if action == LivenessAction.LOOK_UP:
        return target.baseline_pitch - pose.pitch_ratio
    if action == LivenessAction.LOOK_DOWN:
        return pose.pitch_ratio - target.baseline_pitch
    return face_width / max(target.baseline_width, 1e-6)


def estimate_pose(lm: FaceLandmarks) -> HeadPose:
    left_eye, right_eye = sorted((lm.eye_a, lm.eye_b))  # por coordenada x de la imagen
    eye_mid_x = (left_eye[0] + right_eye[0]) / 2
    eye_mid_y = (left_eye[1] + right_eye[1]) / 2
    eye_dx = right_eye[0] - left_eye[0]
    eye_dy = right_eye[1] - left_eye[1]
    eye_dist = max(math.hypot(eye_dx, eye_dy), 1e-6)

    yaw_ratio = (lm.nose[0] - eye_mid_x) / eye_dist
    roll = math.degrees(math.atan2(eye_dy, eye_dx))

    mouth_mid_y = (lm.mouth_a[1] + lm.mouth_b[1]) / 2
    pitch_ratio = (lm.nose[1] - eye_mid_y) / max(mouth_mid_y - eye_mid_y, 1e-6)
    return HeadPose(yaw_ratio=round(yaw_ratio, 4), roll_degrees=round(roll, 2), pitch_ratio=round(pitch_ratio, 4))
