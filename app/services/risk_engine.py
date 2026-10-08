"""Motor de riesgo (servicio): las señales de un intento facial, su decisión explicable y su registro.

Corre DENTRO de la petición, después de que el rostro coincidió (el candado de identidad ya pasó) y antes de
registrar el éxito o de que la galería aprenda (docs/rd/antifraude-identidad.md §4.6):

- Señales que ya se midieron en el intento (`face_signals`): probabilidad de rostro real, respuesta al destello y
  su cociente rostro/fondo, holgura de la coincidencia, cámara sin nombre, empresa reforzada.
- Señales del lugar (asistencia del empleado): las anota `location_context` antes de verificar (precisión, borde de
  la geocerca, lecturas idénticas, velocidad desde el registro anterior, país del sitio y red del registro anterior).
- Antifraude 1b (lo que informó el cliente, `client_evidence`): telemetría del navegador, tablas JPEG de las
  capturas, red de la IP con la base LOCAL DB-IP (`ip_intel`, sin consultas), el dispositivo del empleado
  (`employee_devices`) y el 1:N en cada 1:1 (el otro empleado más parecido, calculado antes, sin transacción).
- Antifraude 2a (`capture_protocol.protocol_hits`, sin consultas): la ráfaga de recortes, el destello dictado por el
  servidor, el moiré y el ruido de las frontales y el paralaje de los giros, ya medidos en el intento.
- Antifraude 2b (ya verificado antes del intento, `ClientEvidence.checks`): la firma por petición y la ubicación del
  validador (`validator_presence`) y el código de sitio del empleado (`site_codes`, por la ubicación del registro).
- A lo más TRES lecturas indexadas, solo si hacen falta: las capturas recientes del empleado (reenvío perceptual), la
  lista de bloqueo (en memoria, con su caché) y el dispositivo del empleado (su fila y quién más usó su llave).
- La decisión (`risk_rules.decide`) con la configuración de la empresa (catálogo `risk_signals` + su política).

Si algo falla (BD lenta, dato corrupto) se aplica la acción de respaldo de la política (permitir, permitir y avisar
o un paso más), se registra el error (`logger.exception` → "Errores del sistema") y nunca se deja a la persona sin
respuesta. La decisión viaja con el intento (`face_signals.assessment`) y se guarda con la bitácora.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import try_decrypt
from app.core.ip_intel import ip_intel
from app.facial_recognition import FaceAnalysis
from app.facial_recognition.jpeg_tables import unexpected
from app.facial_recognition.matcher import cosine_similarity, embedding_from_bytes
from app.facial_recognition.phash import distance
from app.models import RiskAction, RiskAssessment, RiskSignal, RiskTier, SignalMode
from app.repositories.risk_repository import AttackSignatureRepository, CaptureTraceRepository
from app.schemas.capture import LocationSample
from app.services import attack_signatures, employee_devices, face_signals
from app.services.capture_protocol import protocol_hits
from app.services.catalog_service import get_catalogs
from app.services.client_evidence import telemetry_hits
from app.services.face_security import thresholds
from app.services.policy_service import PolicySnapshot
from app.services.risk_rules import ENGINE_VERSION, Decision, Hit, RiskConfig, SignalSetting, decide, review_reasons

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreviousPunch:
    """El registro anterior del empleado con ubicación: hace cuánto y desde qué red (NETWORK_JUMP)."""

    minutes_ago: float
    ip_country: str | None = None
    ip_asn: int | None = None


@dataclass(frozen=True)
class LocationEvidence:
    """La ubicación de un registro de asistencia (para las señales del lugar y de la red)."""

    accuracy_m: float
    #: Distancia al sitio y su radio (None si fue remoto).
    distance_m: float | None = None
    radius_m: int | None = None
    #: Las lecturas que tomó la app en su ventana corta (LOCATION_STATIC y la constancia de la precisión).
    samples: tuple[LocationSample, ...] = ()
    #: Velocidad desde el registro anterior (km/h, descontando la incertidumbre) y desde cuál es LOCATION_JUMP.
    speed_kmh: float | None = None
    jump_kmh: float | None = None
    #: País del sitio donde checa (NETWORK_COUNTRY_MISMATCH) y el registro anterior (NETWORK_JUMP).
    site_country: str | None = None
    previous: PreviousPunch | None = None
    #: Lo que el servidor ya verificó del lugar antes del intento (antifraude 2b: el código de sitio, `site_codes`).
    checks: tuple[Hit, ...] = ()


_location: ContextVar[LocationEvidence | None] = ContextVar("risk_location", default=None)


@contextmanager
def location_context(evidence: LocationEvidence) -> Iterator[None]:
    """La ubicación del registro en curso, visible para el motor solo dentro del bloque (la petición)."""
    token = _location.set(evidence)
    try:
        yield
    finally:
        _location.reset(token)


def config_for(policy: PolicySnapshot) -> RiskConfig:
    """La configuración de riesgo de la empresa: cada señal activa del catálogo con los ajustes de su política."""
    overrides: Mapping[str, Mapping[str, Any]] = policy.risk_signals
    signals = {}
    for row in get_catalogs().entries["risk_signals"]:
        if not row["active"]:
            continue
        own = overrides.get(row["code"], {})
        signals[row["code"]] = SignalSetting(
            code=row["code"],
            kind=row["kind"],
            points=int(own.get("points", row["points"])),
            mode=str(own.get("mode", row["mode"])),
            hard=bool(row["hard"]),
            review_reason=row["review_reason"],
        )
    return RiskConfig(
        enabled=policy.risk_engine,
        medium=policy.risk_medium_score,
        high=policy.risk_high_score,
        critical=policy.risk_critical_score,
        medium_action=policy.risk_medium_action,
        high_action=policy.risk_high_action,
        critical_action=policy.risk_critical_action,
        fallback=policy.risk_fallback_action,
        family_cap=settings.RISK_FAMILY_MAX_POINTS,
        device_mode=policy.employee_device_mode,
        signals=signals,
    )


def measures(policy: PolicySnapshot, code: RiskSignal) -> bool:
    """La señal se mide en la empresa (existe, está activa en el catálogo y su modo no es «Apagada»): lo que cuesta
    medirla (una lectura, la galería del 1:N) solo se paga si sirve."""
    setting = config_for(policy).signals.get(code)
    return setting is not None and setting.mode != SignalMode.OFF


#: Motivo de la bitácora de cada regla dura (lo que se le responde a la persona sale de `face_errors`).
HARD_REASONS = {RiskSignal.REPLAY_PERCEPTUAL: "REPLAY_PERCEPTUAL", RiskSignal.KNOWN_ATTACK: "KNOWN_ATTACK"}
#: Motivo de la bitácora cuando el puntaje (no una regla dura) niega el intento.
DENIED_REASON = "RISK_DENIED"


@dataclass(frozen=True)
class RiskOutcome:
    """Lo que el flujo hace con la decisión."""

    decision: Decision
    config: RiskConfig
    fallback: bool = False

    @property
    def action(self) -> RiskAction:
        return self.decision.action

    @property
    def deny_reason(self) -> str:
        hard = self.decision.hard
        return HARD_REASONS.get(RiskSignal(hard), DENIED_REASON) if hard else DENIED_REASON

    @property
    def review(self) -> bool:
        return self.action == RiskAction.REVIEW

    @property
    def clean(self) -> bool:
        """Nada pesó (ni lo que solo se mide de las reglas duras): la galería puede aprender de esta captura."""
        hard_seen = any(self.config.signals[r.code].hard for r in self.decision.reasons)
        return self.action == RiskAction.ALLOW and self.decision.tier == RiskTier.LOW and not hard_seen


@dataclass(frozen=True)
class Match:
    """La coincidencia que se evalúa: la similitud de cada captura y la que exige la empresa."""

    similarities: Sequence[float]
    required: float


class RiskEngine:
    """Evalúa un intento que ya pasó los candados y coincidió (ver el docstring del módulo)."""

    def __init__(self, db: Session, company_id: int, policy: PolicySnapshot) -> None:
        self.db = db
        self.company_id = company_id
        self.policy = policy

    def evaluate(
        self, *, employee_id: int | None, frontal: Sequence[FaceAnalysis], match: Match, can_step_up: bool = True
    ) -> RiskOutcome:
        config = config_for(self.policy)
        signals = face_signals.current()
        try:
            # Sus lecturas van en un SAVEPOINT: si fallan, solo se deshace lo suyo (las huellas anti-reenvío que ya
            # reclamó el intento siguen en la transacción y se guardan con la bitácora).
            with self.db.begin_nested():
                hits = self._hits(config, employee_id, frontal, match)
            outcome = RiskOutcome(decide(hits, config, step_up_done=signals.step_up, can_step_up=can_step_up), config)
        except Exception:
            # Nunca deja a la persona sin respuesta: la acción de respaldo de la política y el error registrado.
            logger.exception(
                "El motor de riesgo falló: se aplica la acción de respaldo de la empresa %s", self.company_id
            )
            outcome = RiskOutcome(Decision(0, RiskTier.LOW, RiskAction(config.fallback), ()), config, fallback=True)
        signals.assessment = RiskAssessment(
            company_id=self.company_id,
            score=outcome.decision.score,
            tier=outcome.decision.tier,
            action=outcome.action,
            reasons=[r.as_dict() for r in outcome.decision.reasons],
            policy_version=config.version(),
            engine=ENGINE_VERSION,
            step_up=signals.step_up,
            fallback=outcome.fallback,
        )
        signals.review_reasons = review_reasons(outcome.decision, config) if outcome.review else ()
        return outcome

    # ------------------------------------------------------------------ señales

    def _hits(
        self, config: RiskConfig, employee_id: int | None, frontal: Sequence[FaceAnalysis], match: Match
    ) -> list[Hit]:
        def on(code: RiskSignal) -> bool:
            setting: SignalSetting | None = config.signals.get(code)
            return setting is not None and setting.mode != SignalMode.OFF

        signals = face_signals.current()
        hits = [*self._capture_hits(frontal, match), *self._location_hits(), *self._identity_hits(match)]
        hits.extend(self._client_hits(employee_id))
        if signals.camera_required and not signals.camera:
            hits.append(Hit(RiskSignal.CAMERA_LABEL_MISSING))
        if signals.reinforced:
            hits.append(Hit(RiskSignal.COMPANY_UNDER_ATTACK))
        if on(RiskSignal.REPLAY_PERCEPTUAL) and employee_id is not None:
            hits.extend(self._replay(employee_id, frontal))
        if on(RiskSignal.KNOWN_ATTACK):
            hits.extend(self._known_attack(frontal))
        return hits

    def _capture_hits(self, frontal: Sequence[FaceAnalysis], match: Match) -> list[Hit]:
        """Lo que ya se midió de las capturas: rostro real, destello, holgura de la coincidencia y el protocolo de
        captura (ráfaga, destello dictado, moiré, ruido y paralaje)."""
        hits: list[Hit] = []
        reals = [f.real_probability for f in frontal if f.real_probability is not None]
        low_real = self.policy.antispoof_threshold * settings.RISK_SPOOF_LOW_FACTOR
        if self.policy.anti_spoofing and reals and min(reals) < low_real:
            hits.append(Hit(RiskSignal.SPOOF_PROB_LOW, round(min(reals), 4), round(low_real, 4)))
        flash = face_signals.current().flash
        if flash is not None and flash.conclusive(settings.FACE_FLASH_MIN_MAGNITUDE):
            limits = thresholds(self.db)
            if flash.score < limits.flash_min_score:
                hits.append(Hit(RiskSignal.FLASH_WEAK, flash.score, limits.flash_min_score))
            ratio = flash.face_ratio
            if ratio is not None and ratio < limits.flash_min_ratio:
                hits.append(Hit(RiskSignal.FLASH_FLAT, ratio, limits.flash_min_ratio))
        if match.similarities:
            margin = round(min(match.similarities) - match.required, 4)
            if margin < settings.RISK_MATCH_MARGIN:
                hits.append(Hit(RiskSignal.MATCH_MARGIN_LOW, margin, settings.RISK_MATCH_MARGIN))
        hits.extend(protocol_hits(face_signals.current(), thresholds(self.db)))
        return hits

    @staticmethod
    def _location_hits() -> list[Hit]:
        """Ubicación simulada: precisión "redonda" y constante (5.0, 10.0... en todas las lecturas), siempre en el
        borde de la geocerca, lecturas idénticas (un simulador no tiembla) o un salto más rápido de lo creíble.

        La precisión redonda y las lecturas idénticas solo se juzgan en una lectura de GPS
        (`RISK_LOCATION_GPS_MAX_ACCURACY_M`): un GPS real tiembla y su precisión cambia, pero la ubicación de la red
        (una computadora con Wi-Fi), del celular o aproximada (iOS sin "Ubicación exacta", Android aproximada) se
        repite exacta durante minutos sin ser un simulador. Ahí la señal no se puede medir y no cuenta (compatibilidad
        universal, docs/rd/compatibilidad-biometria.md)."""
        evidence = _location.get()
        if evidence is None:
            return []
        hits: list[Hit] = list(evidence.checks)
        gps = evidence.accuracy_m <= settings.RISK_LOCATION_GPS_MAX_ACCURACY_M
        accuracies = {evidence.accuracy_m, *(sample.accuracy for sample in evidence.samples)}
        if gps and len(accuracies) == 1 and _round_accuracy(evidence.accuracy_m):
            hits.append(Hit(RiskSignal.LOCATION_ROUND_ACCURACY, evidence.accuracy_m))
        if evidence.distance_m is not None and evidence.radius_m:
            ratio = round(evidence.distance_m / evidence.radius_m, 3)
            if ratio > settings.RISK_LOCATION_EDGE_RATIO:
                hits.append(Hit(RiskSignal.LOCATION_EDGE, ratio, settings.RISK_LOCATION_EDGE_RATIO))
        samples = evidence.samples
        if gps and len(samples) >= settings.RISK_LOCATION_STATIC_MIN_SAMPLES and len(set(samples)) == 1:
            hits.append(Hit(RiskSignal.LOCATION_STATIC, float(len(samples)), settings.RISK_LOCATION_STATIC_MIN_SAMPLES))
        speed, jump = evidence.speed_kmh, evidence.jump_kmh
        if speed is not None and jump is not None and speed > jump:
            hits.append(Hit(RiskSignal.LOCATION_JUMP, round(speed, 1), round(jump, 1)))
        return hits

    @staticmethod
    def _identity_hits(match: Match) -> list[Hit]:
        """1:N en cada 1:1: el rostro se parece a OTRO empleado de la empresa al menos tanto como al que se verifica
        (registro aprobado con el rostro de otra persona, empleado fantasma o suplantación entre parecidos)."""
        rival = face_signals.current().rival
        if rival is None or not match.similarities:
            return []
        gap = round(rival[1] - min(match.similarities), 4)
        if gap < settings.RISK_IDENTITY_MISMATCH_MARGIN:
            return []
        return [Hit(RiskSignal.IDENTITY_MISMATCH, gap, settings.RISK_IDENTITY_MISMATCH_MARGIN)]

    def _client_hits(self, employee_id: int | None) -> list[Hit]:
        """Lo que informó el cliente: tablas JPEG de las capturas, telemetría del navegador, red de la IP y el
        dispositivo del empleado (este último, una lectura indexada que también decide el modo del dispositivo)."""
        signals = face_signals.current()
        hits: list[Hit] = []
        odd, quality = unexpected(signals.jpeg, settings.FACE_CAPTURE_JPEG_QUALITY)
        if odd:
            value = None if quality is None else float(quality)
            hits.append(Hit(RiskSignal.JPEG_TABLE_UNKNOWN, value, float(settings.FACE_CAPTURE_JPEG_QUALITY)))
        client = signals.client
        if client is None:
            return hits
        hits.extend(client.checks)
        hits.extend(telemetry_hits(client.telemetry, client.user_agent))
        hits.extend(_network_hits(client.ip))
        if client.device is not None and employee_id is not None and signals.actor_id is not None:
            device = employee_devices.check(
                self.db,
                self.company_id,
                employee_id,
                signals.actor_id,
                client.device,
                mode=self.policy.employee_device_mode,
                user_agent=client.user_agent,
                now=datetime.now(UTC),
            )
            signals.device = device
            hits.extend(device.hits() if device is not None else ())
        return hits

    def _replay(self, employee_id: int, frontal: Sequence[FaceAnalysis]) -> list[Hit]:
        """Reenvío perceptual (P2): alguna captura casi idéntica (pHash cerca Y embedding casi igual) a otra de un
        intento anterior del mismo empleado."""
        since = datetime.now(UTC) - timedelta(days=settings.FACE_REPLAY_RETENTION_DAYS)
        rows = CaptureTraceRepository(self.db, self.company_id).recent(
            employee_id, since, settings.FACE_PERCEPTUAL_MAX_CAPTURES
        )
        best: tuple[int, float] | None = None
        for capture in frontal:
            if capture.face_phash is None:
                continue
            for face_phash, _frame, encrypted, dimension in rows:
                bits = distance(capture.face_phash, face_phash)
                if bits > settings.FACE_PHASH_MAX_DISTANCE:
                    continue
                similarity = _similarity(capture.embedding, encrypted, dimension)
                if similarity >= settings.FACE_PERCEPTUAL_MIN_COSINE and (best is None or bits < best[0]):
                    best = (bits, similarity)
        if best is None:
            return []
        return [Hit(RiskSignal.REPLAY_PERCEPTUAL, float(best[0]), float(settings.FACE_PHASH_MAX_DISTANCE))]

    def _known_attack(self, frontal: Sequence[FaceAnalysis]) -> list[Hit]:
        captures = [
            (f.face_phash, f.frame_phash) for f in frontal if f.face_phash is not None and f.frame_phash is not None
        ]
        found = attack_signatures.matches(self.db, self.company_id, captures)
        if not found:
            return []
        AttackSignatureRepository(self.db).record_hits(found, datetime.now(UTC))
        return [Hit(RiskSignal.KNOWN_ATTACK, float(len(found)))]


def _round_accuracy(accuracy: float) -> bool:
    """Una precisión "redonda" (5.0, 10.0, 25.0...): la que suele inventar un simulador de ubicación."""
    return accuracy > 0 and float(accuracy).is_integer() and accuracy % 5 == 0


def _network_hits(ip: str | None) -> list[Hit]:
    """La red de la IP con la base LOCAL (`ip_intel`, microsegundos y sin consultas; la IP nunca sale del servidor):
    nube o centro de datos, otro país que el del sitio donde checa, o un cambio de país o de red respecto del registro
    anterior en menos de lo creíble. Sin base o con una IP privada no se mide nada."""
    info = ip_intel.lookup(ip)
    face_signals.current().network = info
    if info is None:
        return []
    hits = [Hit(RiskSignal.NETWORK_HOSTING)] if info.hosting else []
    evidence = _location.get()
    if evidence is None:
        return hits
    site = (evidence.site_country or "").upper()
    if site and info.country and info.country != site:
        hits.append(Hit(RiskSignal.NETWORK_COUNTRY_MISMATCH))
    previous = evidence.previous
    if previous is None:
        return hits
    minutes = round(previous.minutes_ago, 1)
    country_jump = previous.ip_country and info.country and previous.ip_country != info.country
    network_jump = previous.ip_asn and info.asn and previous.ip_asn != info.asn
    if country_jump and minutes < settings.RISK_NETWORK_COUNTRY_JUMP_MINUTES:
        hits.append(Hit(RiskSignal.NETWORK_JUMP, minutes, settings.RISK_NETWORK_COUNTRY_JUMP_MINUTES))
    elif network_jump and minutes < settings.RISK_NETWORK_ASN_JUMP_MINUTES:
        hits.append(Hit(RiskSignal.NETWORK_JUMP, minutes, settings.RISK_NETWORK_ASN_JUMP_MINUTES))
    return hits


def _similarity(embedding: np.ndarray, encrypted: bytes, dimension: int) -> float:
    """Similitud con el embedding guardado (cifrado); uno ilegible no coincide con nada."""
    data = try_decrypt(encrypted)
    if data is None:
        return -1.0
    return float(cosine_similarity(embedding, embedding_from_bytes(data, dimension)))
