-- Volumen sintético para medir los planes de las consultas (perf/db/run.sh, base AISLADA: jamás la de
-- trabajo). 200 empresas: la 1 con 20 000 empleados (la empresa grande) y las demás con 400 (≈100 k
-- empleados); 5 M de registros en la bitácora, 2 M de jornadas (20 días por empleado; hoy, 60 % abiertas),
-- ≈4.8 M de registros de asistencia, 300 k ausencias, 1 M de QR, de huellas de capturas y de métricas
-- faciales, 150 k sesiones y 200 k ocurrencias de errores; cobranza (≈2 k cargos) y consumo (≈4 M filas).
-- Los embeddings son bytes de relleno (no se miden comparaciones faciales, solo consultas). Una tabla nueva
-- se siembra aquí con su volumen esperado.
\timing on
SET search_path TO public;
SET synchronous_commit = off;

CREATE OR REPLACE FUNCTION pg_temp.co(i int) RETURNS int LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN i <= 20000 THEN 1 ELSE 2 + (i - 20001) / 400 END $$;

-- Tablas particionadas por mes (migración 0055): sus meses desde hace 14 meses (la historia que se siembra), como
-- quedarían tras un año de operación; lo de antes cae en `_default`.
SELECT ops.ensure_partitions(t, (current_date - interval '14 months')::date, 3, NULL)
FROM unnest(ARRAY['attendance.verification_logs', 'attendance.attendance_events', 'ops.face_attempt_metrics',
                  'ops.error_occurrences', 'ops.usage_routes', 'ops.usage_users', 'ops.storage_snapshots']) t;
-- Rendimiento por minuto (migración 0063): el mes pasado (su retención es de 7 días) y los que vienen.
SELECT ops.ensure_partitions('ops.perf_minutes', (current_date - interval '1 month')::date, 3, NULL);

-- ---------------- tenancy
-- Identificador fiscal (migración 0074): el RFC de cada una como su identificador (y en la columna anterior `rfc`).
INSERT INTO tenancy.companies (id, name, legal_name, rfc, tax_country, tax_id_type, tax_id, phone, active, api_enabled,
                               created_at, updated_at)
SELECT c, 'Empresa ' || c, 'Razon Social ' || c || ' SA de CV', r, 'MX', 'MX_RFC', r,
       '+5266210' || lpad(c::text, 5, '0'), c % 20 <> 0, c % 3 = 0, now() - interval '400 days', now()
FROM generate_series(1, 200) c, LATERAL (SELECT 'EMP' || lpad(c::text, 6, '0') || 'AB' || (c % 10) AS r) rfc;

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

-- ---------------- auth.user_avatars: foto de perfil de la mitad de las cuentas (dos tamaños; la imagen vive en el bucket)
INSERT INTO auth.user_avatars (user_id, size_px, version, content_type, object_name, byte_size, sha256, uploaded_at)
SELECT u.id, s.size_px, 'v' || u.id, 'image/webp',
       'perf/people/users/' || u.id || '/avatar/v' || u.id || '/' || s.size_px || '.webp.enc', 9000, repeat('a', 64), now()
FROM auth.users u, (VALUES (96), (512)) s(size_px)
WHERE u.id % 2 = 0;
UPDATE auth.users SET avatar_version = 'v' || id WHERE id % 2 = 0;

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

INSERT INTO workforce.shifts (id, company_id, name, start_time, end_time, weekdays, remote_weekdays, breaks_count, break_minutes,
                              early_check_in_minutes, late_tolerance_minutes, early_check_out_minutes, late_check_out_minutes, active, created_at, updated_at)
SELECT (c - 1) * 3 + s, c, (ARRAY['Matutino','Vespertino','Nocturno'])[s],
       (ARRAY[time '07:00', time '15:00', time '22:00'])[s], (ARRAY[time '15:00', time '23:00', time '06:00'])[s],
       CASE WHEN s = 3 THEN 127 ELSE 31 END, CASE WHEN s = 2 THEN 16 ELSE 0 END, 1, 30, 30, 10, 10, 120, true, now(), now()
