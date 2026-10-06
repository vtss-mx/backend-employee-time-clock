"""Presencia del validador en CADA identificación (antifraude fase 2b; docs/rd/antifraude-identidad.md §5.2 y §7.1).

Hasta la fase 2a, el dispositivo y la ubicación de un validador se revisaban solo al iniciar sesión (riesgo R7: el token
servía después desde cualquier lugar y equipo). Ahora cada identificación (rostro 1:N, QR y QR + rostro) trae:

- la firma de la llave del dispositivo de la sesión sobre un reto del servidor y su contenido (`request_signing`);
- si el validador requiere ubicación, una lectura fresca con su precisión (y las varias lecturas de la ventana corta de
  la app, como la asistencia del empleado: `location_samples`), revisada con la MISMA regla del inicio de sesión
  (`location_service.check`).

Cada prueba sigue su modo de la política (`validator_signing`, `validator_location`; modos de `signal_modes`): apagada,
«Solo medir» (por omisión: señales del motor de riesgo, nunca un rechazo) u obligatoria (403 con el código del inicio de
sesión, ANTES de consumir el reto o el QR). Las señales viajan con el intento (`ClientEvidence.checks`) y la ubicación
alimenta además las señales del lugar de la fase 1b (`risk_engine.location_context`: precisión redonda, lecturas
idénticas, borde del radio y país de la red frente al del validador). Una identificación solo con QR no pasa por el
motor (no hay rostro): con «Solo medir» lo que no cumple queda en el log del proceso.

Sin estado ni consultas de más: el reto es un HMAC, la sesión ya está en memoria y la distancia es aritmética.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import RiskSignal, SignalMode, Validator
from app.schemas.auth import DeviceLocation
from app.schemas.capture import LocationSample
from app.services.location_service import IDENTIFY_KEYS, LocationVerdict, check
from app.services.policy_service import PolicySnapshot
from app.services.request_signing import RequestProof, RequestSigning
from app.services.risk_engine import LocationEvidence
from app.services.risk_rules import Hit


@dataclass(frozen=True)
class IdentificationRequest:
    """Lo que trae una identificación además de lo que identifica: la firma y la ubicación del dispositivo."""

    proof: RequestProof = field(default_factory=RequestProof)
    location: DeviceLocation | None = None
    samples: tuple[LocationSample, ...] = ()


@dataclass(frozen=True)
class Presence:
    """Lo que se concluyó de la presencia del validador: sus señales y la ubicación para el motor (None sin ella)."""

    hits: tuple[Hit, ...] = ()
    evidence: LocationEvidence | None = None


def _location_hit(verdict: LocationVerdict) -> Hit:
    """La señal de un problema de la ubicación (lo medido y su umbral)."""
    if verdict.code == "LOCATION_REQUIRED":
        return Hit(RiskSignal.VALIDATOR_LOCATION_MISSING)
    if verdict.code == "LOCATION_INACCURATE":
        accuracy = (verdict.details or {}).get("accuracy_m")  # None: el dispositivo no la informó
        return Hit(RiskSignal.VALIDATOR_LOCATION_INACCURATE, accuracy)
    return Hit(RiskSignal.VALIDATOR_OUT_OF_ZONE, round(verdict.distance or 0.0, 1), float(verdict.radius))


class ValidatorPresence:
    """La firma y la ubicación de las identificaciones de UN validador en su sesión."""

    def __init__(
        self, db: Session, validator: Validator, session: tuple[str | None, str | None], policy: PolicySnapshot
    ) -> None:
        """`session`: el id de la sesión de la petición y la llave a la que está ligada (`RequestSigning`)."""
        self.validator = validator
        self.policy = policy
        self.signing = RequestSigning(db, validator, session, policy)

    @property
    def location_required(self) -> bool:
        """La app manda la ubicación en cada identificación (el validador la requiere y la empresa la revisa)."""
        return self.validator.location_required and self.policy.validator_location != SignalMode.OFF

    def next_nonce(self) -> str | None:
        return self.signing.next_nonce()

    def check(self, request: IdentificationRequest, action: str, digest: str) -> Presence:
        """Las señales de la firma y de la ubicación de esta identificación; lo obligatorio que no cumple se rechaza
        aquí (403), antes de consumir el reto o el QR."""
        hits = self.signing.check(request.proof, action, digest)
        if not self.location_required:
            return Presence(tuple(hits))
        verdict = check(self.validator, request.location)
        if verdict.code is not None:
            if self.policy.validator_location == SignalMode.ENFORCE:
                raise verdict.error(IDENTIFY_KEYS)
            hits.append(_location_hit(verdict))
        if request.location is None:
            return Presence(tuple(hits))
        evidence = LocationEvidence(
            accuracy_m=request.location.accuracy or 0.0,
            distance_m=verdict.distance,
            radius_m=verdict.radius,
            samples=request.samples,
            site_country=self.validator.country_code,
        )
        return Presence(tuple(hits), evidence)
