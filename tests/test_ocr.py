"""OCR de los documentos del empleado EN ESTE SERVIDOR (decisión del dueño del producto, 2026-10-07): la lectura de la
MRZ (ICAO 9303), la extracción de campos por tipo y el motor Tesseract (con sus llamadas simuladas; el real tiene
`tests/test_ocr_real.py`). Mejor esfuerzo: una lectura imperfecta nunca bloquea."""

import io
from datetime import date

import pytest
from PIL import Image

from app import ocr
from app.core.config import settings
from app.ocr import OcrResult, OcrText, OcrUnavailable, RealBackend
from app.ocr import engine as ocr_engine
from app.ocr.fields import extract_fields
from app.ocr.mrz import parse_mrz

# MRZ canónicas de ICAO 9303 (ejemplos públicos del documento): sus dígitos verificadores cuadran.
PASSPORT_MRZ = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<\nL898902C36UTO7408122F1204159ZE184226B<<<<<10"
ID_MRZ = "I<UTOD231458907<<<<<<<<<<<<<<<\n7408122F1204159UTO<<<<<<<<<<<6\nERIKSSON<<ANNA<MARIA<<<<<<<<<<"
# CURP con dígito verificador válido para la prueba (homoclave «0» = siglo XX → 1990).
CURP = "PEXJ900510HSRRNN09"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), (200, 200, 200)).save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- MRZ (mrz.py)


def test_passport_mrz_reads_every_field_and_verifies_its_check_digits():
    result = parse_mrz(f"basura previa\n{PASSPORT_MRZ}\nbasura")
    assert result is not None and result.document_type == "PASSPORT" and result.verified
    assert result.document_number == "L898902C3" and result.nationality == "UTO" and result.sex == "F"
    assert result.birth_date == date(1974, 8, 12) and result.expiry_date == date(2012, 4, 15)
    assert result.fields["full_name"] == "ANNA MARIA ERIKSSON"
    assert result.checks_passed == result.checks_total == 3


def test_id_card_td1_mrz():
    result = parse_mrz(ID_MRZ)
    assert result is not None and result.document_type == "ID_CARD" and result.verified
    assert result.document_number == "D23145890" and result.sex == "F" and result.nationality == "UTO"
    assert result.birth_date == date(1974, 8, 12) and result.fields["full_name"] == "ANNA MARIA ERIKSSON"


def test_a_wrong_check_digit_still_returns_the_fields_but_not_verified():
    # Mejor esfuerzo: un dígito verificador de la fecha de nacimiento equivocado no descarta la lectura.
    broken = PASSPORT_MRZ.replace("7408122F", "7408120F")  # el check de la fecha ya no cuadra
    result = parse_mrz(broken)
    assert result is not None and result.verified is False
    assert result.checks_passed == 2 and result.checks_total == 3 and result.document_number == "L898902C3"


def test_no_mrz_in_plain_text_returns_none():
    assert parse_mrz("Nombre: Ana\nDirección: calle 1\n") is None
    assert parse_mrz("") is None


def test_mrz_dates_handle_the_century_and_invalid_values():
    # Una vigencia con mes 13 no es una fecha: queda sin vigencia, pero el resto se lee (la fecha es YYMMDD, así que el
    # mes son los caracteres 3 y 4; «120415» → «121315» deja YY=12, MM=13, DD=15 y conserva el dígito verificador «9»).
    bad_expiry = PASSPORT_MRZ.replace("1204159", "1213159")
    result = parse_mrz(bad_expiry)
    assert result is not None and result.expiry_date is None and result.birth_date == date(1974, 8, 12)