FROM generate_series(1, 200) c, generate_series(1, 3) s;

-- Sitios de cada turno (el turno dice dónde se checa): dos o tres de los cinco sitios de su empresa.
INSERT INTO workforce.shift_sites (shift_id, site_id, company_id)
SELECT (c - 1) * 3 + s, (c - 1) * 5 + k, c
FROM generate_series(1, 200) c, generate_series(1, 3) s, generate_series(1, 5) k
WHERE k <= s + 1 OR k = 5;

-- Asignación vigente de cada empleado (id = empleado) y una histórica para un tercio (id = 200000 + empleado).
INSERT INTO workforce.shift_assignments (id, company_id, employee_id, shift_id, valid_from, valid_to, created_by_id, created_at, updated_at)
SELECT i, pg_temp.co(i), i, (pg_temp.co(i) - 1) * 3 + 1 + i % 3, current_date - 200, NULL,
       100000 + pg_temp.co(i), now() - interval '200 days', now()
FROM generate_series(1, 99600) i;
INSERT INTO workforce.shift_assignments (id, company_id, employee_id, shift_id, valid_from, valid_to, created_by_id, created_at, updated_at)
SELECT 200000 + i, pg_temp.co(i), i, (pg_temp.co(i) - 1) * 3 + 1 + (i + 1) % 3, current_date - 400, current_date - 201,
       100000 + pg_temp.co(i), now() - interval '400 days', now()
FROM generate_series(1, 99600) i WHERE i % 3 = 0;

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
INSERT INTO tenancy.company_api_key_scopes (api_key_id, company_id, scope)
SELECT c, c, s FROM generate_series(1, 200) c, unnest(ARRAY['EMPLOYEES_READ','ATTENDANCE_READ']) s;

-- ---------------- biometrics
INSERT INTO biometrics.face_enrollments (id, employee_id, company_id, status, photo_content_type, quality_score, samples, liveness_passed, submitted_at, reviewed_at, reviewed_by_id)
SELECT i, i, pg_temp.co(i), CASE WHEN i % 10 = 1 THEN 'PENDING' ELSE 'APPROVED' END, 'image/jpeg', 0.8, 5, true,
       now() - make_interval(days => i % 300), CASE WHEN i % 10 = 1 THEN NULL ELSE now() END,
       CASE WHEN i % 10 = 1 THEN NULL ELSE 100000 + pg_temp.co(i) END
FROM generate_series(1, 99600) i WHERE i % 10 <> 0;
-- Imágenes en el bucket (migraciones 0051 y 0053): cada registro con la referencia de su foto cifrada; la
-- BD nunca guarda la imagen.
UPDATE biometrics.face_enrollments
SET photo_object = 'production/companies/' || company_id || '/employees/' || employee_id || '/face-enrollments/' || id || '.jpg.enc',
    photo_size = 180000,
    photo_sha256 = md5(id::text) || md5(id::text),
    photo_uploaded_at = now();
INSERT INTO biometrics.face_embeddings (employee_id, company_id, enrollment_id, embedding_encrypted, model_name, dimension, detection_score, quality_score, active, learned, matches, last_matched_at, created_at)
SELECT i, pg_temp.co(i), i, decode(md5(i::text || k), 'hex'), 'sface+facenet', 640, 0.9, 0.8, true, k = 4, (i * k) % 50,
       CASE WHEN k = 4 THEN now() - make_interval(days => i % 200) END, now() - make_interval(days => i % 300)
FROM generate_series(1, 99600) i, generate_series(1, 4) k WHERE i % 10 > 1 AND (k < 4 OR i % 3 = 0);

INSERT INTO biometrics.capture_fingerprints (digest, company_id, created_at)
SELECT md5(n::text) || md5((n * 3)::text), 1 + n % 200, now() - make_interval(secs => n * 2.5) FROM generate_series(1, 1000000) n;
INSERT INTO biometrics.face_challenges (id, user_id, direction, issued_at, expires_at)
SELECT md5('ch' || n), 1 + n * 9 % 99600, 'TURN_LEFT', now() - make_interval(secs => n), now() - make_interval(secs => n) + interval '60 seconds'
FROM generate_series(1, 10000) n;

