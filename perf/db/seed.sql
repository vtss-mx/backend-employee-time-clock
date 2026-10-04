-- Volumen sintético para medir los planes de las consultas (perf/db/run.sh, base AISLADA: jamás la de
-- trabajo). 200 empresas: la 1 con 20 000 empleados (la empresa grande) y las demás con 400 (≈100 k
-- empleados); 5 M de registros en la bitácora, 2 M de jornadas (20 días por empleado; hoy, 60 % abiertas),
-- ≈4.8 M de registros de asistencia, 300 k ausencias, 1 M de QR, de huellas de capturas y de métricas
-- faciales, 150 k sesiones y 200 k ocurrencias de errores. Los embeddings son bytes de relleno (no se
-- miden comparaciones faciales, solo consultas). Una tabla nueva se siembra aquí con su volumen esperado.
\timing on
SET search_path TO public;
SET synchronous_commit = off;

CREATE OR REPLACE FUNCTION pg_temp.co(i int) RETURNS int LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN i <= 20000 THEN 1 ELSE 2 + (i - 20001) / 400 END $$;

-- ---------------- tenancy
INSERT INTO tenancy.companies (id, name, legal_name, rfc, phone, active, api_enabled, created_at, updated_at)
SELECT c, 'Empresa ' || c, 'Razon Social ' || c || ' SA de CV', 'EMP' || lpad(c::text, 6, '0') || 'AB' || (c % 10),
       '+5266210' || lpad(c::text, 5, '0'), c % 20 <> 0, c % 3 = 0, now() - interval '400 days', now()
FROM generate_series(1, 200) c;

INSERT INTO tenancy.verification_policy (company_id, block_glasses, block_headwear, block_mask, liveness_challenge, anti_spoofing, qr_enabled)
SELECT c, true, true, true, true, true, true FROM generate_series(1, 200) c;

-- ---------------- auth.users: empleados 1..99600, admins 100001..100200, validadores 110001..111000, ADMIN 120001
INSERT INTO auth.users (id, email, password_hash, role, active, company_id, phone, created_at, updated_at)
SELECT i, 'empleado' || i || '@correo.mx', 'x', 'EMPLOYEE', i % 50 <> 0, NULL, '+52' || (6000000000 + i)::text,
       now() - make_interval(days => i % 400), now()
FROM generate_series(1, 99600) i;
INSERT INTO auth.users (id, email, password_hash, role, active, company_id, created_at, updated_at)
SELECT 100000 + c, 'admin' || c || '@empresa' || c || '.mx', 'x', 'COMPANY', true, c, now() - interval '400 days', now()
FROM generate_series(1, 200) c;
INSERT INTO auth.users (id, email, password_hash, role, active, company_id, created_at, updated_at)
SELECT 110000 + v, 'validador' || v || '@empresa.mx', 'x', 'VALIDATOR', true, (v - 1) / 5 + 1, now() - interval '300 days', now()
FROM generate_series(1, 1000) v;
INSERT INTO auth.users (id, email, password_hash, role, active, created_at, updated_at)
VALUES (120001, 'admin@plataforma.mx', 'x', 'ADMIN', true, now(), now());

-- ---------------- workforce
INSERT INTO workforce.departments (id, company_id, name, created_at, updated_at)
SELECT (c - 1) * 10 + d, c, 'Departamento ' || d, now(), now() FROM generate_series(1, 200) c, generate_series(1, 10) d;

INSERT INTO workforce.employees (id, user_id, company_id, employee_number, rfc, curp, nss, first_name, last_name, birth_date,
                                 active, headwear_exempt, face_status, department_id, created_at, updated_at)
