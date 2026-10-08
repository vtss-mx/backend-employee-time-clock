"""Monitoreo de deriva de las señales (antifraude fase 3, I+D §3.5): ventanas, PSI y colas, el trabajo semanal del
mantenimiento con sus alertas al ADMIN, la tasa de casos y las aprobaciones sin mirar por empresa, la bitácora de
versiones del motor y la pantalla del ADMIN."""

import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.clock import business_day_bounds, business_today
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.devices import platform_of
from app.models import (
    Company,
    CompanyFraudWeekly,
    Employee,
    EngineVersion,
    ErrorReport,
    FaceAttemptMetric,
    FraudCase,
    SignalDrift,
    WorkSession,
)
from app.services import drift_service, engine_log, maintenance_service
from app.services.drift_stats import compare, last_closed_window, psi, quick_approval, window_start
from app.services.error_reporter import error_reporter
from tests.conftest import create_employee, login
from tests.test_shifts import assign, create_shift

URL = "/api/admin/drift"
TODAY = business_today()
WEEK = last_closed_window(TODAY, settings.DRIFT_WINDOW_DAYS)


def _bounds(week: date) -> tuple[datetime, datetime]:
    return business_day_bounds(week)[0], business_day_bounds(week + timedelta(days=settings.DRIFT_WINDOW_DAYS))[0]


def _company_id() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(Company.id).order_by(Company.id)) or 0)


def _metrics(week: date, platform: str, values: list[float], column: str = "yaw_min", **extra) -> None:
    """Intentos exitosos de una plataforma repartidos dentro de la ventana, con el valor de una señal."""
    start, end = _bounds(week)
    step = (end - start) / (len(values) + 1)
    company_id = _company_id()
    with SessionLocal() as db:
        db.add_all(
            FaceAttemptMetric(
                company_id=company_id,
                success=True,
                platform=platform,
                created_at=start + step * (i + 1),
                **{column: value},
                **extra,
            )
            for i, value in enumerate(values)
        )
        db.commit()


def _reviewed_sessions(client, company_headers, decisions: list[tuple[str, int]]) -> None:
    """Jornadas con su revisión decidida en la ventana: (decisión, segundos desde que se abrió hasta decidirla)."""
    employee = create_employee(client, company_headers, number="EMP-100", email="rev@empresa.com").json()["data"]
    shift = create_shift(client, company_headers, name="Deriva")
    assignment = assign(client, company_headers, employee["id"], shift["id"], TODAY).json()["data"]
    start, _ = _bounds(WEEK)
    with SessionLocal() as db:
        company_id = db.get(Employee, employee["id"]).company_id
        for i, (decision, seconds) in enumerate(decisions):
            opened = start + timedelta(hours=8 + i)
            db.add(
                WorkSession(
                    company_id=company_id,
                    employee_id=employee["id"],
                    assignment_id=assignment["id"],
                    work_date=(opened + timedelta(days=i)).date(),
                    shift_name="Deriva",
                    scheduled_start=opened + timedelta(days=i),
                    scheduled_end=opened + timedelta(days=i, hours=8),
                    check_out_deadline=opened + timedelta(days=i, hours=9),
                    breaks_allowed=1,
                    break_minutes_allowed=30,
                    early_check_out_minutes=5,
                    status="CLOSED",
                    check_in_at=opened,
                    check_in_mode="REMOTE",
                    check_out_at=opened + timedelta(hours=8),
                    check_out_mode="REMOTE",
                    worked_minutes=450,
                    review_status=decision,
                    review_reasons="DEVICE",
                    reviewed_at=opened + timedelta(seconds=seconds),
                )
            )
        db.commit()


def _fraud_case(week: date) -> None:
    start, _ = _bounds(week)
    with SessionLocal() as db:
        db.add(
            FraudCase(
                company_id=_company_id(),
                created_at=start + timedelta(hours=3),
                last_attempt_at=start + timedelta(hours=3),
                status="OPEN",
                kind="PRESENTATION",
                subject="actor:1",
                reason="SPOOF_DETECTED",
            )
        )
        db.commit()


# ---------------------------------------------------------------- reglas puras


def test_windows_align_to_monday_and_the_last_closed_one_is_the_week_before():
    assert window_start(date(2026, 10, 7), 7) == date(2026, 10, 5)  # miércoles → su lunes
    assert window_start(date(2026, 10, 5), 7) == date(2026, 10, 5)
    assert window_start(date(2026, 10, 4), 7) == date(2026, 9, 28)  # domingo → el lunes anterior
    assert last_closed_window(date(2026, 10, 7), 7) == date(2026, 9, 28)
    assert window_start(date(2026, 10, 7), 14) == date(2026, 9, 28) and window_start(date(2025, 12, 31), 7) == date(
        2025, 12, 29
    )