def test_a_blank_passport_mrz_reads_nothing_but_does_not_crash():
    # Dos renglones de pasaporte válidos en forma (el primero empieza por «P», 44 caracteres) pero todo en relleno:
    # ningún campo se arma, ningún dígito verificador cuadra (son «<», no números) y no se verifica. Mejor esfuerzo.
    blank = "P<UTO" + "<" * 39 + "\n" + "<" * 44
    result = parse_mrz(blank)
    assert result is not None and result.document_type == "PASSPORT" and result.verified is False
    assert result.fields == {} and result.checks_total == 0
    assert result.document_number is None and result.birth_date is None and result.expiry_date is None
    assert result.nationality is None and result.sex is None and result.surname is None and result.given_names is None


def test_td1_skips_candidates_that_do_not_fit_or_are_not_an_identification():
    # Cuatro renglones candidatos pero ninguno arma un TD1: el primero es demasiado largo (40 > 30) y el segundo no
    # empieza por el tipo de una identificación (I/A/C). Sin MRZ reconocible devuelve None.
    text = "<" * 40 + "\n" + "X" + "<" * 29 + "\n" + "<" * 30 + "\n" + "<" * 30
    assert parse_mrz(text) is None


# ---------------------------------------------------------------- campos por tipo (fields.py)


def test_passport_fields_come_from_the_mrz():
    fields, mrz = extract_fields("PASSPORT", PASSPORT_MRZ)
    assert mrz is not None and mrz.verified
    assert fields["document_number"] == "L898902C3" and fields["birth_date"] == "1974-08-12"


def test_ine_reads_curp_and_voter_key_and_derives_birth_and_sex():
    text = f"INSTITUTO NACIONAL ELECTORAL\nCURP {CURP}\nCLAVE DE ELECTOR PRLZJN85010101H200\nNOMBRE ANA"
    fields, mrz = extract_fields("NATIONAL_ID", text)
    assert mrz is None  # sin MRZ en el anverso: solo las expresiones regulares
    assert fields["curp"] == CURP and fields["document_number"] == CURP and fields["sex"] == "M"
    assert fields["birth_date"] == "1990-05-10" and fields["voter_key"] == "PRLZJN85010101H200"


def test_ine_without_a_curp_leaves_everything_empty():
    fields, _ = extract_fields("NATIONAL_ID", "credencial para votar")
    assert fields == {}


def test_proof_of_address_reads_the_postal_code_and_a_snippet():
    text = "Comisión Federal de Electricidad\nCalle Dr. Paliza 71, Centro\nC.P. 83000 Hermosillo"
    fields, mrz = extract_fields("PROOF_OF_ADDRESS", text)
    assert mrz is None and fields["postal_code"] == "83000"
    assert "Hermosillo" in fields["address"] and "Paliza" in fields["address"]


def test_proof_of_address_without_a_postal_code_or_lines():
    fields, _ = extract_fields("PROOF_OF_ADDRESS", "corto")
    assert fields == {}


def test_a_license_takes_the_first_believable_birth_date():
    fields, _ = extract_fields("DRIVER_LICENSE", "LICENCIA\nF. NAC 10/05/1990\nVENCE 2030-01-01")
    assert fields["birth_date"] == "1990-05-10"


def test_a_license_with_no_past_date_leaves_birth_empty():
    fields, _ = extract_fields("OTHER_OFFICIAL_ID", "sin fechas útiles 2099-01-01")
    assert "birth_date" not in fields


def test_a_license_reads_a_two_digit_year_and_skips_an_impossible_date():
    # La primera fecha imposible (día y mes fuera de rango) se descarta; la siguiente, con año de dos cifras, se
    # completa al siglo (85 → 1985).
    fields, _ = extract_fields("DRIVER_LICENSE", "VENCE 45/45/2020\nF. NAC 15/03/85")
    assert fields["birth_date"] == "1985-03-15"


def test_ine_with_an_impossible_curp_birth_reads_the_rest_without_the_date():
    # La CURP tiene mes 13 en su fecha: no se deriva la fecha de nacimiento, pero la CURP y el sexo (de la propia
    # CURP) sí se leen. Mejor esfuerzo: una parte ilegible no descarta las demás.
    fields, _ = extract_fields("NATIONAL_ID", "INSTITUTO NACIONAL ELECTORAL\nCURP PEXJ901345HSRRNN09")
    assert fields["curp"] == "PEXJ901345HSRRNN09" and fields["sex"] == "M" and "birth_date" not in fields


