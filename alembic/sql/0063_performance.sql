-- Migración 0063 (observabilidad de rendimiento): comentarios de las tablas nuevas y la función que entrega al ADMIN
-- las consultas que más consumen la base (`pg_stat_statements`).

COMMENT ON SCHEMA ops IS 'Operación de la plataforma: errores, rendimiento, métricas faciales, consumo, almacenamiento y tareas del mantenimiento.';

COMMENT ON TABLE ops.perf_minutes IS 'Rendimiento por minuto, tipo (HTTP, FUNCTION, WEB_*) y nombre (ruta, función o pantalla): conteos, fallas, tiempos, base de datos e histograma de cubetas fijas (particionada por mes en minute).';
COMMENT ON TABLE ops.perf_hours IS 'Rendimiento por hora (UTC): resumen de perf_minutes que arma el mantenimiento para los periodos largos.';
COMMENT ON TABLE ops.perf_days IS 'Rendimiento por día del negocio: resumen de perf_hours que arma el mantenimiento.';
COMMENT ON TABLE ops.slow_request_alerts IS 'Peticiones más lentas que SLOW_REQUEST_THRESHOLD_MS agrupadas por ruta (regla 18): contador, tiempos, último traceId, muestra sin secretos y su seguimiento por el ADMIN.';

-- ops.top_statements: las consultas que más consumen la base, para la pantalla "Rendimiento" del ADMIN.
--
-- - Solo el texto NORMALIZADO de pg_stat_statements (las constantes ya vienen como $1, $2...; nunca parámetros), de
--   esta base de datos, recortado a 2 000 caracteres; con su conteo total y el tiempo total de todas (porcentaje).
-- - SECURITY DEFINER del dueño: la API (sin `pg_read_all_stats`) solo vería el texto de sus propias consultas; así
--   el rol de la plataforma ve todas, solo por aquí y solo lo de esta base. Nadie más la ejecuta.
-- - Si la extensión no está (PostgreSQL sin `shared_preload_libraries`), no devuelve nada: la consulta va con
--   EXECUTE para que la función exista aunque la vista no.
CREATE OR REPLACE FUNCTION ops.top_statements(p_order text, p_limit integer, p_offset integer)
RETURNS TABLE (
  query_id bigint,
  query_text text,
  calls bigint,
  total_ms double precision,
  mean_ms double precision,
  max_ms double precision,
  row_count bigint,
  total_count bigint,
  grand_total_ms double precision
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
  IF p_order IS NULL OR p_order NOT IN ('total', 'mean', 'max', 'calls') THEN
    RAISE EXCEPTION 'Orden inválido: %', p_order USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 OR p_offset IS NULL OR p_offset < 0 THEN
    RAISE EXCEPTION 'Página inválida: % desde %', p_limit, p_offset USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF to_regclass('public.pg_stat_statements') IS NULL THEN
    RETURN;
  END IF;
  RETURN QUERY EXECUTE
    'SELECT s.queryid, left(s.query, 2000), s.calls, s.total_exec_time, s.mean_exec_time, s.max_exec_time, s.rows,
            count(*) OVER (), sum(s.total_exec_time) OVER ()
       FROM public.pg_stat_statements s
      WHERE s.dbid = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database())
      ORDER BY CASE $1 WHEN ''total'' THEN s.total_exec_time WHEN ''mean'' THEN s.mean_exec_time
                       WHEN ''max'' THEN s.max_exec_time ELSE s.calls::double precision END DESC, s.queryid
      LIMIT $2 OFFSET $3'
    USING p_order, p_limit, p_offset;
END;
$function$;

COMMENT ON FUNCTION ops.top_statements(text, integer, integer) IS
  'Consultas que más consumen la base (pg_stat_statements, texto normalizado sin parámetros, solo esta base). '
  'SECURITY DEFINER: solo el rol de la plataforma la ejecuta (pantalla Rendimiento del ADMIN).';

-- Nadie la ejecuta salvo el rol de la plataforma (se le concede al aprovisionar los roles: app/core/db_roles.py).
REVOKE ALL ON FUNCTION ops.top_statements(text, integer, integer) FROM PUBLIC;
