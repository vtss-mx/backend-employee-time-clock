"""Fila FIFO del pool de workers faciales cuando una solicitud de más atrás se rinde antes que la de
adelante (app/facial_recognition/worker_pool.py).

Los plazos de espera no vencen siempre en orden de llegada: el hilo de adelante puede tardar en
retomar el candado. Un ticket que expira sin ser el turno se marca y, cuando la fila llega a él, se
salta; sin eso, la fila se detendría para siempre en un ticket que ya nadie espera.
"""

import threading
import time

import pytest

from app.facial_recognition.worker_pool import QueueTimeoutError, WorkerPool


def _wait_until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "la condición no se cumplió a tiempo"
        time.sleep(0.005)


def test_a_ticket_that_expires_behind_the_front_is_skipped_when_its_turn_comes():
    pool = WorkerPool(lambda i: f"worker-{i}", size=1, max_waiting=5, wait_timeout=5.0)
    served: list[str] = []

    def patient(name: str) -> None:
        with pool.lease():
            served.append(name)

    with pool.lease():  # el único worker está ocupado
        front = threading.Thread(target=patient, args=("adelante",))
        front.start()
        _wait_until(lambda: pool.stats().waiting == 1)

        pool.wait_timeout = 0.05  # quien llega después se rinde antes que el de adelante
        with pytest.raises(QueueTimeoutError), pool.lease():
            pass
        pool.wait_timeout = 5.0

        behind = threading.Thread(target=patient, args=("detrás",))
        behind.start()
        _wait_until(lambda: pool.stats().waiting == 2)
    front.join(timeout=5)
    behind.join(timeout=5)

    # La fila saltó el ticket vencido: atendió a los dos que esperaban, en su orden.
    assert served == ["adelante", "detrás"]
    stats = pool.stats()
    assert (stats.processed, stats.rejected, stats.waiting, stats.busy) == (3, 1, 0, 0)