# ---------------------------------------------------------------- motor Tesseract (engine.py)


def test_read_text_assembles_words_and_averages_confidence(monkeypatch):
    def fake_image_to_data(_image, **_kwargs):
        # Tesseract entrega relleno (conf -1) y palabras con su confianza; solo cuentan las de conf >= 0.
        return {"text": ["", "Ana", "Ruiz", " "], "conf": ["-1", "90", "80", "50"]}

    monkeypatch.setattr(ocr_engine.pytesseract, "image_to_data", fake_image_to_data)
    result = RealBackend().read_text(_png(), "spa+eng")
    assert result.text == "Ana\nRuiz" and result.confidence == pytest.approx(0.85)


def test_read_text_ignores_a_word_whose_confidence_is_not_a_number(monkeypatch):
    # Tesseract a veces devuelve una confianza ilegible (None o un texto): esa palabra se descarta (conf < 0), nunca
    # tumba la lectura (mejor esfuerzo).
    def fake_image_to_data(_image, **_kwargs):
        return {"text": ["Ana", "Ruiz"], "conf": [None, "90"]}

    monkeypatch.setattr(ocr_engine.pytesseract, "image_to_data", fake_image_to_data)
    result = RealBackend().read_text(_png(), "spa")
    assert result.text == "Ruiz" and result.confidence == pytest.approx(0.90)


def test_read_text_with_no_words_is_empty_with_zero_confidence(monkeypatch):
    monkeypatch.setattr(ocr_engine.pytesseract, "image_to_data", lambda *_a, **_k: {"text": [], "conf": []})
    result = ocr_engine.read_text(_png(), "spa")
    assert result == OcrText(text="", confidence=0.0)


def test_read_text_rejects_an_image_that_is_not_decodable():
    with pytest.raises(OcrUnavailable):
        ocr_engine.read_text(b"esto no es una imagen", "spa")


def test_read_text_rejects_an_image_over_the_megapixel_cap(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MAX_MEGAPIXELS", 0.0001)  # 100 px: la de 12×12 ya lo supera
    with pytest.raises(OcrUnavailable):
        ocr_engine.read_text(_png(), "spa")


def test_read_text_turns_a_tesseract_failure_into_ocr_unavailable(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("tesseract no está instalado")

    monkeypatch.setattr(ocr_engine.pytesseract, "image_to_data", boom)
    with pytest.raises(OcrUnavailable):
        ocr_engine.read_text(_png(), "spa")


# ---------------------------------------------------------------- orquestación (app/ocr/__init__.py)


class _StubBackend:
    def __init__(self, text: str, *, down: bool = False) -> None:
        self.text = text
        self.down = down

    def read_text(self, image: bytes, languages: str) -> OcrText:
        if self.down:
            raise OcrUnavailable("caído")
        return OcrText(text=self.text, confidence=0.7)


def test_extract_reads_the_fields_when_the_engine_is_available():
    ocr.use_backend(_StubBackend(PASSPORT_MRZ))
    try:
        result = ocr.extract("PASSPORT", b"img", "spa+eng")
    finally:
        ocr.use_backend(None)
    assert result.available and result.mrz_verified and result.confidence == 0.7
    assert result.fields["document_number"] == "L898902C3"


def test_extract_degrades_to_empty_when_the_engine_is_down():
    ocr.use_backend(_StubBackend("", down=True))
    try:
        result = ocr.extract("NATIONAL_ID", b"img", "spa")
    finally:
        ocr.use_backend(None)
    assert result == OcrResult(document_type="NATIONAL_ID")
    assert not result.available and result.fields == {}


def test_use_backend_none_restores_the_real_one():
    ocr.use_backend(_StubBackend("x"))
    ocr.use_backend(None)
    assert isinstance(ocr.backend(), RealBackend)