SELECT i, i, pg_temp.co(i), 'E' || lpad(i::text, 7, '0'),
       'ABCD' || lpad(i::text, 6, '0') || 'XY1', 'ABCD' || lpad(i::text, 6, '0') || 'HSRXYZ01', lpad(i::text, 11, '0'),
       (ARRAY['Juan','María','José','Ana','Luis','Laura','Carlos','Sofía','Miguel','Lucía','Pedro','Elena'])[1 + i % 12],
       (ARRAY['García','Hernández','López','Martínez','González','Pérez','Rodríguez','Sánchez','Ramírez','Cruz','Flores','Gómez','Morales','Vázquez','Reyes','Jiménez','Torres','Díaz'])[1 + (i / 12) % 18]
         || ' ' || (ARRAY['Ruiz','Ortiz','Castillo','Moreno','Romero','Álvarez','Mendoza','Aguilar'])[1 + (i / 7) % 8],
       date '1975-01-01' + (i % 9000), i % 25 <> 0, false,
       CASE i % 10 WHEN 0 THEN 'NOT_ENROLLED' WHEN 1 THEN 'PENDING_REVIEW' ELSE 'APPROVED' END,
       CASE WHEN i % 7 = 0 THEN NULL ELSE (pg_temp.co(i) - 1) * 10 + 1 + i % 10 END,
       now() - make_interval(days => i % 400), now() - make_interval(days => i % 30)
FROM generate_series(1, 99600) i;

INSERT INTO workforce.department_managers (department_id, employee_id, company_id)
SELECT d.id, e.id, d.company_id FROM workforce.departments d
JOIN LATERAL (SELECT id FROM workforce.employees WHERE company_id = d.company_id AND department_id = d.id ORDER BY id LIMIT 1) e ON true;

INSERT INTO workforce.work_sites (id, company_id, name, radius_m, latitude, longitude, active, created_at, updated_at)
SELECT (c - 1) * 5 + s, c, 'Sitio ' || s, 200, 29.0 + c * 0.001, -110.9 - s * 0.001, true, now(), now()
FROM generate_series(1, 200) c, generate_series(1, 5) s;

INSERT INTO workforce.shifts (id, company_id, name, start_time, end_time, weekdays, breaks_count, break_minutes,
                              early_check_in_minutes, late_tolerance_minutes, early_check_out_minutes, late_check_out_minutes, active, created_at, updated_at)
SELECT (c - 1) * 3 + s, c, (ARRAY['Matutino','Vespertino','Nocturno'])[s],
       (ARRAY[time '07:00', time '15:00', time '22:00'])[s], (ARRAY[time '15:00', time '23:00', time '06:00'])[s],
       CASE WHEN s = 3 THEN 127 ELSE 31 END, 1, 30, 30, 10, 10, 120, true, now(), now()
FROM generate_series(1, 200) c, generate_series(1, 3) s;

-- Asignación vigente de cada empleado (id = empleado) y una histórica para un tercio (id = 200000 + empleado).
INSERT INTO workforce.shift_assignments (id, company_id, employee_id, shift_id, valid_from, valid_to, remote_weekdays, created_by_id, created_at, updated_at)
SELECT i, pg_temp.co(i), i, (pg_temp.co(i) - 1) * 3 + 1 + i % 3, current_date - 200, NULL, CASE WHEN i % 4 = 0 THEN 31 ELSE 0 END,
       100000 + pg_temp.co(i), now() - interval '200 days', now()
FROM generate_series(1, 99600) i;
INSERT INTO workforce.shift_assignments (id, company_id, employee_id, shift_id, valid_from, valid_to, remote_weekdays, created_by_id, created_at, updated_at)
SELECT 200000 + i, pg_temp.co(i), i, (pg_temp.co(i) - 1) * 3 + 1 + (i + 1) % 3, current_date - 400, current_date - 201, 0,
       100000 + pg_temp.co(i), now() - interval '400 days', now()
FROM generate_series(1, 99600) i WHERE i % 3 = 0;
INSERT INTO workforce.shift_assignment_sites (assignment_id, site_id, company_id)
SELECT id, (company_id - 1) * 5 + 1 + employee_id % 5, company_id FROM workforce.shift_assignments;

INSERT INTO workforce.shift_change_requests (id, company_id, employee_id, shift_id, valid_from, reason, status, reviewed_by_id, reviewed_at, created_at, updated_at)
SELECT n, pg_temp.co(e), e, (pg_temp.co(e) - 1) * 3 + 1 + (e + 2) % 3, current_date + 7, 'Cambio de horario',
       CASE WHEN n % 5 = 0 THEN 'PENDING' WHEN n % 5 = 1 THEN 'REJECTED' ELSE 'APPROVED' END,
       CASE WHEN n % 5 = 0 THEN NULL ELSE 100000 + pg_temp.co(e) END, CASE WHEN n % 5 = 0 THEN NULL ELSE now() END,
       now() - make_interval(days => n % 300), now()
