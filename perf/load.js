// Prueba de carga de la API (k6): llegada CONSTANTE de RATE peticiones por segundo durante DURATION,
// con una mezcla realista de lo que usa una empresa (autenticada). Lo lanza perf/run.sh por etapas.
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://localhost:8100';
const RATE = Number(__ENV.RATE || 1000);
const DURATION = __ENV.DURATION || '20s';
const MAX_VUS = Number(__ENV.MAX_VUS || 1500); // acotado: el generador comparte la máquina con la API

// 503 SERVER_BUSY = el control de admisión descartó a propósito (saturado): no es una falla.
const shed = new Counter('shed_503');
const failed = new Counter('errors_other');

export const options = {
  discardResponseBodies: true,
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.min(RATE, 1000),
      maxVUs: MAX_VUS,
    },
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

/** Una sola sesión de la empresa de prueba (el login tiene su propio límite por persona). */
export function setup() {
  const res = http.post(`${BASE}/api/auth/login`, JSON.stringify({ email: 'carga@carga-timeclock.com', password: 'Carga12345' }), {
    headers: { 'Content-Type': 'application/json' },
    responseType: 'text',
  });
  if (res.status !== 200) throw new Error(`No se pudo iniciar sesión: ${res.status} ${res.body}`);
  return { token: res.json('data.access_token') };
}

// Mezcla: perfil y menú, listado paginado, búsqueda por trigramas, catálogos y departamentos.
const ROUTES = [
  [35, () => '/api/users/me'],
  [25, () => `/api/employees?size=10&page=${1 + Math.floor(Math.random() * 50)}`],
  [15, () => `/api/employees?size=10&search=apellido${Math.floor(Math.random() * 97)}`],
  [15, () => '/api/catalogs'],
  [10, () => '/api/departments?size=10'],
];
const TOTAL = ROUTES.reduce((sum, [weight]) => sum + weight, 0);

function pick() {
  let roll = Math.random() * TOTAL;
  for (const [weight, path] of ROUTES) {
    roll -= weight;
    if (roll < 0) return path();
  }
  return ROUTES[0][1]();
}

export default function (data) {
  const res = http.get(`${BASE}${pick()}`, { headers: { Authorization: `Bearer ${data.token}` }, timeout: '30s' });
  if (res.status === 503) shed.add(1);
  else if (res.status !== 200) failed.add(1);
  check(res, { 'respuesta 200': (r) => r.status === 200 });
}
