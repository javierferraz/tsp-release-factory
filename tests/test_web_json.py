"""Tests de S-1352: la web sirve HTML en vez del JSON a los runners (web_json.py, anterior.py y la
puerta 6d de publish.py). Sin red: un servidor HTTP local imita al hosting."""
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY / 'scripts'))

import anterior  # noqa: E402
import publish  # noqa: E402
import web_json  # noqa: E402
from web_json import NoEsJSON  # noqa: E402

# La página que llegó a los runners el 28-11 (HTTP 200, text/html, ~12 KB, run 46134313478)
PAGINA = (b'<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf8">\n'
          b'  <meta name="viewport" content="width=device-width">\n  <title>One moment, please...</title>\n'
          + b' ' * 12000 + b'</head></html>')
APP, REPO = 'snappy-wasp', 'javierferraz/2606-snappy-wasp'
EXE = b'MZ instalador de verdad'
SHA = hashlib.sha256(EXE).hexdigest()
MANIFEST = {'tools': [{'tool': APP, 'version': '1.6.0', 'platforms': {'windows-x64': {
    'version': '1.6.0', 'sha256': SHA,
    'installer_url': f'https://thestromboliproject.com/releases/{APP}/v1.6.0/{APP}-v1.6.0-windows-x64-setup.exe'}}}]}


@pytest.fixture
def hosting():
    """Servidor local: `respuestas` es la cola de (content-type, cuerpo) que irá sirviendo."""
    respuestas = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ctype, cuerpo = respuestas.pop(0) if respuestas else ('application/json', b'{}')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}', respuestas
    srv.shutdown()


# ── web_json ──

def test_html_con_200_es_no_es_json_y_dice_que_llego(hosting):
    url, cola = hosting
    cola.append(('text/html', PAGINA))
    with pytest.raises(NoEsJSON) as e:
        web_json.leer_una_vez(f'{url}/updates/manifest.json', 'ua')
    assert e.value.html
    assert 'text/html' in str(e.value) and 'One moment, please...' in str(e.value)


def test_reintenta_hasta_que_llega_el_json(hosting):
    url, cola = hosting
    cola += [('text/html', PAGINA), ('text/html', PAGINA), ('application/json', b'{"tools": [1]}')]
    pausas, log = [], []
    got = web_json.leer_json(f'{url}/x.json?nocache=1', 'ua', intentos=3, pausa=7,
                             _dormir=pausas.append, log=log.append)
    assert got == {'tools': [1]} and pausas == [7, 7]
    assert len([x for x in log if x.startswith('  intento')]) == 2


def test_sin_json_tras_los_intentos_falla_con_el_ultimo_error(hosting):
    url, cola = hosting
    cola += [('text/html', PAGINA)] * 2
    with pytest.raises(NoEsJSON) as e:
        web_json.leer_json(f'{url}/x.json', 'ua', intentos=2, pausa=0, log=lambda *_: None)
    assert e.value.html


def test_un_json_que_no_vale_cuenta_como_fallo(hosting):
    url, cola = hosting
    cola += [('application/json', b'{}')] * 2
    with pytest.raises(NoEsJSON):
        web_json.leer_json(f'{url}/x.json', 'ua', intentos=2, pausa=0, valido=lambda m: m.get('tools'),
                           log=lambda *_: None)


# ── anterior.py ──

def gh_falso(llamadas, manifest=MANIFEST, exe=EXE):
    def gh(*args):
        llamadas.append(args)
        if args[0] == 'api':
            return json.dumps(manifest)
        if args[:2] == ('release', 'download'):
            sub = Path(args[args.index('-D') + 1])
            sub.mkdir(parents=True, exist_ok=True)
            (sub / args[args.index('-p') + 1]).write_bytes(exe)
            return ''
        raise AssertionError(args)
    return gh


def test_manifest_de_la_web_en_html_se_lee_del_central(hosting, monkeypatch, tmp_path):
    """Reproduce el fallo de los builds 46153099040 y 46153460316: antes, JSONDecodeError y fuera."""
    url, cola = hosting
    cola += [('text/html', PAGINA)] * 3 + [('application/octet-stream', EXE)]
    llamadas = []
    monkeypatch.setattr(anterior, 'MANIFEST_URL', f'{url}/updates/manifest.json')
    monkeypatch.setattr(anterior, 'leer_json', lambda *a, **k: web_json.leer_json(
        *a, **dict(k, pausa=0)))
    manifest = json.loads(json.dumps(MANIFEST))
    manifest['tools'][0]['platforms']['windows-x64']['installer_url'] = f'{url}/{APP}-v1.6.0-windows-x64-setup.exe'
    monkeypatch.setattr(anterior, 'gh', gh_falso(llamadas, manifest))
    res = anterior.buscar(APP, REPO, '1.7.0', tmp_path)
    assert res['version'] == '1.6.0' and res['sha256'] == SHA
    assert llamadas[0][:2] == ('api', 'repos/javierferraz/2606-thestromboliproject/contents/updates/manifest.json')


