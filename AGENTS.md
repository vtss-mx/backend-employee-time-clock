# Backend · Employee Time Clock — reglas de arquitectura

Reglas obligatorias para cualquier cambio (personas o agentes). Si una petición choca con ellas,
se señala el conflicto antes de escribir código; no se "rodean".

Stack: Python 3.14 · FastAPI · SQLAlchemy 2 · PostgreSQL (Alembic) · pytest. Idioma del código:
identificadores en inglés; docstrings, comentarios, mensajes y documentación en **español**.

## 1. El backend es la única fuente de verdad

- **Permisos, menús y reglas de negocio viven aquí**, nunca en el frontend. El frontend solo dibuja
  lo que el backend le envía.
- **Pantallas por rol**: `catalog.screens` + `catalog.role_screens` (seed en
  `alembic/seed/catalogs.json`). `/api/users/me`, el login y la renovación devuelven
  `user.screens` y `user.home` (`app/services/navigation_service.py`).
  - Permiso (BD): qué pantallas tiene cada rol.
  - Disponibilidad (código, `AVAILABILITY` en navigation_service): cuáles aplican al estado del
    usuario (p. ej. registro facial pendiente o aprobado).
- **Autorización estricta: cada endpoint exige su pantalla Y su rol**:
  `dependencies=[Depends(require_screen(Screen.X, ...))]` (permiso configurable en la BD) más el
  guardia de rol o de alcance en el código (`AdminUser`, `CompanyScope`, `EmployeeUser`,
  `require_roles(...)`), que define además **de qué datos** se trata. Las dos capas siempre: si la
  BD se configura mal, el código sigue impidiendo que un rol use APIs de otro.
  - Excepciones explícitas y cerradas (listas `PUBLIC` y `ANY_SESSION` de
    `tests/test_authorization.py`): salud, iniciar/renovar/cerrar sesión, cuenta recordada, JWKS,
    la cuenta propia, catálogos, validación en vivo (cada campo exige su pantalla) y leer la política.
  - `tests/test_authorization.py` recorre TODAS las rutas (`API_ROUTERS` de `app/main.py`): falla si
    una ruta nueva no declara autorización, y llama cada ruta con cada rol sin permiso: siempre
    403 (401 sin sesión), nunca 200/404/422/500. Un router nuevo se agrega a `API_ROUTERS`.
- **API de integración** (`/integrations/v1`, `app/routers/integrations.py`): no usa sesiones ni
  pantallas; se autentica SOLO con la llave de la empresa (`ApiClientDep`, cabecera `X-API-Key`) y
  cada endpoint exige su permiso (`require_api_scope`, catálogo `api_scopes`). La empresa sale de la
  llave (`client.company_id`) y se consulta con los repositorios aislados por empresa; nunca datos
  biométricos. Un endpoint nuevo ahí lleva su permiso de lectura y su prueba de aislamiento.
- **Validación en vivo**: todo correo, teléfono o dato único de un formulario se valida mientras
  se escribe por el canal `/api/ws/validation` (respaldo `GET /api/validation`). Campos, permisos
  (por pantalla) y reglas viven en `app/services/live_validation.py`: un campo nuevo es una
  entrada ahí, nunca un endpoint de disponibilidad aparte.
- **Listas de valores en la BD** (esquema `catalog`): roles, estados, motivos, países, mensajes...
  Los `StrEnum` de `app/models/enums.py` solo nombran códigos que la lógica necesita y una prueba
  verifica que coincidan con las tablas (`tests/test_catalogs.py`).

### Agregar una pantalla
1. Registro en `screens` y su permiso en `role_screens` de `alembic/seed/catalogs.json`.
2. Migración nueva que lleve las bases existentes al mismo contenido (nunca editar una migración
   ya aplicada).
3. Valor en `Screen` (`enums.py`) y, si depende del estado del usuario, su regla en `AVAILABILITY`.
4. `require_screen` en los endpoints que la usan.
5. Frontend: su vista en `src/routes/screens.tsx` (lo valida su prueba de contrato).

## 2. Capas (de afuera hacia adentro; nunca al revés)

| Capa | Carpeta | Responsabilidad |
|---|---|---|
| Rutas | `app/routers/` | HTTP: validar entrada (schemas), permisos (dependencias), llamar a UN servicio, responder con `ok(...)`. Sin SQL ni reglas de negocio. |
| Servicios | `app/services/` | Reglas de negocio y transacciones (`commit`). Lanzan excepciones de `app/core/exceptions.py`. |
| Repositorios | `app/repositories/` | Consultas SQLAlchemy. Sin reglas de negocio ni `commit`. |
| Modelos | `app/models/` | ORM. Deben coincidir con las migraciones. |
| Esquemas | `app/schemas/` | Pydantic: contrato de la API (entrada y salida). |
| Núcleo | `app/core/` | Configuración, BD, seguridad, respuestas, errores, utilidades transversales. |

