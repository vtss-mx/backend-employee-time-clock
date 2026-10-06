-- Comentarios de la migración 0065 (antifraude de identidad, fase 1b): la tabla nueva y las columnas de red de los
-- registros de asistencia. Los esquemas no cambian de descripción.

COMMENT ON TABLE workforce.employee_devices IS 'Dispositivos desde los que cada empleado checa o verifica su identidad: solo el hash de la llave no exportable que la app genera en el navegador, un nombre amigable, la decisión de la empresa y su primer y último uso (decisión D2).';

COMMENT ON COLUMN attendance.attendance_events.ip_country IS 'País de la IP del registro según la base local DB-IP (nunca la IP); el siguiente registro lo compara (NETWORK_JUMP).';

COMMENT ON COLUMN attendance.attendance_events.ip_asn IS 'Sistema autónomo (red) de la IP del registro según la base local DB-IP (nunca la IP); el siguiente registro lo compara (NETWORK_JUMP).';
