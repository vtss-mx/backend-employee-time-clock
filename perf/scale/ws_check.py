"""Canal WebSocket de validación A TRAVÉS DEL GATEWAY con varias réplicas (lo lanza perf/scale/run.sh dentro
de la red aislada): cada conexión se autentica con un token y valida un dato; el gateway reparte las
conexiones entre réplicas (el reparto se cuenta en su log). Imprime un JSON con lo que respondió el canal."""

import asyncio
import json
import sys
import urllib.request
from collections import Counter

import websockets

GATEWAY = "frontend"


def login() -> str:
    body = json.dumps({"email": "carga@carga-timeclock.com", "password": "Carga12345"}).encode()
    request = urllib.request.Request(
        f"http://{GATEWAY}/api/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (URL fija http:// del gateway)
        return json.load(response)["data"]["access_token"]


async def one(token: str, number: int) -> tuple[str, str]:
    async with websockets.connect(f"ws://{GATEWAY}/api/ws/validation", open_timeout=10) as socket:
        await socket.send(json.dumps({"type": "auth", "token": token}))
        auth = json.loads(await asyncio.wait_for(socket.recv(), 10))
        await socket.send(
            json.dumps(
                {"type": "validate", "id": f"scale-{number:04d}", "field": "department_name", "value": f"Área {number}"}
            )
        )
        answer = json.loads(await asyncio.wait_for(socket.recv(), 10))
        assert answer["traceId"] == f"scale-{number:04d}", answer  # la respuesta es la de esta pregunta
        return auth["code"], answer["code"]


async def main(connections: int) -> None:
    token = login()
    results = await asyncio.gather(*(one(token, n) for n in range(1, connections + 1)), return_exceptions=True)
    failures = [repr(r) for r in results if isinstance(r, BaseException)]
    codes = Counter(f"{auth}/{answer}" for auth, answer in (r for r in results if not isinstance(r, BaseException)))
    print(json.dumps({"connections": connections, "codes": dict(codes), "failures": failures}))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
