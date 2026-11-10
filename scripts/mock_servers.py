"""Servidores simulados para la publicacion en seco (R6, S-1069).

Levanta en 127.0.0.1, en el mismo proceso:

- un FTPS (pyftpdlib + TLS con certificado autofirmado) con la carpeta --root como raiz,
  para que ftp_release.py, deploy_site.py y audit_releases.py del repo central hablen con
  el de verdad sin cambiar una linea: el host de credentials.json apunta aqui;
- un sign-manifest falso: comprueba la cabecera X-Release-Secret y que el manifest llega
  sin "sig", y devuelve una firma "DRYRUN." derivada del contenido. No firma nada real.

Uso: python mock_servers.py --root DIR --state DIR --user U --password P --secret S
                            [--ftp-port 21] [--sign-port 8765]
(publish.py le pasa un --sign-port libre; el FTP va en el 21 porque ftp_release.py no deja elegir.)
Escribe <state>/ready cuando los dos escuchan y apunta cada peticion de firma en
<state>/sign-requests.jsonl. Solo escucha en 127.0.0.1.
"""
import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = '127.0.0.1'


def dryrun_sig(manifest):
    canon = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'DRYRUN.' + hashlib.sha256(canon.encode('utf-8')).hexdigest()


def self_signed_cert(path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'tsp-dry-run')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    with open(path, 'wb') as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
        fh.write(cert.public_bytes(serialization.Encoding.PEM))


def sign_handler(secret, log_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            entry = {'path': self.path, 'secret_ok': self.headers.get('X-Release-Secret') == secret}
            try:
                manifest = json.loads(body.decode('utf-8'))['manifest']
                entry['has_sig'] = 'sig' in manifest
                entry['tools'] = len(manifest.get('tools', []))
            except Exception as exc:
                manifest, entry['error'] = None, str(exc)
            ok = (self.path == '/api/sign-manifest' and entry['secret_ok']
                  and manifest is not None and not entry.get('has_sig'))
            if ok:
                entry['sig'] = dryrun_sig(manifest)
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry) + '\n')
            out = json.dumps({'sig': entry['sig']} if ok else {'error': 'rechazada'}).encode()
            self.send_response(200 if ok else 401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--state', required=True)
    ap.add_argument('--user', required=True)
    ap.add_argument('--password', required=True)
    ap.add_argument('--secret', required=True)
    ap.add_argument('--ftp-port', type=int, default=21)
    ap.add_argument('--sign-port', type=int, default=8765)
    a = ap.parse_args()

    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    logging.basicConfig(level=logging.WARNING)
    os.makedirs(a.root, exist_ok=True)
    os.makedirs(a.state, exist_ok=True)
    cert = os.path.join(a.state, 'mock-cert.pem')
    self_signed_cert(cert)

    auth = DummyAuthorizer()
    auth.add_user(a.user, a.password, a.root, perm='elradfmwMT')
    TLS_FTPHandler.certfile = cert
    TLS_FTPHandler.authorizer = auth
    TLS_FTPHandler.tls_control_required = True
    TLS_FTPHandler.tls_data_required = True
    TLS_FTPHandler.passive_ports = range(60000, 60200)
    ftp = FTPServer((HOST, a.ftp_port), TLS_FTPHandler)

    http = HTTPServer((HOST, a.sign_port), sign_handler(a.secret, os.path.join(a.state, 'sign-requests.jsonl')))
    threading.Thread(target=http.serve_forever, daemon=True).start()

    with open(os.path.join(a.state, 'ready'), 'w', encoding='utf-8') as fh:
        fh.write(f'{os.getpid()}\n')
    print(f'mock: FTPS {HOST}:{a.ftp_port}, sign-manifest http://{HOST}:{a.sign_port}/api/sign-manifest', flush=True)
    try:
        ftp.serve_forever(handle_exit=True)
    finally:
        http.shutdown()


if __name__ == '__main__':
    sys.exit(main())