- **Multiempresa**: el alcance sale SIEMPRE del usuario autenticado (`CompanyScope`,
  `MemberCompany`) y cada repositorio/servicio se construye con él. Un id de otra empresa
  responde 404. Jamás se acepta `company_id` desde el cliente.
- **Contrato de respuesta único, también en errores y en TODAS las capas**: éxito con
  `ok(data, mensaje, code=...)` y `response_model=ApiResponse[...]`; error lanzando una excepción de
  `app/core/exceptions.py` (o, en un middleware, con `error_response`) con `code` estable
  (`COMPANY_HAS_EMPLOYEES`, `EMAIL_TAKEN`...). Nunca un `JSONResponse`/`HTTPException` con otra
  forma. `tests/test_envelope.py` lo verifica para 404, 405, 401, 403, 409, 413, 422, 429, 500 y 503.
  Un error que debe atravesar la lectura del cuerpo de FastAPI (formulario/JSON) es un
  `HTTPException` de FastAPI (`BodyTooLargeError`): cualquier otra excepción ahí se vuelve un 400
  genérico. Única excepción documentada: el preflight de CORS rechazado (texto plano; el navegador
  no lo expone a JavaScript).
- **Listados paginados**: todo endpoint que lista recibe `page: Pagination` (`app/dependencies.py`:
  `size` por omisión `PAGE_SIZE_DEFAULT` = 10, máximo `PAGE_SIZE_MAX` = 50), el repositorio usa
  `paginate()` (`aggregates.py`) y la respuesta es un esquema que hereda de `Page[T]`
  (`app/schemas/common.py`: `items`, `total`, `page`, `size`). Nunca listas sin límite.
- **Sin duplicación**: antes de escribir una función, buscar si ya existe (servicios, repositorios,
  `app/core`, `aggregates.py`). Lógica repetida en dos sitios se extrae.

## 3. Base de datos

- Todo cambio de estructura o de datos de catálogo = **migración Alembic nueva** en
  `alembic/versions/` (numeración consecutiva, docstring en español que explique el porqué).
- Esquemas por dominio (`app/core/db_schemas.py`): auth, tenancy, workforce, biometrics,
  attendance, catalog.
- Llaves foráneas con `ondelete` explícito; índices para cada consulta frecuente.
- Borrados irreversibles solo con regla de negocio explícita (p. ej. eliminar empresa solo sin
  empleados; si no, desactivar).
- **SQL solo en repositorios** (los servicios no usan `select`/`update`/`delete`): así cada consulta
  tiene un solo lugar donde medirla e indexarla.
- **Nada de depuraciones dentro de una petición.** Lo que vence (sesiones, retos, QR, huellas,
  contadores...) lo borra `maintenance_service` (lista `PURGES`, por lotes, con candado de
  PostgreSQL para que trabaje una instancia a la vez). Una tabla nueva con vencimiento agrega ahí su
  `Purge`; nunca un `DELETE` masivo en el login o al emitir algo.
- **Presupuesto de consultas.** Autenticar una petición son 2 consultas (`_AUTH_LOAD` en
  `session_repository`); los listados cargan relaciones por lotes (`emails_by_ids`, `by_ids`,
  `selectinload`), nunca una consulta por elemento. `tests/test_performance.py` lo vigila.
- **Tablas que crecen sin límite** (bitácora de identificaciones, jornadas, registros de asistencia,
  métricas faciales): conteos con tope (`count_cap` de `paginate`, también el historial de jornadas),
  el orden del listado lo entrega un índice (nunca se ordena la historia en memoria) y, para
  sincronizar, cursor por `id` en lugar de `OFFSET` (`/integrations/v1/attendance/feed`).
- Una restricción única que choca entre dos peticiones simultáneas responde 409
  `CONCURRENT_UPDATE` (manejador global); no hace falta capturarla en cada servicio, salvo para
  dar un código propio (p. ej. `EMAIL_TAKEN`).
- UPDATE/DELETE masivos con `affected_rows` (`aggregates.py`): una sentencia para toda la empresa
  (p. ej. nueva verificación a todos), nunca un ciclo por fila.
- Cerrar sesiones solo con `SessionService` (`revoke_all(..., commit=False)` dentro de la
  transacción del cambio que las invalida; `close_company` para toda una empresa).

### 3.1 Lista de verificación de base de datos (OBLIGATORIA en cada consulta nueva o cambiada)

Se recorre completa en cada petición que toque un repositorio, un modelo o una migración, y el reporte
final dice qué se hizo en cada punto (o por qué no aplica). Origen: la auditoría de rendimiento
(migración `0047`, cifras en el README "Índices de base de datos").

