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
- **Tablas que crecen sin límite** (bitácora): orden por `id`, conteos con tope (`count_cap` de
  `paginate`) y, para sincronizar, cursor en lugar de `OFFSET` (`/integrations/v1/attendance/feed`).
- Una restricción única que choca entre dos peticiones simultáneas responde 409
  `CONCURRENT_UPDATE` (manejador global); no hace falta capturarla en cada servicio, salvo para
  dar un código propio (p. ej. `EMAIL_TAKEN`).
- UPDATE/DELETE masivos con `affected_rows` (`aggregates.py`): una sentencia para toda la empresa
  (p. ej. nueva verificación a todos), nunca un ciclo por fila.
- Cerrar sesiones solo con `SessionService` (`revoke_all(..., commit=False)` dentro de la
  transacción del cambio que las invalida; `close_company` para toda una empresa).

## 4. Rendimiento y escalabilidad

- **Sin estado que impida varias instancias**: lo compartido vive en PostgreSQL. Una caché local
  (catálogos, política, galería facial) se valida contra la BD (TTL corto o huella) y se acota
  (LRU por memoria o cantidad).
- **Control de admisión adaptativo** (`app/core/admission.py`): el límite de peticiones a la vez se
  ajusta solo a la latencia real; la fila atiende primero lo crítico (iniciar sesión, identificar,
  verificar, QR) y, dentro de cada nivel, lo más demandado; al saturarse se descarta primero lo
  menos importante con 503 `SERVER_BUSY` reintentable. Una ruta crítica nueva se agrega a
  `CRITICAL_PREFIXES`; un tablero o reporte, a `BACKGROUND_PREFIXES`. Solo las respuestas exitosas
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

- **Todo error se registra** en `ops.error_reports` (agrupado por huella con su contador) y el ADMIN
  le da seguimiento en "Errores del sistema" (pendiente, en proceso, en revisión, solucionado; uno
  solucionado que vuelve a ocurrir se reabre solo):
  - respuestas 4xx/5xx: las anota `error_response` y las reporta el middleware del traceId;
  - excepciones no controladas: con su stack trace;
  - cualquier `logger.error` / `logger.exception` (segundo plano, procesos internos): `ErrorLogHandler`;
  - errores del canal WebSocket.
- Nunca se atrapa un error para silenciarlo: o se responde con su código (y queda registrado) o se
  registra con `logger.exception` si es de mejor esfuerzo. El registro jamás frena una petición
  (cola en memoria acotada, guardado en lotes por un hilo, cada error en su transacción) ni la
  rompe (si registrar falla, la respuesta sale igual).
- **Contexto literal, salvo secretos y archivos** (decisión del dueño del producto: para reproducir
  un error hace falta todo). Cada ocurrencia guarda quién (correo y rol), empresa, método, ruta,
  query, IP, cabeceras de la lista blanca, cuerpo enviado, estado y cuerpo de la respuesta y
  duración (`app/core/error_context.py`, copiado mientras viaja en `RequestIdMiddleware`).
  - Un campo secreto nuevo (contraseña, token, llave, firma...) debe coincidir con `_SECRET` o se
    agrega ahí: se guarda como `[oculto]` en el cuerpo, la query y cualquier nivel del JSON.
  - `Authorization` y `Cookie` nunca se copian (lista blanca de cabeceras).
  - De un archivo (multipart) solo queda nombre, tipo y tamaño.
  - El motor de BD sigue con `hide_parameters`.
- **Filas acotadas**: la ubicación es la plantilla de la ruta (`location_of`); un 404 sin ruta es
  `(ruta inexistente)`. Lo que no es una falla (el resultado de una validación en vivo) no se reporta.
- El detalle interno (errores de componentes, cola del motor, capacidad adaptativa) lo ve solo el
  ADMIN (`GET /api/admin/errors/server`); las sondas públicas solo dicen si está listo.

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
- Sin formateadores ajenos al proyecto; se respeta el estilo existente.

## 8. Seguridad

- Contraseñas con hash; tokens ES256; sesiones validadas en cada petición.
- Secretos solo en `.env` (nunca en el código ni en el repositorio).
- Datos biométricos cifrados. El ADMIN de la plataforma solo consulta la **ficha de trabajo** de los
  empleados, de solo lectura (`CompanyEmployeeRead`: número, nombre, departamento, correo, teléfono,
  estado y estado facial). Nunca RFC, CURP, NSS, fecha de nacimiento, fotos ni plantillas.
- **Módulos por empresa que concede el ADMIN**: hoy Integraciones (`companies.api_enabled`). Se
  aplican en tres lugares que no se separan: disponibilidad de la pantalla
  (`navigation_service.AVAILABILITY`), la dependencia del router (`require_api_module`) y la
  autenticación de la llave (`api_key_service.authenticate`), todos con 403 `API_ACCESS_DISABLED`.
- **Asistente de reportes sin IA externa** (`app/services/reporting/`): ningún dato de una empresa
  sale del servidor. La empresa sale de la sesión y `ReportRepository` filtra siempre por ella. Toda
  tabla con `company_id` debe estar en `DATASETS` o en `EXCLUDED_TABLES` con su motivo (lo exige
  `tests/test_reports_catalog.py`). Así, un módulo nuevo suma su reporte al asistente.
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
  `check_liveness` (giros en orden, anti-spoofing también en el giro) y por `attempt_guard`
  (bloqueo). Un intento sospechoso es `SuspiciousCapture`: se registra en la bitácora con su motivo
  antes de responder. Cada candado se activa por empresa en `verification_policy`; uno nuevo lleva
  su interruptor, su motivo en `verification_reasons` y su mensaje en `face_errors`.
- **Reconocimiento evolutivo** (`face_learning`): toda identificación facial exitosa (1:1, en persona,
  QR + rostro, 1:N) llama a `FaceLearning.reinforce` ANTES de registrar el intento (misma
  transacción). Las muestras del registro aprobado (`learned = False`) son el ancla y nunca se
  reemplazan; una aprendida solo entra con prueba de vida, holgura sobre el nivel de la empresa,
  parecido propio al ancla, novedad y ritmo (reglas en el módulo), y compite por su lugar por
  utilidad (`matches`, `last_matched_at`). El mantenimiento retira la que dejó de servir. Nada
  automático baja el nivel de confianza de la empresa. La galería en memoria descifra solo lo nuevo.
- Toda autenticación por token (HTTP y canal WebSocket) usa `SessionService.authenticate_access`:
  las mismas reglas en ambos canales.
- Reglas del inicio de sesión por cuenta (dispositivo permitido, ubicación del validador) se aplican
  en `POST /auth/login` antes de crear la sesión (`ensure_device_allowed`, `ensure_device_authorized`,
  `ensure_location_allowed`).
  Cambiar una regla que la sesión abierta ya no cumple cierra sus sesiones con su motivo de catálogo.

## 9. Documentación

- Docstring en cada módulo, clase y función pública explicando el **porqué**.
- Cambios de API o de arquitectura se reflejan en el `README.md` de la raíz (tabla de endpoints).