def test_psi_measures_a_shift_of_the_distribution():
    baseline = [i / 100 for i in range(100)]
    assert psi(baseline, baseline) == pytest.approx(0.0, abs=1e-6)
    assert (psi(baseline, [v + 0.5 for v in baseline]) or 0) > 0.2  # la población se movió
    assert psi([], baseline) is None and psi(baseline, []) is None
    assert psi([0.3] * 50, [0.3] * 50) == 0.0  # una señal constante: sin cubetas, sin deriva


def test_compare_decides_the_status_in_order():
    rules = {"min_samples": 5, "psi_alert": 0.2, "tail_drop_alert": 0.15, "version_changed": False}
    steady = [0.3, 0.31, 0.29, 0.3, 0.32, 0.28, 0.3]
    ok = compare(steady, steady, upper=False, **rules)
    assert ok.status == "OK" and ok.tail_percentile == 10 and ok.tail_change == 0.0 and ok.psi == 0.0
    dropped = compare([v * 0.5 for v in steady], steady, upper=False, **rules)
    assert dropped.status == "ALERT" and (dropped.tail_change or 0) < -0.15
    # Un máximo (moiré) vigila el p90: subir es lo sospechoso; bajar, no (con el PSI fuera de juego: la forma cambia
    # en los dos sentidos y aquí se prueba solo la dirección de la cola).
    tail_only = {**rules, "psi_alert": 100.0}
    assert compare([v * 1.5 for v in steady], steady, upper=True, **tail_only).status == "ALERT"
    assert compare([v * 1.5 for v in steady], steady, upper=False, **tail_only).status == "OK"
    assert compare([v * 0.5 for v in steady], steady, upper=True, **tail_only).status == "OK"
    assert compare([v * 0.5 for v in steady], steady, upper=False, **tail_only).status == "ALERT"
    assert compare(steady[:3], steady, upper=False, **rules).status == "INSUFFICIENT"
    assert compare(steady, steady[:3], upper=False, **rules).status == "NO_BASELINE"
    changed = compare(steady, steady, upper=False, **{**rules, "version_changed": True})
    assert changed.status == "VERSION_CHANGE" and changed.median == 0.3
    # Una línea base en cero no da un cambio relativo.
    assert compare(steady, [0.0] * 7, upper=False, **rules).tail_change is None
    assert compare([], [], upper=False, **rules).median is None


def test_platform_of_groups_browsers_coarsely():
    assert platform_of("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit Safari") == "IOS_SAFARI"
    assert platform_of("Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) CriOS/120") == "IOS_SAFARI"
    assert platform_of("Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/120 Mobile Safari") == "ANDROID_CHROME"
    assert platform_of("Mozilla/5.0 (Android 14; Mobile; rv:120.0) Gecko/120.0 Firefox/120.0") == "OTHER"
    assert platform_of("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120") == "DESKTOP"
    assert platform_of("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Safari/605") == "DESKTOP"
    assert platform_of(None) == "OTHER" and platform_of("curl/8.0") == "OTHER"


def test_quick_approval_rules():
    opened = datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert quick_approval("CONFIRMED", opened, opened + timedelta(seconds=10), 30)
    assert not quick_approval("CONFIRMED", opened, opened + timedelta(seconds=45), 30)
    assert not quick_approval("REJECTED", opened, opened + timedelta(seconds=10), 30)
    assert not quick_approval("CONFIRMED", None, opened, 30)


# ---------------------------------------------------------------- el trabajo semanal