1. **Cada consulta tiene su índice** y su comentario en el modelo dice qué consulta sirve. Orden de las
   columnas: primero las de igualdad (`company_id` primero en toda tabla multiempresa), luego la de
   rango y al final las del `ORDER BY` del listado, **en el mismo orden y dirección**, incluido el
   desempate (`id`). Si el `ORDER BY` no coincide con un índice, PostgreSQL ordena todo el resultado
   (o, con `LIMIT`, recorre la llave primaria descartando millones de filas: así tardaba 467 ms la
   bitácora de la API de integración por periodo).
2. **La empresa va en CADA tabla de un JOIN o subconsulta correlacionada** (`Employee.company_id ==`,
   `Shift.company_id ==`, `WorkSession.company_id ==` dentro del `EXISTS`...): PostgreSQL no deduce la
   igualdad a través del JOIN y, sin ella, recorre las filas de TODAS las empresas.
3. **Sin JOIN ni carga que nadie lee**: un conteo no se une con `users` si ningún filtro lo necesita;
   en una consulta con `LIMIT` sobre muchas filas, las relaciones `lazy="joined"` que la pantalla no
   usa se apagan (`lazyload(...)`, como `ShiftRepository.assigned_on`); una relación que solo usa una
   cuenta (p. ej. `User.validator`) es `lazy="select"`, nunca `selectin` (una consulta extra en cada
   listado).
4. **Nunca se cargan filas para contarlas o agruparlas en Python**: un `GROUP BY` / `count()` en la
   base (`AttendanceRepository.day_counts`: los conteos del tablero de una empresa de 20 000
   empleados pasaron de 256 ms a 3 ms).
5. **Rangos abiertos se acotan** con una cota que implica una restricción de la tabla (sin cambiar el
   resultado): `absence_touches` acota `starts_on` porque una ausencia dura a lo más
   `ABSENCE_MAX_DAYS` (CHECK `dates`).
6. **Índice parcial** para el subconjunto caliente (`status = 'OPEN'`, `IS NOT NULL` de una FK
   opcional, `WHERE learned`), con `postgresql_where` y el mismo `sqlite_where`. La consulta debe
   implicar el predicado (con parámetros funciona: PostgreSQL planea cada sentencia con sus valores).
7. **Súper-índice (INCLUDE)** cuando un *index-only scan* quita las lecturas de la tabla en una ruta
   caliente o en un conteo grande (`postgresql_include=[...]`; SQLite lo ignora). Nunca se incluye una
   columna que se actualiza seguido: cada cambio dejaría de ser HOT y reescribiría todos los índices.
   Ojo: el *index-only scan* solo se salta la tabla en páginas ya "visibles para todos"; las filas de
   los últimos minutos todavía se leen de la tabla. Si la consulta filtra lo reciente, la columna del
   filtro va en la CLAVE del índice, antes del rango (`ix_face_attempt_metrics_company_reason`:
   `company_id, reason, created_at`), no en el INCLUDE.
8. **Llaves foráneas**: índice cuyo prefijo sean las columnas de la FK en tablas grandes con
   `CASCADE`/`SET NULL`/`RESTRICT` (si no, borrar el padre recorre la tabla hija). Una FK opcional va
   con índice parcial `IS NOT NULL`. Una FK a un catálogo que nunca se borra va **sin** índice (cuesta
   en cada inserción y ninguna consulta lo usa; migraciones `0031` y `0047`).
9. **Sin índices redundantes**: ninguno que sea prefijo de otro, igual a una restricción única, de
   baja cardinalidad sin consulta que lo use, ni "por si acaso". Cada índice de más cuesta en cada
   INSERT/UPDATE de la tabla.
10. **BRIN** solo en una tabla de solo inserción, sin depuración y con consultas por rango de tiempo
    de toda la tabla. Hoy ninguna lo cumple (las consultas filtran por empresa, empleado o cuenta, y
    las tablas con depuración reutilizan páginas, lo que arruina la correlación): por eso no hay BRIN.
11. **Listados**: `paginate` con `count_cap` si la tabla crece sin fin; presupuesto en
    `tests/test_performance.py` (`BUDGETS` + `assert_flat`: la página de 50 cuesta lo mismo que la de
    1). **Operaciones masivas**: una sentencia (`affected_rows`) o una inserción (`insert_many`), con
    un número fijo de consultas para 1 o para 500. **Vencimientos**: su `Purge` (la depuración repite
    la condición en el `DELETE` para ir por su índice).
12. **Medir con volumen en PostgreSQL**: toda consulta nueva o cambiada en una ruta caliente o sobre
    una tabla que crece agrega su caso a `perf/db/explain.py` y se mide con `perf/db/run.sh` (base
    AISLADA de 16, ≈100 k empleados, 5 M de bitácora; resultado en `perf/results/db-plans.txt`). No
    se acepta un `Seq Scan` de una tabla grande, un orden en disco ni miles de filas descartadas por
    filtro en una ruta caliente sin justificarlo en el reporte. `PERF_DB_KEEP=1` deja la base para
    medir antes y después de una migración. Los recorridos que quedan hoy y su motivo (galería facial
    sin `company_id`, búsquedas de 1-2 letras, depuración atrasada) están en el README, "Pendiente de
    decisión del dueño del producto".
