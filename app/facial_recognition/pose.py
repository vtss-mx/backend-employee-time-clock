"""Estimación de pose de la cabeza a partir de los 5 landmarks faciales.

yaw_ratio = desplazamiento horizontal de la nariz respecto al punto medio de los ojos,
            dividido entre la distancia entre ojos. Por geometría facial
            (nariz ≈ 2.5 cm delante del plano de los ojos, distancia interocular ≈ 6.3 cm)
            yaw_ratio ≈ 0.4 · tan(ángulo):  15° → 0.11,  27° → 0.20,  45° → 0.40.
            Signo en la imagen original (sin espejo): positivo = la nariz apunta hacia la
            derecha de la imagen = la persona gira hacia SU izquierda.

Una fotografía impresa o una pantalla girada se deforma en perspectiva pero sus puntos
permanecen coplanares, por lo que el yaw_ratio se mantiene cercano a 0: esto es lo que
hace útil el reto de "girar la cabeza" como prueba de vida.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

from app.facial_recognition.engine import FaceLandmarks


class TurnDirection(StrEnum):
    LEFT = "TURN_LEFT"  # hacia la izquierda de la persona
    RIGHT = "TURN_RIGHT"


@dataclass(frozen=True)
class HeadPose:
    yaw_ratio: float
    roll_degrees: float
    pitch_ratio: float  # posición vertical de la nariz entre ojos (0) y boca (1)

    def turned(self, direction: TurnDirection, min_ratio: float) -> bool:
        sign = 1 if direction == TurnDirection.LEFT else -1
        return sign * self.yaw_ratio >= min_ratio


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