INSERT INTO workforce.employee_qr_codes (employee_id, company_id, token_hash, active, expires_at, used_at, used_by_id, completed_at, created_at, updated_at)
SELECT 1 + n % 99600, pg_temp.co(1 + n % 99600), md5('qr' || n) || md5(n::text), false, now() - make_interval(secs => (1000000 - n) * 2.6),
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

-- ---------------- cobranza y consumo (0050)
-- Historial de altas (una por empleado) y bajas (los inactivos); plan de cobro en 172 empresas (las demás
-- sin plan), plantilla diaria de 400 días, 12 cargos por empresa con su línea (los 2 más recientes
-- abiertos, uno ya vencido) y 10 pagos aplicados (más un saldo a favor en una de cada 5); consumo por día
-- de 400 días (también la "empresa" 0), por ruta de 90 días × 40 rutas (≈720 k), por cuenta de 30 días
-- (≈3 M) y fotos del almacenamiento de 400 días × 5 grupos.
INSERT INTO workforce.employee_status_events (company_id, employee_id, active, occurred_at)
SELECT company_id, id, true, created_at FROM workforce.employees;
INSERT INTO workforce.employee_status_events (company_id, employee_id, active, occurred_at)
SELECT company_id, id, false, updated_at FROM workforce.employees WHERE NOT active;
-- Validadores (0067): cada uno con su alta (se cobran como empleados); su límite, los que tiene cada empresa.
INSERT INTO workforce.validator_status_events (company_id, validator_id, active, occurred_at)
SELECT company_id, id, true, created_at FROM workforce.validators;
UPDATE tenancy.companies SET max_validators = 5;

INSERT INTO billing.plans (company_id, pricing_mode, unit_price, price_period, interval_months, starts_on, trial_days,
                           tax_rate, grace_days, next_cut_on, forecast_total, forecast_at, created_at, updated_at)
SELECT c, CASE WHEN c % 10 = 0 THEN 'FLAT' ELSE 'PER_USER' END, 45, 'MONTH', 1,
       (date_trunc('month', current_date) - interval '13 months')::date, 0, 16, 10,
       (date_trunc('month', current_date) + interval '1 month - 1 day')::date, 20000, now() - interval '1 day', now(), now()
FROM generate_series(1, 200) c WHERE c % 7 <> 0;

INSERT INTO billing.headcount_days (company_id, day, active_employees, active_validators)
SELECT c, current_date - d, CASE WHEN c = 1 THEN 19000 ELSE 380 END, 5 FROM generate_series(1, 200) c, generate_series(1, 400) d;
INSERT INTO ops.daily_tasks (task, day, done_at) SELECT 'HEADCOUNT', current_date - d, now() FROM generate_series(1, 400) d;

INSERT INTO billing.charges (id, company_id, sequence, cut_on, period_start, period_end, issued_on, due_on, billable_days,
                             units, pricing_mode, unit_price, price_period, tax_rate, subtotal, discount, tax, total, paid,
                             status, created_at)
SELECT (c - 1) * 12 + m, c, m, k.cut, s.start, k.cut, k.cut + 1, k.cut + 1, k.cut - s.start + 1, 11400, 'PER_USER', 45,
       'MONTH', 16, 17100, 0, 2736, 19836, CASE WHEN m <= 10 THEN 19836 ELSE 0 END,
       CASE WHEN m <= 10 THEN 'PAID' ELSE 'OPEN' END, now()
FROM generate_series(1, 200) c, generate_series(1, 12) m,
     LATERAL (SELECT (date_trunc('month', current_date) - make_interval(months => 13 - m))::date AS start) s,
     LATERAL (SELECT (s.start + interval '1 month - 1 day')::date AS cut) k