13. **Migraciones de índices** sobre tablas grandes: `CREATE/DROP INDEX CONCURRENTLY` dentro de
    `op.get_context().autocommit_block()`; un índice que cambia de definición se construye con nombre
    temporal, se borra el anterior y se renombra (la consulta nunca se queda sin índice); cada paso
    con `IF EXISTS` (repetible tras una falla a la mitad) y un `downgrade` que deja exactamente lo
    anterior (patrón de `0047_performance_audit.py`). El modelo dice lo mismo que la migración
    (`./scripts/quality.sh --postgres` compara ambas bases).

## 4. Rendimiento y escalabilidad

- **Sin estado que impida varias instancias**: lo compartido vive en PostgreSQL. Una caché local
  (catálogos, política, galería facial) se valida contra la BD (TTL corto o huella) y se acota
  (LRU por memoria o cantidad).
- **Control de admisión adaptativo** (`app/core/admission.py`): el límite de peticiones a la vez se
  ajusta solo a la latencia real; la fila atiende primero lo crítico (iniciar sesión, identificar,
  verificar, QR) y, dentro de cada nivel, lo más demandado; al saturarse se descarta primero lo
  menos importante con 503 `SERVER_BUSY` reintentable. Una ruta crítica nueva se agrega a
  `CRITICAL_PREFIXES`; un tablero o una estadística pesada, a `BACKGROUND_PREFIXES` (hoy: estadísticas
  y seguridad facial del ADMIN, el tablero de asistencia y las fallas del navegador). Solo las respuestas exitosas
  miden la capacidad, la latencia típica se aprende solo con holgura, ningún grupo ocupa más de la
  mitad de la fila y la memoria está acotada (grupos inactivos se olvidan; los que no caben
  comparten uno por nivel).
- **Presupuesto de consultas** (ver §3) vigilado por `tests/test_performance.py`; operaciones
  masivas en una sentencia (`affected_rows`), nunca un ciclo por fila.
- **Trabajo pesado fuera de la petición**: depuraciones en `maintenance_service`, guardado de errores
  en lotes (`error_reporter`), cola acotada del motor facial.
- **Pruebas de carga** (`perf/run.sh`, k6 en un entorno aislado con base en memoria): suben por
  etapas hasta 1 000 000 peticiones por segundo y reportan lo logrado, los 503 controlados, los
  errores no controlados (deben ser 0) y la latencia. Un cambio en una ruta caliente o en la
  admisión se mide antes y después.
- **Planes con volumen** (`perf/db/run.sh`, ver §3.1 punto 12): el costo real de cada consulta en
  PostgreSQL con cientos de miles de empleados y millones de registros. La suite usa SQLite y pocos
  datos: un N+1 lo detecta `test_performance.py`, pero un índice que falta o un orden en memoria solo
  se ve con volumen.
- **Cachés por proceso, siempre acotadas y validadas**: política (LRU de 10 000 empresas, 5 s),
  catálogos (`CATALOG_CACHE_SECONDS`), umbrales faciales (60 s), galería facial (LRU por memoria y
  huella contra la BD en cada uso) y bloqueos de migración facial (tope de 10 000). Una caché nueva
  declara su tope y cómo se invalida.

## 5. Resiliencia y tolerancia a fallas

- **Toda espera tiene tiempo límite** (BD: `statement_timeout`, pool, conexión; colas; descargas;
  WebSocket) y toda falla de una dependencia responde con su código (`DATABASE_UNAVAILABLE`,
  `DATABASE_TIMEOUT`, `FACE_SERVICE_UNAVAILABLE`...), nunca un 500 opaco ni un servicio caído.
- **Degradar en vez de caer**: un dato cifrado ilegible se omite y se registra (`try_decrypt`,
  `readable_embedding`); los catálogos sirven lo anterior si la BD parpadea; lo accesorio (recordar
  la cuenta, aprender del rostro) es de mejor esfuerzo y nunca hace fallar lo principal; cada tarea
  del mantenimiento falla sola.
- **Idempotencia y carreras**: los reintentos de la red no duplican ni fallan (asignar, nombrar,
  renovar la sesión con el mismo siguiente token); "consultar y luego actuar" se reemplaza por una
  sentencia atómica (`ON CONFLICT ... RETURNING`, `UPDATE ... WHERE`) o un candado de fila
  (`FOR UPDATE OF`). Un choque de unicidad o de transacciones responde 409 reintentable.
- **Hilos en segundo plano que nunca mueren**: capturan su excepción, la registran y siguen.
- `tests/test_fault_tolerance.py`: cada falla nueva que se tolera lleva su prueba que la provoca.

