import math

from app.facial_recognition.engine import FaceLandmarks
from app.facial_recognition.pose import LivenessAction, StepTarget, estimate_pose, step_measure


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


TARGET = StepTarget(
    min_yaw_ratio=0.2, min_pitch_delta=0.08, min_closer_scale=1.25, baseline_pitch=0.5, baseline_width=100
)


def _turned(yaw_deg: float, action: LivenessAction) -> bool:
    return step_measure(action, estimate_pose(_landmarks(yaw_deg)), 100, TARGET) >= TARGET.required(action)


def test_turn_direction_and_magnitude():
    # La persona gira hacia SU izquierda: la nariz va a la derecha de la imagen.
    assert _turned(30, LivenessAction.TURN_LEFT) and not _turned(30, LivenessAction.TURN_RIGHT)
    assert _turned(-30, LivenessAction.TURN_RIGHT) and not _turned(-30, LivenessAction.TURN_LEFT)
    assert not _turned(10, LivenessAction.TURN_LEFT)


def test_look_up_down_and_closer_are_measured_against_the_frontal_baseline():
    pose = estimate_pose(_landmarks(0))
    up = type(pose)(yaw_ratio=0.0, roll_degrees=0.0, pitch_ratio=0.38)
    down = type(pose)(yaw_ratio=0.0, roll_degrees=0.0, pitch_ratio=0.62)
    assert step_measure(LivenessAction.LOOK_UP, up, 100, TARGET) == 0.5 - 0.38
    assert step_measure(LivenessAction.LOOK_DOWN, down, 100, TARGET) == 0.62 - 0.5
    assert step_measure(LivenessAction.LOOK_UP, down, 100, TARGET) < 0  # miró al revés
    assert step_measure(LivenessAction.MOVE_CLOSER, pose, 140, TARGET) == 1.4
    assert TARGET.required(LivenessAction.LOOK_DOWN) == 0.08 and TARGET.required(LivenessAction.MOVE_CLOSER) == 1.25


def test_roll_detected():
    assert abs(estimate_pose(_landmarks(0, roll_deg=20)).roll_degrees - 20) < 0.5