FROM (SELECT n, 1 + (n * 7) % 99600 AS e FROM generate_series(1, 20000) n) s;

-- Calendario
INSERT INTO workforce.company_holidays (company_id, holiday_date, name, official, created_by_id, created_at, updated_at)
SELECT c, make_date(y, m, d), 'Festivo ' || m || '/' || d, true, 100000 + c, now(), now()
FROM generate_series(1, 200) c, generate_series(extract(year FROM current_date)::int - 1, extract(year FROM current_date)::int) y,
     (VALUES (1, 1), (2, 3), (3, 17), (5, 1), (9, 16), (11, 17), (12, 25), (12, 12), (11, 2), (4, 17)) md(m, d);

INSERT INTO workforce.employee_absences (id, company_id, employee_id, type_code, starts_on, ends_on, note, status, requested_by_id, decided_by_id, decided_at, created_at, updated_at)
SELECT (i - 1) * 3 + k, pg_temp.co(i), i, (ARRAY['VACATION','PERMISSION','SICK_LEAVE','OTHER'])[1 + (i + k) % 4],
       current_date - 300 + k * 120 + i % 60, current_date - 300 + k * 120 + i % 60 + i % 10, 'Ausencia',
       CASE (i + k) % 20 WHEN 0 THEN 'PENDING' WHEN 1 THEN 'PENDING' WHEN 2 THEN 'REJECTED' WHEN 3 THEN 'REJECTED' WHEN 4 THEN 'CANCELLED' ELSE 'APPROVED' END,
       CASE WHEN k = 1 THEN i ELSE 100000 + pg_temp.co(i) END, 100000 + pg_temp.co(i), now(), now(), now()
FROM generate_series(1, 99600) i, generate_series(0, 2) k;

INSERT INTO workforce.employee_workdays (company_id, employee_id, work_date, note, created_by_id, created_at, updated_at)
SELECT pg_temp.co(i), i, current_date - 300 + k * 120 + i % 60, 'Se necesita', 100000 + pg_temp.co(i), now(), now()
FROM generate_series(1, 99600) i, generate_series(0, 2) k WHERE i % 15 = 0;

-- Validadores, dispositivos, llaves
INSERT INTO workforce.validators (id, user_id, company_id, name, mode, location_required, created_at, updated_at)
SELECT v, 110000 + v, (v - 1) / 5 + 1, 'Recepción ' || v, 'QR_OR_FACE', false, now(), now() FROM generate_series(1, 1000) v;
INSERT INTO workforce.validator_devices (validator_id, company_id, key_hash, public_key, name, status, created_at)
SELECT v, (v - 1) / 5 + 1, md5(v || '-' || k) || md5(k || '-' || v), 'pk', 'Safari · iPadOS', CASE WHEN k = 1 THEN 'APPROVED' ELSE 'PENDING' END, now()
FROM generate_series(1, 1000) v, generate_series(1, 2) k;
INSERT INTO tenancy.company_api_keys (id, company_id, name, prefix, key_hash, created_by_id)
SELECT c, c, 'Nómina', 'tc_' || c, md5('key' || c) || md5('k' || c), 100000 + c FROM generate_series(1, 200) c;
INSERT INTO tenancy.company_api_key_scopes (api_key_id, scope)
SELECT c, s FROM generate_series(1, 200) c, unnest(ARRAY['EMPLOYEES_READ','ATTENDANCE_READ']) s;

-- ---------------- biometrics
INSERT INTO biometrics.face_enrollments (id, employee_id, company_id, status, photo_content_type, quality_score, samples, liveness_passed, submitted_at, reviewed_at, reviewed_by_id)
SELECT i, i, pg_temp.co(i), CASE WHEN i % 10 = 1 THEN 'PENDING' ELSE 'APPROVED' END, 'image/jpeg', 0.8, 5, true,
       now() - make_interval(days => i % 300), CASE WHEN i % 10 = 1 THEN NULL ELSE now() END,
       CASE WHEN i % 10 = 1 THEN NULL ELSE 100000 + pg_temp.co(i) END
