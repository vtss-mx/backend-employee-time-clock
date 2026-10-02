"""Modelos ORM: deben coincidir con las migraciones de Alembic (lo verifica scripts/quality.sh)."""

from app.models.auth_session import AuthSession, RateLimitCounter
from app.models.catalog import (
    CatalogAccessory,
    CatalogConfidenceLevel,
    CatalogCountry,
    CatalogEnrollmentFlag,
    CatalogEnrollmentRejectionReason,
    CatalogEnrollmentStatus,
    CatalogFaceError,
    CatalogFaceStatus,
    CatalogLivenessAction,
    CatalogReverificationReason,
    CatalogRole,
    CatalogScreen,
    CatalogSessionRevocationReason,
    CatalogValidatorMode,
    CatalogVerificationMethod,
    CatalogVerificationReason,
    RoleScreen,
    ValidatorModeMethod,
)
from app.models.company import Company
from app.models.employee import Employee
from app.models.employee_qr import EmployeeQr
from app.models.enums import (
    EnrollmentStatus,
    FaceStatus,
    Screen,
    SessionRevocationReason,
    UserRole,
    ValidatorMode,
    VerificationMethod,
)
from app.models.face_challenge import FaceChallenge
from app.models.face_embedding import FaceEmbedding
from app.models.face_enrollment import FaceEnrollment, FaceEnrollmentFlag
from app.models.remembered_account import RememberedAccount
from app.models.user import User
from app.models.validator import Validator
from app.models.verification_log import VerificationLog
from app.models.verification_policy import VerificationPolicy

__all__ = [
    "AuthSession",
    "CatalogAccessory",
    "CatalogConfidenceLevel",
    "CatalogCountry",
    "CatalogEnrollmentFlag",
    "CatalogEnrollmentRejectionReason",
    "CatalogEnrollmentStatus",
    "CatalogFaceError",
    "CatalogFaceStatus",
    "CatalogLivenessAction",
    "CatalogReverificationReason",
    "CatalogRole",
    "CatalogScreen",
    "CatalogSessionRevocationReason",
    "CatalogValidatorMode",
    "CatalogVerificationMethod",
    "CatalogVerificationReason",
    "Company",
    "Employee",
    "EmployeeQr",
    "EnrollmentStatus",
    "FaceChallenge",
    "FaceEmbedding",
    "FaceEnrollment",
    "FaceEnrollmentFlag",
    "FaceStatus",
    "RateLimitCounter",
    "RememberedAccount",
    "RoleScreen",
    "Screen",
    "SessionRevocationReason",
    "User",
    "UserRole",
    "Validator",
    "ValidatorMode",
    "ValidatorModeMethod",
    "VerificationLog",
    "VerificationMethod",
    "VerificationPolicy",
]
