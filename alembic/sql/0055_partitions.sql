-- ops.ensure_partitions: particiones MENSUALES de las tablas que crecen sin límite (app/core/partitions.py).
--
-- La crea la migración 0055 y la ejecuta en cada vuelta el mantenimiento (rol de la plataforma). Las bases que
-- salen de los modelos (create_all de las pruebas y de quality.sh) ejecutan TODOS los archivos de alembic/sql en
-- orden (app/core/db_sql.py). Si cambia, va en un archivo nuevo con el número de su migración
-- (`<revisión>_partitions.sql`, CREATE OR REPLACE): este, como su migración, ya no se edita.
--
-- - Crea los meses que falten desde el mes de `p_from` (NULL = el actual) hasta `p_months_ahead` meses después
--   del actual (UTC); un mes que ya cubre otra partición (p. ej. `_legacy`) se salta.
-- - Si `_default` ya tiene filas de un mes nuevo (el mantenimiento no corrió a tiempo), las mueve a su
--   partición antes de engancharla: ninguna fila se queda sin su mes ni se pierde.
-- - Borra (DROP TABLE, instantáneo) las particiones que terminan antes de `p_keep_from` (NULL = no borra nada):
--   la retención por partición en lugar de un DELETE fila por fila. Nunca borra `_default`.
-- - Crear un mes usa ATTACH PARTITION (bloqueo SHARE UPDATE EXCLUSIVE del padre: no frena lecturas ni
--   escrituras); borrar uno toma un instante el bloqueo exclusivo del padre. `lock_timeout` de 3 s: si hay
--   espera, falla sola y la siguiente vuelta lo vuelve a intentar (se crean meses por adelantado).
-- - SECURITY DEFINER con `search_path` fijo: corre con los permisos del dueño de la base, así el rol de la API
--   (sin permisos de DDL) solo puede hacer ESTO y solo en las tablas de la lista.
CREATE OR REPLACE FUNCTION ops.ensure_partitions(p_table text, p_from date, p_months_ahead integer, p_keep_from date)
RETURNS TABLE (created integer, dropped integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET "TimeZone" = 'UTC'
SET lock_timeout = '3s'
AS $function$
DECLARE
  v_parent regclass;
  v_schema text;
  v_name text;
  v_key text;
  v_default regclass;
  v_month date;
  v_next date;
  v_until date;
  v_part text;
  v_created integer := 0;
  v_dropped integer := 0;
  r record;
BEGIN
  IF p_table NOT IN ('attendance.verification_logs', 'attendance.attendance_events', 'ops.face_attempt_metrics',
                     'ops.error_occurrences', 'ops.usage_routes', 'ops.usage_users', 'ops.storage_snapshots') THEN
    RAISE EXCEPTION 'La tabla % no tiene particiones administradas', p_table USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p_months_ahead IS NULL OR p_months_ahead NOT BETWEEN 0 AND 24 THEN
    RAISE EXCEPTION 'Meses por adelantado fuera de rango: %', p_months_ahead USING ERRCODE = 'invalid_parameter_value';
  END IF;
  v_parent := p_table::regclass;
  SELECT n.nspname, c.relname INTO v_schema, v_name
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = v_parent;
  SELECT a.attname INTO v_key
  FROM pg_partitioned_table pt JOIN pg_attribute a ON a.attrelid = pt.partrelid AND a.attnum = pt.partattrs[0]
  WHERE pt.partrelid = v_parent;
  v_default := to_regclass(format('%I.%I', v_schema, v_name || '_default'));

  v_month := date_trunc('month', coalesce(p_from, current_date))::date;
  v_until := (date_trunc('month', current_date) + make_interval(months => p_months_ahead))::date;
  WHILE v_month <= v_until LOOP
    v_next := (v_month + interval '1 month')::date;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_inherits i
      JOIN pg_class c ON c.oid = i.inhrelid
      CROSS JOIN LATERAL (SELECT pg_get_expr(c.relpartbound, c.oid) AS bound) b
      WHERE i.inhparent = v_parent
        AND b.bound <> 'DEFAULT'
        AND coalesce(substring(b.bound FROM 'FROM \(''([^'']*)''\)')::timestamptz, '-infinity') < v_next::timestamptz
        AND coalesce(substring(b.bound FROM 'TO \(''([^'']*)''\)')::timestamptz, 'infinity') > v_month::timestamptz
    ) THEN
      v_part := format('%I.%I', v_schema, v_name || '_p' || to_char(v_month, 'YYYYMM'));
      -- Las de consumo se actualizan en cada lote del medidor: espacio libre para actualizaciones HOT.
      EXECUTE format('CREATE TABLE %s (LIKE %s INCLUDING DEFAULTS INCLUDING CONSTRAINTS)%s', v_part, v_parent,
                     CASE WHEN p_table IN ('ops.usage_routes', 'ops.usage_users') THEN ' WITH (fillfactor = 80)' ELSE '' END);
      IF v_default IS NOT NULL THEN
        EXECUTE format(
          'WITH moved AS (DELETE FROM %s WHERE %I >= %L AND %I < %L RETURNING *) INSERT INTO %s SELECT * FROM moved',
          v_default, v_key, v_month, v_key, v_next, v_part);
      END IF;
      EXECUTE format('ALTER TABLE %s ATTACH PARTITION %s FOR VALUES FROM (%L) TO (%L)', v_parent, v_part, v_month, v_next);
      v_created := v_created + 1;
    END IF;
    v_month := v_next;
  END LOOP;

  IF p_keep_from IS NOT NULL THEN
    FOR r IN
      SELECT c.oid::regclass AS part
      FROM pg_inherits i
      JOIN pg_class c ON c.oid = i.inhrelid
      CROSS JOIN LATERAL (SELECT pg_get_expr(c.relpartbound, c.oid) AS bound) b
      WHERE i.inhparent = v_parent
        AND b.bound <> 'DEFAULT'
        AND substring(b.bound FROM 'TO \(''([^'']*)''\)')::timestamptz <= p_keep_from::timestamptz
    LOOP
      EXECUTE format('DROP TABLE %s', r.part);
      v_dropped := v_dropped + 1;
    END LOOP;
  END IF;
  RETURN QUERY SELECT v_created, v_dropped;
END;
$function$;

COMMENT ON FUNCTION ops.ensure_partitions(text, date, integer, date) IS
  'Crea las particiones mensuales que faltan (y mueve lo que haya caído en _default) y borra las vencidas; '
  'solo para las tablas de app/core/partitions.py. SECURITY DEFINER: la API no tiene permisos de DDL.';

-- Nadie la ejecuta salvo el rol de la plataforma (se le concede al aprovisionar los roles: app/core/db_roles.py).
REVOKE ALL ON FUNCTION ops.ensure_partitions(text, date, integer, date) FROM PUBLIC;