FROM generate_series(1, 99600) i WHERE i % 10 <> 0;
INSERT INTO biometrics.face_embeddings (employee_id, enrollment_id, embedding_encrypted, model_name, dimension, detection_score, quality_score, active, learned, matches, last_matched_at, created_at)
SELECT i, i, decode(md5(i::text || k), 'hex'), 'sface+facenet', 640, 0.9, 0.8, true, k = 4, (i * k) % 50,
       CASE WHEN k = 4 THEN now() - make_interval(days => i % 200) END, now() - make_interval(days => i % 300)
FROM generate_series(1, 99600) i, generate_series(1, 4) k WHERE i % 10 > 1 AND (k < 4 OR i % 3 = 0);

INSERT INTO biometrics.capture_fingerprints (digest, company_id, created_at)
SELECT md5(n::text) || md5((n * 3)::text), 1 + n % 200, now() - make_interval(secs => n * 2.5) FROM generate_series(1, 1000000) n;
INSERT INTO biometrics.face_challenges (id, user_id, direction, issued_at, expires_at)
SELECT md5('ch' || n), 1 + n * 9 % 99600, 'TURN_LEFT', now() - make_interval(secs => n), now() - make_interval(secs => n) + interval '60 seconds'
FROM generate_series(1, 10000) n;

INSERT INTO workforce.employee_qr_codes (employee_id, token_hash, active, expires_at, used_at, used_by_id, completed_at, created_at, updated_at)
SELECT 1 + n % 99600, md5('qr' || n) || md5(n::text), false, now() - make_interval(secs => (1000000 - n) * 2.6),
       CASE WHEN n % 3 = 0 THEN now() - make_interval(secs => (1000000 - n) * 2.6) END,
       CASE WHEN n % 3 = 0 THEN 110000 + 1 + n % 1000 END,
       CASE WHEN n % 3 = 0 THEN now() - make_interval(secs => (1000000 - n) * 2.6) END,
       now() - make_interval(secs => (1000000 - n) * 2.6), now()
FROM generate_series(1, 1000000) n;
UPDATE workforce.employee_qr_codes q SET active = true, used_at = NULL, used_by_id = NULL, completed_at = NULL, expires_at = now() + interval '30 seconds'
WHERE id IN (SELECT max(id) FROM workforce.employee_qr_codes GROUP BY employee_id);

-- ---------------- auth
INSERT INTO auth.auth_sessions (id, user_id, refresh_hash, expires_at, created_at, last_used_at, revoked_at, revoked_reason, persistent, company_id)
SELECT md5('s' || n), 1 + n % 99600, md5('r' || n) || md5(n::text),
       CASE WHEN n <= 60000 THEN now() + interval '20 days' ELSE now() - make_interval(hours => n % 500) END,
       now() - make_interval(hours => n % 700), now(), CASE WHEN n % 7 = 0 THEN now() END, CASE WHEN n % 7 = 0 THEN 'LOGOUT' END,
       n % 2 = 0, pg_temp.co(1 + n % 99600)
FROM generate_series(1, 150000) n;
INSERT INTO auth.remembered_accounts (id, token_hash, user_id, created_at, expires_at)
SELECT md5('ra' || n), md5('t' || n) || md5(n::text), 1 + n % 99600, now(), now() + make_interval(days => n % 60 - 20) FROM generate_series(1, 50000) n;
INSERT INTO auth.rate_limit_counters (key, count, expires_at)
SELECT 'login:' || n, n % 10, now() + make_interval(secs => n % 600 - 300) FROM generate_series(1, 10000) n;