WHERE c % 7 <> 0;
INSERT INTO billing.charge_lines (charge_id, company_id, month, days, units, amount)
SELECT id, company_id, period_start, billable_days, units, subtotal FROM billing.charges;
INSERT INTO billing.payments (id, company_id, amount, paid_on, method, reference, status, applied, recorded_by, created_at)
SELECT id, company_id, total, issued_on + 5, 'TRANSFER', 'REF-' || id, 'CONFIRMED', total, 'admin@plataforma.mx', now()
FROM billing.charges WHERE status = 'PAID';
INSERT INTO billing.payment_allocations (payment_id, charge_id, company_id, amount)
SELECT id, id, company_id, amount FROM billing.payments;
INSERT INTO billing.payments (id, company_id, amount, paid_on, method, reference, status, applied, recorded_by, created_at)
SELECT 100000 + c, c, 500, current_date - 3, 'CASH', NULL, 'CONFIRMED', 0, 'admin@plataforma.mx', now()
FROM generate_series(1, 200) c WHERE c % 5 = 0 AND c % 7 <> 0;

INSERT INTO ops.usage_daily (company_id, day, requests, bytes_in, bytes_out, duration_ms, max_ms, server_errors, client_errors)
SELECT c, current_date - d, CASE WHEN c = 1 THEN 500000 ELSE 10000 END, 2000000, 40000000, 900000, 3200, c % 3, 40
FROM generate_series(0, 200) c, generate_series(0, 399) d;
INSERT INTO ops.usage_routes (company_id, day, route, requests, bytes_in, bytes_out, duration_ms, max_ms, server_errors, client_errors)
SELECT c, current_date - d, 'GET /api/route/' || r, 250 + r, 50000, 1000000, 22000, 800, 0, r % 2
FROM generate_series(0, 200) c, generate_series(0, 89) d, generate_series(1, 40) r;
INSERT INTO ops.usage_users (company_id, day, user_id, requests, bytes_in, bytes_out, duration_ms, max_ms, server_errors, client_errors)
SELECT e.company_id, current_date - d, e.user_id, 20 + e.id % 50, 4000, 80000, 1800, 300, 0, e.id % 2
FROM workforce.employees e, generate_series(0, 29) d;
INSERT INTO ops.storage_snapshots (company_id, day, category, rows, bytes)
SELECT c, current_date - d, g, 1000 * c, 4096000 * c
FROM generate_series(1, 200) c, generate_series(0, 399) d,
     unnest(ARRAY['PEOPLE', 'BIOMETRICS', 'ATTENDANCE', 'SECURITY', 'BILLING']) g;

-- ---------------- rendimiento (migración 0063): 7 días por minuto de 150 rutas, 40 funciones y 30 pantallas (sus Web
-- Vitals cada 5 minutos); 90 días por hora; 2 años por día; 300 alertas de peticiones lentas.
INSERT INTO ops.perf_minutes (kind, minute, name, count, errors, client_errors, total_ms, max_ms, db_ms, db_queries,
                              bytes_in, bytes_out, h_10, h_20, h_50, h_100, h_1000)
SELECT 'HTTP', date_trunc('minute', now()) - make_interval(mins => m), 'GET /api/route/' || r, 20, r % 2, 1, 900, 1200,
       200, 40, 2000, 40000, 5, 6, 5, 3, 1
FROM generate_series(0, 10079) m, generate_series(1, 150) r;
INSERT INTO ops.perf_minutes (kind, minute, name, count, errors, total_ms, max_ms, h_5, h_50)
SELECT 'FUNCTION', date_trunc('minute', now()) - make_interval(mins => m), 'area.function_' || f, 10, 0, 120, 60, 8, 2
FROM generate_series(0, 10079) m, generate_series(1, 40) f;
INSERT INTO ops.perf_minutes (kind, minute, name, count, total_ms, max_ms, h_600, h_2000)
SELECT k, date_trunc('minute', now()) - make_interval(mins => m * 5), '/screen/' || s, 2, 1800, 1500, 1, 1
FROM generate_series(0, 2015) m, generate_series(1, 30) s,
     unnest(ARRAY['WEB_LCP', 'WEB_INP', 'WEB_CLS', 'WEB_FCP', 'WEB_TTFB', 'WEB_LONG_TASK']) k;