## 6. Errores del sistema (registro y seguimiento)

- **Solo las fallas se registran** en `ops.error_reports` (agrupadas por huella con su contador) y el
  ADMIN les da seguimiento en "Errores del sistema" (pendiente, en proceso, en revisión, solucionado;
  una solucionada que vuelve a ocurrir se reabre sola). Decisión del dueño del producto: señal, no
  ruido; la bandeja muestra lo que alguien tiene que corregir.
  - excepciones no controladas (500, CRITICAL): con su stack trace;
  - fallas controladas del servidor o de una dependencia (5xx, ERROR: `DATABASE_UNAVAILABLE`,
    `DATABASE_TIMEOUT`, `FACE_SERVICE_UNAVAILABLE`, `SERVER_BUSY`...): las anota `error_response` y
    las reporta el middleware del traceId;
  - cualquier `logger.error` / `logger.exception` (segundo plano, procesos internos): `ErrorLogHandler`;
  - fallas del canal WebSocket del lado del servidor (BD caída, saturación, excepción);
  - fallas de la app web (origen `CLIENT`, `POST /api/client-errors`, ver abajo).
- **Un 4xx NO se registra** (validación, permisos, reglas de negocio, 404, 401, 409, 429...): es un
  resultado normal que se responde con su código estable. Queda una línea INFO en el log del proceso
  (`Respuesta 403 FORBIDDEN en GET /api/... [traceId]`; en el canal: `Canal de validación: 400
  BAD_MESSAGE [traceId]`) y no se arma su contexto (lo costoso). Los intentos faciales sospechosos
  siguen en `verification_logs`. La regla vive en UN lugar que ningún productor puede rodear:
  `error_reporter.report` descarta lo que no es CRITICAL ni ERROR (`is_recorded` en
  `app/core/error_events.py`); el middleware además corta antes de copiar el contexto de un 4xx. Las
  filas WARNING que ya existían quedan como historial (el catálogo `error_severities` se conserva).
- **Fallas de la app web** (`POST /api/client-errors`, `app/routers/client_errors.py`): pública a
  propósito (una pantalla puede romperse en el login; está en la lista `PUBLIC` de
  `tests/test_authorization.py`). Si trae un Bearer válido anota quién y su empresa
  (`SessionService.authenticate_access`); uno inválido o la BD caída nunca hacen fallar el reporte.
  Acotada: límite por IP (`RATE_LIMIT_CLIENT_ERRORS_PER_MINUTE`, 429) antes de leer el cuerpo, cuerpo
  JSON ≤ 16 KB (413) y campos estrictos (`ClientErrorIn`: `kind` CRASH/UNHANDLED/CONFIG, `message`,
  `stack`, `path` sin query, `component`, `detail`, `app_version`; 422). CRASH y UNHANDLED son
  CRITICAL; CONFIG, ERROR. La ubicación es la plantilla de la pantalla (`route_of`: ids → `{id}`) y
  el tipo de excepción sale del prefijo del mensaje (`TypeError: ...`). Responde 202: se guarda con el
  siguiente lote. En la admisión va en `BACKGROUND_PREFIXES` (se descarta primero al saturarse).
- Nunca se atrapa un error para silenciarlo: o se responde con su código (y, si es una falla, queda
  registrado) o se registra con `logger.exception` si es de mejor esfuerzo. El registro jamás frena
  una petición (cola en memoria acotada, guardado en lotes por un hilo, cada error en su transacción)
  ni la rompe (si registrar falla, la respuesta sale igual).
- **Contexto literal, salvo secretos y archivos** (decisión del dueño del producto: para reproducir
  un error hace falta todo). Cada ocurrencia guarda quién (correo y rol), empresa, método, ruta,
  query, IP, cabeceras de la lista blanca, cuerpo enviado, estado y cuerpo de la respuesta y
  duración (`app/core/error_context.py`, copiado mientras viaja en `RequestIdMiddleware`).
  - Un campo secreto nuevo (contraseña, token, llave, firma...) debe coincidir con `_SECRET` o se
    agrega ahí: se guarda como `[oculto]` en el cuerpo, la query y cualquier nivel del JSON. En el
    texto libre que manda el navegador (mensaje, stack trace, componente) `redact_text` oculta el
    valor que sigue a un nombre de secreto, un `Bearer ...` y un JWT.
  - `Authorization` y `Cookie` nunca se copian (lista blanca de cabeceras).
  - De un archivo (multipart) solo queda nombre, tipo y tamaño.
  - El motor de BD sigue con `hide_parameters`.
- **Filas acotadas**: la ubicación es la plantilla de la ruta (`location_of`); una falla antes de
  enrutar (saturación) usa la URL sin ids (`route_of`). Lo que no es una falla (un 4xx, el resultado
  de una validación en vivo) no se reporta.
