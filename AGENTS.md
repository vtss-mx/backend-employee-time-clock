# Backend · Employee Time Clock — reglas de arquitectura

Reglas obligatorias para cualquier cambio (personas o agentes). Si una petición choca con ellas,
se señala el conflicto antes de escribir código; no se "rodean".

Stack: Python 3.14 · FastAPI · SQLAlchemy 2 · PostgreSQL (Alembic) · pytest. Idioma del código:
identificadores en inglés; docstrings, comentarios y documentación en **español**. Lo que lee una persona (mensajes y
textos de los catálogos) existe en **es-MX y en-US** y se responde en el idioma de la petición (§11).

## 1. El backend es la única fuente de verdad

- **Permisos, menús y reglas de negocio viven aquí**, nunca en el frontend. El frontend solo dibuja
  lo que el backend le envía.
- **Pantallas por rol**: `catalog.screens` + `catalog.role_screens` (seed en
  `alembic/seed/catalogs.json`). `/api/users/me`, el login y la renovación devuelven
  `user.screens` y `user.home` (`app/services/navigation_service.py`).
  - Permiso (BD): qué pantallas tiene cada rol.
  - Disponibilidad (código, `AVAILABILITY` en navigation_service): cuáles aplican al estado del
    usuario (p. ej. registro facial pendiente o aprobado).
  - **Sin identidad aprobada, solo identidad y cuenta** (decisión del dueño del producto): un empleado
    cuyo registro facial no está aprobado solo tiene "Registrar rostro" / "En validación" y su
    cuenta (perfil, elegir empresa). Las pantallas que exigen la identidad aprobada son
    `IDENTITY_SCREENS` (verificarse, asistencia, QR) y `require_screen` cierra con 403
    `FACE_NOT_APPROVED` todo endpoint que solo sirve a ellas (`tests/test_identity_gate.py` recorre
    todas las rutas): una API nueva de esas pantallas queda cerrada sin escribir nada más.
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
1. Registro en `screens` y su permiso en `role_screens` de `alembic/seed/catalogs.json`, y su nombre y descripción en
   inglés en `alembic/seed/catalogs.en-US.json` (§11.3).
2. Migración nueva que lleve las bases existentes al mismo contenido, también su fila en `catalog.translations` (nunca
   editar una migración ya aplicada).
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

- **Multiempresa: aislamiento total en cuatro capas, todas obligatorias** (regla 14 de la raíz; detalle en §3.2):
  1. **Código**: el alcance sale SIEMPRE de la sesión o de la llave (`CompanyScope`, `MemberCompany`,
     `ApiClientDep`); cada repositorio/servicio de datos de empresa se construye con él y filtra por
     `company_id` (también en cada tabla de un JOIN, §3.1.2). Un id de otra empresa responde 404 (también dentro
     del cuerpo); un candado `FOR UPDATE` lleva la empresa en el WHERE (`aggregates.get_scoped`). Jamás se
     acepta `company_id` desde el cliente.
  2. **Estructura**: toda tabla de empresa tiene `company_id NOT NULL` y sus FK hacia otras tablas de empresa
     son COMPUESTAS `(x_id, company_id)` (`models.mixins.company_fk`).
  3. **Seguridad por fila** (PostgreSQL): cada transacción declara su empresa (`app/core/row_security.py`);
     sin ella las tablas de empresa no devuelven ni aceptan nada.
  4. **Pruebas**: `tests/test_tenant_isolation.py` (cada ruta con id, cada listado, la API de integración, el
     validador, la base) y `tests/test_row_security.py` (clasificación de tablas, FK compuestas).
- **Contrato de respuesta único, también en errores y en TODAS las capas**: éxito con
  `ok(data, code=..., params=...)` y `response_model=ApiResponse[...]`; error lanzando una excepción de
  `app/core/exceptions.py` (o, en un middleware, con `error_response(..., t("LLAVE"))`) con `code` estable
  (`COMPANY_HAS_EMPLOYEES`, `EMAIL_TAKEN`...). El texto NUNCA se escribe ahí: sale del catálogo de mensajes en el idioma
  de la petición (§11). Nunca un `JSONResponse`/`HTTPException` con otra
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
  attendance, catalog, ops (errores, consumo y almacenamiento) y billing (cobranza).
- Llaves foráneas con `ondelete` explícito; índices para cada consulta frecuente.
- Borrados irreversibles solo con regla de negocio explícita (p. ej. eliminar empresa solo sin
  empleados; si no, desactivar). **Lo que una persona elimina es un borrado lógico** (regla 20 de la raíz, §3.5):
  ningún servicio vuelve a borrar filas de esas tablas; solo la depuración de lo vencido.
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
- **Domicilio único** para todo lo que tiene lugar físico (validadores, sitios de trabajo): columnas de
  `AddressMixin` (`app/models/mixins.py`) + esquema `Address` (`app/schemas/address.py`, el mismo para
  guardar y para leer) + `apply_address` / `address_of`. Campos en el orden que pidió el dueño del
  producto (el mismo del formulario): `country_code`, `state`, `municipality`, `city`, `neighborhood`
  (colonia, obligatoria al guardar, 2-120), `postal_code`, `street`, `exterior_number`,
  `interior_number` (opcional), `reference_notes` (referencias, opcionales, ≤ 300, varios renglones;
  `references` es palabra reservada de PostgreSQL) y el punto (`latitude`, `longitude`). Un campo nuevo
  del domicilio va en el mixin, el esquema y una migración (nulo si ya hay filas: `address_of` lee lo
  guardado sin volver a validarlo, y el esquema lo exige al guardar con `validate_default` para dar el
  mensaje en español también cuando falta la llave), y en `AddressFields`/`utils/address.ts` de la app.
  La API de integración lo entrega dentro de `address` (solo se agregan campos: compatible hacia atrás).
- **Identificador fiscal de la empresa** (cualquier país, migración `0074`; README «Identificador fiscal de la empresa»):
  país + tipo (`catalog.tax_id_types`) + número, opcional y validado solo por `app/schemas/tax_ids.py` (`TaxIdFields` en
  los esquemas, `resolve_tax_id` en la validación en vivo `company_tax_id`): la regla de formato es DATO del catálogo;
  el código solo agrega las reglas propias del RFC y los dígitos verificadores conocidos (`CHECK_DIGITS`). Se guarda con
  `Company.set_tax` (los tres juntos y, mientras exista, la columna anterior `rfc`) y se lee con `Company.tax` /
  `legacy_rfc`; nada nuevo lee `rfc` (pendiente de quitar, README).

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
    medir antes y después de una migración. Los planes se miden como en producción: con el usuario de la API
    (seguridad por fila) y cada caso con su alcance (`scope_of_case`); `PERF_DB_ROLE=owner` mide sin la
    política, para comparar. Los recorridos que quedan hoy y su motivo (búsquedas de 1-2 letras,
    depuración atrasada, la foto diaria del almacenamiento) están en el README, "Pendiente de decisión del
    dueño del producto".
13. **Migraciones de índices** sobre tablas grandes: `CREATE/DROP INDEX CONCURRENTLY` dentro de
    `op.get_context().autocommit_block()`; un índice que cambia de definición se construye con nombre
    temporal, se borra el anterior y se renombra (la consulta nunca se queda sin índice); cada paso
    con `IF EXISTS` (repetible tras una falla a la mitad) y un `downgrade` que deja exactamente lo
    anterior (patrón de `0047_performance_audit.py`). El modelo dice lo mismo que la migración
    (`./scripts/quality.sh --postgres` compara ambas bases: columnas, restricciones, índices, políticas,
    particiones, funciones, comentarios y `fillfactor`). Una llave foránea nueva sobre una tabla grande: `NOT
    VALID` y después `VALIDATE CONSTRAINT` (no frena lecturas ni escrituras; patrón de `0054`).
14. **Empresa y seguridad por fila** (§3.2): toda tabla de empresa con `company_id NOT NULL`, FK compuestas, su
    política y su lugar en `TENANT_TABLES`; un súper-índice que debe resolver sin leer la tabla incluye
    `company_id`; una búsqueda con `LIKE`/`lower()` (no *leakproof*) se mide con el usuario de la API.
15. **Tabla que crece sin límite**: particionada por mes (§3.3) con su retención por partición; sus consultas
    acotan la columna de partición siempre que pueden.
16. **Actualizaciones frecuentes** de columnas sin índice (último uso, contadores, sumas del medidor): `fillfactor`
    en el modelo (`postgresql_with`) y su migración, para que el cambio quepa en la misma página (HOT). Nunca un
    índice sobre esa columna "por si acaso" (rompería el HOT).
17. **Sin inyección SQL** (§3.6): los valores como parámetros, toda búsqueda con `contains_text`, un orden elegible por
    un diccionario de columnas y un identificador dinámico con `sql_identifier`; `tests/test_sql_safety.py` lo vigila.

### 3.2 Aislamiento entre empresas en la base: seguridad por fila (Row-Level Security)

La última barrera (decisión del dueño: por ninguna razón se mezclan datos de dos empresas). Aunque el código
olvidara un filtro, PostgreSQL no entrega ni acepta filas de otra empresa. Mecanismo (`app/core/row_security.py`,
migración `0056`):

- **Tablas de empresa** (`TENANT_TABLES`, 49): `ENABLE` + `FORCE ROW LEVEL SECURITY` y la política
  `tenant_isolation`: `company_id = NULLIF(current_setting('app.company_id', true), '')::integer` (USING y WITH
  CHECK). Las tablas de la plataforma con `company_id` (`auth.users`, `auth.auth_sessions`, `ops.error_occurrences`)
  están en `PLATFORM_TABLES_WITH_COMPANY` con su motivo; `tests/test_row_security.py` falla con una sin clasificar.
- **El alcance es de la TRANSACCIÓN**, nunca de la sesión: el evento `after_begin` ejecuta UNA sentencia al empezar
  cada una, según `Session.info` (`use_company`, `use_platform`, `clear_scope`): empresa →
  `set_config('app.company_id', N, true)`; plataforma → `set_config('role', DB_PLATFORM_ROLE, true)` (rol sin login
  con `BYPASSRLS`); ninguno → nada (falla cerrado). `true` = solo esa transacción: correcto tras PgBouncer en modo
  transacción (medido: 0 fugas con 8 clientes sobre 4 conexiones reales). Cambiar a mitad de una transacción aplica
  al instante.
- **Quién declara qué** (un camino nuevo que toque tablas de empresa entra en uno de estos):
  - petición con sesión: `get_db` empieza SIN alcance; `authenticate_request` autentica como plataforma (la cuenta y
    sus empleos en todas sus empresas), confirma y declara la empresa en que opera (`scope_to`): el ADMIN es
    plataforma, un empleado que aún no elige empresa no ve ninguna;
  - llave de integración: plataforma para buscarla por su hash, después su empresa (`get_api_client`);
  - rutas de identidad (iniciar, renovar y cerrar sesión, elegir empresa, cuenta recordada, contraseña) y el
    reporte de fallas de la app: `PlatformDb` (declárala DESPUÉS del usuario autenticado);
  - canal WebSocket: `authenticate_request` y `scope_to` en cada mensaje;
  - segundo plano (mantenimiento, medidor de consumo, arranque, CLI): `platform_session()`.
- **Cruzar empresas en una petición de empresa es explícito y raro**: `with crossing_tenants(db):` solo para la
  sentencia que lo necesita, con su porqué en el código. Hoy: "¿esta persona también trabaja en otra empresa?"
  (`EmployeeRepository.shared_accounts`: un conteo, sin datos de la otra) y la huella anti-reenvío de una captura
  (`CaptureFingerprintRepository.claim`: única en toda la plataforma). OJO: con la política, `user.employees` de una
  cuenta cargada en una petición de empresa solo trae los empleos de ESA empresa; lo que necesite todos los empleos
  va por la autenticación (plataforma) o por un cruce explícito.