INSERT INTO ops.perf_hours (kind, hour, name, count, errors, client_errors, total_ms, max_ms, db_ms, db_queries, h_20,
                            h_100, h_1000)
SELECT 'HTTP', date_trunc('hour', now()) - make_interval(hours => h), 'GET /api/route/' || r, 1200, 30, 60, 54000, 1200,
       12000, 2400, 700, 400, 100
FROM generate_series(0, 2159) h, generate_series(1, 150) r;
INSERT INTO ops.perf_hours (kind, hour, name, count, total_ms, max_ms, h_5, h_50)
SELECT 'FUNCTION', date_trunc('hour', now()) - make_interval(hours => h), 'area.function_' || f, 600, 7200, 60, 480, 120
FROM generate_series(0, 2159) h, generate_series(1, 40) f;
INSERT INTO ops.perf_hours (kind, hour, name, count, total_ms, max_ms, h_600, h_2000)
SELECT k, date_trunc('hour', now()) - make_interval(hours => h), '/screen/' || s, 24, 21600, 1500, 12, 12
FROM generate_series(0, 2159) h, generate_series(1, 30) s,
     unnest(ARRAY['WEB_LCP', 'WEB_INP', 'WEB_CLS', 'WEB_FCP', 'WEB_TTFB', 'WEB_LONG_TASK']) k;
INSERT INTO ops.perf_days (kind, day, name, count, errors, total_ms, max_ms, h_20, h_100, h_1000)
SELECT 'HTTP', current_date - d, 'GET /api/route/' || r, 28800, 720, 1296000, 1500, 16800, 9600, 2400
FROM generate_series(0, 729) d, generate_series(1, 150) r;
INSERT INTO ops.perf_days (kind, day, name, count, total_ms, max_ms, h_5, h_50)
SELECT 'FUNCTION', current_date - d, 'area.function_' || f, 14400, 172800, 60, 11520, 2880
FROM generate_series(0, 729) d, generate_series(1, 40) f;
INSERT INTO ops.slow_request_alerts (route, method, path, status, count, total_ms, last_ms, max_ms, threshold_ms,
                                     first_seen_at, last_seen_at, opened_at, last_trace_id, last_status, sample)
SELECT 'GET /api/slow/' || a, 'GET', '/api/slow/' || a, (ARRAY['OPEN', 'ACKNOWLEDGED', 'RESOLVED'])[1 + a % 3], 10 + a,
       15000 * (10 + a), 1500, 4000, 1000, now() - interval '30 days', now() - make_interval(mins => a),
       now() - make_interval(mins => a), 'trace' || a, 200, '{"method": "GET"}'
FROM generate_series(1, 300) a;

-- ---------------- antifraude (migración 0062): la decisión del motor de riesgo de cada intento facial (1 M en 30 días,
-- particionada por mes; 10 % con señales, 2 % de riesgo alto, 0.2 % etiquetados como fraude), las huellas de captura
-- de los últimos 30 días (6 por empleado ≈ 600 k), 20 k casos de fraude (5 % por revisar) con 3 intentos, 2 eventos y
-- 3 fotogramas cada uno, 2 k firmas de ataque, 10 cambios de política por empresa y 1 % de jornadas "en revisión".
SELECT ops.ensure_partitions('ops.risk_assessments', (current_date - interval '14 months')::date, 3, NULL);
INSERT INTO ops.risk_assessments (company_id, created_at, verification_log_id, score, tier, action, reasons, policy_version,
                                  engine, step_up, fallback, fraud_label)
SELECT CASE WHEN n % 5 = 0 THEN 1 ELSE 1 + n % 200 END, now() - make_interval(secs => (1000000 - n) * 2.6), n,
       CASE WHEN n % 50 = 0 THEN 65 WHEN n % 10 = 0 THEN 30 ELSE 0 END,
       CASE WHEN n % 50 = 0 THEN 'HIGH' WHEN n % 10 = 0 THEN 'MEDIUM' ELSE 'LOW' END,
       CASE WHEN n % 50 = 0 THEN 'REVIEW' WHEN n % 10 = 0 THEN 'STEP_UP' ELSE 'ALLOW' END,
       CASE WHEN n % 10 = 0 THEN '[{"code": "CAMERA_LABEL_MISSING", "points": 30, "mode": "ENFORCE", "kind": "INJECTION", "value": null, "threshold": null}]'::jsonb
            ELSE '[]'::jsonb END,
       'abcdef012345', '1.0.0', false, false, CASE WHEN n % 500 = 0 THEN 'FRAUD' END