- El detalle interno (errores de componentes, cola del motor, capacidad adaptativa) lo ve solo el
  ADMIN (`GET /api/admin/errors/server`); las sondas públicas solo dicen si está listo.
- **Marcar en bloque** (`POST /api/admin/errors/resolve`): solo con un estado o una gravedad
  específicos (nunca toda la bandeja, 422 `ERROR_FILTER_REQUIRED`), con las mismas condiciones del
  listado (`_filters` del repositorio, una sola fuente) y solo hasta `seen_until` (el `as_of` de la
  lista): lo que volvió a ocurrir después de que el ADMIN lo vio sigue abierto. Una sentencia.

## 7. Calidad (obligatoria antes de dar algo por terminado)

- `ruff check` + `ruff format` (línea 120, complejidad ≤ 12), `mypy` estricto en `app/`.
- **Pruebas** en `tests/` (SQLite con FKs activas; `TEST_DATABASE_URL` para PostgreSQL real).
  **Cobertura del 100 %** (líneas y ramas) cubriendo todos los escenarios: éxito, cada error de
  negocio, permiso denegado (403/404/409), estados vacíos, límites (paginación, tamaños, tiempos) y
  fallas de dependencias (BD, motor facial, cifrado). El código que no se puede ejecutar se elimina;
  no se excluye con `# pragma: no cover`.
- Tipos de prueba según el cambio: unitarias (reglas puras), integración por la API (TestClient),
  autorización (`test_authorization.py`), contrato (`test_envelope.py`), tolerancia a fallas
  (`test_fault_tolerance.py`), rendimiento (`test_performance.py` y `perf/run.sh`).
- Calidad global de ambos proyectos (duplicación, librerías repetidas, dependencias circulares,
  vulnerabilidades, migraciones = modelos, cobertura): `../scripts/quality.sh --postgres`.
- Sin formateadores ajenos al proyecto; se respeta el estilo existente (`ruff format <archivos>` es el
  único formateador).

### 7.1 Comandos de cierre (cada cambio, sin excepción; el reporte pega su resultado)

La lista completa (backend + frontend + documentación) está en el `AGENTS.md` de la raíz, §2.1. Del
backend, desde `backend-employee-time-clock/`:

```bash
# 1. Lint, formato, tipos, TODAS las pruebas con cobertura 100 % (incluye test_authorization,
#    test_envelope, test_fault_tolerance y los presupuestos de test_performance)
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e COVERAGE_FILE=/tmp/cov -v "$PWD:/app" time-clock-backend-dev \
  sh -c "ruff check . && ruff format --check . && mypy app && python -m pytest -q -p no:cacheprovider --cov=app --cov-branch --cov-report=term-missing:skip-covered"
# 2. PostgreSQL real + migraciones = modelos + duplicación/ciclos/vulnerabilidades (≈10 min)
../scripts/quality.sh --only=backend --postgres
# 3. Si tocó consultas, índices o una ruta caliente: planes con volumen (§3.1)
perf/db/run.sh            # o solo los casos afectados: perf/db/run.sh attendance
# 4. Si tocó una ruta caliente o la admisión: carga antes y después (entorno aislado)
perf/run.sh 1000 5000
```

## 8. Seguridad

- Contraseñas con hash; tokens ES256; sesiones validadas en cada petición.
- Secretos solo en `.env` (nunca en el código ni en el repositorio).
- Datos biométricos cifrados. El ADMIN de la plataforma solo consulta la **ficha de trabajo** de los
  empleados, de solo lectura (`CompanyEmployeeRead`: número, nombre, departamento, correo, teléfono,
  estado, estado facial y cuánto aprendió el reconocimiento: solo cuántas muestras y cuándo). Nunca
  RFC, CURP, NSS, fecha de nacimiento, fotos ni plantillas.
- **La política de verificación de cada empresa la configura el ADMIN**
  (`/admin/companies/{id}/verification-policy`). La empresa, sus empleados y sus validadores solo la
  leen (`GET /settings/verification`, sin quién la cambió). Una regla nueva de la política se agrega
  ahí (columna, esquema, servicio) y en la pantalla del ADMIN, nunca en una pantalla de la empresa.
- **Módulos por empresa que concede el ADMIN**: hoy Integraciones (`companies.api_enabled`). Se
  aplican en tres lugares que no se separan: disponibilidad de la pantalla
  (`navigation_service.AVAILABILITY`), la dependencia del router (`require_api_module`) y la
  autenticación de la llave (`api_key_service.authenticate`), todos con 403 `API_ACCESS_DISABLED`.
- **QR dinámicos de un solo uso** (`qr_service`): un QR solo se consume con `QrService.use` /
  `complete_hold` (sentencia atómica `WHERE used_at IS NULL`); uno usado, vencido o reemplazado no
  vuelve a servir. La BD guarda solo el hash del token (nunca el token ni una copia cifrada).
