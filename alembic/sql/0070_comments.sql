-- Comentarios de la migración 0070 (antifraude de identidad, fase 2b: presencia del validador y código de sitio).
-- Ningún código ni secreto en claro: el secreto de cada sitio va cifrado y del código solo se guarda su periodo.

COMMENT ON TABLE workforce.site_kiosks IS 'Kioscos de los sitios: la tableta que muestra el código rotativo del sitio. Solo el SHA-256 del código de vinculación (un solo uso, vence) y, ya vinculada, el hash y la llave pública de su llave WebCrypto no exportable (decisión D9).';

COMMENT ON COLUMN workforce.work_sites.presence_code IS 'Código de sitio activado: la entrada y la salida aquí llevan el código rotativo de su kiosco (apagado por omisión; decisión D9).';

COMMENT ON COLUMN workforce.work_sites.presence_secret IS 'Secreto del código del sitio (32 bytes al azar) cifrado con DATA_ENCRYPTION_KEY (Fernet): nunca sale del servidor.';

COMMENT ON COLUMN attendance.attendance_events.presence_window IS 'Periodo del código de sitio con que se confirmó la presencia (nunca el código): el siguiente registro del empleado en ese sitio no puede repetirlo.';

COMMENT ON COLUMN auth.auth_sessions.device_key_hash IS 'Validador: SHA-256 de la llave del dispositivo con que se inició la sesión (o la primera que firmó); cada identificación debe venir firmada por ella.';

COMMENT ON COLUMN tenancy.verification_policy.validator_signing IS 'Firma por petición del dispositivo del validador en cada identificación: apagada, solo medir (señal) u obligatoria (403).';

COMMENT ON COLUMN tenancy.verification_policy.validator_location IS 'Ubicación en cada identificación de los validadores que la requieren: apagada, solo medir (señal) u obligatoria (403).';

COMMENT ON COLUMN tenancy.verification_policy.site_codes IS 'Código de sitio en la entrada y la salida de los sitios que lo activan: apagado, solo medir (señal) u obligatorio (rechazo).';