def test_the_weekly_job_measures_each_signal_by_platform_alerts_the_admin_and_is_idempotent(
    client, company_headers, admin_headers, monkeypatch
):
    monkeypatch.setattr(settings, "DRIFT_MIN_SAMPLES", 20)
    monkeypatch.setattr(settings, "DRIFT_MIN_REVIEWS", 2)
    baseline_week = WEEK - timedelta(days=settings.DRIFT_WINDOW_DAYS)
    # iPhone: la gente giraba 0.30 y ahora 0.15 (una versión de iOS cambió la cámara): deriva.
    _metrics(baseline_week, "IOS_SAFARI", [0.30 + (i % 5) / 100 for i in range(30)])
    _metrics(WEEK, "IOS_SAFARI", [0.15 + (i % 5) / 100 for i in range(30)])
    # Android: igual que antes (y un fraude confirmado que NO cuenta como persona real).
    _metrics(baseline_week, "ANDROID_CHROME", [0.30 + (i % 5) / 100 for i in range(30)])
    _metrics(WEEK, "ANDROID_CHROME", [0.30 + (i % 5) / 100 for i in range(30)])
    _metrics(WEEK, "ANDROID_CHROME", [0.01] * 30, fraud_label="FRAUD")
    # Escritorio: datos ahora pero no antes; otros: muy pocos.
    _metrics(WEEK, "DESKTOP", [0.3] * 25)
    _metrics(WEEK, "OTHER", [0.3] * 3)
    _fraud_case(WEEK)
    _reviewed_sessions(client, company_headers, [("CONFIRMED", 5), ("CONFIRMED", 8), ("REJECTED", 400)])

    with SessionLocal() as db:
        removed = maintenance_service.purge_expired(db)
    signals, platforms = len(drift_service.DRIFT_SIGNALS), 4
    assert removed[maintenance_service.DRIFT_ROWS] == signals * platforms + 1
    with SessionLocal() as db:
        rows = {(r.signal, r.platform): r for r in db.scalars(select(SignalDrift))}
        company = db.scalar(select(CompanyFraudWeekly))
    assert len(rows) == signals * platforms and {r.week_start for r in rows.values()} == {WEEK}
    yaw = rows[("LIVENESS_YAW", "IOS_SAFARI")]
    assert yaw.status == "ALERT" and yaw.samples == 30 and yaw.baseline_samples == 30
    assert yaw.tail_percentile == 10 and (yaw.tail_change or 0) < -0.15 and (yaw.psi or 0) > 0.2
    android = rows[("LIVENESS_YAW", "ANDROID_CHROME")]
    assert android.status == "OK" and android.samples == 30  # el fraude confirmado no entra
    assert rows[("LIVENESS_YAW", "DESKTOP")].status == "NO_BASELINE"
    assert rows[("LIVENESS_YAW", "OTHER")].status == "INSUFFICIENT"
    assert rows[("MOIRE", "IOS_SAFARI")].tail_percentile == 90 and rows[("MOIRE", "IOS_SAFARI")].samples == 0
    assert {r.signal for r in rows.values()}.isdisjoint({"FLASH_SCORE", "FLASH_RATIO"})  # el destello se retiró
    # La empresa: sus intentos, su caso y las revisiones aprobadas sin mirar (2 de 3 decididas: 67 % → todavía OK...
    # Los intentos de la ventana (también el fraude confirmado: fue un intento), su caso y su tasa.
    assert company.attempts == 30 * 3 + 25 + 3 and company.fraud_cases == 1 and company.case_rate == round(1 / 118, 4)
    assert (company.reviews, company.approved, company.quick_approvals) == (3, 2, 2)
    assert company.quick_rate == pytest.approx(0.6667, abs=1e-3) and company.status == "OK"
    assert company.company_name  # el nombre copiado al calcular
    # ...y con el umbral más estricto, alerta de fraude interno.
    monkeypatch.setattr(settings, "DRIFT_QUICK_APPROVAL_RATIO", 0.5)
    with SessionLocal() as db:
        again = drift_service.compute_window(db, WEEK, datetime.now(UTC))
    assert again == signals * platforms + 1  # idempotente: reemplaza la ventana
    with SessionLocal() as db:
        assert db.scalar(select(CompanyFraudWeekly)).status == "ALERT"
        assert db.scalar(select(SignalDrift.id).where(SignalDrift.week_start == WEEK)) is not None
        removed = maintenance_service.purge_expired(db)
    assert removed[maintenance_service.DRIFT_ROWS] == 0  # la ventana ya estaba
    # Las alertas llegan al ADMIN por el camino de siempre: "Errores del sistema", una fila por llave estable.
    error_reporter.flush()
    with SessionLocal() as db:
        codes = set(db.scalars(select(ErrorReport.code)))
        message = db.scalar(select(ErrorReport.message).where(ErrorReport.code == "app.drift.company"))
    assert {"app.drift.LIVENESS_YAW.IOS_SAFARI", "app.drift.company"} <= codes
    assert message and "aprobó sin mirar 2 de 3" in message
    # Y la pantalla del ADMIN lo muestra.
    summary = client.get(f"{URL}/summary", headers=admin_headers).json()
    assert summary["code"] == "DRIFT_SUMMARY"
    data = summary["data"]
    assert data["latest_week"] == WEEK.isoformat() and data["weeks"] == [WEEK.isoformat()]
    assert data["alerts"] == 1 and data["insufficient"] >= 1 and data["companies_alerted"] == 1
    assert data["platforms"] == ["IOS_SAFARI", "ANDROID_CHROME", "DESKTOP", "OTHER"] and data["computed_at"]
    assert {v["component"] for v in data["versions"]} == {"risk_engine", "face_models", "api"}
    listed = client.get(URL, params={"status": "ALERT"}, headers=admin_headers).json()
    assert listed["code"] == "DRIFT_SIGNALS" and listed["message"] == "1 señal"
    row = listed["data"]["items"][0]
    assert row["signal"] == "LIVENESS_YAW" and row["signal_name"] == "Giro mínimo de la cabeza" and not row["upper"]
    assert listed["data"]["week_start"] == WEEK.isoformat()
    by_platform = client.get(URL, params={"platform": "DESKTOP", "week": WEEK.isoformat()}, headers=admin_headers)
    assert by_platform.json()["data"]["total"] == signals
    moire = client.get(URL, params={"platform": "DESKTOP", "size": 50}, headers=admin_headers).json()["data"]
    assert any(item["signal"] == "MOIRE" and item["upper"] and item["tail_percentile"] == 90 for item in moire["items"])
    companies = client.get(f"{URL}/companies", headers=admin_headers).json()
    assert companies["code"] == "DRIFT_COMPANIES" and companies["data"]["items"][0]["status"] == "ALERT"
    assert companies["data"]["items"][0]["quick_approvals"] == 2
    assert (
        client.get(f"{URL}/companies", params={"search": "nadie"}, headers=admin_headers).json()["data"]["total"] == 0
    )
    assert (
        client.get(
            f"{URL}/companies", params={"search": company.company_name[:4].lower()}, headers=admin_headers
        ).json()["data"]["total"]
        == 1
    )