-- ---------------- attendance.verification_logs: 5M en orden de llegada (el id crece con la hora)
INSERT INTO attendance.verification_logs (id, employee_id, user_id, company_id, method, success, score, reason, ip_address, user_agent, created_at)
SELECT n, e, CASE WHEN n % 10 < 7 THEN e ELSE 110000 + (pg_temp.co(e) - 1) * 5 + 1 + n % 5 END, pg_temp.co(e),
       CASE WHEN n % 10 < 7 THEN 'FACE' WHEN n % 10 < 9 THEN 'QR' ELSE 'QR_FACE' END,
       n % 10 <> 3, 0.97, CASE WHEN n % 10 = 3 THEN (ARRAY['NO_MATCH','LIVENESS_FAILED','SPOOF_DETECTED','EXPIRED'])[1 + n % 4] END,
       '10.0.0.1', 'Mozilla/5.0', now() - make_interval(secs => (5000000 - n) * 6.3)
FROM (SELECT n, (1 + (n::bigint * 7919) % 99600)::int AS e FROM generate_series(1, 5000000) n) s;

-- ---------------- attendance: 20 días por empleado (≈2M jornadas). Hoy: 60 % abiertas.
INSERT INTO attendance.work_sessions (id, company_id, employee_id, assignment_id, work_date, shift_name, scheduled_start, scheduled_end, check_out_deadline,
    breaks_allowed, break_minutes_allowed, early_check_out_minutes, status, check_in_at, check_in_mode, check_in_site_id,
    check_out_at, check_out_mode, check_out_site_id, late_minutes, early_leave_minutes, break_minutes, worked_minutes, created_at)
SELECT (i - 1) * 20 + d + 1, pg_temp.co(i), i, i, current_date - d, 'Matutino',
       (current_date - d) + time '13:00', (current_date - d) + time '21:00', (current_date - d) + time '23:00',
       1, 30, 10,
       CASE WHEN d = 0 AND i % 10 < 6 THEN 'OPEN' WHEN i % 20 = 7 THEN 'MISSED_CHECKOUT' ELSE 'CLOSED' END,
       (current_date - d) + time '12:55' + make_interval(mins => i % 15), CASE WHEN i % 4 = 0 THEN 'REMOTE' ELSE 'ON_SITE' END,
       CASE WHEN i % 4 = 0 THEN NULL ELSE (pg_temp.co(i) - 1) * 5 + 1 + i % 5 END,
       CASE WHEN (d = 0 AND i % 10 < 6) OR i % 20 = 7 THEN NULL ELSE (current_date - d) + time '21:05' END,
       CASE WHEN (d = 0 AND i % 10 < 6) OR i % 20 = 7 THEN NULL WHEN i % 4 = 0 THEN 'REMOTE' ELSE 'ON_SITE' END,
       CASE WHEN (d = 0 AND i % 10 < 6) OR i % 20 = 7 OR i % 4 = 0 THEN NULL ELSE (pg_temp.co(i) - 1) * 5 + 1 + i % 5 END,
       i % 15, 0, 30, 450, (current_date - d) + time '12:55'
FROM generate_series(1, 99600) i, generate_series(0, 19) d;

INSERT INTO attendance.work_breaks (id, company_id, session_id, started_at, ended_at, exceeded_minutes)
SELECT s.id, s.company_id, s.id, s.check_in_at + interval '4 hours',
       CASE WHEN s.status = 'OPEN' AND s.id % 8 = 0 THEN NULL ELSE s.check_in_at + interval '4 hours 30 minutes' END, 0
FROM attendance.work_sessions s WHERE s.id % 4 = 0;

INSERT INTO attendance.attendance_events (company_id, employee_id, session_id, action, mode, site_id, occurred_at, latitude, longitude, accuracy_m, distance_m, verification_log_id, actor_id)
SELECT company_id, employee_id, id, 'CHECK_IN', check_in_mode, check_in_site_id, check_in_at, 29.07, -110.95, 12, 30, NULL, employee_id
FROM attendance.work_sessions ORDER BY check_in_at;
INSERT INTO attendance.attendance_events (company_id, employee_id, session_id, action, mode, site_id, occurred_at, latitude, longitude, accuracy_m, distance_m, verification_log_id, actor_id)
SELECT b.company_id, s.employee_id, b.session_id, a.action, s.check_in_mode, s.check_in_site_id,
       CASE a.action WHEN 'BREAK_START' THEN b.started_at ELSE b.ended_at END, 29.07, -110.95, 12, 30, NULL, s.employee_id