- **Pruebas**: en SQLite no hay RLS; la guarda de `tests/conftest.py` falla si una sentencia toca una tabla de
  empresa sin alcance (la misma regla). En PostgreSQL (`./scripts/quality.sh --postgres`) toda la suite corre con el
  usuario de la API: un camino que olvidó su alcance no ve nada y su prueba falla.
- **Costo** (medido, README "Seguridad por fila"): una sentencia de ida y vuelta por transacción con alcance; la
  política es una igualdad `int4eq` *leakproof*: con el `company_id = :empresa` de la consulta, PostgreSQL la vuelve
  un filtro de una sola vez y los índices que empiezan por `company_id` se usan igual. Un operador que NO es
  *leakproof* (`LIKE`, `lower()`, comparaciones de `numeric`) no puede usarse como condición de índice antes de la
  política: mide con `perf/db/run.sh` (corre con el usuario de la API) toda búsqueda nueva.
- **Migraciones con datos** de estas tablas corren con el dueño: en docker compose es superusuario y la política no
  le aplica; en una base administrada, el usuario de migraciones necesita `BYPASSRLS`.

**Agregar una tabla de empresa** (una sola vez, todo en el mismo cambio):
1. `company_id` `NOT NULL` (FK a `tenancy.companies` o compuesta hacia su padre) y sus FK hacia otras tablas de
   empresa compuestas con `company_fk(...)`; si otras la van a referenciar, su `UniqueConstraint("id", "company_id")`.
2. Su nombre en `TENANT_TABLES` (`app/core/row_security.py`): los modelos crean su política en `create_all`.
3. En su migración: `ENABLE` + `FORCE ROW LEVEL SECURITY`, la política `tenant_isolation` y el comentario de
   `company_id` (la misma SQL que `row_security.policy_ddl`); `scripts/quality.sh --postgres` compara ambas bases.
4. Índice que empiece por `company_id` para sus listados (§3.1.1) y, si un súper-índice resuelve un conteo sin leer
   la tabla, `company_id` en su clave o INCLUDE (la política lo pide: sin él, el *index-only scan* lee la tabla).
5. Su caso en `tests/test_tenant_isolation.py` (si tiene ruta con id: `ROUTES`; la prueba falla si falta).

### 3.3 Tablas que crecen sin límite: particionadas por mes

`app/core/partitions.py` (`PARTITIONED`), migración `0055`. Hoy: `attendance.verification_logs`,
`attendance.attendance_events`, `ops.face_attempt_metrics`, `ops.error_occurrences`, `ops.usage_routes`,
`ops.usage_users`, `ops.storage_snapshots`, `ops.risk_assessments` (`0062`, la decisión del motor de riesgo de cada
intento, con la retención de las métricas faciales) y `ops.perf_minutes` (`0063`); las dos últimas, particionadas desde
su creación. Una tabla
NUEVA que crece sin límite se crea ya particionada (su partición `_default` y `ops.ensure_partitions` en la migración),
sin el paso de `_legacy`.

- `PARTITION BY RANGE` de su columna de tiempo; la llave primaria la incluye (PostgreSQL lo exige: `(id, fecha)`;
  en SQLite sigue siendo `id`, la regla `@compiles` de `partitions.py`); el ORM identifica la fila por `id`.
- Las particiones (`<tabla>_pAAAAMM`) las crea y las borra SOLO `ops.ensure_partitions` (SECURITY DEFINER del
  dueño, `alembic/sql/0055_partitions.sql`): la API no tiene DDL. El mantenimiento la llama en cada vuelta: deja
  `PARTITION_MONTHS_AHEAD` meses por delante y BORRA (`DROP TABLE`, instantáneo) los meses vencidos según la
  retención de cada tabla, en lugar de un `DELETE` fila por fila. `_default` recibe lo que llegue sin su mes (la
  función lo mueve al crear el mes) y `_legacy` es la tabla anterior a la migración (sin copiarla). Su `Purge` se
  queda: limpia lo vencido de `_legacy` y `_default`.
- Nadie tiene permisos sobre una partición (solo sobre la padre, donde vive la política): no se la salta nadie.
- Ninguna FK APUNTA a una tabla particionada (exigiría copiar su fecha y frenaría borrar meses): la referencia
  queda por id con su porqué (`attendance_events.verification_log_id`).
- Toda consulta lleva, si puede, una cota de la columna de partición (poda): `last_located_event` busca solo la
  ventana en que un viaje aún puede ser imposible (§3.1.5). Sin cota, cada partición es una búsqueda más.
- **No se particiona** lo que necesita una unicidad global que no incluye la fecha (`work_sessions`: una jornada
  abierta por empleado; `capture_fingerprints`: huella única en la plataforma) ni lo que su retención ya acota
  (`usage_daily`, `headcount_days`, sesiones, retos, QR).
- **Agregar una**: su entrada en `PARTITIONED`, `**partitioned("columna")` en su `__table_args__`, su nombre en la
  lista de la función (archivo SQL nuevo con el número de su migración) y una migración con el patrón de `0055`
  (índices y llave nueva `CONCURRENTLY` sobre la tabla actual, CHECK `NOT VALID` + `VALIDATE` del rango de
  `_legacy`, cambio de nombre y `ATTACH PARTITION` en un instante; `downgrade` copia de vuelta).

### 3.4 Roles, privilegios, tiempos límite y respaldos

- **Roles** (`app/core/db_roles.py`; los crea y pone al día `python -m app.cli db roles`, que corre `migrate` en
  cada despliegue): el dueño `POSTGRES_USER` (DDL: solo migraciones, roles y respaldos; la API ni conoce su
  contraseña), la API `DB_APP_USER` (DML en los esquemas de dominio, catálogos solo lectura; sin DDL, sin
  superusuario, sin `BYPASSRLS`), la plataforma `DB_PLATFORM_ROLE` (sin login, `BYPASSRLS`; la API cambia a él por
  transacción) y solo lectura `DB_READONLY_USER` (SELECT, sujeto a la política). `PUBLIC` no tiene nada en la base ni
  en los esquemas. Las contraseñas viajan como verificador SCRAM (nunca en claro). Permisos tabla por tabla, nunca
  `ALTER DEFAULT PRIVILEGES` (las particiones no deben recibirlos): una tabla nueva los recibe en el mismo despliegue.
- **Tiempos límite**: la API (por rol) `statement_timeout` = `DB_STATEMENT_TIMEOUT_MS`, `lock_timeout` =
  `DB_LOCK_TIMEOUT_MS` y `idle_in_transaction_session_timeout` = `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS`; el servidor,
  10 min de transacción inactiva para cualquiera (`postgres/start.sh`). La única transacción que espera a
  propósito (el candado del mantenimiento) apaga el suyo con `SET LOCAL`.
- **Parámetros del servidor**: `backend-employee-time-clock/postgres/start.sh` los calcula con `PG_MEMORY_MB`,
  `PG_CPUS`... del `.env` de la raíz (README "Parámetros de PostgreSQL"); `pg_stat_statements` siempre activo.
- **Respaldos** (`app/services/db_backup.py`, servicio `backup`): `pg_dump` custom verificado cada
  `BACKUP_INTERVAL_HOURS`, retención `BACKUP_RETENTION_DAYS`, copia al bucket SOLO cifrada por partes
  (`BACKUP_UPLOAD`, decisión del dueño: encendida). El manifiesto local se escribe ANTES de subir: con el bucket caído
  el respaldo queda completo y pendiente, la falla llega al ADMIN y la subida se reintenta cada `BACKUP_RETRY_MINUTES`
  (idempotente: cada objeto lleva el SHA-256 de su contenido legible). La regla de ciclo de vida del bucket
  (`python -m app.cli storage lifecycle [--apply]`, `app/services/storage_lifecycle.py`) borra las copias vencidas aunque
  se pierda la carpeta local; nunca toca `<GCS_PREFIX>/pitr/`. Restaurar desde el bucket: `python -m app.cli db
  fetch-backup` (baja, descifra, verifica) + `scripts/db_restore_check.sh`; el simulacro completo es
  `scripts/db_bucket_restore_check.sh`. Un cambio que afecte la restauración se prueba con esos scripts. El cliente
  de PostgreSQL de la imagen es de la misma versión mayor que el servidor (`PG_MAJOR` del `Dockerfile`): subir de
  versión el servidor exige subirla también ahí (un `pg_restore` más nuevo deja sentencias que el viejo no conoce).
- **Recuperación a un punto en el tiempo (PITR, pgBackRest)**: el servicio `db` es la etapa `db` del `Dockerfile`
  (postgres:16-alpine + pgBackRest compilado de su tarball FIJO y verificado por SHA-256) y archiva cada segmento del WAL
  al bucket, cifrado (`postgres/pgbackrest.sh archive`: asíncrono, con el guardián del disco `PITR_WAL_MAX_MB`); el
  servicio `pitr` (misma imagen, `scheduler`) toma los respaldos base y aplica la retención (`PITR_RETENTION_DAYS`); el
  monitor del servicio `backup` (`app/services/pitr_monitor.py`) avisa al ADMIN en "Errores del sistema" (lag, WAL
  acumulado, WAL descartado, respaldo base fallido o atrasado, servicio pitr detenido). Reglas que no se rodean:
  - `db` y `pitr` corren con `init: true`: el proceso asíncrono de pgBackRest queda huérfano y, si PostgreSQL fuera el
    PID 1, al terminar con error (bucket caído) el postmaster reiniciaría TODAS las conexiones (medido; el simulacro lo
    verifica).
  - Una configuración incompleta de PITR nunca deja la base abajo: arranca sin archivar y el monitor lo avisa.
  - `PITR_CIPHER_PASS` es propia (no derivada de `DATA_ENCRYPTION_KEY`) y vive solo en el `.env`; nunca se imprime.
  - Ninguna variable de PITR es una constante del script: van en `config.py` y `${VAR:-valor}` usa el mismo valor
    (`tests/test_env_files.py` revisa `postgres/pgbackrest.sh`).
  - Subir de versión pgBackRest (`PGBACKREST_VERSION` + `PGBACKREST_SHA256`) o tocar el archivo, la restauración o el
    guardián exige correr `scripts/db_pitr_check.sh` (bucket falso detrás de `scripts/fake_gcs_proxy.py`; jamás el real).

### 3.5 Borrado lógico («Eliminados», regla 20 de la raíz; migración `0068`)

Decisión del dueño: «todos los delete de la aplicación deben ser softdelete». Detalle e inventario en el README,
"Borrado lógico («Eliminados»)".

- **Tablas**: `companies`, `users`, `employees`, `validators`, `departments`, `work_sites`, `site_kiosks` (`0070`),
  `shifts`, `shift_assignments`, `company_holidays`, `employee_workdays` y `company_documents` (`0075`) (`SoftDeleteMixin`: `deleted_at` y `deleted_by`, el
  correo literal de quien eliminó, como la cobranza: sobrevive a que esa cuenta se depure y no necesita JOIN).
- **Mecanismo único y automático** (`app/core/soft_delete.py`): el evento `do_orm_execute` agrega
  `deleted_at IS NULL` de cada tabla con borrado lógico a TODA consulta y UPDATE masivo del ORM (FROM, JOIN,
  subconsultas, conteos, `Session.get`, colecciones uno a muchos). No lo lleva: la referencia muchos a uno (el
  historial sigue nombrando a quien se eliminó), la recarga de un objeto, un `DELETE` y la sentencia que lo pide con
  `with_deleted(...)` / `WITH_DELETED` (papelera, eliminar, restaurar, depuración y las búsquedas por id que resuelven
  referencias del historial: `by_ids`, `emails_by_ids`, `names_by_ids`, `shifts_by_ids`, `sites_of_shifts`...).
  `paginate` cuenta la papelera con lo eliminado; `trash_page` da la página (lo más reciente primero).
