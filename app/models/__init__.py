"""Modelos ORM: deben coincidir con las migraciones de Alembic (lo verifica scripts/quality.sh)."""

from app.models.auth_session import AuthSession, RateLimitCounter
from app.models.capture_fingerprint import CaptureFingerprint
from app.models.catalog import (
    CatalogAccessory,
    CatalogAntispoofLevel,
    CatalogApiKeyStatus,
    CatalogApiScope,
    CatalogConfidenceLevel,
    CatalogCountry,
    CatalogDeviceStatus,
    CatalogEnrollmentFlag,
    CatalogEnrollmentRejectionReason,
    CatalogEnrollmentStatus,
    CatalogErrorSeverity,
    CatalogErrorStatus,
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
from app.models.company_api_key import CompanyApiKey, CompanyApiKeyScope
from app.models.department import Department, DepartmentManager
from app.models.employee import Employee
from app.models.employee_qr import EmployeeQr
from app.models.enums import (
    ApiKeyStatus,
    ApiScope,
    DeviceStatus,
    EnrollmentStatus,
    ErrorSeverity,
    ErrorStatus,
    FaceStatus,
    Screen,
    SessionRevocationReason,
    UserRole,
    ValidatorMode,
    VerificationMethod,
)
from app.models.error_report import ErrorOccurrence, ErrorReport
from app.models.face_challenge import FaceChallenge
from app.models.face_embedding import FaceEmbedding
from app.models.face_enrollment import FaceEnrollment, FaceEnrollmentFlag
from app.models.remembered_account import RememberedAccount
from app.models.report import AssistantQuery, LearnedPhrase, SavedReport
from app.models.user import User
from app.models.validator import Validator
from app.models.validator_device import ValidatorDevice
from app.models.verification_log import VerificationLog
from app.models.verification_policy import VerificationPolicy

__all__ = [
    "ApiKeyStatus",
    "ApiScope",
    "AssistantQuery",
    "AuthSession",
    "CaptureFingerprint",
    "CatalogAccessory",
    "CatalogAntispoofLevel",
    "CatalogApiKeyStatus",
    "CatalogApiScope",
    "CatalogConfidenceLevel",
    "CatalogCountry",
    "CatalogDeviceStatus",
    "CatalogEnrollmentFlag",
    "CatalogEnrollmentRejectionReason",
    "CatalogEnrollmentStatus",
    "CatalogErrorSeverity",
    "CatalogErrorStatus",
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
    "CompanyApiKey",
    "CompanyApiKeyScope",
    "Department",
    "DepartmentManager",
    "DeviceStatus",
    "Employee",
    "EmployeeQr",
    "EnrollmentStatus",
    "ErrorOccurrence",
    "ErrorReport",
    "ErrorSeverity",
    "ErrorStatus",
    "FaceChallenge",
    "FaceEmbedding",
    "FaceEnrollment",
    "FaceEnrollmentFlag",
    "FaceStatus",
    "LearnedPhrase",
    "RateLimitCounter",
    "RememberedAccount",
    "RoleScreen",
    "SavedReport",
    "Screen",
    "SessionRevocationReason",
    "User",
    "UserRole",
    "Validator",
    "ValidatorDevice",
    "ValidatorMode",
    "ValidatorModeMethod",
    "VerificationLog",
    "VerificationMethod",
    "VerificationPolicy",
]