- **Hora del negocio**: "hoy" y los conteos diarios con `app/core/clock.py` (`business_now`,
  `business_today`, `business_day_start`; `APP_TIMEZONE` = hora del Centro), nunca la del servidor.
- **Toda captura facial** (registro, verificación, identificación) se arma con las piezas de
  `identity_core`: `take_challenge` (cámara real y reto, antes de analizar), `confirm_live` (prueba de
  vida, suplantación y toma única) y `match_one` (1:1); las referencias del empleado salen de
  `FaceService.references_for`. Pasa así por los candados de
  `capture_guard` (cámara real, tiempo humano, toma única, sin fotos fijas ni reenvíos), por
  `check_liveness` (destello y movimientos en orden, anti-spoofing también en cada paso) y por
  `attempt_guard` (bloqueo). Un intento sospechoso es `SuspiciousCapture`: se registra en la bitácora
  con su motivo antes de responder. Cada candado se activa por empresa en `verification_policy`; uno
  nuevo lleva su interruptor, su motivo en `verification_reasons` y su mensaje en `face_errors`.
  - La respuesta al reto llega como `LivenessResponse` (dependencia `Liveness`: `challenge_id`,
    `challenge_image` por movimiento y `flash_image` por color); ningún router lee esos campos a mano.
  - **Movimientos** (`LivenessAction`: girar, mirar arriba/abajo, acercarse; 1 a 3, sin repetir
    seguidos) medidos contra las frontales de la misma toma (`pose.step_measure`, `StepTarget`). Uno
    nuevo va al `StrEnum`, a `catalog.liveness_actions` (instrucción) y a `step_measure`.
  - **Destello de colores** (`photometry`, `check_flash`): siempre se mide; solo decide con
    `flash_liveness = ENFORCE`. En `OBSERVE` nada del destello bloquea (ni una captura ilegible ni una
    falla del motor: se registra y el intento sigue). Decisión del dueño: no se exige hasta calibrarlo
    con capturas reales.
- **Seguridad que se mejora sola** (`face_security`, decisión del dueño: siempre activa, tenga o no la
  empresa el aprendizaje de su galería):
  - Cada intento facial deja sus números en `ops.face_attempt_metrics` (`face_signals`: un recolector
    por petición en una `ContextVar`; lo empieza `take_challenge` y lo guarda `IdentityLog.record` en la
    misma transacción). Solo números: nunca imágenes, plantillas ni quién era.
  - La autocalibración (mantenimiento, `recalibrate_if_due`) **solo endurece**: percentil de las
    personas reales × margen, nunca bajo el piso de la configuración ni sobre su tope. Nada
    automático relaja un umbral ni el nivel de una empresa. Los umbrales vigentes (`thresholds()`,
    caché de 60 s validada contra la BD) se aplican en el servidor y se envían en el reto.
  - Refuerzo ante ataques (`under_attack`): retos con el máximo de movimientos para la empresa atacada.
  - Una señal nueva se agrega a `SIGNALS` con su columna, piso y tope (y su prueba de "solo endurece").
- **Reconocimiento evolutivo** (`face_learning`): toda identificación facial exitosa (1:1, en persona,
  QR + rostro, 1:N) llama a `FaceLearning.reinforce` ANTES de registrar el intento (misma
  transacción). Las muestras del registro aprobado (`learned = False`) son el ancla y nunca se
  reemplazan; una aprendida solo entra con prueba de vida, holgura sobre el nivel de la empresa,
  parecido propio al ancla, novedad y ritmo (reglas en el módulo), y compite por su lugar por
  utilidad (`matches`, `last_matched_at`). El mantenimiento retira la que dejó de servir. Nada
  automático baja el nivel de confianza de la empresa. La galería en memoria descifra solo lo nuevo.
  **Todo el aprendizaje lo administra el ADMIN** (decisión del dueño del producto): su interruptor en
  la política, su evolución (`GET /admin/companies/{id}/face-learning`) y olvidar lo aprendido de un
  empleado (`DELETE /admin/companies/{id}/employees/{employee_id}/face/learned`, en `CompanyService`).
  Ninguna API ni pantalla de la empresa lo muestra.