- **Eliminar** (`services/trash.py`: `ensure_live` → 409 `ALREADY_DELETED`) marca con `mark_deleted` en la misma
  transacción de lo que cierra (sesiones, eventos del cobro, solicitudes pendientes canceladas). Las reglas de antes se
  conservan (sin empleados, sin turnos que lo usen...). **Una relación entre dos registros no se borra** (decisión del
  dueño, 2026-10-06: «restaurar regresa todo como estaba»): los responsables de un departamento
  (`department_managers`, sin `SoftDeleteMixin`) se quedan y las lecturas los unen con el otro lado (`managers_of`,
  `managed_by`), que el mecanismo oculta mientras esté en «Eliminados»; regresan solos en cuanto los dos están vigentes,
  en cualquier orden. Una relación nueva sigue el mismo patrón (nunca un `DELETE` al eliminar uno de sus lados). El detalle (`GET .../{id}`)
  también devuelve lo eliminado (con `deleted_at`); editar o sus sub-recursos responden 404 como lo que no existe.
- **Restaurar** (`POST .../{id}/restore`, misma pantalla y rol que eliminar; `ensure_deleted` → 409 `NOT_DELETED`):
  revisa de nuevo lo que pudo cambiar (datos únicos tomados por otro → 409 `RESTORE_CONFLICT` con su campo y su
  llave `RESTORE_*`; límites de empleados y validadores; una asignación, con las MISMAS reglas que asignar) y confirma
  con `commit_restore` (el índice parcial es la última barrera). Lo que se eliminó junto (las cuentas y validadores de
  una empresa) lleva la misma marca de tiempo y regresa con él.
- **Papelera** (`?deleted=true` en el listado; con varias pantallas que listan, `trash_of(pantalla)` exige la que
  elimina): `ix_<tabla>_deleted` parcial `WHERE deleted_at IS NOT NULL` (solo lo eliminado: nada vigente lo toca).
- **Índices**: únicos parciales `WHERE deleted_at IS NULL` (`live_unique`: los datos se reutilizan); el índice que
  además es de una FK (debe ver todas las filas para el CASCADE o el RESTRICT) se queda completo con `deleted_at` en
  el INCLUDE; un índice del camino caliente que no es de una FK se vuelve parcial.
- **Excepción (LFPDPPP, regla 13)**: al eliminar a una persona, sus datos biométricos y fotos se borran DE VERDAD
  (`services/person_erasure.py`: plantillas, registros faciales, huellas perceptuales, evidencia de fraude, foto de
  perfil y cuenta recordada; los objetos a la cola `storage_jobs`, así un bucket caído nunca detiene ni deja nada para
  siempre). Restaurar no los regresa (su estado facial queda "sin registro" con el motivo `FACE_ERASED_ON_DELETE`).
- **Depuración** (`maintenance_service.SOFT_DELETE_PURGES`, lotes de `SOFT_DELETE_PURGE_BATCH_SIZE`): pasados
  `SOFT_DELETE_RETENTION_DAYS`, en orden y saltando lo que aún tiene una referencia RESTRICT (nunca falla el lote);
  una empresa con cobranza nunca se depura.
- **Una tabla nueva con eliminación**: `SoftDeleteMixin`, sus únicos con `live_unique`, `trash_index`, sus rutas
  (papelera y restaurar) con sus pruebas (autorización, aislamiento, conflictos), su `Purge` en `SOFT_DELETE_PURGES` y
  sus casos en `perf/db/explain.py`.

### 3.6 SQL sin inyección (regla 21 de la raíz)

Decisión del dueño del producto: «agrega todo lo necesario para evitar SQL injection». Detalle, auditoría y cifras en el
README, "Sin inyección SQL". Defensa en capas; ninguna se omite:

1. **Los VALORES nunca van en el texto de una sentencia**: viajan como parámetros (las expresiones del ORM; en `text()`,
   `:nombre` con su diccionario). Nada de f-strings, `%`, `.format`, `.join` ni `+` para armar SQL con un dato, aunque
   "venga validado".
2. **En el texto solo hay** palabras fijas, identificadores de una lista cerrada (constantes del código, `Literal`, los
   metadatos de los modelos o la configuración validada) escritos con `sql_identifier(...)` y, en sentencias de utilidad
   que PostgreSQL no deja parametrizar (`ALTER ROLE ... PASSWORD`, `COMMENT ON ...`), literales que arma el propio código
   con `sql_literal(...)` (`app/core/sql_safety.py`: revisa el juego de caracteres y entrecomilla con SQLAlchemy).
