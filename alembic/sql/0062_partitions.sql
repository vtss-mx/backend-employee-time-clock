-- ops.ensure_partitions con la tabla de la migración 0062: `ops.risk_assessments` (la decisión del motor de riesgo de
-- cada intento facial, particionada por mes en `created_at` con la retención de las métricas faciales). El resto es
-- igual a `0055_partitions.sql` (que ya no se edita): crea los meses que faltan, mueve lo que haya caído en
-- `_default` y borra los vencidos; SECURITY DEFINER del dueño para que la API (sin DDL) solo pueda hacer esto y solo
-- en estas tablas. Si vuelve a cambiar, va en un archivo nuevo con el número de su migración.
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
                     'ops.risk_assessments', 'ops.error_occurrences', 'ops.usage_routes', 'ops.usage_users',
                     'ops.storage_snapshots') THEN
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