def test_a_version_change_makes_the_window_not_comparable(admin_headers, client, monkeypatch):
    monkeypatch.setattr(settings, "DRIFT_MIN_SAMPLES", 20)
    baseline_week = WEEK - timedelta(days=settings.DRIFT_WINDOW_DAYS)
    _metrics(baseline_week, "IOS_SAFARI", [0.3] * 25)
    _metrics(WEEK, "IOS_SAFARI", [0.1] * 25)  # cambió mucho, pero también cambió el motor
    start, _ = _bounds(WEEK)
    with SessionLocal() as db:
        db.add(EngineVersion(component="risk_engine", version="0.9.0", noted_at=start + timedelta(days=1)))
        db.commit()
        drift_service.compute_window(db, WEEK, datetime.now(UTC))
        row = db.scalar(
            select(SignalDrift).where(SignalDrift.signal == "LIVENESS_YAW", SignalDrift.platform == "IOS_SAFARI")
        )
    assert row.status == "VERSION_CHANGE" and row.median == 0.1 and row.baseline_median == 0.3
    error_reporter.flush()
    with SessionLocal() as db:
        assert db.scalar(select(ErrorReport).where(ErrorReport.code.like("app.drift.%"))) is None  # sin alerta
    # Toda la ventana queda como no comparable (también las plataformas sin datos: el cambio de versión manda).
    assert client.get(URL, params={"status": "VERSION_CHANGE"}, headers=admin_headers).json()["data"]["total"] == (
        len(drift_service.DRIFT_SIGNALS) * 4
    )


def test_the_engine_log_notes_each_version_once_and_what_the_browser_reports():
    engine_log.clear()
    engine_log.observe_webapp(None)
    engine_log.observe_webapp("build-abc")
    engine_log.observe_webapp("build-abc")
    for i in range(100):  # acotado: nunca crece sin tope
        engine_log.observe_webapp(f"build-{i}")
    assert len(engine_log.pending_webapp()) == engine_log._OBSERVED_MAX
    now = datetime.now(UTC)
    with SessionLocal() as db:
        added = engine_log.record_versions(db, now)
        db.commit()
        assert added == 3 + engine_log._OBSERVED_MAX and engine_log.pending_webapp() == set()
        assert engine_log.record_versions(db, now) == 0  # las mismas versiones: nada nuevo
        rows = db.scalars(select(EngineVersion)).all()
    assert {r.component for r in rows} == {"risk_engine", "face_models", "api", "webapp"}
    assert len(engine_log.face_models_fingerprint()) == 16
    # La telemetría del navegador la observa solo con sesión (nunca escribe en la base en la petición).
    from app.schemas.performance import WebPerfBatch
    from app.services import web_performance
    from tests.test_performance_api import ROUTE

    engine_log.clear()
    batch = WebPerfBatch.model_validate(
        {"app_version": "build-web", "samples": [{"kind": "API", "name": ROUTE, "value": 10, "status": 200}]}
    )
    web_performance.record(batch, authenticated=False, index=web_performance.RouteIndex({}))
    assert engine_log.pending_webapp() == set()
    web_performance.record(batch, authenticated=True, index=web_performance.RouteIndex({}))
    assert engine_log.pending_webapp() == {"build-web"}
    engine_log.clear()


