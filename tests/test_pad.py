"""PAD de frontera (`app/facial_recognition/pad.py`): más de 1000 rasgos enumerables en 7 familias, deterministas y
acotados a [0, 1]; agregación por familia; «no medible» → {}. El cableado al motor (señales en «Solo medir» que nunca
niegan) se prueba en `tests/test_capture_protocol.py`."""

import numpy as np
import pytest

from app.facial_recognition import pad


def _frame(seed: int, side: int = 80) -> np.ndarray:
    """Un cuadro BGR pseudoaleatorio pero determinista (una toma con textura, ruido y color)."""
    return np.random.default_rng(seed).integers(0, 256, size=(side, side, 3), dtype=np.uint8)


def test_the_registry_enumerates_more_than_1000_unique_named_features():
    assert len(pad.PAD_FEATURES) == 1428 >= 1000
    assert len(set(pad.PAD_FEATURES)) == len(pad.PAD_FEATURES)  # todos únicos
    # Rejilla: 7 familias × 17 regiones (4×4 + entero) × 3 escalas × 4 canales.
    assert len(pad.FAMILIES) == 7 and len(pad.REGIONS) == 17 and len(pad.SCALES) == 3 and len(pad.CHANNELS) == 4
    assert pad.REGIONS[0] == "whole" and "r2c1" in pad.REGIONS
    assert "texture.r2c1.s2.Y.lbp" in pad.PAD_FEATURES  # el formato familia.región.escala.canal.estadístico
    # Cada rasgo empieza por una familia conocida y termina con su estadístico.
    for name in pad.PAD_FEATURES:
        family = name.split(".", 1)[0]
        assert family in pad.FAMILIES and name.endswith(f".{pad.STATS[family]}")


def test_extract_measures_every_feature_bounded_and_deterministic():
    vector = pad.extract([_frame(1)])
    assert set(vector) == set(pad.PAD_FEATURES)  # mide TODOS los rasgos
    assert all(0.0 <= value <= 1.0 for value in vector.values())  # acotados por construcción
    assert pad.extract([_frame(1)]) == vector  # mismo cuadro → mismo vector (determinista)
    assert pad.extract([_frame(2)]) != vector  # otro cuadro → otro vector


def test_extract_averages_across_frames():
    one, two = pad.extract([_frame(1)]), pad.extract([_frame(2)])
    both = pad.extract([_frame(1), _frame(2)])
    assert set(both) == set(pad.PAD_FEATURES)
    sample = "color.whole.s0.R.mean"
    assert both[sample] == pytest.approx((one[sample] + two[sample]) / 2, abs=1e-5)


def test_a_flat_frame_has_no_frequency_energy():
    """Una región sin textura (todo del mismo tono) no tiene espectro que medir: la familia de frecuencia da 0."""
    flat = np.full((64, 64, 3), 128, dtype=np.uint8)
    vector = pad.extract([flat])
    assert all(value == 0.0 for name, value in vector.items() if name.startswith("frequency."))
    # Una zona plana es toda "textura uniforme" (LBP) y sin reflejos ni ruido.
    assert vector["texture.whole.s0.Y.lbp"] == 1.0
    assert vector["specular.whole.s0.Y.spec"] == 0.0 and vector["noise.whole.s0.Y.res"] == 0.0


def test_a_bright_frame_is_all_specular():
    white = np.full((64, 64, 3), 255, dtype=np.uint8)
    vector = pad.extract([white])
    assert all(value == 1.0 for name, value in vector.items() if name.startswith("specular."))


@pytest.mark.parametrize(
    "frames",
    [
        [],  # sin cuadros
        [np.zeros((80, 80), dtype=np.uint8)],  # no es BGR (2D)
        [np.zeros((80, 80, 4), dtype=np.uint8)],  # cuatro canales
        [np.zeros((8, 8, 3), dtype=np.uint8)],  # demasiado pequeño
    ],
)
def test_without_a_usable_frame_nothing_is_measured(frames):
    assert pad.extract(frames) == {}  # «no medible», nunca sospechoso


def test_an_unusable_frame_is_skipped_but_a_usable_one_is_measured():
    assert pad.extract([np.zeros((8, 8, 3), dtype=np.uint8), _frame(3)]) == pad.extract([_frame(3)])


def test_families_aggregate_each_to_one_bounded_score():
    scores = pad.families(pad.extract([_frame(4)]))
    assert set(scores) == set(pad.FAMILIES)
    assert all(0.0 <= value <= 1.0 for value in scores.values())
    # La familia es la media de sus rasgos.
    vector = pad.extract([_frame(4)])
    texture = [value for name, value in vector.items() if name.startswith("texture.")]
    assert scores["texture"] == pytest.approx(round(sum(texture) / len(texture), 6), abs=1e-6)


def test_families_of_nothing_is_nothing():
    assert pad.families({}) == {}