- **Turnos y asistencia por turno** (`shift_service`, `site_service`, `shift_request_service`,
  `attendance_service`, `attendance_overview`, reglas puras en `shift_rules`):
  - **La hora es la del servidor**: siempre `attendance_service.now_utc()` (las pruebas la fijan ahí),
    nunca un dato del cliente ni `datetime.now()` suelto. Lo programado se guarda en UTC.
  - **Qué se puede registrar lo decide el servidor** (`_State.actions`); el cliente solo muestra.
  - Cada jornada guarda una **copia de lo programado** al entrar (turno, horas, descansos,
    tolerancias): editar un turno o cambiar la asignación nunca altera lo ya registrado.
  - Un cambio de turno es una asignación nueva desde `today + ASSIGNMENT_NOTICE_DAYS` (la vigente
    termina el día anterior, `close_before`); la primera puede empezar hoy.
  - Todo registro pasa por `identity_core` (rostro) y por `_place` (geocerca o día remoto, precisión
    de la política, viaje imposible). Lo que se registra va en la misma transacción, con el empleado
    bloqueado (`FOR UPDATE OF employees`) y los índices únicos parciales (una jornada abierta, un
    descanso abierto, una solicitud pendiente) como última barrera.
  - Las jornadas vencidas sin salida las cierra el mantenimiento (`close_missed_checkouts`) o el
    siguiente registro del empleado (bajo su candado); nunca una consulta de lectura.
  - Estados que ve la pantalla = catálogos (`board_states`, `assignment_states`,
    `work_session_statuses`, `shift_request_statuses`, `day_off_types`): uno nuevo va al seed, al
    `StrEnum` (si la lógica lo nombra) y a la prueba de catálogos.
  - **Solo dentro de sus turnos** (decisión del dueño del producto; `_State` en `attendance_service`):
    la entrada en la ventana de la jornada y antes de la salida programada; la salida mientras la
    jornada esté abierta y hasta su límite; el **descanso es libre dentro del horario**: cuando el
    empleado quiera, pero solo entre la entrada y la salida programadas (`shift_rules.in_working_hours`)
    y mientras le queden; terminarlo siempre se puede. Un descanso abierto al salir termina al checar
    la salida (o en el límite si no la checa): el tiempo en descanso nunca cuenta como trabajado. Cada
    rechazo dice por qué (`_State.denial`) y `today.break_window` le da a la app la ventana (nunca la
    calcula el teléfono).
  - **Calendario de días libres** (`calendar_service`, `absence_service`, reglas puras en
    `calendar_rules`): festivos de la empresa (los oficiales de la LFT con `official_holidays`,
    idempotente), ausencias (catálogo `day_off_types`; estados de `shift_request_statuses`: la empresa
    las registra aprobadas para uno o varios, el empleado pide las `requestable` y la empresa decide) y
    días laborables especiales. Un día es libre si es festivo o lo cubre una ausencia APROBADA, salvo
    que sea laborable para esa persona (`day_off_on`); cuenta el día de la jornada (un nocturno es del
    día en que entra). En un día libre no se checa la entrada (409 `DAY_OFF` con el motivo, también en
    el validador), la siguiente jornada se salta los días libres y el tablero lo muestra como
    `DAY_OFF` (no falta). `CalendarService.days_off` arma los días libres de muchos empleados en un
    rango con tres consultas; el conteo del tablero es una consulta (`assigned_off_on`). Dos ausencias
    activas del mismo empleado no se enciman (revisado con el empleado bloqueado).
  - **Operaciones para varios empleados** (asignar un turno, vacaciones colectivas; `schemas/bulk.py`):
    hasta `BULK_MAX` (500) ids sin repetir, una transacción, empleados bloqueados en orden de id
    (`EmployeeRepository.lock_many`), lecturas por lotes y una inserción (`aggregates.insert_many`): un
    número fijo de consultas (lo vigila `test_performance.py`). Lo igual para todos rechaza la
    petición; lo de cada empleado lo omite con su código (`BulkOutcome`: DONE / UNCHANGED / SKIPPED).
    Repetir la operación no duplica (UNCHANGED). Un id de otra empresa responde 404 sin hacer nada. Las
    reglas de una asignación viven en un solo lugar (`shift_service._plan`), igual para uno o varios.
  - **La empresa registra o corrige una jornada** (`attendance_manual`, SOLO COMPANY): sin rostro ni
    ubicación y con motivo obligatorio; quién, cuándo y por qué en la jornada (`edited_by_id`,
    `edited_at`, `edit_reason`) y un registro por hora declarada en la bitácora con la modalidad
    `COMPANY` y el motivo (lo anterior se conserva). Mismas reglas que en vivo (`attendance_rules`) y
    mismos cálculos (`new_session`, `end_break`, `finish`), con el empleado bloqueado; un día libre se
    marca primero como laborable.
- Toda autenticación por token (HTTP y canal WebSocket) usa `SessionService.authenticate_access`:
  las mismas reglas en ambos canales.
- Reglas del inicio de sesión por cuenta (dispositivo permitido, ubicación del validador) se aplican
  en `POST /auth/login` antes de crear la sesión (`ensure_device_allowed`, `ensure_device_authorized`,
  `ensure_location_allowed`).
  Cambiar una regla que la sesión abierta ya no cumple cierra sus sesiones con su motivo de catálogo.

## 9. Documentación

- Docstring en cada módulo, clase y función pública explicando el **porqué**.
- Cambios de API o de arquitectura se reflejan en el `README.md` de la raíz (tabla de endpoints).