def test_without_computed_windows_the_screen_is_empty_and_compute_fills_it(client, admin_headers, monkeypatch):
    summary = client.get(f"{URL}/summary", headers=admin_headers).json()["data"]
    assert summary["latest_week"] is None and summary["weeks"] == [] and summary["alerts"] == 0
    assert client.get(URL, headers=admin_headers).json()["data"] == {
        "items": [],
        "total": 0,
        "page": 1,
        "size": 10,
        "week_start": None,
    }
    assert client.get(f"{URL}/companies", headers=admin_headers).json()["data"]["week_start"] is None
    computed = client.post(f"{URL}/compute", headers=admin_headers)
    assert computed.status_code == 200 and computed.json()["code"] == "DRIFT_COMPUTED"
    assert computed.json()["message"].startswith("Deriva calculada (")
    assert computed.json()["data"]["latest_week"] == WEEK.isoformat()
    # Sin intentos, cada señal y plataforma queda "sin datos suficientes".
    rows = client.get(URL, params={"size": 50}, headers=admin_headers).json()["data"]
    assert rows["total"] == len(drift_service.DRIFT_SIGNALS) * 4 and {r["status"] for r in rows["items"]} == {
        "INSUFFICIENT"
    }
    monkeypatch.setattr(settings, "DRIFT_ENABLED", False)
    disabled = client.post(f"{URL}/compute", headers=admin_headers)
    assert disabled.status_code == 409 and disabled.json()["code"] == "DRIFT_DISABLED"
    with SessionLocal() as db:
        assert drift_service.run_if_due(db, datetime.now(UTC)) == 0


def test_a_failing_drift_job_does_not_stop_maintenance(monkeypatch, caplog):
    def locked(*_args):
        raise OperationalError("SELECT", {}, Exception("lock timeout"))

    monkeypatch.setattr(drift_service, "run_if_due", locked)
    with SessionLocal() as db, caplog.at_level(logging.ERROR):
        removed = maintenance_service.purge_expired(db)
    assert removed[maintenance_service.DRIFT_ROWS] == 0 and "fotos de almacenamiento" in removed
    assert "Falló el monitoreo de deriva" in caplog.text


def test_old_drift_rows_and_version_notes_are_purged():
    old = TODAY - timedelta(days=settings.DRIFT_RETENTION_DAYS + 7)
    now = datetime.now(UTC)
    with SessionLocal() as db:
        db.add(
            SignalDrift(
                week_start=old,
                signal="LIVENESS_YAW",
                platform="DESKTOP",
                samples=0,
                baseline_samples=0,
                tail_percentile=10,
                status="INSUFFICIENT",
                computed_at=now,
            )
        )
        db.add(
            CompanyFraudWeekly(
                company_id=_company_id(),
                company_name="Vieja",
                week_start=old,
                attempts=0,
                fraud_cases=0,
                reviews=0,
                approved=0,
                quick_approvals=0,
                status="INSUFFICIENT",
                computed_at=now,
            )
        )
        db.add(
            EngineVersion(
                component="api", version="0.0.1", noted_at=now - timedelta(days=settings.DRIFT_RETENTION_DAYS + 1)
            )
        )
        db.commit()
        removed = maintenance_service.purge_expired(db)
    assert (removed["deriva de señales"], removed["deriva por empresa"], removed["bitácora del motor"]) == (1, 1, 1)
    # El mantenimiento también calculó la ventana que faltaba (todas sus filas "sin datos").
    assert removed[maintenance_service.DRIFT_ROWS] == len(drift_service.DRIFT_SIGNALS) * 4


def test_login_records_the_platform_of_each_face_attempt(client, company_headers):
    """La plataforma del navegador queda en la métrica del intento (categoría gruesa, nunca el User-Agent)."""
    from tests.test_validators import approved

    approved(client, company_headers, "juan", number="EMP-001")
    headers = {
        **login(client, "juan@empresa.com", "Empleado123"),
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari",
    }
    from tests.test_fault_tolerance import _verify

    assert _verify(client, headers).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(FaceAttemptMetric.platform).order_by(FaceAttemptMetric.id.desc())) == "IOS_SAFARI"
