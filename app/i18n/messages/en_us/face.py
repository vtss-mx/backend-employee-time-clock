"""Identidad: registro facial, identificación, punto de control del validador y QR.

Textos en inglés de Estados Unidos (en-US): misma llave, mismos `{parámetros}` y mismas formas de plural
en los dos idiomas (`tests/test_i18n.py`)."""

from app.i18n.messages.base import Messages

MESSAGES: Messages = {
    "CHALLENGE_ISSUED": "Liveness check challenge issued",
    "CHECKPOINT_LOCATION_OUT_OF_RANGE": (
        "You're {distance} from this validator's location. Move within {radius} to identify people."
    ),
    "CHECKPOINT_LOCATION_REQUIRED": (
        "This validator only identifies people at its location. Allow access to your location."
    ),
    "CHECKPOINT_PROFILE": "Validator",
    "CHECKPOINT_RECENT": "Recent identifications",
    "EMPLOYEE_FACE_NOT_APPROVED": "The employee doesn't have an approved face yet: enroll it first",
    "EMPLOYEE_INACTIVE_ENROLL": "The employee is inactive: activate them before enrolling their face",
    "ENROLLMENTS_LISTED": {
        "one": "{count} face enrollment",
        "other": "{count} face enrollments",
    },
    "ENROLLMENT_ALREADY_APPROVED": "Your face enrollment was already approved",
    "ENROLLMENT_ALREADY_REVIEWED": "This enrollment was already reviewed",
    "ENROLLMENT_APPROVED_DONE": "User accepted. They can now verify their identity.",
    "ENROLLMENT_EMPLOYEE_INACTIVE": "Only active employees can enroll their face",
    "ENROLLMENT_FOUND": "Face enrollment found",
    "ENROLLMENT_NOT_FOUND": "Face enrollment not found",
    "ENROLLMENT_PENDING": "Your face enrollment was already sent and is pending validation",
    "ENROLLMENT_REJECTED": "User rejected. They'll need to enroll their face again.",
    "ENROLLMENT_SENT": "Your face enrollment was sent and is pending validation",
    "ENROLLMENT_SUBMITTED": "Face enrollment sent. Your identity is pending validation.",
    "FACE_ALREADY_REGISTERED_AS": "{message} ({name}, {number})",
    "FACE_CHECK_PASSED": "The capture is valid",
    "FACE_ENROLLED_IN_PERSON": "Face enrolled and approved: the employee can now identify themselves",
    "FACE_SIGNAL_ANTISPOOF_REAL": "Minimum real-face probability",
    "FACE_SIGNAL_BURST_MOTION": "Minimum natural motion in the burst",
    "FACE_SIGNAL_FLASH_RATIO": "Minimum face-to-background flash ratio",
    "FACE_SIGNAL_FLASH_SCORE": "Minimum response to the color flash",
    "FACE_SIGNAL_LIVENESS_CLOSER": "Minimum movement toward the camera",
    "FACE_SIGNAL_LIVENESS_PITCH": "Minimum movement when looking up or down",
    "FACE_SIGNAL_LIVENESS_YAW": "Minimum head turn",
    "FACE_SIGNAL_MOIRE": "Maximum screen pattern",
    "FACE_SIGNAL_NOISE_RATIO": "Minimum face-to-background noise ratio",
    "FACE_SIGNAL_PARALLAX": "Minimum parallax when turning the head",
    "FLASH_COLORS": "Flash colors",
    "FLASH_TOKEN_INVALID": "This challenge's flash expired or isn't valid. Request another challenge.",
    "IDENTIFICATION_SUCCESS": "Identity confirmed",
    "IMAGE_REQUIRED": "Send at least one capture",
    "IMAGE_VALID": "Valid image",
    "INVALID_FRAME_COUNT": "Send between {min} and {max} front-facing captures",
    "LIVENESS_NOT_REQUIRED": "Liveness check not required",
    "PHOTO_ERROR": "Photo {number}: {message}",
    "QR_FACE_MISMATCH": "The face doesn't belong to the QR code's owner",
    "QR_HOLDER_FOUND": "Now validate {name}'s face",
    "QR_NOT_FOUND": "QR code not found",
    "QR_REQUIRED": "Scan the employee's QR code first",
    "QR_WITHOUT_ATTENDANCE": "Identified with QR. To record attendance, identify with your face.",
    "REJECTION_REASON_REQUIRED": "Enter the reason for the rejection",
    "SIGNATURE_INVALID": "This device's signature isn't valid. Sign in again.",
    "SIGNATURE_KEY_MISMATCH": "This isn't the device you signed in with. Sign in again on this device.",
    "SIGNATURE_REQUIRED": (
        "This validator must sign each identification with its device. Use the app on an approved device."
    ),
    "SIGNATURE_STALE": "This device's signature expired. Try again.",
    "VALIDATOR_METHOD_NOT_ALLOWED": "This validator identifies people in “{mode}” mode",
    "VALIDATOR_REQUIRED": "This account isn't an identity validator",
}
