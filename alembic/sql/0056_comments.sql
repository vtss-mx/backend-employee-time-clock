-- Comentarios de la base (COMMENT ON): qué guarda cada esquema y cada tabla, para quien la abre con psql o
-- DBeaver sin leer el código. Los ejecuta la migración 0056; las bases de los modelos ejecutan todos los
-- archivos de alembic/sql en orden (app/core/db_sql.py). Una tabla nueva agrega su comentario en el archivo de
-- su propia migración (`<revisión>_comments.sql`). Los de `company_id` de cada tabla de empresa los pone su
-- política de seguridad por fila (app/core/row_security.py).

COMMENT ON SCHEMA auth IS 'Identidad: cuentas, sesiones, cuentas recordadas y límites de peticiones (de la plataforma, sin seguridad por fila).';
COMMENT ON SCHEMA tenancy IS 'Empresas (los inquilinos), su política de verificación y sus llaves de la API de integración.';
COMMENT ON SCHEMA workforce IS 'Personal de cada empresa: empleados, QR, validadores, departamentos, sitios, turnos y calendario.';
COMMENT ON SCHEMA biometrics IS 'Biometría de cada empresa: registros faciales, plantillas cifradas, retos y huellas de capturas.';
COMMENT ON SCHEMA attendance IS 'Asistencia de cada empresa: bitácora de identificaciones, jornadas, descansos y registros.';
COMMENT ON SCHEMA catalog IS 'Catálogos (listas de valores) de toda la plataforma; se cargan desde alembic/seed/catalogs.json.';
COMMENT ON SCHEMA ops IS 'Operación de la plataforma: errores, métricas faciales, consumo, almacenamiento y tareas del mantenimiento.';
COMMENT ON SCHEMA billing IS 'Cobranza de cada empresa: plan, plantilla diaria, cargos, pagos y su aplicación (solo el ADMIN).';

COMMENT ON TABLE auth.users IS 'Cuentas de acceso. COMPANY y VALIDATOR pertenecen a una empresa (company_id); ADMIN a la plataforma; EMPLOYEE trabaja en una o varias empresas a través de workforce.employees.';
COMMENT ON TABLE auth.auth_sessions IS 'Sesiones (una por dispositivo): hash del refresh token que rota en cada uso, vencimiento, revocación y la empresa elegida por el empleado.';
COMMENT ON TABLE auth.remembered_accounts IS '"Recordar mi cuenta" de un dispositivo: el secreto de la cookie como hash SHA-256.';
COMMENT ON TABLE auth.rate_limit_counters IS 'Contadores de ventana fija del límite de peticiones, compartidos por todas las réplicas (UPSERT atómico).';

COMMENT ON TABLE tenancy.companies IS 'Empresas cliente (inquilinos). Cada dato de una empresa en las demás tablas lleva su company_id.';
COMMENT ON TABLE tenancy.verification_policy IS 'Política de verificación de cada empresa (una fila por empresa); la configura el ADMIN de la plataforma.';
COMMENT ON TABLE tenancy.company_api_keys IS 'Llaves de la API de integración de una empresa: solo el SHA-256 del secreto y su prefijo.';
COMMENT ON TABLE tenancy.company_api_key_scopes IS 'Permisos de lectura de cada llave de la API (catalog.api_scopes).';

COMMENT ON TABLE workforce.employees IS 'Empleos: una persona (auth.users) en una empresa. Datos únicos por empresa (número, RFC, CURP, NSS).';
COMMENT ON TABLE workforce.employee_qr_codes IS 'QR dinámicos de un solo uso: solo el hash del token, vigencia y quién lo usó.';
COMMENT ON TABLE workforce.validators IS 'Validadores de identidad de una empresa (cuenta VALIDATOR en una tableta o teléfono) con su domicilio.';
COMMENT ON TABLE workforce.validator_devices IS 'Dispositivos de cada validador: llave pública ECDSA y su autorización por la empresa.';
COMMENT ON TABLE workforce.departments IS 'Departamentos de una empresa.';
COMMENT ON TABLE workforce.department_managers IS 'Responsables de cada departamento (empleados de la misma empresa).';
COMMENT ON TABLE workforce.work_sites IS 'Sitios de trabajo con su geocerca (punto y radio).';
COMMENT ON TABLE workforce.shifts IS 'Turnos: horario, días, descansos, tolerancias y días remotos.';
COMMENT ON TABLE workforce.shift_sites IS 'Sitios donde se checa en persona con cada turno.';
COMMENT ON TABLE workforce.shift_assignments IS 'Turno de cada empleado desde una fecha (y hasta otra).';
COMMENT ON TABLE workforce.shift_change_requests IS 'Solicitudes de cambio de turno del empleado y su decisión.';
COMMENT ON TABLE workforce.company_holidays IS 'Días festivos de una empresa.';
COMMENT ON TABLE workforce.employee_absences IS 'Ausencias de un empleado (vacaciones, permisos, incapacidades) y su estado.';
COMMENT ON TABLE workforce.employee_workdays IS 'Días que un empleado sí trabaja aunque sean festivos o caigan en su ausencia.';
COMMENT ON TABLE workforce.employee_status_events IS 'Historial de altas, bajas y reactivaciones de cada empleo (base del cobro prorrateado); solo inserciones.';

