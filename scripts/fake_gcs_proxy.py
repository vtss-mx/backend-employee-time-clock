"""Proxy de PRUEBAS delante de fsouza/fake-gcs-server para los simulacros de PITR (scripts/db_pitr_check.sh).

pgBackRest sube a GCS los archivos grandes por partes (subida reanudable): Google responde el identificador de la
subida en la cabecera `X-GUploader-UploadID` y fake-gcs-server solo lo pone en la URL de `Location`. Este proxy copia
cada petición tal cual y, si la respuesta trae `upload_id` en `Location`, agrega esa cabecera: así el simulacro pasa
por el MISMO camino de subida que el bucket real (partes de `repo1-storage-upload-chunk-size`), no por uno simplificado.
Nunca se usa fuera de un simulacro aislado.

    python scripts/fake_gcs_proxy.py <host:puerto del fake-gcs-server> [puerto propio, 4443]
"""

import http.client
import http.server
import sys
from urllib.parse import parse_qs, urlsplit

#: Cabeceras de una conexión (no de la petición): cada lado maneja las suyas.
_HOP = frozenset({"host", "connection", "keep-alive", "transfer-encoding", "content-length"})


class Proxy(http.server.BaseHTTPRequestHandler):
    """Reenvía al fake-gcs-server (`server.upstream`) y completa la cabecera de la subida reanudable."""

    protocol_version = "HTTP/1.1"
    # Sin Nagle: cabeceras y cuerpo salen en dos escrituras y, con el ACK diferido del cliente, cada respuesta esperaría
    # ≈40 ms (medido: una restauración de 23 MB pasaba de 1 s a 28 s).
    disable_nagle_algorithm = True

    def _relay(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        upstream = http.client.HTTPConnection(self.server.upstream, timeout=300)  # type: ignore[attr-defined]
        headers = {key: value for key, value in self.headers.items() if key.lower() not in _HOP}
        upstream.request(self.command, self.path, body=body, headers=headers)
        response = upstream.getresponse()
        data = response.read()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in _HOP:
                self.send_header(key, value)
        location = response.getheader("Location") or ""
        if upload_id := parse_qs(urlsplit(location).query).get("upload_id"):
            self.send_header("X-GUploader-UploadID", upload_id[0])
        # HEAD no trae cuerpo pero sí el tamaño del objeto: ese se respeta.
        size = response.getheader("Content-Length") if self.command == "HEAD" else None
        self.send_header("Content-Length", size or str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        upstream.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _relay

    def log_message(self, format: str, *args: object) -> None:  # la misma firma de la biblioteca estándar
        return None


def main() -> None:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", int(sys.argv[2]) if len(sys.argv) > 2 else 4443), Proxy)
    server.upstream = sys.argv[1]  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