FROM attendance.work_breaks b JOIN attendance.work_sessions s ON s.id = b.session_id
CROSS JOIN (VALUES ('BREAK_START'), ('BREAK_END')) a(action)
WHERE a.action = 'BREAK_START' OR b.ended_at IS NOT NULL;
INSERT INTO attendance.attendance_events (company_id, employee_id, session_id, action, mode, site_id, occurred_at, latitude, longitude, accuracy_m, distance_m, verification_log_id, actor_id)
SELECT company_id, employee_id, id, 'CHECK_OUT', check_out_mode, check_out_site_id, check_out_at, 29.07, -110.95, 12, 30, NULL, employee_id
FROM attendance.work_sessions WHERE check_out_at IS NOT NULL ORDER BY check_out_at;

-- ---------------- ops
INSERT INTO ops.face_attempt_metrics (company_id, created_at, success, reason, steps, flash_mode, response_seconds, frontal_real_min, frontal_real_mean,
    step_real_min, yaw_min, pitch_min, closer_min, flash_score, flash_magnitude, flash_background, quality_mean, brightness_mean)
SELECT CASE WHEN n % 5 = 0 THEN 1 ELSE 1 + n % 200 END, now() - make_interval(secs => (1000000 - n) * 7.7), n % 7 <> 0,
       CASE WHEN n % 7 <> 0 THEN NULL WHEN n % 21 = 0 THEN 'SPOOF_DETECTED' ELSE 'NO_MATCH' END,
       2, 'OBSERVE', 8.5, 0.9, 0.95, 0.88, 0.25, 0.11, 1.4, 0.6, 12.0, 0.1, 0.7, 120.0
FROM generate_series(1, 1000000) n;
INSERT INTO ops.security_thresholds (key, value, samples, computed_at)
VALUES ('LIVENESS_YAW', 0.2, 20000, now()), ('LIVENESS_PITCH', 0.09, 20000, now()), ('LIVENESS_CLOSER', 1.3, 20000, now()),
       ('FLASH_SCORE', 0.4, 20000, now()), ('ANTISPOOF_REAL', 0.3, 20000, now());

INSERT INTO ops.error_reports (id, fingerprint, source, severity, status, code, message, http_status, method, location, exception_type, detail,
    occurrences, reopened, first_seen_at, last_seen_at, last_trace_id)
SELECT n, md5('err' || n), (ARRAY['HTTP','LOG','WEBSOCKET','CLIENT'])[1 + n % 4], CASE WHEN n % 3 = 0 THEN 'CRITICAL' ELSE 'ERROR' END,
       (ARRAY['PENDING','IN_PROGRESS','IN_REVIEW','RESOLVED','RESOLVED','RESOLVED'])[1 + n % 6], 'CODE_' || n % 300,
       'Mensaje de error ' || n, 500, 'GET', '/api/employees/{id}', 'ValueError', 'Traceback...', 1 + n % 1000, 0,
       now() - make_interval(days => n % 300), now() - make_interval(hours => n % 4000), md5(n::text)
FROM generate_series(1, 5000) n;
INSERT INTO ops.error_occurrences (report_id, occurred_at, trace_id, user_id, company_id, message, context)
SELECT 1 + n % 5000, now() - make_interval(mins => n % 43200), md5(n::text), 1 + n % 99600, 1 + n % 200, 'Mensaje', '{"request": {}}'
FROM generate_series(1, 200000) n;

-- ---------------- secuencias al día
DO $$
DECLARE r record; m bigint;
BEGIN
  FOR r IN SELECT n.nspname, c.relname, a.attname, pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname) AS seq
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid
           WHERE c.relkind = 'r' AND n.nspname IN ('auth','tenancy','workforce','biometrics','attendance','ops') AND a.attnum > 0 AND NOT a.attisdropped
             AND pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT coalesce(max(%I), 0) FROM %I.%I', r.attname, r.nspname, r.relname) INTO m;
    PERFORM setval(r.seq, greatest(m, 1));
  END LOOP;
END $$;

VACUUM (ANALYZE);