COMMENT ON TABLE biometrics.face_enrollments IS 'Registros faciales y su revisión; la foto vive cifrada en el bucket (aquí solo su referencia).';
COMMENT ON TABLE biometrics.face_enrollment_flags IS 'Marcas de un registro facial para el revisor (accesorios, posible suplantación).';
COMMENT ON TABLE biometrics.face_embeddings IS 'Plantillas faciales cifradas de cada empleado (nunca la foto); las aprendidas compiten por su lugar.';
COMMENT ON TABLE biometrics.face_challenges IS 'Retos de prueba de vida de un solo uso (se consumen con DELETE ... RETURNING).';
COMMENT ON TABLE biometrics.capture_fingerprints IS 'Huella (SHA-256) de cada captura facial recibida: un reenvío se detecta en toda la plataforma.';

COMMENT ON TABLE attendance.verification_logs IS 'Bitácora de cada intento de identificación (particionada por mes en created_at).';
COMMENT ON TABLE attendance.work_sessions IS 'Jornadas: la entrada y salida de un turno con la copia de lo programado.';
COMMENT ON TABLE attendance.work_breaks IS 'Descansos de cada jornada.';
COMMENT ON TABLE attendance.attendance_events IS 'Bitácora de solo inserción de cada registro de asistencia con su evidencia (particionada por mes en occurred_at).';

COMMENT ON TABLE ops.error_reports IS 'Fallas del sistema agrupadas por huella y su seguimiento por el ADMIN (no los 4xx).';
COMMENT ON TABLE ops.error_occurrences IS 'Ocurrencias recientes de cada falla con su contexto literal sin secretos (particionada por mes en occurred_at).';
COMMENT ON TABLE ops.face_attempt_metrics IS 'Números de cada intento facial para la autocalibración (solo números; particionada por mes en created_at).';
COMMENT ON TABLE ops.security_thresholds IS 'Umbrales faciales que la plataforma ajustó sola (solo endurecen).';
COMMENT ON TABLE ops.usage_daily IS 'Consumo de cada empresa por día (company_id 0 = plataforma).';
COMMENT ON TABLE ops.usage_routes IS 'Consumo de cada empresa por día y ruta de la API (particionada por mes en day).';
COMMENT ON TABLE ops.usage_users IS 'Consumo de cada empresa por día y cuenta (particionada por mes en day).';
COMMENT ON TABLE ops.storage_snapshots IS 'Foto diaria del almacenamiento de cada empresa por grupo de tablas (particionada por mes en day).';
COMMENT ON TABLE ops.daily_tasks IS 'Tareas diarias del mantenimiento ya hechas (una vez por día entre todas las réplicas).';
COMMENT ON TABLE ops.storage_deletions IS 'Objetos del bucket por borrar porque su fila ya no los conserva.';
COMMENT ON TABLE ops.storage_status IS 'Resultado de cada tarea del almacenamiento de imágenes.';

COMMENT ON TABLE billing.plans IS 'Plan de cobro de cada empresa (una fila por empresa).';
COMMENT ON TABLE billing.headcount_days IS 'Empleados activos de cada empresa por día (base del prorrateo).';
COMMENT ON TABLE billing.charges IS 'Cargos emitidos al corte, con la copia del plan con que se calcularon; nunca se borran (se anulan).';
COMMENT ON TABLE billing.charge_lines IS 'Detalle por mes de cada cargo.';
COMMENT ON TABLE billing.payments IS 'Pagos registrados por el ADMIN; el comprobante vive cifrado en el bucket (aquí solo su referencia).';
COMMENT ON TABLE billing.payment_allocations IS 'Cuánto de cada pago se aplicó a cada cargo (de la misma empresa).';
