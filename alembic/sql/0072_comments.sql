-- Comentarios de la migración 0072 (decisiones del dueño del 2026-10-06: desglose de los días-persona del cobro).

COMMENT ON COLUMN billing.charges.validator_units IS 'De los días-persona del cargo (units, cobro por empleado activo), cuántos fueron de validadores; los demás son de empleados. Solo el desglose que se muestra: no cambia el importe. NULL = monto fijo o cargo emitido antes de guardarlo.';

COMMENT ON COLUMN billing.charge_lines.validator_units IS 'De los días-persona de la línea (units), cuántos fueron de validadores. NULL = monto fijo o línea anterior a la migración 0072.';