def test_instalador_en_html_se_baja_de_la_release_con_el_hash_del_manifest(hosting, monkeypatch, tmp_path):
    url, cola = hosting
    cola.append(('text/html', PAGINA))
    llamadas = []
    monkeypatch.setattr(anterior, 'gh', gh_falso(llamadas))
    ruta, origen = anterior.instalador_publicado(REPO, '1.6.0', f'{url}/{APP}-v1.6.0-windows-x64-setup.exe',
                                                 SHA, tmp_path)
    assert ruta.read_bytes() == EXE and 'GitHub Release v1.6.0' in origen
    assert llamadas[0][:3] == ('release', 'download', 'v1.6.0')


def test_instalador_de_la_release_con_otro_hash_sigue_fallando(hosting, monkeypatch, tmp_path):
    url, cola = hosting
    cola.append(('text/html', PAGINA))
    monkeypatch.setattr(anterior, 'gh', gh_falso([], exe=b'MZ otro'))
    with pytest.raises(anterior.Fallo, match='no es el del manifest'):
        anterior.instalador_publicado(REPO, '1.6.0', f'{url}/x-setup.exe', SHA, tmp_path)


def test_binario_del_hosting_con_otro_hash_falla_sin_buscar_alternativa(hosting, monkeypatch, tmp_path):
    url, cola = hosting
    cola.append(('application/octet-stream', b'MZ otro'))
    llamadas = []
    monkeypatch.setattr(anterior, 'gh', gh_falso(llamadas))
    with pytest.raises(anterior.Fallo, match='no es el del manifest'):
        anterior.instalador_publicado(REPO, '1.6.0', f'{url}/x-setup.exe', SHA, tmp_path)
    assert llamadas == []


# ── publish.py, puerta 6d ──

class Rep:
    def __init__(self):
        self.avisos = []


def falla_con(e):
    def leer(url):
        raise e
    return leer


def test_latest_en_html_queda_como_aviso():
    rep = Rep()
    publish.latest_en_vivo('snappy-wasp-latest.json', '1.7.0', rep, leer=falla_con(NoEsJSON('x', html=True)))
    assert len(rep.avisos) == 1 and 'HTML' in rep.avisos[0]


def test_latest_con_otra_version_sigue_parando():
    with pytest.raises(publish.Fallo, match='sirve 1.6.0'):
        publish.latest_en_vivo('a-latest.json', '1.7.0', Rep(), leer=lambda u: {'version': '1.6.0'})


def test_latest_roto_que_no_es_html_sigue_parando():
    with pytest.raises(publish.Fallo, match='no da JSON'):
        publish.latest_en_vivo('a-latest.json', '1.7.0', Rep(), leer=falla_con(NoEsJSON('x', html=False)))


def test_latest_correcto_no_avisa():
    rep = Rep()
    publish.latest_en_vivo('a-latest.json', '1.7.0', rep, leer=lambda u: {'version': '1.7.0'})
    assert rep.avisos == []


# ── publish.py, despliegue simulado en seco (6c) ──

def test_en_seco_el_deploy_simulado_deja_las_fichas_en_el_ftp_simulado(tmp_path):
    """Reproduce el ensayo 46178211357: la puerta veía las fichas de la tanda «sin desplegar»."""
    ficha = tmp_path / 'site' / 'tools' / 'handy-raccoon' / 'index.html'
    ficha.parent.mkdir(parents=True)
    ficha.write_bytes(b'<a href="/releases/handy-raccoon/v1.9.1/x.exe">')
    (tmp_path / 'secreto.txt').write_text('no')

    class Mock:
        def __init__(self):
            self.ficheros = {}

        def seed(self, rel, data):
            self.ficheros[rel] = data

    salida = ('Conectando...\n12 ficheros a subir\n\n[DRY-RUN] Ficheros que se subirían:\n'
              '  /tools/handy-raccoon/index.html  (31.2 KB)\n  /../secreto.txt  (0.1 KB)\n'
              '  /tools/no-existe/index.html  (1.0 KB)\n')
    mock = Mock()
    listed = publish.simular_deploy(tmp_path, mock, salida)
    assert listed[0] == '/tools/handy-raccoon/index.html'
    assert mock.ficheros == {'/tools/handy-raccoon/index.html': ficha.read_bytes()}
