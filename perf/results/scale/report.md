# Escalado horizontal (perf/scale/run.sh)

## 1. Migraciones: 3 réplicas a la vez sobre una base vacía

```
timeclock-scale-backend-1: migraciones aplicadas=80, esperó su turno=0
timeclock-scale-backend-2: migraciones aplicadas=0, esperó su turno=1
timeclock-scale-backend-3: migraciones aplicadas=0, esperó su turno=2
alembic_version: 0083
migrate: 0 migraciones nuevas (ya estaba al día)
```

## 2. Carga por el gateway (k6, misma mezcla que perf/run.sh)

| Réplicas | Objetivo (pet/s) | Logradas | 200 OK | 503 controlado | Otros errores | p50 (ms) | p95 (ms) | p99 (ms) | Reparto por réplica (log del gateway) | Máx. conexiones PostgreSQL | PgBouncer: servidor / clientes activos / en espera / espera máx. (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|
| 1 | 300 | 272 | 3,374 | 2,545 | 0 | 435.6 | 4687.1 | 5081.2 | backend-1 5,919 (100 %) | 26 | 25 / 25 / 0 / 0 |
| 1 | 600 | 522 | 4,244 | 7,413 | 0 | 244.1 | 6369.4 | 9571.4 | backend-1 11,657 (100 %) | 26 | 25 / 25 / 0 / 0 |
| 1 | 1,200 | 755 | 4,227 | 17,123 | 0 | 50.3 | 13079.8 | 18131.2 | backend-1 21,350 (100 %) | 26 | 25 / 25 / 0 / 0 |
| 5 | 300 | 295 | 6,002 | 0 | 0 | 14.2 | 72.1 | 167.9 | backend-1 1,207 (20 %) · backend-2 1,193 (20 %) · backend-3 1,208 (20 %) · backend-4 1,182 (20 %) · backend-5 1,212 (20 %) | 35 | 34 / 61 / 0 / 0 |
| 5 | 600 | 597 | 12,002 | 0 | 0 | 40.6 | 135.3 | 193.7 | backend-1 2,321 (19 %) · backend-2 2,442 (20 %) · backend-3 2,427 (20 %) · backend-4 2,361 (20 %) · backend-5 2,451 (20 %) | 41 | 40 / 90 / 29 / 0 |
| 5 | 1,200 | 1,139 | 11,803 | 12,069 | 0 | 170.1 | 2418.0 | 5018.9 | backend-1 9,498 (40 %) · backend-2 3,785 (16 %) · backend-3 3,314 (14 %) · backend-4 3,637 (15 %) · backend-5 3,638 (15 %) | 41 | 40 / 100 / 82 / 0 |

## 3. Canal WebSocket por el gateway

- 60 conexiones: {'WS_AUTHENTICATED/TAKEN': 10, 'WS_AUTHENTICATED/AVAILABLE': 50}; fallas: 0
- Reparto: backend-1 12 (20 %) · backend-2 12 (20 %) · backend-3 12 (20 %) · backend-4 12 (20 %) · backend-5 12 (20 %)

## 4. Mantenimiento: una instancia a la vez

```
Las réplicas intentan la vuelta a la vez (cada una sostiene el candado 4 s si lo obtiene):
d474f76ec51b se saltó la vuelta (otra instancia tiene el candado)
e7135316ade1 se saltó la vuelta (otra instancia tiene el candado)
85601e42d44d tomó el candado
b6707964e920 se saltó la vuelta (otra instancia tiene el candado)
866fd55e0dd3 se saltó la vuelta (otra instancia tiene el candado)
85601e42d44d trabajó
Un proceso con el candado muere a la mitad (kill -9): ¿queda pegado en PgBouncer?
866fd55e0dd3 tomó el candado
e7135316ade1 tomó el candado
e7135316ade1 trabajó
Vueltas programadas (MAINTENANCE_INTERVAL_SECONDS=5) en el log de cada réplica:
  fallas del mantenimiento: 0
```

## 5. Una réplica se apaga y otra se cae en plena carga

- 18:08:59 docker stop timeclock-scale-backend-5 (apagado ordenado)
- 18:09:15 docker kill timeclock-scale-backend-1 (caída abrupta)
- Lecturas: **9,001 correctas**, 0 descartadas a propósito (503 SERVER_BUSY), 0 con una falla pasajera al primer intento, 0 correctas tras reintentar y **0 fallidas tras los reintentos**.
- Latencia: p50 8.7 ms, p95 17.0 ms, p99 32.1 ms, máx. 256 ms.
- Reparto: backend-1 1,222 (14 %) · backend-2 2,375 (26 %) · backend-3 2,362 (26 %) · backend-4 2,362 (26 %) · backend-5 681 (8 %)
- Respuestas 5xx del gateway en el periodo: ninguna

## 6. Despliegue en plena carga

### `docker compose up -d` (recrea TODAS las réplicas a la vez)

- 18:10:15 dc up -d --force-recreate --no-deps --scale backend=5 backend
- 18:10:22 terminó: 0 réplicas sanas
- Lecturas: **10,963 correctas**, 3,931 con una falla pasajera al primer intento, 300 correctas tras reintentar y **3,631 fallidas tras los reintentos**.
- Latencia: p95 17.1 ms, máx. 315 ms.
- Respuestas 5xx del gateway en el periodo: 11393 " 503

### `scripts/deploy.sh` (primero las nuevas; las viejas se retiran una por una)

- 18:11:46 env COMPOSE_FILE=/Users/server/Desktop/APPS/TIME-CLOCK/docker-compose.yml:/Users/server/Desktop/APPS/TIME-CLOCK/backend-employee-time-clock/perf/scale/docker-compose.scale.yml /Users/server/Desktop/APPS/TIME-CLOCK/scripts/deploy.sh
- 18:15:26 terminó: 5 réplicas sanas
- Lecturas: **10,457 correctas**, 1 con una falla pasajera al primer intento, 1 correctas tras reintentar y **0 fallidas tras los reintentos**.
- Latencia: p95 673.6 ms, máx. 22372 ms.
- Respuestas 5xx del gateway en el periodo: 1 " 503


## 7. Caché compartida (Redis)

```
memoria: used_memory:1262072 maxmemory:268435456 
llaves: 5
instantánea timeclock:catalogs:6f298f8f6321392b bytes=163904 ttl=500
contadores de límites (rl:*): 4
aciertos/fallos: total_commands_processed:251 keyspace_hits:11 keyspace_misses:2 
caídas registradas por las réplicas (Redis no responde): 0
```

## Memoria por réplica (docker stats, al final de cada etapa)

```
load-1-1200: 577.2MiB
load-1-300: 563.2MiB
load-1-600: 566.9MiB
load-5-1200: 579.9MiB · 543.6MiB · 537.9MiB · 544.2MiB · 542.6MiB
load-5-300: 574.9MiB · 523.5MiB · 517.8MiB · 525.6MiB · 517.3MiB
load-5-600: 575.6MiB · 526.5MiB · 520.6MiB · 528.3MiB · 526MiB
```