FROM generate_series(1, 1000000) n;
INSERT INTO biometrics.capture_traces (company_id, employee_id, created_at, face_phash, frame_phash, embedding_encrypted, dimension)
SELECT pg_temp.co(e), e, now() - make_interval(hours => k * 120 + e % 97), (e::bigint * 2654435761 + k) % 9007199254740991,
       (e::bigint * 40503 + k) % 9007199254740991, decode(repeat('00', 300), 'hex'), 128
FROM generate_series(1, 99600) e, generate_series(0, 5) k;
INSERT INTO ops.fraud_cases (id, company_id, created_at, last_attempt_at, status, kind, subject, employee_id, actor_id, attempts,
                             max_score, tier, reason, evidence, decided_by, decided_at, decision_note)
SELECT c, pg_temp.co(e), now() - make_interval(hours => c), now() - make_interval(hours => c) + interval '10 minutes',
       CASE WHEN c % 20 = 0 THEN 'OPEN' WHEN c % 20 = 1 THEN 'IN_REVIEW' WHEN c % 3 = 0 THEN 'CONFIRMED' ELSE 'FALSE_POSITIVE' END,
       'PRESENTATION', 'employee:' || e, e, e, 3, 65, 'HIGH', 'SPOOF_DETECTED', 3,
       CASE WHEN c % 20 > 1 THEN 'admin@plataforma.com' END, CASE WHEN c % 20 > 1 THEN now() - make_interval(hours => c) END,
       CASE WHEN c % 20 > 1 THEN 'Revisado' END
FROM (SELECT c, 1 + (c * 4973) % 99600 AS e FROM generate_series(1, 20000) c) s;
INSERT INTO ops.fraud_case_attempts (company_id, case_id, attempted_at, verification_log_id, success, reason, score, action,
                                     signals, metrics, phashes)
SELECT f.company_id, f.id, f.created_at + make_interval(mins => a), f.id * 3 + a, false, 'SPOOF_DETECTED', 65, 'DENY',
       '[]'::jsonb, '{"frontal_real_min": 0.01}'::jsonb, '["0000000000000000:0000000000000000"]'::jsonb
FROM ops.fraud_cases f, generate_series(0, 2) a;
INSERT INTO ops.fraud_case_events (company_id, case_id, created_at, kind, actor, note)
SELECT f.company_id, f.id, f.created_at + make_interval(mins => a), CASE WHEN a = 0 THEN 'OPENED' ELSE 'NOTE' END,
       CASE WHEN a = 1 THEN 'admin@plataforma.com' END, 'SPOOF_DETECTED'
FROM ops.fraud_cases f, generate_series(0, 1) a;
INSERT INTO ops.fraud_evidence (company_id, case_id, employee_id, created_at, kind, position, uid, content_type, object_name,
                                byte_size, sha256, uploaded_at)
SELECT f.company_id, f.id, f.employee_id, f.created_at, 'FRONTAL', p, md5(f.id || '-' || p), 'image/jpeg',
       'perf/companies/' || f.company_id || '/fraud-cases/' || f.id || '/' || p, 40000, repeat('0', 64), f.created_at
FROM ops.fraud_cases f, generate_series(0, 2) p;
INSERT INTO ops.attack_signatures (kind, value, company_id, case_id, companies, hits, allowed, created_at, expires_at)
SELECT 'CAPTURE_PHASH', lpad(to_hex(s), 16, '0') || ':' || lpad(to_hex(s * 7), 16, '0'), CASE WHEN s % 10 = 0 THEN NULL ELSE 1 + s % 200 END,
       s, 1, 0, s % 25 = 0, now() - make_interval(days => s % 300), now() + make_interval(days => 365 - s % 300)
