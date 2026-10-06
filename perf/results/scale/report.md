# Escalado horizontal (perf/scale/run.sh)

## 1. Migraciones: 3 réplicas a la vez sobre una base vacía

```
timeclock-scale-backend-1: migraciones aplicadas=69, esperó su turno=0
timeclock-scale-backend-2: migraciones aplicadas=0, esperó su turno=1
timeclock-scale-backend-3: migraciones aplicadas=0, esperó su turno=2
alembic_version: 0072
migrate: 0 migraciones nuevas (ya estaba al día)
```

## 2. Carga por el gateway (k6, misma mezcla que perf/run.sh)

| Réplicas | Objetivo (pet/s) | Logradas | 200 OK | 503 controlado | Otros errores | p50 (ms) | p95 (ms) | p99 (ms) | Reparto por réplica (log del gateway) | Máx. conexiones PostgreSQL | PgBouncer: servidor / clientes activos / en espera / espera máx. (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|
| 1 | 300 | 283 | 5,829 | 135 | 0 | 617.5 | 2547.8 | 5016.0 | backend-1 5,964 (100 %) | 41 | 40 / 50 / 4 / 0 |
| 1 | 600 | 549 | 6,835 | 5,144 | 0 | 416.9 | 5033.9 | 5398.9 | backend-1 11,979 (100 %) | 41 | 40 / 50 / 9 / 0 |
| 1 | 1,200 | 1,073 | 8,859 | 14,410 | 0 | 278.9 | 5045.5 | 7018.3 | backend-1 23,269 (100 %) | 41 | 40 / 50 / 4 / 0 |
| 3 | 300 | 299 | 6,002 | 0 | 0 | 8.8 | 32.2 | 185.8 | backend-1 2,077 (35 %) · backend-2 1,957 (33 %) · backend-3 1,968 (33 %) | 41 | 40 / 77 / 0 / 0 |
| 3 | 600 | 597 | 12,002 | 0 | 0 | 14.2 | 34.8 | 47.5 | backend-1 3,949 (33 %) · backend-2 3,963 (33 %) · backend-3 4,090 (34 %) | 41 | 40 / 77 / 0 / 0 |
| 3 | 1,200 | 1,145 | 16,135 | 7,589 | 0 | 281.2 | 2822.7 | 5008.8 | backend-1 10,976 (46 %) · backend-2 6,582 (28 %) · backend-3 6,166 (26 %) | 41 | 40 / 120 / 103 / 0 |

## 3. Canal WebSocket por el gateway

- 60 conexiones: {'WS_AUTHENTICATED/TAKEN': 10, 'WS_AUTHENTICATED/AVAILABLE': 50}; fallas: 0
- Reparto: backend-1 20 (33 %) · backend-2 20 (33 %) · backend-3 20 (33 %)

## 4. Mantenimiento: una instancia a la vez

```
Las réplicas intentan la vuelta a la vez (cada una sostiene el candado 4 s si lo obtiene):
678152b8a514 tomó el candado
d7bb10b2ed56 se saltó la vuelta (otra instancia tiene el candado)
a54c61cb1a45 se saltó la vuelta (otra instancia tiene el candado)
678152b8a514 trabajó
Un proceso con el candado muere a la mitad (kill -9): ¿queda pegado en PgBouncer?
a54c61cb1a45 tomó el candado
d7bb10b2ed56 tomó el candado
d7bb10b2ed56 trabajó
Vueltas programadas (MAINTENANCE_INTERVAL_SECONDS=5) en el log de cada réplica:
  fallas del mantenimiento: 0
```

## 5. Una réplica se apaga y otra se cae en plena carga

- 05:08:27 docker stop timeclock-scale-backend-3 (apagado ordenado)
- 05:08:43 docker kill timeclock-scale-backend-1 (caída abrupta)
- Lecturas: **9,001 correctas**, 0 descartadas a propósito (503 SERVER_BUSY), 0 con una falla pasajera al primer intento, 0 correctas tras reintentar y **0 fallidas tras los reintentos**.
- Latencia: p50 6.9 ms, p95 10.6 ms, p99 16.8 ms, máx. 140 ms.
- Reparto: backend-1 2,161 (24 %) · backend-2 5,681 (63 %) · backend-3 1,160 (13 %)
- Respuestas 5xx del gateway en el periodo: ninguna

## 6. Despliegue en plena carga

### `docker compose up -d` (recrea TODAS las réplicas a la vez)

- 05:09:41 dc up -d --force-recreate --no-deps --scale backend=3 backend
- 05:09:47 terminó: 0 réplicas sanas
- Lecturas: **13,220 correctas**, 1,717 con una falla pasajera al primer intento, 292 correctas tras reintentar y **1,425 fallidas tras los reintentos**.
- Latencia: p95 11.3 ms, máx. 1487 ms.
- Respuestas 5xx del gateway en el periodo: 4760 " 503

### `scripts/deploy.sh` (primero las nuevas; las viejas se retiran una por una)

- 05:11:12 env COMPOSE_FILE=/Users/server/Desktop/APPS/TIME-CLOCK/docker-compose.yml:/Users/server/Desktop/APPS/TIME-CLOCK/backend-employee-time-clock/perf/scale/docker-compose.scale.yml /Users/server/Desktop/APPS/TIME-CLOCK/scripts/deploy.sh
- 05:12:35 terminó: 3 réplicas sanas
- Lecturas: **10,550 correctas**, 1 con una falla pasajera al primer intento, 1 correctas tras reintentar y **0 fallidas tras los reintentos**.
- Latencia: p95 100.5 ms, máx. 19457 ms.
- Respuestas 5xx del gateway en el periodo: ninguna
