// Caída de réplicas con tráfico (perf/scale/run.sh): llegada constante de lecturas por el gateway mientras se
// apaga una réplica ordenadamente (docker stop) y se mata otra (docker kill). Cada respuesta se clasifica por
// su código del contrato y, como la app web, una lectura que falla de forma pasajera (red, 502/503/504 que no
// sea SERVER_BUSY) se reintenta hasta 2 veces con espera corta. Lo que importa: errores tras reintentar = 0.
import http from 'k6/http';
import { sleep } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://frontend:80';
const RATE = Number(__ENV.RATE || 200);
const DURATION = __ENV.DURATION || '45s';

const ok = new Counter('ok');
const busy = new Counter('server_busy');
const transient = new Counter('transient_first_try');
const retried = new Counter('ok_after_retry');
const failed = new Counter('failed_after_retry');

export const options = {
  scenarios: {
    failover: { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: DURATION, preAllocatedVUs: 200, maxVUs: 600 },
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const res = http.post(`${BASE}/api/auth/login`, JSON.stringify({ email: 'carga@carga-timeclock.com', password: 'Carga12345' }), {
    headers: { 'Content-Type': 'application/json' },
  });
  if (res.status !== 200) throw new Error(`No se pudo iniciar sesión: ${res.status} ${res.body}`);
  return { token: res.json('data.access_token') };
}

const PATHS = ['/api/users/me', '/api/employees?size=10&page=2', '/api/catalogs', '/api/departments?size=10'];

function code(res) {
  try {
    return res.json('code');
  } catch (_) {
    return null;
  }
}

export default function (data) {
  const path = PATHS[Math.floor(Math.random() * PATHS.length)];
  const params = { headers: { Authorization: `Bearer ${data.token}` }, timeout: '30s' };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const res = http.get(`${BASE}${path}`, params);
    if (res.status === 200) {
      ok.add(1);
      if (attempt > 0) retried.add(1);
      return;
    }
    if (res.status === 503 && code(res) === 'SERVER_BUSY') {
      busy.add(1); // descarte controlado del control de admisión: no es una falla
      return;
    }
    if (attempt === 0) transient.add(1);
    sleep(0.5 * (attempt + 1));
  }
  failed.add(1);
}