FROM generate_series(1, 2000) s;
INSERT INTO ops.policy_changes (company_id, created_at, status, relaxes, changes, requested_by, decided_by, decided_at)
SELECT c, now() - make_interval(days => k * 7), CASE WHEN k = 0 THEN 'PENDING' ELSE 'APPLIED' END, k % 2 = 0,
       '[{"field": "liveness_steps", "before": 2, "after": 3, "relaxes": false}]'::jsonb, 'admin@plataforma.com',
       CASE WHEN k > 0 THEN 'otro@plataforma.com' END, CASE WHEN k > 0 THEN now() - make_interval(days => k * 7) END
FROM generate_series(1, 200) c, generate_series(0, 9) k;
UPDATE attendance.work_sessions SET review_status = CASE WHEN id % 300 = 0 THEN 'PENDING' ELSE 'CONFIRMED' END,
                                    review_reasons = 'CAPTURE'
WHERE id % 100 = 0;

-- ---------------- dispositivos de los empleados (migración 0065, antifraude 1b): uno por empleado (la mitad aprobado), un
-- segundo ya revocado para uno de cada cinco y, en uno de cada cien, la llave del compañero anterior de su empresa (el
-- teléfono compartido de DEVICE_SHARED). ≈120 k filas; solo hashes de llaves.
INSERT INTO workforce.employee_devices (company_id, employee_id, key_hash, name, status, first_seen_at, last_seen_at, uses)
SELECT pg_temp.co(e), e, md5('dev-' || e) || md5(e || '-dev'), 'iPhone · Safari',
       CASE WHEN e % 2 = 0 THEN 'APPROVED' ELSE 'PENDING' END, now() - make_interval(days => 30 + e % 300),
       now() - make_interval(mins => e % 1440), 20 + e % 200
FROM generate_series(1, 99600) e;
INSERT INTO workforce.employee_devices (company_id, employee_id, key_hash, name, status, first_seen_at, last_seen_at, uses)
SELECT pg_temp.co(e), e, md5('old-' || e) || md5(e || '-old'), 'Android · Chrome', 'REVOKED',
       now() - make_interval(days => 400 + e % 300), now() - make_interval(days => 300 + e % 60), 5 + e % 50
FROM generate_series(5, 99600, 5) e;
INSERT INTO workforce.employee_devices (company_id, employee_id, key_hash, name, status, first_seen_at, last_seen_at, uses)
SELECT pg_temp.co(e), e, md5('dev-' || (e - 1)) || md5((e - 1) || '-dev'), 'iPhone · Safari', 'PENDING',
       now() - make_interval(days => 3), now() - make_interval(mins => e % 30), 3
FROM generate_series(100, 99600, 100) e WHERE pg_temp.co(e) = pg_temp.co(e - 1);

-- ---------------- «Eliminados» (borrado lógico, migración 0068): ≈1.5 % de los empleados con su cuenta, uno de cada diez
-- validadores con la suya y una parte de turnos, sitios, departamentos, festivos, días laborables y cambios de turno,
-- eliminados en los últimos 400 días (una parte ya pasó la retención de 365: la depuración la encuentra). Lo vigente
-- sigue siendo la gran mayoría, como en producción; los planes miden que lo eliminado nunca se lea en el camino normal.
UPDATE workforce.employees SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 67 = 0;
UPDATE workforce.validators SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 10 = 0;
UPDATE auth.users AS u SET deleted_at = e.deleted_at, deleted_by = e.deleted_by
FROM workforce.employees AS e WHERE e.user_id = u.id AND e.deleted_at IS NOT NULL;
UPDATE auth.users AS u SET deleted_at = v.deleted_at, deleted_by = v.deleted_by
FROM workforce.validators AS v WHERE v.user_id = u.id AND v.deleted_at IS NOT NULL;
UPDATE workforce.shifts SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 40 = 0;
UPDATE workforce.work_sites SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 40 = 0;
UPDATE workforce.departments SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 40 = 0;
UPDATE workforce.company_holidays SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 20 = 0;
UPDATE workforce.employee_workdays SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 20 = 0;
UPDATE workforce.shift_assignments SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 50 = 0 AND valid_from > current_date;

