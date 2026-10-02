import math

from app.facial_recognition.engine import FaceLandmarks
from app.facial_recognition.pose import TurnDirection, estimate_pose


def _landmarks(yaw_deg: float, roll_deg: float = 0.0) -> FaceLandmarks:
    """Modelo geométrico simple: ojos a ±3.15 cm, nariz 2.5 cm delante del plano de los ojos."""
    t = math.radians(yaw_deg)
    eyes = [(-3.15 * math.cos(t), 0.0), (3.15 * math.cos(t), 0.0)]
    nose = (2.5 * math.sin(t), 3.0)
    mouth = [(-2.5 * math.cos(t), 6.0), (2.5 * math.cos(t), 6.0)]
    r = math.radians(roll_deg)

    def rot(p):
        return (
            100 + 10 * (p[0] * math.cos(r) - p[1] * math.sin(r)),
            100 + 10 * (p[0] * math.sin(r) + p[1] * math.cos(r)),
        )

    return FaceLandmarks(rot(eyes[0]), rot(eyes[1]), rot(nose), rot(mouth[0]), rot(mouth[1]))


def test_frontal_pose():
    pose = estimate_pose(_landmarks(0))
    assert abs(pose.yaw_ratio) < 0.01 and abs(pose.roll_degrees) < 0.5
    assert 0.4 < pose.pitch_ratio < 0.6


def test_turn_direction_and_magnitude():
    left = estimate_pose(_landmarks(30))  # persona gira hacia SU izquierda -> nariz a la derecha de la imagen
    right = estimate_pose(_landmarks(-30))
    assert left.turned(TurnDirection.LEFT, 0.20) and not left.turned(TurnDirection.RIGHT, 0.20)
    assert right.turned(TurnDirection.RIGHT, 0.20) and not right.turned(TurnDirection.LEFT, 0.20)
    assert not estimate_pose(_landmarks(10)).turned(TurnDirection.LEFT, 0.20)


def test_roll_detected():
    assert abs(estimate_pose(_landmarks(0, roll_deg=20)).roll_degrees - 20) < 0.5
