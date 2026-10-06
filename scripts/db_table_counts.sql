-- Filas de cada tabla de la plataforma (sin particiones: la tabla madre las suma), una línea `esquema.tabla|filas`.
-- La usan scripts/db_restore_check.sh (lo restaurado) y scripts/db_bucket_restore_check.sh (el origen): la misma
-- consulta de los dos lados, para comparar tabla por tabla.
DO $$ BEGIN CREATE TEMP TABLE restore_counts (name text, n bigint); END $$;
DO $$
DECLARE r record; n bigint;
BEGIN
  FOR r IN SELECT format('%I.%I', ns.nspname, c.relname) AS name FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
           WHERE ns.nspname IN ('auth','tenancy','workforce','biometrics','attendance','catalog','ops','billing')
             AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1
  LOOP
    EXECUTE 'SELECT count(*) FROM ' || r.name INTO n;
    INSERT INTO restore_counts VALUES (r.name, n);
  END LOOP;
END $$;
SELECT name || '|' || n FROM restore_counts ORDER BY name;
