-- Comentarios de la migración 0067 (validadores por empresa: límite que fija el ADMIN y cobro como empleados).

COMMENT ON COLUMN tenancy.companies.max_validators IS 'Validadores ACTIVOS que puede tener la empresa (lo decide el ADMIN). 0 = sin el módulo de validadores: su pantalla no aparece y sus APIs responden 403. Nunca menos de los activos.';

COMMENT ON TABLE workforce.validator_status_events IS 'Altas, bajas y reactivaciones de cada validador (solo inserciones). Con employee_status_events, la única fuente de la plantilla diaria del cobro: un validador activo cuenta como un empleado.';

COMMENT ON COLUMN workforce.validator_status_events.validator_id IS 'Validador (sin llave foránea a propósito: eliminarlo no borra los días que estuvo activo).';

COMMENT ON COLUMN billing.headcount_days.active_validators IS 'Validadores activos en algún momento del día (se cobran como empleados; aparte para el desglose).';