-- ---------------- Kioscos de los sitios (antifraude 2b, migración 0070): uno de cada tres sitios con su código y dos
-- kioscos por sitio (el primero vinculado, el segundo con su código de vinculación pendiente); ≈5 % en «Eliminados».
UPDATE workforce.work_sites SET presence_code = (id % 3 = 0), presence_secret = CASE WHEN id % 3 = 0 THEN 'cifrado' END;
INSERT INTO workforce.site_kiosks (company_id, site_id, name, pairing_hash, pairing_expires_at, key_hash, public_key,
                                   device_name, paired_at, last_seen_at, created_at)
SELECT s.company_id, s.id, 'Kiosco ' || k,
       CASE WHEN k = 2 THEN md5('p' || s.id) || md5(s.id || 'p') END, CASE WHEN k = 2 THEN now() + interval '1 day' END,
       CASE WHEN k = 1 THEN md5('k' || s.id) || md5(s.id || 'k') END, CASE WHEN k = 1 THEN 'MFkwEwYHKoZIzj0CAQ' END,
       CASE WHEN k = 1 THEN 'iPad · Safari' END, CASE WHEN k = 1 THEN now() - interval '30 days' END,
       CASE WHEN k = 1 THEN now() - make_interval(mins => s.id % 600) END, now() - interval '40 days'
FROM workforce.work_sites s, generate_series(1, 2) k ORDER BY s.id, k;
UPDATE workforce.site_kiosks SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx',
       pairing_hash = NULL, pairing_expires_at = NULL
WHERE id % 20 = 0;

-- ---------------- Documentos de la empresa (migración 0075): 50 por empresa y 2 000 en la grande (años de contratos,
-- constancias y comprobantes), ≈5 % en «Eliminados» en los últimos 400 días (una parte ya pasó la retención: la
-- depuración la encuentra). Solo la referencia: el archivo cifrado vive en el bucket.
INSERT INTO tenancy.company_documents (company_id, type, file_name, note, content_type, uid, object_name, byte_size,
                                       sha256, uploaded_at, uploaded_by, uploaded_by_platform)
SELECT c, (ARRAY['TAX_CERTIFICATE', 'INCORPORATION', 'PROOF_OF_ADDRESS', 'REPRESENTATIVE_ID', 'CONTRACT', 'OTHER'])[1 + d % 6],
       'documento-' || d || '.pdf', CASE WHEN d % 4 = 0 THEN 'Nota ' || d END, 'application/pdf', md5(c || '-' || d),
       'perf/companies/' || c || '/documents/' || md5(c || '-' || d) || '.pdf.enc', 100000 + d * 37,
       md5(d || '-' || c) || md5(c || '+' || d), now() - make_interval(days => d % 400),
       'admin' || c || '@empresa' || c || '.mx', d % 5 = 0
FROM generate_series(1, 200) c, generate_series(1, CASE WHEN c = 1 THEN 2000 ELSE 50 END) d ORDER BY c, d;
UPDATE tenancy.company_documents SET deleted_at = now() - make_interval(days => id % 400), deleted_by = 'seed@empresa.mx'
WHERE id % 20 = 0;

-- ---------------- secuencias al día
DO $$
DECLARE r record; m bigint;
BEGIN
  FOR r IN SELECT n.nspname, c.relname, a.attname, pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname) AS seq
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid
           WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname IN ('auth','tenancy','workforce','biometrics','attendance','ops','billing') AND a.attnum > 0 AND NOT a.attisdropped
             AND pg_get_serial_sequence(quote_ident(n.nspname) || '.' || quote_ident(c.relname), a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT coalesce(max(%I), 0) FROM %I.%I', r.attname, r.nspname, r.relname) INTO m;
    PERFORM setval(r.seq, greatest(m, 1));
  END LOOP;
END $$;

VACUUM (ANALYZE);