3. **Búsquedas**: TODO `LIKE` es `contains_text(expresión, search_term(search))` (`app/repositories/search.py`): el
   término es un parámetro, sus `%`, `_` y `/` se escapan (`autoescape`) y la sentencia lleva `ESCAPE '/'`; un `%`, un `_`
   o una `\` se buscan literales. Es la misma SQL que miden `perf/db/run.sh` y los índices de trigramas.
4. **Orden y filtros que elige el cliente**: un `Literal[...]` (o `pattern`) en la ruta y un diccionario
   `{valor: columna}` en el repositorio (`usage_repository`, `performance_repository._order`); jamás
   `getattr(Modelo, lo_que_llegó)` ni un nombre de columna desde la query.
5. **Texto que la API no acepta** (`app/core/input_guard.py`): NUL, los demás controles C0 (salvo tabulador y saltos de
   línea) y los sustitutos UTF-16 sueltos responden 422 `INVALID_CHARACTERS` con el campo. Una sola regla en tres
   lugares: la dependencia de toda la app (`reject_invalid_input`: ruta, query, cabeceras y el cuerpo que leyó FastAPI,
   antes de la autenticación), el canal en vivo (cada mensaje) y la última barrera en el motor (`refuse_unstorable`: lo
   que PostgreSQL no puede recibir nunca llega como un `DataError`, 500). Una ruta que lee su cuerpo por su cuenta usa
   `request_body.bounded_json` (ya aplica la regla) o llama `ensure_valid_text`.
6. **Funciones de la base** (`alembic/sql/*.sql`): toda `SECURITY DEFINER` lleva `SET search_path = pg_catalog, pg_temp`
   y su SQL dinámica es `EXECUTE format('... %I ... %L', ...)` o un texto constante con `USING`; nunca concatenada. Sin
   `EXECUTE` para `PUBLIC` ni para la API (solo el rol de la plataforma, `db_roles.PLATFORM_FUNCTIONS`).
7. **Defensa en profundidad** (ya existe; no se rodea): la API sin DDL ni superusuario y sujeta a la seguridad por fila
   (una inyección no vería otra empresa ni podría crear o borrar tablas), `statement_timeout` por rol, sin sentencias
   preparadas (`prepare_threshold=None`), `hide_parameters` en el motor y errores con el mensaje del catálogo: quien
   llama nunca ve SQL ni parámetros; el ADMIN ve la sentencia de una falla, sin sus valores.

**Guardianes** (fallan solos, en cada cambio):
- `ruff check` (`S608`): también en las migraciones nuevas (`pyproject.toml`: de todo el lint, solo `S608` en
  `alembic/versions`; las aplicadas que arman SQL con sus constantes están listadas con su motivo y no se editan).
- `tests/test_sql_safety.py` (AST de `app/` y `alembic/`): ninguna SQL armada con algo que no sea constante,
  `sql_identifier`/`sql_literal` o `int`; ningún `LIKE` fuera de `search.py`; ninguna columna elegida por nombre; las
  funciones de la base con su `search_path` y sin concatenar. Las excepciones viven en `ALLOWED`, `FROZEN_MIGRATIONS` y
  `DYNAMIC_ATTRIBUTES` con su motivo y su cuenta exacta (una construcción nueva o una entrada que sobra la hacen fallar).
  En PostgreSQL además revisa la base viva: funciones `SECURITY DEFINER` con su `search_path`, sin `EXECUTE` para
  `PUBLIC` ni para la API, y la API sin superusuario, `BYPASSRLS`, `CREATE` ni tablas propias.
- `tests/test_sql_injection.py` (matriz, como `test_authorization.py`): cada ruta de `API_ROUTERS` con cargas clásicas
  en cada parámetro y campo de texto, con el rol que sí tiene permiso: nunca un 5xx, NUL → 422, nada de otra empresa y la
  base intacta; además el canal en vivo, la cabecera `X-API-Key`, búsquedas literales y errores sin SQL. Corre en SQLite
  y en PostgreSQL con el usuario de la API (`./scripts/quality.sh --postgres`).
- `tests/test_input_guard.py`: la regla de texto y la última barrera del motor.

**Cómo agregar, sin abrir una puerta:**
- *Una búsqueda*: `term = search_term(search)` y `contains_text(func.lower(Columna), term)` (o la expresión de su índice
  de trigramas); el parámetro de la ruta con `max_length`. Su caso con término en `perf/db/explain.py` (§3.1.12).
- *Un orden o filtro elegible*: `Literal[...]` en la ruta → diccionario de columnas en el repositorio → `order_by(...)`
  con su desempate (`id`) y su índice (§3.1.1).
- *Un identificador dinámico* (esquema, tabla, rol, partición): de una constante o de los metadatos, con
  `sql_identifier(nombre)` en la f-string; nunca de la petición. Si el AST no puede demostrarlo (un parámetro de una
  función que solo recibe constantes), su entrada en `ALLOWED` con el motivo, en el mismo cambio.
- *Una función de la base*: archivo SQL nuevo con su migración (§3), `SECURITY DEFINER` solo si hace falta, con
  `SET search_path = pg_catalog, pg_temp`, `format('%I'/'%L')` o `USING`, `REVOKE ALL ... FROM PUBLIC` y su firma en
  `PLATFORM_FUNCTIONS`.
- *Un campo de texto nuevo* (cuerpo, query, formulario, mensaje del canal): no se hace nada; la regla de `input_guard`
  ya lo cubre (la matriz lo prueba sola).

## 4. Rendimiento y escalabilidad

- **Lista para escalar en cualquier momento** (README, "Escalar en cualquier momento"): N réplicas detrás
  del gateway Nginx, PgBouncer en modo transacción entre ellas y PostgreSQL, y migración única (servicio
  `migrate`). Todo cambio respeta estas reglas; si toca la topología (compose, Nginx, PgBouncer, arranque o
  apagado) se prueba con `perf/scale/run.sh`.
- **Sin estado que impida varias instancias**: lo compartido vive en PostgreSQL. Una caché local
  (catálogos, política, galería facial) se valida contra la BD (TTL corto o huella) y se acota
  (LRU por memoria o cantidad). Lo que es de cada proceso (control de admisión, conexiones del canal, cola
  facial, hilos de Argon2) solo protege a ESE proceso; nunca decide un resultado de negocio. Inventario
  vigente y por qué cada pieza es correcta con N réplicas: README, "Escalar en cualquier momento".
- **La BD llega por PgBouncer en modo transacción: nada de estado de SESIÓN de PostgreSQL** en el código de
  la API. Una conexión real cambia de cliente en cada transacción, así que:
  - ni `SET` (si hiciera falta, `SET LOCAL` / `set_config(..., true)` dentro de la transacción, como la empresa de
    la seguridad por fila, §3.2), ni parámetros al conectar (`options`:
    `database.session_options` no manda nada con `DB_POOLER=pgbouncer`; `statement_timeout` lo pone el
    `connect_query` de PgBouncer y la API verifica al arrancar que exista);
  - ni candados de sesión: `pg_try_advisory_xact_lock` con su transacción abierta mientras se trabaja
    (`maintenance_service.exclusive`), nunca `pg_advisory_lock` (se quedaría pegado a una conexión del
    servidor que luego usa otro cliente: medido, el candado sobrevive al cliente que lo tomó);
  - ni `LISTEN/NOTIFY`, tablas temporales, cursores `WITH HOLD` ni sentencias preparadas
    (`prepare_threshold=None`).
  Lo que de verdad necesite una sesión propia va por la conexión directa `DATABASE_DIRECT_URL` (hoy solo las
  migraciones: candado de sesión y `CREATE INDEX CONCURRENTLY`).
- **Transacciones cortas**: detrás de PgBouncer una transacción abierta ocupa una conexión REAL del servidor.
  No se espera CPU (análisis facial), red (bucket) ni otro servicio con una transacción abierta si se puede
  cerrar antes (el login cierra la suya antes de Argon2). Las capturas faciales cierran la suya antes de analizar
  (`take_challenge` y `confirm_live`; lo registrado va en su propia transacción y la asistencia vuelve a leer y
  bloquear al empleado): `tests/test_face_transactions.py` verifica 0 conexiones prestadas durante el análisis.
- **Migraciones compatibles con la versión anterior** (despliegue gradual): mientras `migrate` aplica una
  migración, las réplicas viejas siguen atendiendo. Primero se agrega (columna nula o con valor por omisión,
  tabla, índice `CONCURRENTLY`) y se despliega el código que lo usa; quitar o renombrar va en una migración
  posterior, cuando ningún código en marcha lo lee. `alembic/env.py` además serializa las migraciones con un
  candado de sesión en la conexión directa (con tope de espera): dos réplicas nunca migran a la vez.
- **Apagado ordenado** (`app/core/lifecycle.py`): SIGTERM → readiness 503 `SHUTTING_DOWN` y `Connection:
  close` durante `SHUTDOWN_DRAIN_SECONDS`, luego uvicorn termina lo que lleva (`SHUTDOWN_GRACE_SECONDS`) y el
  `lifespan` detiene el mantenimiento y guarda consumo y errores. Nada del apagado puede tardar más que eso
  (docker compose espera 45 s y después mata).
- **IP del cliente solo con `client_ip(request)`**: la decide el gateway (reemplaza `X-Forwarded-For`) y
  uvicorn solo la acepta de la red del gateway (`FORWARDED_ALLOW_IPS=auto`). Nunca se lee `X-Forwarded-For`
  a mano.
- **Control de admisión adaptativo** (`app/core/admission.py`): el límite de peticiones a la vez se
  ajusta solo a la latencia real; la fila atiende primero lo crítico (iniciar sesión, identificar,
  verificar, QR) y, dentro de cada nivel, lo más demandado; al saturarse se descarta primero lo
  menos importante con 503 `SERVER_BUSY` reintentable. Una ruta crítica nueva se agrega a
  `CRITICAL_PREFIXES`; un tablero o una estadística pesada, a `BACKGROUND_PREFIXES` (hoy: estadísticas
  y seguridad facial del ADMIN, su consumo, el resumen de su cobranza y las lecturas de "Rendimiento", el tablero de
  asistencia, las fallas y el rendimiento que manda el navegador y las fotos de perfil, que la app reemplaza por las
  iniciales). Solo las respuestas exitosas
  miden la capacidad, la latencia típica se aprende solo con holgura, ningún grupo ocupa más de la
  mitad de la fila y la memoria está acotada (grupos inactivos se olvidan; los que no caben
  comparten uno por nivel).
- **Lista para millones de operaciones por minuto** (regla 15 de la raíz). Cada cambio la cumple:
  1. Cada consulta con su índice que empieza por `company_id` (y la fecha en tablas que crecen), medida con
     volumen y con el usuario de la API (§3.1); nada de `Seq Scan` de tablas grandes ni órdenes en disco.
  2. Tablas que crecen sin límite particionadas por mes y depuradas por partición (§3.3).
  3. Telemetría en lote, nunca una escritura por petición (`usage_meter`, `error_reporter`); las métricas faciales
     viajan con su intento en la misma transacción.
  4. Transacciones cortas: nada de CPU, red ni esperas con una transacción abierta (§4, "Transacciones cortas").
  5. Conexiones por PgBouncer; consultas acotadas (paginación, `count_cap`, tiempos límite por rol); operaciones
     masivas en una sentencia; sin N+1 (`tests/test_performance.py`).
  6. Alcance de la seguridad por fila: UNA sentencia por transacción; no se abren transacciones de más.
  7. Parámetros del servidor calculados para el equipo (`postgres/start.sh`), `pg_stat_statements` para ver qué
     consume la base, respaldos probados (§3.4).
- **Presupuesto de consultas** (ver §3) vigilado por `tests/test_performance.py`; operaciones
  masivas en una sentencia (`affected_rows`), nunca un ciclo por fila.
- **Trabajo pesado fuera de la petición**: depuraciones en `maintenance_service`, guardado de errores
  en lotes (`error_reporter`), consumo en lotes (`usage_meter`), cola acotada del motor facial y el
  borrado de objetos del bucket (`storage_jobs`, dentro del mantenimiento). Subir una imagen sí ocurre en
  la petición (decisión del dueño: nunca se guarda en la BD), acotado por `GCS_REQUEST_SECONDS`.
- **Medir sin costo por petición (patrón de `usage_meter`)**: el middleware del traceId (y el canal
  WebSocket, por mensaje) solo suma en memoria, bajo un candado, empresa/día, empresa/día/ruta
  (plantilla, nunca la URL) y empresa/día/cuenta (≈2 µs, cero consultas); un hilo guarda cada
  `USAGE_FLUSH_SECONDS` con UN upsert por grano que SUMA (`ON CONFLICT ... DO UPDATE SET requests =
  requests + EXCLUDED.requests`), en orden de llave (sin interbloqueos entre instancias) y cada grano en
  su transacción. Memoria acotada (`USAGE_METER_MAX_KEYS`: lo que no cabe se suma a `OTHER` / cuenta 0
  de su empresa); si la BD falla lo no guardado vuelve al acumulador (nada se pierde) y la falla se
  registra; al apagar se guarda lo pendiente. Una medición nueva sigue este patrón: nunca una escritura
  por petición.
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
  huella contra la BD en cada uso; el 1:N de cada 1:1 la reutiliza sin revalidar durante
  `RISK_IDENTITY_GALLERY_MAX_AGE_SECONDS`, `face_galleries.recent`: solo MIDE una señal, y nunca carga una galería
  que no cabe), bloqueos de migración facial (tope de 10 000) y la base local de IP (`ip_intel`: el archivo
  `.mmdb` en memoria compartida del sistema, `mmap`; cada proceso revisa cada `IP_DB_RELOAD_CHECK_SECONDS` si el
  archivo cambió —fecha, tamaño e inodo— y lo vuelve a abrir; el reemplazo es atómico con candado de archivo, así
  que dos procesos nunca escriben a la vez y uno nunca lee un archivo a medias). Una caché nueva
  declara su tope y cómo se invalida, y cuánto tarda en verse en TODAS las réplicas un cambio hecho en una
  (su TTL: invalidarla solo en el proceso que atendió el cambio no basta).
- **Recursos por réplica**: cada proceso carga sus propios modelos faciales (≈150 MB por worker facial).
  `API_WORKERS` y `FACE_WORKERS` automáticos respetan el límite de CPU del contenedor (`BACKEND_CPUS`): en un
  equipo con varias réplicas se reparte el CPU entre ellas, nunca se deja que cada una vea todos los núcleos.
- **Análisis facial en paralelo solo con workers de repuesto** (`facial_recognition.run_on_spare` y, para N análisis
  iguales, `map_on_spares`): la ráfaga del intento y las fotos del registro facial (las 36 candidatas y CLIP de las
  referencias, `enrollment_selection`) se reparten con los workers LIBRES de ese instante, sin transacción abierta;
  lo que un repuesto no termina a tiempo lo hace el de la petición. Nunca un pool o hilo propio.

### 4.1 Observabilidad de rendimiento (pantalla "Rendimiento", regla 18 de la raíz)

Todo se mide sin trabajo de BD dentro de la petición (patrón de `usage_meter`) y se guarda en lotes; detalle y cifras
en el README, "Observabilidad de rendimiento" (migración `0063`).

- **Piezas**: `app/core/histogram.py` (cubetas fijas, percentiles interpolados), `app/core/perf_meter.py` (acumuladores
  en memoria acotados: `perf_meter` por minuto/tipo/nombre y `slow_requests` por ruta), `app/core/observability.py`
  (`observed`, el temporizador de BD por petición `request_timer_var` con los eventos del motor y `TimedQueuePool`),
  `app/middleware/request_id.py` (`_observe`: cada petición y las lentas), `app/services/perf_store.py` (guardado en
  lotes, `PerfFlusher`), `app/services/perf_rollup.py` (minutos → horas → días, en el mantenimiento),
  `app/services/web_performance.py` (lo que manda el navegador) y `app/services/performance_service.py` (lecturas).
- **Medir una función clave**: `@observed("área.acción")` en la función (o `with observed("área.acción"):` alrededor
  del bloque). El nombre es estable, en minúsculas y con punto (`face.detect`, `billing.issue_charges`,
  `storage.put`): es la llave de sus filas (cambiarlo empieza otra serie). Nunca un nombre con datos (ids, correos,
  empresas): la cardinalidad la acota el código, no el tráfico. Una excepción cuenta como falla y sigue su camino.
  Cuesta ≈1.2 µs por llamada: no se mide un ciclo interno de miles de vueltas, sino su función.
- **Regla 18 (peticiones lentas)**: el umbral es `SLOW_REQUEST_THRESHOLD_MS` y, en las rutas faciales,
  `SLOW_REQUEST_FACE_THRESHOLD_MS` (2 500; excepción del dueño del 2026-10-06). Las rutas faciales son UNA lista,
  `admission.FACE_PREFIXES` (con la ruta completa, `is_face_route`); `slow_threshold_ms` del middleware elige el umbral
  y solo clasifica si la petición pasó el menor. Una ruta nueva que use el motor (`Pipeline`) va a esa lista:
  `tests/test_slow_requests.py` lo exige. La alerta es UNA fila por ruta en
  `ops.slow_request_alerts` (UPSERT por lote; resuelta + vuelve a pasar = se reabre sola; en atención sigue en
  atención). Su muestra (`sample`) lleva método, ruta, *query* con `query_of` (sin secretos), estado, tiempos, BD,
  bytes, rol y empresa: nunca cuerpos ni cabeceras. Un dato nuevo en la muestra pasa por las mismas reglas de
  `error_context`.
- **Lo que manda el navegador** (`POST /api/telemetry/web`, lista `PUBLIC` de la prueba de autorización): un token
  válido solo se verifica (firma y vigencia, sin consultas); sin él solo cuentan `PUBLIC_SCREENS` y
  `PUBLIC_API_PREFIX`. El servidor vuelve a normalizar todo a plantillas y relaciona cada API con su ruta real
  (`route_index`, armado una vez en `app/main.py` desde `API_ROUTERS`). Una métrica nueva del navegador: su valor en
  `WebSampleKind`, su tipo en `PerfKind` y en `KINDS`, y su lectura en `performance_service`.
- **Datos de la plataforma**: las tablas `ops.perf_*` y `ops.slow_request_alerts` no llevan `company_id` ni seguridad
  por fila; solo el código de la plataforma las escribe (`platform_session`) y solo el ADMIN las lee. Toda lectura es
  de un tipo (o de `kind IN (...)`, que recorre la llave primaria) en un rango acotado del grano que le toca al
  periodo; un agregado nuevo se hace en la base (`GROUP BY`), nunca filas en Python.
- **`pg_stat_statements`**: se lee solo con `ops.top_statements` (SECURITY DEFINER, `PLATFORM_FUNCTIONS` de
  `db_roles`), que entrega el texto normalizado de esta base (nunca parámetros).

## 5. Resiliencia y tolerancia a fallas

- **Toda espera tiene tiempo límite** (BD: `statement_timeout`, pool, conexión; colas; descargas;
  WebSocket) y toda falla de una dependencia responde con su código (`DATABASE_UNAVAILABLE`,
  `DATABASE_TIMEOUT`, `FACE_SERVICE_UNAVAILABLE`...), nunca un 500 opaco ni un servicio caído.
- **Degradar en vez de caer**: un dato cifrado ilegible se omite y se registra (`try_decrypt`,
  `readable_embedding`); los catálogos sirven lo anterior si la BD parpadea; lo accesorio (recordar
  la cuenta, aprender del rostro) es de mejor esfuerzo y nunca hace fallar lo principal; cada tarea
  del mantenimiento falla sola. El bucket de imágenes caído nunca deja nada a medias: guardar una
  imagen responde 503 `STORAGE_UNAVAILABLE` reintentable (sin fila ni objeto), la foto del revisor se
  omite (registrado) y descargar un comprobante responde 503; la cola de borrado sigue en otra vuelta.
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
  un error hace falta todo). El mensaje de una falla se guarda como lo recibió la persona, en su idioma (es evidencia;
  el código y la huella no dependen del idioma: un mismo error en es-MX y en en-US es una sola fila). Cada ocurrencia guarda quién (correo y rol), empresa, método, ruta,
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
#    test_envelope, test_fault_tolerance, los presupuestos de test_performance y los guardianes de la regla 21:
#    S608 de ruff, test_sql_safety, test_sql_injection y test_input_guard)
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e COVERAGE_FILE=/tmp/cov -v "$PWD:/app" time-clock-backend-dev \
  sh -c "ruff check . && ruff format --check . && mypy app && python -m pytest -q -p no:cacheprovider --cov=app --cov-branch --cov-report=term-missing:skip-covered"
# 2. PostgreSQL real (también la matriz de inyección con el usuario de la API) + migraciones = modelos +
#    duplicación/ciclos/vulnerabilidades (≈10 min)
../scripts/quality.sh --only=backend --postgres
# 3. Si tocó consultas, índices o una ruta caliente: planes con volumen (§3.1)
perf/db/run.sh            # o solo los casos afectados: perf/db/run.sh attendance
# 4. Si tocó una ruta caliente o la admisión: carga antes y después (entorno aislado)
perf/run.sh 1000 5000
# 5. Si tocó la topología (compose, Nginx, PgBouncer, arranque/apagado, conexiones): réplicas, caídas y presupuesto
perf/scale/run.sh
# 6. Si tocó respaldos, restauración o PITR (db_backup, pitr_monitor, postgres/*.sh, la imagen db): con un bucket FALSO
docker build --target db -t time-clock-db . && docker build --target backup -t time-clock-backend-backup .
scripts/db_pitr_check.sh               # PITR: respaldo base, restaurar a un instante, RPO/RTO, guardián del disco
scripts/db_bucket_restore_check.sh     # copia cifrada del pg_dump: subir, borrar lo local, bajar, restaurar, filas
```

## 8. Seguridad

- Contraseñas con hash; tokens ES256; sesiones validadas en cada petición.
- **Base de datos con mínimo privilegio y aislada por empresa** (§3.2 y §3.4): la API se conecta con un usuario sin
  DDL, sin superusuario y sujeto a la seguridad por fila; solo el código de la plataforma cambia, por transacción,
  al rol con `BYPASSRLS`. El arranque registra como error del sistema si la API se conecta con un rol que se salta
  la política (`row_security_bypassed`). Un respaldo nunca sale del servidor legible (`BACKUP_UPLOAD`: cifrado con
  `DATA_ENCRYPTION_KEY`, por partes).
- **Sin inyección SQL** (regla 21 de la raíz, §3.6): valores siempre como parámetros, identificadores solo de listas
  cerradas con `sql_identifier`, búsquedas literales con `contains_text`, texto con NUL o controles → 422
  `INVALID_CHARACTERS` (nunca un 500) y sus guardianes (`S608`, `test_sql_safety`, `test_sql_injection`).
- Secretos solo en `.env` (nunca en el código ni en el repositorio).
- **Ninguna imagen ni archivo se guarda en la base de datos: van cifrados al bucket; la BD solo guarda la
  referencia** (decisión del dueño del producto; README "Almacenamiento de imágenes"). Todo proceso que
  recibe una imagen o un archivo (hoy la foto de referencia del registro facial, el comprobante de un
  pago, la foto de perfil de una persona, la evidencia de un caso de fraude y los documentos de una empresa) es UNA
  entrada en `STORED_IMAGES` (`app/services/image_storage.py`:
  fila dueña, columnas de la referencia —objeto, tipo, tamaño, SHA-256, `*_uploaded_at`— y ruta solo con ids
  bajo `<GCS_PREFIX>/companies/<empresa>/...`; lo que es de la PERSONA y no de una empresa, con `company=None`,
  bajo `<GCS_PREFIX>/people/users/<cuenta>/...`), nunca un camino aparte. Reglas que no se rodean:
  - **Se sube en la misma petición** con `image_storage.store(db, TIPO, fila, bytes)` después de insertar
    la fila (su id nombra el objeto) y antes del `commit`: cifrado con `DATA_ENCRYPTION_KEY`, "solo si no
    existe" (`if_generation_match=0`), verificado (tamaño, MD5, SHA-256). Si la transacción no se confirma,
    el objeto se borra solo (eventos de la sesión) o queda en la cola de borrado. Sin bucket o con el
    bucket caído: 503 `STORAGE_UNAVAILABLE` y nada a medias; jamás un respaldo en la BD ni en disco local
    (rompería varias instancias).
  - **Nunca se sirve desde el bucket**: ni URLs públicas ni firmadas. Se lee con `image_storage.read`
    (descarga con tiempo límite, verifica el SHA-256, descifra en memoria) y se responde por la API con
    sus permisos.
  - **Lo que se deja de conservar sale del bucket**: `image_storage.forget(db, TIPO, fila)` (p. ej. un
    registro rechazado) o, antes de BORRAR filas con imagen (también en cascada), `image_storage.release`
    (hoy `release_employee_images` al eliminar un empleado; `reject_pending` encola en su misma
    sentencia). Todo en la misma transacción; el mantenimiento (`storage_jobs`) vacía la cola.
  - **Foto de perfil** (`avatar_service`, `avatar_image`, `USER_AVATARS`; README "Foto de perfil"): es de la
    persona (`auth.user_avatars`, una fila por tamaño, y `users.avatar_version`, que viaja con la cuenta: la URL
    versionada sale sin consultas de más). La imagen se procesa en el servidor (formato por su contenido, tope
    de megapíxeles, orientación EXIF, TODOS los metadatos fuera —GPS incluido—, recorte validado, 512 y 96 px
    WebP). Se sube ANTES de abrir la transacción (ninguna conexión espera al bucket; si falla a la mitad,
    `image_storage.abandon` borra lo subido) y la transacción corta bloquea la cuenta, encola la foto anterior
    y reemplaza las referencias. Leer una foto ajena se decide en UNA consulta (`AvatarRepository.visible`): la
    empresa solo ve a sus empleados ACTIVOS y a sus cuentas activas; el ADMIN, solo cuentas que no son de
    empleados (nunca fotos de empleados); cualquier otra, 404 `AVATAR_NOT_FOUND` igual que sin foto. Toda ruta
    que borre cuentas encola antes sus fotos (`release_user_avatars`: empleado sin otro empleo, validador,
    empresa con sus cuentas). Un listado de la empresa que muestre personas agrega `avatar` desde la cuenta ya
    cargada (`avatar_path`), solo de empleados activos; la API de integración y las pantallas del ADMIN no.
  - **Documentos de la empresa** (decisión del dueño, 2026-10-06, migración `0075`; `company_document_service`,
    `document_files`, `COMPANY_DOCUMENTS`; README "Documentos de la empresa"): el ADMIN (pantalla "Empresas",
    `/admin/companies/{id}/documents`) y la empresa (pantalla `COMPANY_DOCUMENTS`, `/documents`) usan EL MISMO servicio
    con su alcance. Reglas que no se rodean: el formato se reconoce por su CONTENIDO (`document_files.inspect`: PDF,
    DOC/DOCX, XLS/XLSX, XML con su declaración, JPG/PNG), sin macros (`vbaProject.bin`, `macroEnabled`, el directorio
    OLE leído según [MS-CFB]), XML con expat SIN DTD ni entidades (nada de `xml.etree`/`minidom` sin esas defensas ni
    una dependencia nueva), imágenes con tope de megapíxeles y sin metadatos (`to_srgb` de `avatar_image`) y el nombre
    limpio con la extensión de su formato real. El objeto se nombra con la empresa y una clave al azar (`uid`): se sube
    ANTES de abrir la transacción y la fila nace con su referencia (NOT NULL); cualquier falla después la deshace
    (`image_storage.abandon`). La descarga va en base64 dentro del contrato (como el comprobante), solo de lo vigente y
    con la lectura de la base cerrada antes de esperar al bucket. Borrado lógico; la empresa elimina y restaura solo lo
    que ella subió (403 `DOCUMENT_UPLOADED_BY_PLATFORM`) y cada documento dice a quien lo lee si puede (`can_delete`).
    La depuración borra la fila y encola su objeto en la MISMA sentencia (`Purge(objects=...)` →
    `delete_batch(... RETURNING)`); los de una empresa que se depura salen antes que ella (su borrado en cascada no
    avisaría al bucket). Una tabla nueva con archivos y borrado lógico sigue este patrón.
  - **No hay camino legado**: las imágenes de antes de esta decisión ya se llevaron al bucket y su
    almacenamiento en la BD (`photo_encrypted`, `payment_receipts`) se borró con la migración `0053`.
    Jamás se agrega una columna o tabla para los bytes de una imagen o un archivo: la única columna
    binaria es la plantilla facial cifrada y `tests/test_image_storage.py` falla con cualquier otra.
  - Sin bucket o sin llave (`GCS_BUCKET`, `GCS_CREDENTIALS_FILE`) queda apagado con un aviso al arrancar.
    La llave vive FUERA del repositorio y docker compose la monta de solo lectura.
- Datos biométricos cifrados. El ADMIN de la plataforma solo consulta la **ficha de trabajo** de los
  empleados, de solo lectura (`CompanyEmployeeRead`: número, nombre, departamento, correo, teléfono,
  estado, estado facial y cuánto aprendió el reconocimiento: solo cuántas muestras y cuándo). Nunca
  RFC, CURP, NSS, fecha de nacimiento, fotos ni plantillas. Única excepción (decisión D1 del dueño): los fotogramas de evidencia de un caso de fraude
  (`GET /admin/fraud-cases/{id}/evidence/{evidence_id}`, descifrados en memoria y registrados en el historial del caso).
- **Documentos opcionales** (decisión del dueño: la plataforma se abre a otros países): RFC, CURP y NSS del
  empleado y el RFC de la empresa. Uno nuevo usa las mismas piezas, sin copiarlas: `optional_document` en su esquema
  (null, vacío o solo separadores = `NULL`, nunca ""; con valor, todas sus reglas), `not_captured` en su validación en
  vivo (`EMPTY` válido, sin consultar) y, al editar, null o vacío lo borra (`OPTIONAL_DOCUMENTS`,
  `_CLEARABLE_FIELDS`). Su único parcial admite varios `NULL` (sin migración) y restaurar solo revisa el que tiene valor.
- **La política de verificación de cada empresa la configura el ADMIN**
  (`/admin/companies/{id}/verification-policy`). La empresa, sus empleados y sus validadores solo la
  leen (`GET /settings/verification`, sin quién la cambió). Una regla nueva de la política se agrega
  ahí (columna, esquema, servicio) y en la pantalla del ADMIN, nunca en una pantalla de la empresa.
- **Módulos por empresa que concede el ADMIN**: hoy Integraciones (`companies.api_enabled`) y Validadores
  (`companies.max_validators` > 0). Se aplican en todos los lugares que tocan el módulo, que no se separan:
  disponibilidad de la pantalla (`navigation_service.AVAILABILITY`), la dependencia del router
  (`require_api_module` / `require_validators_module`), la autenticación de la llave
  (`api_key_service.authenticate`) o su listado (`require_validators_api`) y, para validadores, la validación en
  vivo de su correo (`live_validation`), todos con 403 `API_ACCESS_DISABLED` / `VALIDATORS_DISABLED`. La regla de
  validadores es UNA función (`validator_service.validators_module`, sobre `Company.validators_enabled`).
- **Validadores por empresa** (decisión del dueño, migración `0067`; README "Validadores por empresa"):
  `max_validators` son los validadores ACTIVOS permitidos (0 por omisión: el módulo es opcional y cuesta). Dar de
  alta o activar uno revisa el límite con la empresa bloqueada (`BillingRepository.lock_company`, vía `Ledger.lock`:
  409 `VALIDATOR_LIMIT_REACHED` sin carreras) y el ADMIN nunca baja el límite de los activos (409
  `VALIDATOR_LIMIT_BELOW_ACTIVE`, con el mismo candado): ningún validador se queda sin servicio, así que su inicio
  de sesión e identificaciones no llevan otra regla. Los activos se cuentan por sus cuentas (`auth.users`, rol
  VALIDATOR, índice `(company_id, role)`). Un cambio nuevo del estado de un validador escribe su evento
  (`BillingRepository.record_validator_status`) en la misma transacción.
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
  - **Registro con muchas fotos** (decisión del dueño, 2026-10-06; `services/enrollment_selection.py`): el registro
    (propio o en persona) recibe hasta `FACE_ENROLL_MAX_PHOTOS` (36) fotos completas y el SERVIDOR elige la plantilla.
    Reglas que se conservan: cada foto pasa las comprobaciones reales salvo CLIP (lo que no cumple se DESCARTA con su
    motivo; un motivo de `SECURITY_REASONS` rechaza el intento como `SuspiciousCapture`: un engaño nunca se descarta en
    silencio); los fotogramas repetidos cuentan una vez; con menos de `FACE_ENROLL_MIN_USABLE` útiles, 422 con el motivo
    de descarte más frecuente; UNA sola persona (cada útil contra el medoide y las referencias entre sí con
    `FACE_ENROLL_CONSISTENCY_THRESHOLD`; con `similarity_matrix`, nunca un ciclo de pares); las referencias por calidad,
    sin casi idénticas (`FACE_LEARNING_REDUNDANCY`), hasta `FACE_MAX_SAMPLES_PER_EMPLOYEE` y en el ORDEN DE LA TOMA (la
    continuidad usa la última); CLIP solo en ellas (`FacePipeline.accessories_of`) y accesorios por mayoría entre ellas;
    la suplantación por la MAYORÍA de todas las útiles o por la regla de la empresa sobre las referencias (el «una sola
    basta» del nivel Máximo nunca sobre las 36: marcaría a casi la mitad de las personas reales). Solo UNA foto (la mejor de las referencias) va al bucket
    (`STORED_IMAGES`); las demás nunca se guardan (BD, bucket ni logs). La toma única (`confirm_live(others=...)` →
    `inspect_take` → `ensure_not_replayed`) recuerda las huellas de TODAS las útiles; las fotos fijas y la continuidad
    se revisan solo en las referencias (36 fotos de una persona quieta pueden parecer "fijas" entre sí). Las referencias
    se guardan en una sola inserción (`FaceEmbeddingRepository.add_all`): el registro cuesta lo mismo con 3 que con 36.
  - **Consenso de la ráfaga** (decisión del dueño, 2026-10-06; `identity_core.burst_rejects`): SOLO ENDURECE. La
    ráfaga (36 recortes de 160 px) trae los embeddings de sus mejores recortes quietos (`BurstAnalysis.identity`,
    `FACE_CONSENSUS_FRAMES`); su consenso es la MEDIANA de la mejor similitud de cada uno contra las muestras de la
    persona y, si es medible (`FACE_CONSENSUS_MIN_FRAMES`), debe llegar a lo exigido menos `FACE_CONSENSUS_MARGIN`; si
    no, el intento es `NO_MATCH` aunque las frontales coincidan. La decisión de siempre (CADA frontal con
    `required_match`, pisos por modelo incluidos) no cambia; sin ráfaga o sin medirla, se decide como antes. Aplica en
    `match_one` (1:1, asistencia, en persona, QR + rostro) y en el 1:N del validador (contra las filas del ganador con
    `required_match(among_all=True)`); el valor queda en `face_attempt_metrics.burst_consensus` (migración `0073`). No es
    una señal del motor de riesgo: es parte de la coincidencia.
- **Antifraude de identidad** (`docs/rd/antifraude-identidad.md`; fases 0 y 1, migración `0062`):
  - **Motor de riesgo** (`risk_rules`: reglas puras; `risk_engine`: señales y decisión; `risk_simulation`): corre
    DESPUÉS de los candados y de la coincidencia (`identity_core.enforce_risk`), suma los puntos de cada señal
    obligatoria con tope por familia (`RISK_FAMILY_MAX_POINTS`: una sola señal nunca niega por sí misma), decide el
    nivel con los cortes de la política y su acción (permitir, avisar, un paso más, en revisión, negar). Una regla dura
    obligatoria (reenvío perceptual, ataque conocido) niega sola. Cada señal se enciende por empresa en
    `verification_policy.risk_signals` sobre su valor de la plataforma (`catalog.risk_signals`): apagada, solo medir
    (se guarda sin puntos) u obligatoria. Toda señal nueva nace en `OBSERVE` (sin rechazos de más hasta calibrarla).
    Si el motor falla, aplica `risk_fallback_action` (solo permitir, avisar o un paso más; nunca negar a ciegas) y lo
    registra. Su lectura va en un SAVEPOINT: nunca deshace lo demás del intento.
  - **Acciones** (decisión D3): un paso más = 422 `STEP_UP_REQUIRED` con el reto nuevo en `details.challenge` (máximo
    de movimientos y destello obligatorio; sin prueba de vida, se registra "en revisión"); en revisión = la asistencia
    se guarda y su jornada queda `review_status = PENDING` (la empresa confirma o rechaza con nota, `AttendanceReview`);
    negar = `SuspiciousCapture` `RISK_DENIED`. Cada decisión queda en `ops.risk_assessments` enlazada a su bitácora y a
    sus métricas (D7).
  - **Casos de fraude** (`fraud_cases`: abrirlos y su evidencia; `fraud_case_service`: la revisión del ADMIN, D10): un
    caso activo por sujeto (inserción atómica con índice único parcial); la evidencia (D1) se sube DESPUÉS de confirmar
    el intento (fuera de su transacción) por `STORED_IMAGES` (`FRAUD_EVIDENCE`) y se purga a los
    `FRAUD_EVIDENCE_RETENTION_DAYS`. Confirmar bloquea las huellas pHash (`attack_signatures`; de la plataforma si se
    confirman en `ATTACK_SIGNATURE_PLATFORM_COMPANIES` empresas, D6: solo hashes), etiqueta sus intentos (la
    calibración deja de contarlos como personas reales) y olvida lo aprendido desde el intento; falso positivo libera
    las huellas y suma a la línea base de sus señales (`risk_signal_stats`). Ver la evidencia es la excepción
    documentada a "el ADMIN no ve biometría" (regla 13 de la raíz) y cada consulta queda en el historial.
  - **Gobierno de la política** (`policy_rules`: qué relaja; `policy_governance`, D12): todo cambio deja su historial
    (`ops.policy_changes`, antes → después). Lo que endurece aplica al momento; lo que RELAJA queda `PENDING` hasta que
    otro ADMIN lo apruebe (`POLICY_TWO_PERSON_RULE`; vence a las `POLICY_CHANGE_APPROVAL_HOURS` y se cancela si la
    política cambió desde que se pidió). Los niveles Estándar/Alto/Máximo (`PRESETS`) pasan por el mismo camino:
    Máximo exige el protocolo de captura (`FLASH_UNPACED`, `FLASH_PACE_TIMING`, `FLASH_PACE_MISMATCH`, `BURST_MISSING`)
    y las pruebas de presencia (`validator_signing`, `validator_location`, `site_codes` en `ENFORCE`); Alto y Estándar
    las dejan en `OBSERVE` (decisión del dueño, 2026-10-06; descripciones del catálogo en la migración `0072`). Un
    cambio del motor lleva su simulación sobre los últimos `RISK_SIMULATION_DAYS` días. La política se bloquea
    (`FOR UPDATE`) al cambiarla: dos ADMIN nunca se pisan.
  - **Fase 0**: el destello mide además el cociente rostro/fondo (`FLASH_RATIO`, autocalibrado: solo endurece);
    reenvío perceptual (pHash ≤ `FACE_PHASH_MAX_DISTANCE` y embedding ≥ `FACE_PERCEPTUAL_MIN_COSINE` contra las
    huellas recientes del empleado, `biometrics.capture_traces`); "acercarse" nunca es el único movimiento; una
    cámara sin nombre es una señal (no un rechazo); sospecha de duplicado al registrarse con un nivel más sensible
    (`duplicate_confidence`: solo marca `POSSIBLE_DUPLICATE` con los más parecidos, nunca bloquea) y la asistencia con
    el QR solo apagada en las empresas nuevas (`qr_only_attendance`, D4).
  - **Fase 1b** (migración `0065`, motor `1.1.0`; README "Fase 1b"): dispositivo del empleado (`employee_devices`,
    D2), red de la IP con la base local DB-IP Lite (`core/ip_intel.py`, `services/ip_database.py`, D8), varias lecturas
    de la ubicación (`location_samples`), 1:N en cada 1:1 (`face_gallery.rival_of`) y telemetría del navegador
    (`schemas/capture.py`, `services/client_evidence.py`) más la tabla JPEG (`facial_recognition/jpeg_tables.py`).
    Reglas que se conservan al agregar señales:
    - **Lo nuevo nunca niega** (decisión del dueño): las señales de `risk_rules.ASK_ONLY_SIGNALS` (la telemetría del
      navegador por diseño, R&D §2.5, y las que se calibran) pueden subir el nivel, pero si sin ellas el intento no se
      negaría, con ellas queda como mucho "en revisión" (`ask_only`). Una señal nueva de ese tipo entra a esa lista;
      sacarla de `CALIBRATING_SIGNALS` es una decisión del dueño con los datos del plan de calibración (R&D §7.4).
    - **El modo del dispositivo es un piso** (`device_floor`): un paso más (`STEP_UP`) o "en revisión" (`APPROVAL`)
      para un dispositivo que no es de confianza, aunque el motor esté apagado o la señal en `OFF`; nunca niega. Su
      motivo de revisión es `DEVICE`. La prueba de posesión reutiliza el reto HMAC de los validadores
      (`device_service.issue_nonce`, ligado a la cuenta) y solo se guarda el hash de la llave.
    - **La evidencia del cliente se valida estricta y nunca rompe el intento**: telemetría con `extra="forbid"` y tope
      `CAPTURE_TELEMETRY_MAX_BYTES` (mal formada = `TELEMETRY_MISSING`, nunca un 422); una firma inválida =
      `DEVICE_KEY_MISSING`; sin base de IP, las señales de red no se miden. Solo las lecturas de ubicación mal formadas
      responden 422 `LOCATION_SAMPLES_INVALID` (son datos del registro, como la ubicación misma).
    - **Costo por intento acotado**: el dispositivo cuesta 2 consultas (lectura por el prefijo `(company_id,
      key_hash)` del único y un upsert en la transacción del intento); la red, 0 (en memoria); el 1:N, 0 consultas
      mientras la galería está fresca y solo CPU después de cerrar la transacción. `LOCATION_JUMP` y `NETWORK_JUMP`
      reutilizan la MISMA lectura del registro anterior que el viaje imposible.
  - **Fase 2a** (migración `0066`, motor `1.2.0`; README "Fase 2a"): protocolo de captura de frontera.
    - **Destello dictado por el servidor** (`services/flash_pacing.py`, política `flash_paced`): el reto no trae sus
      colores; se revelan uno por uno por el canal en vivo (`routers/realtime.py`, mensaje `flash`) y cada captura se
      compromete con su SHA-256 dentro de `FACE_FLASH_PACE_WINDOW_MS`. El estado viaja SELLADO en un token (Fernet con una
      llave derivada de `DATA_ENCRYPTION_KEY`, también las anteriores): ni consultas ni estado en memoria (N réplicas,
      PgBouncer, sin `LISTEN/NOTIFY`); cada color se elige al revelarse. Sin canal, `POST /face/challenge/flash` da los
      colores de respaldo: `FLASH_UNPACED`, nunca un rechazo. Un mensaje nuevo del canal que no toca la BD no usa
      `_validation_slots` ni el threadpool.
    - **Ráfaga** (`facial_recognition/burst.py`, `services/capture_protocol.py`, política `capture_burst`): lo que se pide
      sale de la configuración (`burst_spec`), la descripción se valida ESTRICTA (`burst_layout`) y la hoja se analiza EN
      PARALELO (decisión del dueño, 2026-10-06; `BurstMeasure`): en cuanto empieza la prueba de vida va a otro worker
      LIBRE (`facial_recognition.run_on_spare` → `WorkerPool.try_acquire`: solo si nadie espera turno; nunca se adelanta
      a la fila ni espera) y se recoge al final con tope `FACE_BURST_WAIT_SECONDS`; sin uno libre se mide al final con
      el worker de la petición, como antes. Corre en un hilo del tamaño del pool con la copia del contexto, solo CPU y
      sin transacción abierta; un intento que termina antes no la recoge (el repuesto vuelve solo). Otra parte del
      análisis que valga la pena paralelizar usa `run_on_spare`, nunca un pool o hilo propio. Mal formada = `BURST_MISSING` (valor 1), nunca
      413/422/500; una falla del motor la deja sin medir (registrada). **La hoja NUNCA se guarda** (ni BD ni bucket): la
      única excepción es la evidencia D1 por `STORED_IMAGES` (kind `BURST`, en su propio lugar del intento).
    - **Señales físicas** de las frontales a resolución completa (`FaceAnalysis.moire`, `noise_ratio`) y el paralaje de
      los giros (`landmarks`): todas en `CALIBRATING_SIGNALS`. El pulso (`PULSE_ABSENT`) es `MEASURE_ONLY_SIGNALS`: se
      registra, nunca suma y no se puede exigir (422 `SIGNAL_MEASURE_ONLY`; `measure_only` en la lectura del ADMIN). Una
      señal nueva del protocolo: su `Hit` en `capture_protocol.protocol_hits` (pura, sin consultas) y, si se autocalibra,
      su columna en `face_attempt_metrics` y su `Signal` en `face_security.SIGNALS` (un máximo con `upper=True`: parte de
      su tope y solo baja).
  - **Fase 2b** (migración `0070`, motor `1.2.0` sin cambios de reglas; README "Fase 2b"): presencia del validador y código
    de sitio (D9). Reglas que se conservan:
    - **Cada prueba de presencia tiene su modo en la política** (`validator_signing`, `validator_location`, `site_codes`;
      modos de `signal_modes`, por omisión `OBSERVE`): «Solo medir» = señales del motor de riesgo; obligatoria = 403/409
      con su código ANTES de consumir el reto, el QR o la prueba de vida (se reintenta con lo mismo). Bajar de modo relaja
      (regla de dos personas). No se agregó un catálogo nuevo: la `0064` carga las traducciones de TODOS los catálogos del
      archivo vigente y una tabla de catálogo nueva rompería una base nueva en esa migración (un catálogo nuevo necesita
      otra estrategia: crear su tabla en la misma migración que carga sus traducciones no basta).
    - **Firma por petición** (`request_signing`): la sesión del validador queda ligada a la llave probada al iniciar sesión
      (`auth_sessions.device_key_hash`; la carga la autenticación: `User.session_device_key`, cero consultas por
      identificación). Cada identificación firma `"{reto}.{acción}.{SHA-256 del contenido}"` (`face`: la primera
      captura; `qr`/`inspect`: el texto del QR); el reto es el HMAC sin estado de `device_service` y viaja en cada
      respuesta (`device_nonce`). Una sesión sin llave se liga a la primera que firma (con aprobación de dispositivos,
      solo a una aprobada; una sentencia atómica). Una firma alterada es regla dura (`VALIDATOR_SIGNATURE_INVALID`),
      también nacida en «Solo medir».
    - **Ubicación en cada identificación** (`validator_presence`): la MISMA regla del inicio de sesión
      (`location_service.check`) y, con la ubicación, las señales del lugar de la 1b (`location_context`).
    - **Código de sitio** (`site_codes`, `kiosk_service`): TOTP con HMAC-SHA256 y el secreto del sitio CIFRADO (texto
      Fernet; nunca columna binaria ni un log); vale el periodo actual, `SITE_CODE_GRACE_WINDOWS` anteriores y el
      siguiente; un solo uso por empleado y periodo con `attendance_events.presence_window` comparado con el registro
      anterior que ya se lee para el viaje imposible (cero consultas). Solo la entrada y la salida EN un sitio que lo activó.
    - **Kiosco** (`workforce.site_kiosks`, borrado lógico): no es una cuenta ni un validador (no se cobra); su credencial es
      la llave no exportable de su tableta, vinculada con un código de un solo uso (solo su SHA-256) y probada con una
      firma en cada petición pública (`/kiosk/pair`, `/kiosk/code`: lista `PUBLIC`, limitadas por IP). Se busca como la
      llave de integración: plataforma por su id o hash y después solo su empresa.
    - Sin rostro no hay motor: una identificación solo con QR mide la firma y la ubicación en el log (INFO) y las exige si
      son obligatorias.
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
  - Una señal nueva se agrega a `SIGNALS` con su columna, piso y tope (y su prueba de "solo endurece"). Un umbral que es
    un MÁXIMO (el moiré) lleva `upper=True`: su valor de partida es el tope y endurecer es bajarlo hasta su piso.
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
  - **El turno dice DÓNDE y CUÁNDO se checa** (decisión del dueño del producto, migración `0048`):
    horario, días, descansos y tolerancias, sus sitios (`shift_sites`) y sus días remotos
    (`shifts.remote_weekdays`). **Una asignación es solo empleado + turno + `valid_from` (+ `valid_to`)**:
    ni la asignación ni la aprobación de un cambio llevan lugar. Reglas del lugar en un solo sitio
    (`ShiftService._place`): días remotos dentro de los del turno (`REMOTE_DAY_OUTSIDE_SHIFT`, también
    CHECK `ck_shifts_remote_weekdays`), al menos un sitio si algún día no es remoto (`SITE_REQUIRED`: un
    CHECK no ve otra tabla) y un sitio nuevo en el turno activo (`SITE_NOT_AVAILABLE`; uno ya incluido que
    luego se desactivó puede quedarse). Editar el turno aplica desde ese momento a todos los que lo
    tienen; quien lee el lugar (asistencia `_place` y `today`) lo toma del turno de la jornada
    (`_Slot.shift` o `ShiftRepository.shift_of`) y los sitios de varios turnos se leen por lotes
    (`sites_of_shifts`). Un sitio que un turno usa no se borra (409 `SITE_IN_USE` con los turnos), ni uno
    donde ya se checó (409 `SITE_HAS_RECORDS`).
  - Cada jornada guarda una **copia de lo programado** al entrar (turno, horas, descansos,
    tolerancias) y el sitio donde se checó: editar un turno (también sus sitios o días remotos) o
    cambiar la asignación nunca altera lo ya registrado.
  - Un cambio de turno es una asignación nueva desde `today + ASSIGNMENT_NOTICE_DAYS` (la vigente
    termina el día anterior, `close_before`); la primera puede empezar hoy.
  - Todo registro pasa por `identity_core` (rostro) y por `_place` (geocerca de un sitio activo de su
    turno o día remoto de su turno, precisión de la política, viaje imposible). Lo que se registra va en la misma transacción, con el empleado
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
- **Cobranza** (solo el ADMIN; pantallas `ADMIN_BILLING` y, para el plan, `ADMIN_COMPANIES`):
  - Reglas puras en `billing_rules` (cortes, prorrateo por día, demo, descuentos, IVA, reparto de pagos,
    vencimientos) y su composición en `billing_calc` (qué días y qué plantilla recibe cada regla; vista
    previa y estimación). Ninguna regla se repite fuera de ahí.
  - Dinero siempre en `Decimal` (`NUMERIC` en la base), redondeado a los decimales de SU moneda al cerrar
    cada importe (`money(valor, decimales)`; los decimales salen del catálogo `currencies` con
    `currency_decimals`, nunca escritos en el código); en la API sale con dos decimales (`Money`). Precios
    sin IVA.
  - **Monedas** (MXN, USD, EUR; decisión del dueño del producto, migración `0060`): cada empresa se cobra en
    la moneda de su plan y todo su dinero está en ella; cargos y pagos guardan su `currency` y toda respuesta
    con dinero dice su moneda. Reglas que no se rodean:
    - La moneda del plan se cambia solo mientras la empresa no tiene cargos ni pagos (422 `CURRENCY_LOCKED`,
      `BillingService._check_currency`, con la empresa bloqueada); una nueva debe estar activa en el catálogo
      (422 `CURRENCY_INVALID`).
    - Un pago va en la moneda de la empresa (422 `CURRENCY_MISMATCH`) y `Ledger.settle` solo aplica un pago a
      cargos de su misma moneda. Un monto que no cabe en los decimales de la moneda: 422 `AMOUNT_DECIMALS`.
    - Lo de toda la plataforma se agrupa POR MONEDA (`GROUP BY currency`): nunca se suman importes de monedas
      distintas ni se convierten (no hay tipo de cambio). Una consulta nueva de dinero de varias empresas
      agrupa por moneda o filtra por una.
    - El IVA es de cada empresa e independiente de la moneda (un cliente del extranjero suele llevar 0 %).
  - Movimientos de dinero solo en `billing_ledger`, con la empresa bloqueada (`lock_company`, `FOR
    UPDATE`): emitir el cargo de un corte (único por empresa y corte, repetible), aplicar pagos y saldo a
    favor (el cargo abierto más antiguo primero), anular un pago o un cargo (con motivo; nada se borra) y
    suspender o reactivar. `paid`/`applied` = suma de `payment_allocations`, actualizados en la misma
    transacción. Cargos y pagos tienen FK `RESTRICT`: una empresa con movimientos no se elimina (409
    `COMPANY_HAS_BILLING`). Quién lo hizo se guarda como el correo literal.
  - La plantilla por día (`billing.headcount_days`) sale SOLO de `workforce.employee_status_events` y
    `workforce.validator_status_events`, que `EmployeeService` y `ValidatorService` escriben en la misma transacción
    del alta, la (des)activación o la eliminación de un empleado o de un validador. Un cambio nuevo del estado de un
    empleo o de un validador debe escribir su evento. **Un validador activo cuenta como un empleado** (decisión del
    dueño): `close_headcount` cuenta los dos en UNA sentencia (columnas `active_employees` y `active_validators`, para
    el desglose) y el cobro por empleado activo, la vista previa (`employees` + `validators`) y la estimación los suman
    (`Headcount.total`); el monto fijo no cambia. Sus unidades son **días-persona** y cada cargo, línea, vista previa y
    estimación lleva cuántos fueron de validadores (`validator_units`; `charges`/`charge_lines` desde la migración
    `0072`, nulo en el monto fijo y en lo emitido antes): solo el desglose que se muestra, nunca cambia el importe
    (`line_for` recibe los validadores de cada día aparte de lo que se cobra).
  - El mantenimiento (`billing_jobs`, cada tarea falla sola) cierra la plantilla (y repone días que
    faltan), emite cargos, suspende por falta de pago, refresca pronósticos y toma la foto del
    almacenamiento; las tareas diarias se marcan en `ops.daily_tasks`.
- **Empresa suspendida** (`companies.suspended_at`, junto a `active`: la autenticación ya la carga, sin
  consultas de más): `ensure_account_usable` responde 403 `COMPANY_SUSPENDED` al iniciar sesión, renovar,
  elegir empresa y en cada petición; la llave de la API también (`api_key_service.authenticate`).
  Suspender cierra en la misma transacción todas sus sesiones (`SessionService.close_company`, motivo
  `COMPANY_SUSPENDED`: la siguiente petición responde 401 con ese código). Un empleado de otra empresa
  activa sigue entrando a esa (`usable_employees`). Pagar lo vencido reactiva una suspensión por falta
  de pago; la manual solo la levanta el ADMIN (y da otra gracia completa, `plans.grace_until`).
- Toda autenticación por token (HTTP y canal WebSocket) usa `SessionService.authenticate_access`:
  las mismas reglas en ambos canales.
- Reglas del inicio de sesión por cuenta (dispositivo permitido, ubicación del validador) se aplican
  en `POST /auth/login` antes de crear la sesión (`ensure_device_allowed`, `ensure_device_authorized`,
  `ensure_location_allowed`). La llave que probó el dispositivo queda en la sesión (firma por petición, fase 2b); con la
  firma obligatoria se pide aunque la empresa no apruebe dispositivos.
  Cambiar una regla que la sesión abierta ya no cumple cierra sus sesiones con su motivo de catálogo.

## 9. Documentación

- Docstring en cada módulo, clase y función pública explicando el **porqué**.
- Cambios de API o de arquitectura se reflejan en el `README.md` de la raíz (tabla de endpoints).

## 10. Configuración (`.env` completo; regla 19 de la raíz)

- **`.env` tiene TODAS las variables con su valor** (decisión del dueño del producto). La única definición es
  `app/core/config.py` (tipo, valor por defecto, límites con `Field(ge/gt/le/lt)` o `Literal`, y su
  comentario: arriba del campo o al final de la línea; un grupo de líneas seguidas comparte el del primero).
  Ningún módulo lee `os.environ`; ningún valor ajustable (tiempo, límite, tamaño de caché o de lote, URL,
  puerto, retención, intervalo) queda como constante suelta en el código: va a `Settings`. Lo que es una
  regla de negocio de cada empresa vive en la BD, no aquí.
- **Una variable nueva, en el mismo cambio**: su campo comentado en `config.py`, su línea en `.env` (con el
  valor por defecto) y su fila en el README (§7). `scripts/generate_secrets.py` la toma sola de `config.py`
  (lo lee como texto, sin importarlo). Una variable que solo lee un script de arranque (`entrypoint.sh`,
  `pgbouncer/entrypoint.sh`) va con `${VAR:-valor}` en el script y en `RUNTIME` del generador con el mismo
  valor; si la fija docker compose según la topología, en `TOPOLOGY`.
- `tests/test_env_files.py` es el guardián (corre sin `.env` en CI): el generador escribe cada campo y cada
  variable de los scripts una vez y nada más, con el valor del código y con comentario; el `.env` local
  tiene exactamente esas variables (sus mensajes solo nombran variables, nunca un valor).
- Cambiar un valor por defecto en `config.py` no cambia una instalación existente (su `.env` ya lo tiene):
  se cambia también en el `.env` de cada entorno que deba tomarlo.
- **Rutas de archivos relativas a la API**: un archivo que lee la API (p. ej. la base local de IP,
  `IP_COUNTRY_DB_PATH`/`IP_ASN_DB_PATH`) se configura relativo a la carpeta del proyecto (`_api_path`) para que la
  misma línea sirva en docker (`/app/...`) y en desarrollo. Datos descargados (la base de IP) nunca van al
  repositorio ni a la imagen: `.gitignore` y `.dockerignore` (`data/ipdb/`).

## 11. Idiomas: es-MX y en-US (regla 16 de la raíz)

Decisión del dueño del producto: todo lo que una persona lee sale en el idioma de la petición. El código vive en
`app/i18n/`; cifras y diseño en el README, "Idiomas en el backend".

### 11.1 Cómo funciona
- **El idioma de la petición** lo resuelve UNA vez el middleware del traceId (`negotiate`: `Accept-Language` con sus pesos;
  `es*` → es-MX, `en*` → en-US, lo demás o nada → es-MX; el canal en vivo, `?lang=` y si no la cabecera) y lo deja en una
  `ContextVar`: `current_locale()` en cualquier capa, también en los hilos del threadpool. Fuera de una petición
  (mantenimiento, arranque, CLI) rige es-MX. Las respuestas JSON llevan `Content-Language` y `Vary: Accept-Language`.
- **Catálogo de mensajes** (`app/i18n/messages/es_mx/<área>.py` y `en_us/<área>.py`, las mismas áreas y llaves): llave
  estable → texto, o sus formas de plural (`one`, `other` y, si el cero lleva otro texto, `zero`). Los datos van como
  `{parámetros}`; `t(llave, params)` arma el texto en el idioma vigente y `Text(llave, params)` es el mismo texto diferido
  (se traduce al leerse, p. ej. dentro de otro mensaje). Los parámetros se escriben como se acostumbra en cada idioma:
  `date` (05/10/2026 · 10/05/2026), `DayMonth`, `time` ya en la zona del negocio (14:05 · 2:05 PM), `Megabytes` (regla
  17), una lista (`["A", "B"]` → «A y B» · «A and B»), un `Text`; `count` elige el plural y lleva separador de miles.
- **Errores**: `raise NotFoundError(code="EMPLOYEE_NOT_FOUND")`; con datos, `params={...}`; si el mismo código tiene
  varias frases, `key="OTRA_LLAVE"` (el código no cambia). El mensaje se arma al responder (`exc.message`). El argumento
  `message` solo recibe un texto YA traducido (de un catálogo de la BD, p. ej. `get_catalogs().face_error_message(...)`,
  o de `t()`), nunca uno escrito a mano. `ok(data, code="X", params=...)` igual.
- **Validación de Pydantic (422)**: cada tipo de error tiene su mensaje (`app/core/validation_errors.py`, llaves
  `INPUT_*`); una regla propia lanza `LocalizedValueError("LLAVE", params)` (es un `ValueError`: `str(error)` también sale
  traducido, p. ej. en la validación en vivo). El código del error no cambia (`VALUE_ERROR`, `MISSING`...).
- **Catálogos de la BD**: el español en las columnas de cada catálogo (`alembic/seed/catalogs.json`); los demás idiomas
  en `catalog.translations` (`alembic/seed/catalogs.<idioma>.json`, migración `0064`). `get_catalogs()` entrega la
  instantánea del idioma de la petición (una por idioma en la misma caché: cero consultas por petición).
- **Textos que el sistema guarda** para que alguien los lea después: `stored("LLAVE", params)` en la columna y
  `StoredText` (o `read_stored`) en el esquema de lectura; se traducen al leerse, en el idioma de quien lee. Lo que
  escribe una persona es un dato y se muestra tal cual.

### 11.2 Agregar un mensaje (en el mismo cambio, sin excepción)
1. Llave en MAYÚSCULAS y estable (el código del error o de la respuesta si es uno a uno; si no, una llave propia que diga
   qué es). Nunca se reutiliza una llave para otro sentido ni se cambia la de un mensaje que ya existe.
2. Su texto en `app/i18n/messages/es_mx/<área>.py` y en `app/i18n/messages/en_us/<área>.py` (misma área, mismos
   `{parámetros}`, mismas formas de plural), en orden alfabético. Inglés natural de Estados Unidos con los mismos
   términos que la aplicación web (`webapp-employee-time-clock/src/i18n/locales/en-US/`: *sign in*, *check in*,
   *workday*, *site*, *validator*, *face enrollment*, *liveness check*, *leave*, *sick leave*...).
3. En el código: `code=`/`key=`/`params=` (o `t()`/`Text()`/`LocalizedValueError`); nada de f-strings ni concatenar
   texto. Un plural, con `params={"count": n}` (nunca "empleado(s)").
4. `tests/test_i18n.py` falla si los idiomas difieren, si una llave que el código nombra no existe, si una sobra o si un
   error o `ok(...)` lleva un texto escrito a mano; las pruebas corren en modo estricto (`strict(True)`): una llave o un
   parámetro faltante hace fallar la prueba que lo arma (en producción se registra como error del sistema).
5. Ortografía: `./scripts/quality.sh --only=backend` corre cspell sobre los dos idiomas (§11.4).

### 11.3 Agregar un texto de un catálogo (registro nuevo o columna de texto nueva)
1. El registro con su texto en español en `alembic/seed/catalogs.json` y su traducción en
   `alembic/seed/catalogs.en-US.json` (`{catálogo: {código: {columna: texto}}}`): cada columna de texto con valor
   (`name`, `description`, `message`, `phrase`, `instruction`, `employee_note`, `short_name`) lleva la suya, con los
   mismos `{marcadores}` y sin pasar del largo de su columna.
2. La migración nueva que agrega el registro inserta también su fila en `catalog.translations` (la `0064` solo cargó los
   registros que existían entonces). Cambiar un texto en inglés = migración que actualiza su fila. Un **catálogo nuevo**
   (una tabla que crea una migración, p. ej. `tax_id_types` en `0074`) se puede agregar: su migración crea la tabla y
   carga sus registros y sus traducciones; `0064` salta las tablas que aún no existen en una base nueva (`to_regclass`,
   corregida con la `0074`).
3. Una columna de texto nueva: su nombre en `TRANSLATED_FIELDS` (`app/models/catalog.py`) y en el CHECK `field` de
   `catalog.translations` (migración). Un idioma nuevo: su valor en `LOCALES` (`app/i18n/locale.py`),
   `TRANSLATION_LOCALES` y el CHECK `locale`, su paquete de mensajes, su archivo de catálogos y su configuración de cspell.
4. `tests/test_i18n.py` verifica que cada texto de cada registro tenga su traducción (y ninguna sobre);
   `./scripts/quality.sh --postgres` compara los registros de la base migrada con los de los modelos.

### 11.4 Ortografía (cspell)
- `backend-employee-time-clock/cspell.json` revisa el catálogo de mensajes y los textos de los dos archivos de catálogos:
  es-MX solo contra el diccionario de español y en-US solo contra el de inglés de Estados Unidos. Usa la instalación de
  cspell, el diccionario de español y las listas de palabras revisadas de la aplicación web
  (`webapp-employee-time-clock/cspell-words*.txt`: comunes, solo español, solo inglés); no se duplican. Revisa solo los
  textos (no llaves, códigos, `{marcadores}`, comentarios, docstrings, rutas, `identificadores` entre comillas
  invertidas ni ejemplos de documentos como `PEGJ900515AB1`).
- Correrlo: `./scripts/quality.sh --only=backend` (sección "ortografía"), o directo desde
  `backend-employee-time-clock/`: `../webapp-employee-time-clock/node_modules/.bin/cspell --no-progress`.
- Una palabra desconocida se corrige; solo si está bien escrita se agrega a la lista de su idioma (o a la común si es
  igual en los dos: nombres propios, siglas, marcas), con un comentario de por qué.

### 11.5 Estilo de los textos (mensajes simples pero profesionales)
Decisión del dueño del producto: los `message` y `errors[].message` que la app muestra en sus popups siguen el mismo
estilo que la aplicación web (`webapp-employee-time-clock/AGENTS.md` §7.6), en los dos idiomas:
- **Error**: qué pasó y qué hacer, una frase corta cada uno: «Tu sesión ya no es válida. Inicia sesión de nuevo.».
  «No se pudo…» (nunca «No fue posible», «No pudimos»), «Intenta de nuevo.» (nunca «nuevamente»); en inglés,
  "Couldn't …", "Try again." (sin "Please").
- **Éxito** (`ok(...)`): solo el resultado, sin relleno: «Sesión iniciada», «Foto de perfil guardada» (nunca
  «correctamente», «exitosamente», «Se ha…»).
- **Validación**: qué corregir, con su límite o formato: «Cada descanso debe durar al menos {count} minutos».
- **Tono**: de «tú», sin signos de exclamación, disculpas ni primera persona del plural; mismo vocabulario que la app
  (*check in*, *workday*, *site*, *face enrollment*, *liveness check*). El inglés es natural, no una traducción literal.
- Una frase sin punto final; dos frases, cada una con su punto (la convención de cada archivo).
- No cambian por estilo: llaves, `{parámetros}`, formas de plural, códigos ni el sentido de una regla. Los textos de
  los catálogos de la BD siguen la misma guía; cambiarlos requiere una migración que actualice sus filas y las de
  `catalog.translations` (§11.3).
