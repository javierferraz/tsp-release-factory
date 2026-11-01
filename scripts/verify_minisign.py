"""Comprueba la firma minisign (.sig del actualizador de Tauri) de un fichero.

Uso: python verify_minisign.py <fichero> <fichero.sig> <pubkey-base64-de-tauri.conf.json>

El .sig de Tauri y la pubkey de tauri.conf.json son el texto de minisign codificado en
base64. Se verifica la firma del fichero (prehash BLAKE2b si el algoritmo es "ED") y la
firma global del comentario de confianza, con la clave publica que el actualizador de la
app tiene embebida. Sale con 0 si todo cuadra; con 1 y un mensaje si no.
"""
import base64
import hashlib
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def minisign_lines(b64_text: str) -> list[str]:
    return base64.b64decode(b64_text.strip()).decode("utf-8").splitlines()


def main() -> int:
    path, sig_path, pubkey_b64 = sys.argv[1:4]
    pub = base64.b64decode(minisign_lines(pubkey_b64)[1])
    if pub[:2] != b"Ed" or len(pub) != 42:
        print("ERROR: la clave publica no tiene formato minisign Ed25519")
        return 1
    with open(sig_path, encoding="utf-8") as fh:
        sig_lines = minisign_lines(fh.read())
    raw = base64.b64decode(sig_lines[1])
    alg, key_id, signature = raw[:2], raw[2:10], raw[10:74]
    if key_id != pub[2:10]:
        print(f"ERROR: el .sig es de la clave {key_id[::-1].hex().upper()}, "
              f"la app espera {pub[2:10][::-1].hex().upper()}")
        return 1
    prefix = "trusted comment: "
    if not sig_lines[2].startswith(prefix):
        print("ERROR: el .sig no trae comentario de confianza")
        return 1
    trusted = sig_lines[2][len(prefix):].encode("utf-8")
    global_sig = base64.b64decode(sig_lines[3])
    with open(path, "rb") as fh:
        data = fh.read()
    message = hashlib.blake2b(data, digest_size=64).digest() if alg == b"ED" else data
    key = Ed25519PublicKey.from_public_bytes(pub[10:42])
    try:
        key.verify(signature, message)
        key.verify(global_sig, signature + trusted)
    except InvalidSignature:
        print("ERROR: la firma minisign no corresponde al fichero")
        return 1
    print(f"OK: firma minisign valida (clave {key_id[::-1].hex().upper()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
