"""web_json.py — leer un JSON de thestromboliproject.com desde los runners, con reintentos (S-1352).

Desde GitHub el hosting contesta a veces HTTP 200 con una página HTML de ~12 KB en vez del JSON
(protección antibots, probablemente; desde el portátil se lee bien). Pasó en el central
(manifest_guard, S-1327/S-1320) y aquí: anterior.py tumbó los builds de Windows de Handy Raccoon y
Tiny Chicken el 28-11 y publish.py paró la puerta tras publicar. Cada intento fallido dice qué
llegó, para que el siguiente fallo no sea un misterio.
"""
import json
import re
import time
import urllib.request


class NoEsJSON(ValueError):
    """La web contestó, pero no con el JSON esperado. `html` dice si fue una página HTML."""

    def __init__(self, msg, html=False):
        super().__init__(msg)
        self.html = html


def describir(status, ctype, cuerpo):
    titulo = re.search(rb'<title[^>]*>(.*?)</title>', cuerpo[:20000], re.I | re.S)
    t = f', título «{titulo.group(1).decode("utf-8", "replace").strip()[:80]}»' if titulo else ''
    return f'HTTP {status}, {ctype}, {len(cuerpo)} bytes{t}, empieza por {cuerpo[:80]!r}'


def es_html(ctype, cuerpo):
    return 'html' in (ctype or '').lower() or cuerpo.lstrip()[:1] == b'<'


def leer_una_vez(url, user_agent, timeout=30):
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(f'{url}{sep}nocache={int(time.time())}',
                                 headers={'User-Agent': user_agent, 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        cuerpo, ctype, status = r.read(), r.headers.get('Content-Type'), r.status
    try:
        return json.loads(cuerpo.decode('utf-8'))
    except ValueError as e:
        raise NoEsJSON(f'{e} ({describir(status, ctype, cuerpo)})', html=es_html(ctype, cuerpo)) from None


def leer_json(url, user_agent, intentos=3, pausa=20, valido=None, log=print, _leer=leer_una_vez,
              _dormir=time.sleep):
    """El JSON de `url`, o la excepción del último intento. `valido(dato)` puede rechazar un JSON que
    llegó pero no sirve (p. ej. un «{}»): cuenta como intento fallido."""
    for n in range(1, intentos + 1):
        try:
            dato = _leer(url, user_agent)
            if valido and not valido(dato):
                raise NoEsJSON(f'JSON sin lo esperado: {json.dumps(dato)[:80]}')
            if n > 1:
                log(f'  {url}: leído al intento {n}/{intentos}')
            return dato
        except Exception as e:  # noqa: BLE001 - cada fallo se cuenta y se reintenta
            log(f'  intento {n}/{intentos} de leer {url}: {e}')
            if n == intentos:
                raise
            _dormir(pausa)
