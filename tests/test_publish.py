"""Tests de scripts/publish.py y scripts/mock_servers.py (R6, S-1069).

Las funciones puras se prueban sin red. La prueba de integracion levanta el FTPS y el
sign-manifest simulados y corre el ftp_release.py --fabrica del repo central sobre una
COPIA de sus scripts (su credentials.json lo busca junto a los scripts, asi que nunca ve
el de verdad). Necesita el central (TSP_CENTRAL o la carpeta hermana) y el puerto 21 libre.
La ficha de release (S-1275) se prueba contra release_ficha.py de ese mismo central: sin ficha
publicable ftp_release.py --fabrica no sube, y la comprobacion remota descarga cada binario.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY / 'scripts'))

import publish  # noqa: E402
from mock_servers import dryrun_sig  # noqa: E402
from publish import Fallo  # noqa: E402

SHA_A = 'a' * 64
SHA_B = 'b' * 64


def app(**kw):
    base = {'app': 'bright-cardinal', 'repo': 'javierferraz/2610-bright-cardinal',
            'version': '1.0.0', 'platforms': ['linux', 'windows']}
    base.update(kw)
    return base


# ── entradas ──

def test_parse_apps_normaliza_plataformas():
    out = publish.parse_apps(json.dumps([app()]))
    assert out == [dict(app(), platforms=['windows', 'linux'], tag='v1.0.0')]


def test_parse_apps_tag_de_pruebas():
    out = publish.parse_apps(json.dumps([app(tag='v1.0.0-factory-test')]))
    assert out[0]['tag'] == 'v1.0.0-factory-test'


@pytest.mark.parametrize('bad', [
    app(app='Bright Cardinal'),
    app(repo='otro/2610-bright-cardinal'),
    app(version='1.0'),
    app(platforms=['macos']),
    app(platforms=[]),
    dict(app(), extra=1),
    app(tag='v1.0.1'),
    app(tag='1.0.0'),
])
def test_parse_apps_rechaza(bad):
    with pytest.raises(Fallo):
        publish.parse_apps(json.dumps([bad]))


def test_parse_apps_rechaza_duplicadas_y_vacio():
    with pytest.raises(Fallo):
        publish.parse_apps(json.dumps([app(), app()]))
    with pytest.raises(Fallo):
        publish.parse_apps('[]')
    with pytest.raises(Fallo):
        publish.parse_apps('no es json')


def test_parse_sums():
    text = f'{SHA_A.upper()}  uno.exe\n{SHA_B} *dos.deb\nbasura\n'
    assert publish.parse_sums(text) == {'uno.exe': SHA_A, 'dos.deb': SHA_B}


def test_check_notes():
    ok = publish.check_notes({'windows': ' New  feature.\n', 'linux': 'Same on Linux.'}, ['windows', 'linux'])
    assert ok == {'windows': 'New feature.', 'linux': 'Same on Linux.'}
    with pytest.raises(Fallo):
        publish.check_notes({'windows': 'Fixes S-1069.'}, ['windows'])
    with pytest.raises(Fallo):
        publish.check_notes({'windows': 'Ahora la app guarda el fichero con una copia para el usuario'}, ['windows'])
    with pytest.raises(Fallo):
        publish.check_notes({'windows': 'ok'}, ['windows', 'linux'])


# ── manifest ──

def test_platform_state():
    assert publish.platform_state(None, None, '1.0.0', SHA_A, 'W') == 'nueva'
    assert publish.platform_state('1.0.0', SHA_A, '1.1.0', SHA_B, 'W') == 'nueva'
    assert publish.platform_state('1.0.0', SHA_A, '1.0.0', SHA_A, 'W') == 'igual'
    with pytest.raises(Fallo, match='anterior'):
        publish.platform_state('1.2.0', SHA_A, '1.1.9', SHA_B, 'W')
    with pytest.raises(Fallo, match='no se pisa'):
        publish.platform_state('1.0.0', SHA_A, '1.0.0', SHA_B, 'W')


def test_windows_block_conserva_orden_y_campos_ajenos():
    prev = {'status': 'stable', 'version': '0.9.0', 'installer_url': 'x', 'portable_url': None,
            'sha256': SHA_B, 'format': 'inno', 'notes': 'old', 'installer_size': 1, 'portable_size': None,
            'extra': 'se queda'}
    setup = {'name': 'w-v1.0.0-windows-x64-setup.exe', 'size': 10, 'sha256': SHA_A}
    port = {'name': 'w-v1.0.0-windows-x64-portable.zip', 'size': 20, 'sha256': SHA_B}
    blk = publish.windows_block(prev, 'w', '1.0.0', setup, port, 'inno', 'New.')
    assert list(blk) == list(prev)
    assert blk['extra'] == 'se queda'
    assert blk['installer_url'] == 'https://thestromboliproject.com/releases/w/v1.0.0/w-v1.0.0-windows-x64-setup.exe'
    assert (blk['sha256'], blk['installer_size'], blk['portable_size'], blk['notes']) == (SHA_A, 10, 20, 'New.')


def test_linux_block_experimental_y_etiquetas():
    appimage = {'name': 'c-v1.0.0-linux-x64.AppImage', 'size': 5, 'sha256': SHA_A}
    deb = {'name': 'c-v1.0.0-linux-x64.deb', 'size': 3, 'sha256': SHA_B}
    blk = publish.linux_block(None, 'c', '1.0.0', appimage, deb, True, 'Linux.')
    assert blk['status'] == 'experimental'
    assert [a['status'] for a in blk['assets']] == ['experimental', 'experimental']
    assert [a['label'] for a in blk['assets']] == ['Ubuntu 24.04+ / Debian 13+', 'Other distros / Ubuntu 22.04']
    assert blk['sha256'] == SHA_A and blk['installer_size'] == 5
    prev = {'assets': [{'format': 'deb', 'label': 'Debian a medida'}]}
    blk = publish.linux_block(prev, 'c', '1.0.0', appimage, deb, False, 'Linux.')
    assert [a['label'] for a in blk['assets']] == ['Debian a medida', 'Other distros']


def test_apply_plan_app_nueva():
    manifest = {'version': 1, 'tools': []}
    setup = {'name': 'c-v1.0.0-windows-x64-setup.exe', 'size': 10, 'sha256': SHA_A}
    plan = {'app': 'c', 'version': '1.0.0', 'display_name': 'C App', 'windows_format': 'nsis',
            'notes_url': 'https://github.com/javierferraz/c/blob/main/CHANGELOG.md',
            'windows_block': publish.windows_block(None, 'c', '1.0.0', setup, None, 'nsis', 'First.')}
    entry = publish.apply_plan(manifest, plan, '2026-11-10')
    assert manifest['tools'] == [entry]
    assert list(entry) == ['tool', 'display_name', 'version', 'channel', 'release_date', 'download_page',
                           'notes_url', 'platforms']
    assert list(entry['platforms']) == ['windows-x64', 'linux-x64', 'macos-arm64']
    assert entry['platforms']['linux-x64']['status'] == 'planned'
    assert entry['release_date'] == '2026-11-10'


def test_apply_plan_sin_cambios_no_toca_la_fecha():
    entry = {'tool': 'c', 'version': '1.0.0', 'release_date': '2026-10-01', 'platforms': {}}
    manifest = {'tools': [entry]}
    publish.apply_plan(manifest, {'app': 'c', 'version': '1.0.0'}, '2026-11-10')
    assert entry['release_date'] == '2026-10-01'


def test_latest_json():
    d = publish.latest_json('1.0.0', 'n', '2026-11-10T00:00:00Z', 'SIG', 'https://x/y.exe')
    assert d == {'version': '1.0.0', 'notes': 'n', 'pub_date': '2026-11-10T00:00:00Z',
                 'platforms': {'windows-x86_64': {'signature': 'SIG', 'url': 'https://x/y.exe'}}}


# ── novedades ──

NEWS = ('<main>\r\n        <div class="news-list">\r\n\r\n          <article class="news-item">\r\n'
        '            <p>old</p>\r\n          </article>\r\n</main>\r\n')


def test_news_insert_arriba_conserva_crlf_y_es_idempotente():
    out = publish.news_insert(NEWS, '2026-11-10', 'Bright Cardinal', '1.0.0', 'First <release>.', 'bright-cardinal')
    assert out.index('Version 1.0.0') < out.index('<p>old</p>')
    assert '\n' not in out.replace('\r\n', '')
    assert '<p>First &lt;release&gt;.</p>' in out
    assert publish.news_insert(out, '2026-11-11', 'Bright Cardinal', '1.0.0', 'x', 'bright-cardinal') == out


def test_news_insert_sin_ancla():
    with pytest.raises(Fallo):
        publish.news_insert('<main></main>', '2026-11-10', 'X', '1.0.0', 'n', 'x')


# ── central ──

CATALOG = '''const TOOLS = [
  { id: "bright-cardinal", animal: "Cardinal", name: "Bright Cardinal Presentations", price: 0 },
  { id: "snappy-wasp", name: "Snappy Wasp Screen Capture" },
];
const DOWNLOADS_HIDDEN = ["bright-cardinal", "other"];
'''


def test_display_name_y_downloads_hidden(tmp_path):
    (tmp_path / 'site' / 'assets').mkdir(parents=True)
    (tmp_path / 'site' / 'assets' / 'catalog.js').write_text(CATALOG, encoding='utf-8')
    assert publish.display_name(tmp_path, 'bright-cardinal') == 'Bright Cardinal Presentations'
    assert publish.downloads_hidden(tmp_path, 'bright-cardinal')
    assert not publish.downloads_hidden(tmp_path, 'snappy-wasp')
    with pytest.raises(Fallo):
        publish.display_name(tmp_path, 'no-existe')


# ── Authenticode (osslsigncode 2.9) ──
# Salida armada con los printf de osslsigncode.c del tag 2.9 (verify_signature, verify_authenticode,
# print_cert y main): en Windows no hay osslsigncode; el run de publish.yml la ejecuta de verdad.

def ossl_out(subject='/C=ES/ST=Zaragoza/L=Zaragoza/O=Francisco Javier Ferraz Rives/CN=Francisco Javier Ferraz Rives',
             sig='ok', n=1, final='Succeeded'):
    """Como sale con -ignore-timestamp -time -ignore-crl (el sello se verifica aparte con openssl)."""
    block = ("\nSignature Index: 0  (Primary Signature)\n\n"
             "Message digest algorithm  : SHA256\nCurrent message digest    : AB\nCalculated message digest : AB\n\n"
             "Signer's certificate:\n\t------------------\n\tSigner #0:\n"
             f"\t\tSubject: {subject}\n\t\tIssuer : /C=US/O=Microsoft Corporation/CN=Microsoft ID Verified CS EOC CA 04\n"
             "\t\tSerial : 33\n\t\tCertificate expiration date:\n\t\t\tnotBefore : Oct 28 03:19:15 2026 GMT\n"
             "\t\t\tnotAfter : Oct 31 03:19:15 2026 GMT\n\nCAfile: ms-root.pem\n\n"
             "Timestamp Server Signature verification is disabled\n"
             "Signature verification time: Oct 29 13:24:41 2026 GMT\n"
             "Signing certificate chain verified using:\nCRL online verification disabled\n"
             f"Signature verification: {sig}\n\n")
    return f"PE checksum   : 00C72808\n{block}Number of verified signatures: {n}\n{final}\n"


def test_parse_osslsigncode_ok():
    assert publish.parse_osslsigncode(ossl_out()) == 'Francisco Javier Ferraz Rives'


@pytest.mark.parametrize('kw', [
    {'sig': 'failed', 'final': 'Failed'},
    {'subject': '/C=US/O=Otro/CN=Otro Firmante'},
    {'n': 2},
    {'n': 0},
    {'final': 'Failed'},
])
def test_parse_osslsigncode_rechaza(kw):
    with pytest.raises(Fallo):
        publish.parse_osslsigncode(ossl_out(**kw))


def test_ms_root_pem_es_la_raiz_de_microsoft(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    cert = x509.load_pem_x509_certificate(publish.ms_root_pem(tmp_path).read_bytes())
    assert cert.fingerprint(hashes.SHA256()).hex() == publish.MS_ROOT_SHA256
    assert cert.subject.rfc4514_string().startswith(publish.MS_ROOT)


def test_pe_signature_sin_firma(tmp_path):
    f = tmp_path / 'x.exe'
    f.write_bytes(b'MZ' + bytes(200))
    with pytest.raises(Fallo):
        publish.pe_signature(f)


SAMPLE = os.environ.get('TSP_AUTHENTICODE_SAMPLE')


@pytest.mark.skipif(not shutil.which('openssl') or not SAMPLE, reason='sin openssl o sin TSP_AUTHENTICODE_SAMPLE')
def test_sello_de_tiempo_real(tmp_path):
    root = publish.ms_root_pem(tmp_path)
    when = publish.authenticode_timestamp(Path(SAMPLE), root, tmp_path)
    assert when.year >= 2026
    other = tmp_path / 'otra-raiz.pem'          # con otra raiz como ancla, el sello no verifica
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    import mscerts
    pems = re.findall(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----',
                      Path(mscerts.where()).read_bytes(), re.S)
    other.write_bytes(next(p for p in pems if b'Microsoft' not in
                           x509.load_pem_x509_certificate(p).subject.rfc4514_string().encode()))
    with pytest.raises(Fallo, match='raiz'):
        publish.authenticode_timestamp(Path(SAMPLE), other, tmp_path)


@pytest.mark.skipif(not shutil.which('osslsigncode') or not shutil.which('openssl') or not SAMPLE,
                    reason='sin osslsigncode u openssl o sin TSP_AUTHENTICODE_SAMPLE (un instalador firmado)')
def test_verify_authenticode_real_y_manipulado(tmp_path):
    sample = Path(SAMPLE)
    root = publish.ms_root_pem(tmp_path)
    signer, _ = publish.verify_authenticode(sample, root, tmp_path)
    assert signer == 'Francisco Javier Ferraz Rives'
    bad = tmp_path / sample.name
    data = bytearray(sample.read_bytes())
    data[len(data) // 3] ^= 0xFF
    bad.write_bytes(bytes(data))
    with pytest.raises(Fallo):
        publish.verify_authenticode(bad, root, tmp_path)


def test_dryrun_sig_ignora_el_orden_de_las_claves():
    assert dryrun_sig({'a': 1, 'b': [1, 2]}) == dryrun_sig({'b': [1, 2], 'a': 1})
    assert dryrun_sig({'a': 1}) != dryrun_sig({'a': 2})


def test_dry_wrapper_solo_firma_en_local():
    code = publish.DRY_WRAPPER
    assert '"127.0.0.1", "localhost"' in code and 'SIGN_ENDPOINT = endpoint' in code


# ── ficha de release (S-1275) ──

COMMIT = 'c' * 40
PRUEBAS_OK = {'funcionales': 'ok: tests', 'instalacion_limpia': 'ok: smoke', 'arranque': 'ok: smoke',
              'actualizacion': 'ok: v0.9.0 -> v1.0.0', 'desinstalacion': 'ok: smoke'}
AUT = {'quien': 'javierferraz', 'cuando': '2026-11-23T10:00:00Z', 'texto': 'tanda del 23', 'ejecucion': 'https://run'}


def _pruebas_win(**kw):
    d = {'app': 'demo', 'version': '1.0.0', 'commit': COMMIT, 'plataforma': 'windows-x64',
         'ejecucion': 'https://build', 'herramientas': {'python': '3.12'}, 'pruebas': dict(PRUEBAS_OK)}
    d.update(kw)
    return d


def _ficha(slug='demo', plats=('windows',), pruebas=None, visual=publish.SIN_INFORME_VISUAL, aut=AUT):
    pw = _pruebas_win(pruebas=pruebas or dict(PRUEBAS_OK))
    return publish.ficha_datos(slug, 'javierferraz/demo', '1.0.0', 'v1.0.0', COMMIT, list(plats), pw, visual,
                               'Authenticode del instalador', 'Faster start.', aut)


def _release_ficha():
    c = _central()
    if c is None:
        pytest.skip('sin el repo central (TSP_CENTRAL)')
    mod = publish.load_central_module(c, 'release_ficha')
    if not hasattr(mod, 'publicable'):
        pytest.skip('el central aun no tiene release_ficha.publicable (S-1275)')
    return mod, mod.cargar_politica(str(c / 'docs' / 'distribucion' / 'ficha-obligatoria.json'))


def test_pruebas_validas():
    assert publish.pruebas_validas(_pruebas_win(), 'demo', '1.0.0', COMMIT) == []
    assert publish.pruebas_validas(_pruebas_win(commit='d' * 40), 'demo', '1.0.0', COMMIT)
    assert publish.pruebas_validas(_pruebas_win(version='0.9.0'), 'demo', '1.0.0', COMMIT)
    sin = _pruebas_win(pruebas={k: v for k, v in PRUEBAS_OK.items() if k != 'actualizacion'})
    assert any('actualizacion' in m for m in publish.pruebas_validas(sin, 'demo', '1.0.0', COMMIT))
    assert publish.pruebas_validas([], 'demo', '1.0.0', COMMIT)


def test_informe_visual():
    assert publish.informe_visual(None, 'x.json') == publish.SIN_INFORME_VISUAL
    v = publish.informe_visual({'estado': 'verde', 'commit': COMMIT, 'medidas': 7, 'otro': 1}, 'x.json')
    assert v == {'estado': 'verde', 'commit': COMMIT, 'medidas': 7, 'fuente': 'x.json'}
    assert publish.informe_visual({'estado': 'verde', 'medidas': '7'}, 'x.json')['medidas'] == 0


def test_json_roto_de_la_release_no_tumba_la_tanda(tmp_path):
    # Revision S-1275: un JSON roto deja fuera esa app con su motivo, no lanza una excepcion.
    roto = tmp_path / 'roto.json'
    roto.write_text('{"estado": ', encoding='utf-8')
    data = publish.leer_json(roto)
    assert publish.pruebas_validas(data, 'demo', '1.0.0', COMMIT) == ['no es un informe de pruebas']
    assert publish.informe_visual(data, 'v.json')['estado'] == 'ilegible'
    assert publish.informe_visual([1], 'v.json')['estado'] == 'ilegible'


def test_parse_autorizacion():
    assert publish.parse_autorizacion('') == {}
    assert publish.parse_autorizacion(json.dumps(AUT)) == AUT
    for bad in ('{', '[]', json.dumps({'quien': 1}), json.dumps({**AUT, 'otra': 'x'})):
        with pytest.raises(Fallo):
            publish.parse_autorizacion(bad)


def test_ficha_completa_es_publicable():
    rf, pol = _release_ficha()
    d = _ficha(plats=('windows', 'linux'))
    assert rf.publicable(d, 'demo', 'windows-x64', pol) == []
    assert rf.publicable(d, 'demo', 'linux-x64', pol) == []     # Linux: aun sin pruebas exigidas
    assert d['pruebas_por_plataforma']['linux-x64']['instalacion_limpia'] == publish.SIN_PRUEBAS_LINUX
    assert 'pruebas' not in d


def test_ficha_sin_informe_de_pruebas_o_sin_autorizacion_no_es_publicable():
    rf, pol = _release_ficha()
    d = publish.ficha_datos('demo', 'javierferraz/demo', '1.0.0', 'v1.0.0', COMMIT, ['windows'], None,
                            publish.SIN_INFORME_VISUAL, 'x', 'notas', AUT)
    assert any('instalacion_limpia' in m for m in rf.publicable(d, 'demo', 'windows-x64', pol))
    assert any('autorizacion.quien' in m for m in rf.publicable(_ficha(aut={}), 'demo', 'windows-x64', pol))


def test_ficha_informe_visual_exigido():
    rf, pol = _release_ficha()
    app_exigida = next(iter(pol['informe_visual_exigido']))
    assert rf.publicable(_ficha(app_exigida), app_exigida, 'windows-x64', pol)
    verde = publish.informe_visual({'estado': 'verde', 'commit': COMMIT, 'medidas': 9}, 'v.json')
    assert rf.publicable(_ficha(app_exigida, visual=verde), app_exigida, 'windows-x64', pol) == []
    otro = publish.informe_visual({'estado': 'verde', 'commit': 'e' * 40, 'medidas': 9}, 'v.json')
    assert rf.publicable(_ficha(app_exigida, visual=otro), app_exigida, 'windows-x64', pol)


# ── integracion: ftp_release.py --fabrica contra el FTP simulado ──

def _central():
    for cand in (os.environ.get('TSP_CENTRAL'), FACTORY.parent / '2606-thestromboliproject'):
        if cand and (Path(cand) / 'scripts' / 'ftp_release.py').exists():
            return Path(cand)
    return None


def _port_free(port):
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.skipif(_central() is None, reason='sin el repo central (TSP_CENTRAL)')
@pytest.mark.skipif(not _port_free(21), reason='puerto 21 ocupado o sin permiso (en Linux: ip_unprivileged_port_start)')
def test_ftp_release_fabrica_sin_poda_y_suma_las_sumas_previas(tmp_path, monkeypatch):
    pytest.importorskip('pyftpdlib')
    # el manifest de prueba solo trae «demo»: frente al publicado de verdad retrocederia (S-1069)
    monkeypatch.setenv('TSP_MANIFEST_RETROCEDE_OK', '1')
    central = tmp_path / 'central'
    shutil.copytree(_central() / 'scripts', central / 'scripts',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (central / 'docs' / 'distribucion').mkdir(parents=True)
    shutil.copyfile(_central() / 'docs' / 'distribucion' / 'ficha-obligatoria.json',
                    central / 'docs' / 'distribucion' / 'ficha-obligatoria.json')
    (central / 'updates').mkdir()
    url = 'https://thestromboliproject.com/releases/demo/v1.0.0/demo-v1.0.0-windows-x64-setup.exe'
    manifest = {'version': 1, 'sig': 'vieja', 'tools': [
        {'tool': 'demo', 'version': '1.0.0', 'platforms': {'windows-x64': {'version': '1.0.0', 'installer_url': url}}}]}
    (central / 'updates' / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    setup = tmp_path / 'subir' / 'demo-v1.0.0-windows-x64-setup.exe'
    setup.parent.mkdir()
    setup.write_bytes(b'instalador de prueba')
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()

    mock = publish.Mock(central, tmp_path / 'work')
    mock.start()
    try:
        assert publish.credentials_host(central) == '127.0.0.1'
        sums_name = 'demo-v1.0.0-SHA256SUMS.txt'
        mock.seed(f'/releases/demo/v1.0.0/{sums_name}', f'{SHA_B}  demo-v1.0.0-linux-x64.AppImage\n'.encode())
        mock.seed('/releases/demo/v0.9.0/demo-v0.9.0-windows-x64-setup.exe', b'version vieja')
        mock.seed('/updates/manifest.json', b'{}')

        # Sin ficha publicable (S-1275): no se sube nada.
        mala = tmp_path / 'ficha-mala.json'
        publish.write_json(mala, _ficha('demo', pruebas={**PRUEBAS_OK, 'actualizacion': 'no_ejecutada: x'}))
        with pytest.raises(Fallo):
            publish.run_ftp_release(central, 'demo', '1.0.0', [str(setup)], mock.sign_endpoint,
                                    tmp_path / 'ftp.log', mala)
        assert not (mock.root / 'releases/demo/v1.0.0' / setup.name).exists()

        buena = tmp_path / 'ficha.json'
        publish.write_json(buena, _ficha('demo'))
        publish.run_ftp_release(central, 'demo', '1.0.0', [str(setup)], mock.sign_endpoint,
                                tmp_path / 'ftp.log', buena)

        root = mock.root
        sums = publish.parse_sums((root / 'releases/demo/v1.0.0' / sums_name).read_text(encoding='utf-8'))
        assert sums == {'demo-v1.0.0-linux-x64.AppImage': SHA_B, setup.name: digest}
        assert (root / 'releases/demo/v1.0.0' / setup.name).read_bytes() == setup.read_bytes()
        assert (root / 'releases/demo/v0.9.0/demo-v0.9.0-windows-x64-setup.exe').exists(), 'con --fabrica no se poda'
        signed = json.loads((central / 'updates' / 'manifest.json').read_text(encoding='utf-8'))
        assert signed['sig'] == dryrun_sig({k: v for k, v in signed.items() if k != 'sig'})
        assert json.loads((root / 'updates/manifest.json').read_text(encoding='utf-8')) == signed
        reqs = mock.sign_requests()
        assert len(reqs) == 1 and reqs[0]['secret_ok'] and not reqs[0]['has_sig']

        ficha = json.loads((central / 'releases-archive/demo/v1.0.0/ficha.json').read_text(encoding='utf-8'))
        win = ficha['plataformas']['windows-x64']
        assert win['faltan'] == [] and win['origen']['commit'] == COMMIT
        assert win['autorizacion']['quien'] == 'javierferraz'

        # Comprobacion remota por hash: pasa con el fichero subido y para si cambia un byte.
        files = {setup.name: {'size': setup.stat().st_size, 'sha256': digest}}
        mbytes = (central / 'updates' / 'manifest.json').read_bytes()
        publish.verify_remote(central, 'demo', '1.0.0', files, mbytes, {})
        (root / 'releases/demo/v1.0.0' / setup.name).write_bytes(b'instalador de pruebX')
        with pytest.raises(Fallo, match='SHA256 descargado'):
            publish.verify_remote(central, 'demo', '1.0.0', files, mbytes, {})
    finally:
        mock.stop()
    assert not (central / 'credentials.json').exists()


# ── push a main con otras sesiones subiendo a la vez (2026-11-16) ──

class _Rep:
    def __init__(self):
        self.avisos = []
        self.main_sin_subir = False


def _g(cwd, *args):
    import subprocess
    return subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                          capture_output=True, text=True, check=True).stdout


def _clones(tmp_path):
    """origin desnudo + clon 'fabrica' con su commit de tanda + clon 'otra' sesion."""
    origin = tmp_path / 'origin.git'
    _g(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(origin))
    fab = tmp_path / 'fabrica'
    _g(tmp_path, 'clone', '-q', str(origin), str(fab))
    (fab / 'updates').mkdir()
    (fab / 'updates' / 'manifest.json').write_text('{"v": 1}\n', encoding='utf-8')
    (fab / 'news.html').write_text(''.join(f'linea {i}\n' for i in range(20)), encoding='utf-8')
    _g(fab, 'add', '.')
    _g(fab, 'commit', '-q', '-m', 'inicio')
    _g(fab, 'push', '-q', 'origin', 'HEAD:main')
    base = _g(fab, 'rev-parse', 'HEAD').strip()
    otra = tmp_path / 'otra'
    _g(tmp_path, 'clone', '-q', str(origin), str(otra))
    (fab / 'updates' / 'manifest.json').write_text('{"v": 2}\n', encoding='utf-8')
    texto = (fab / 'news.html').read_text(encoding='utf-8').replace('linea 0\n', 'linea 0\nNOVEDAD\n')
    (fab / 'news.html').write_text(texto, encoding='utf-8')
    _g(fab, 'commit', '-q', '-am', 'release: tanda')
    return origin, fab, otra, base


def test_push_reintenta_si_otra_sesion_subio_antes(tmp_path):
    origin, fab, otra, base = _clones(tmp_path)
    (otra / 'otro.txt').write_text('x\n', encoding='utf-8')
    _g(otra, 'add', '.')
    _g(otra, 'commit', '-q', '-m', 'otra sesion')
    _g(otra, 'push', '-q', 'origin', 'HEAD:main')
    rep = _Rep()
    publish.push(fab, base, rep, espera=0)
    log = _g(origin, 'log', '--format=%s', 'main')
    assert log.splitlines()[:2] == ['release: tanda', 'otra sesion']
    assert any('intento 2' in a for a in rep.avisos)
    assert not any('CRITICO' in a for a in rep.avisos)


def test_push_rehace_el_commit_con_el_parche_si_el_rebase_choca(tmp_path, monkeypatch):
    origin, fab, otra, base = _clones(tmp_path)
    (otra / 'news.html').write_text((otra / 'news.html').read_text(encoding='utf-8')
                                    .replace('linea 19\n', 'linea 19 sellada\n'), encoding='utf-8')
    _g(otra, 'commit', '-q', '-am', 'otra sesion toca news')
    _g(otra, 'push', '-q', 'origin', 'HEAD:main')
    real = publish.git

    def git_rebase_falla(central, *args, check=True):
        if args and args[0] == 'rebase' and args[1] != '--abort':
            return real(central, 'rebase', 'no-existe-esta-ref', check=False)
        return real(central, *args, check=check)

    monkeypatch.setattr(publish, 'git', git_rebase_falla)
    publish.push(fab, base, _Rep(), espera=0)
    news = _g(origin, 'show', 'main:news.html')
    assert 'NOVEDAD' in news and 'linea 19 sellada' in news
    assert _g(origin, 'show', 'main:updates/manifest.json') == '{"v": 2}\n'
    assert _g(origin, 'log', '-1', '--format=%s', 'main').strip() == 'release: tanda'


def test_push_choque_real_para_y_deja_el_clon_limpio(tmp_path):
    origin, fab, otra, base = _clones(tmp_path)
    (otra / 'news.html').write_text((otra / 'news.html').read_text(encoding='utf-8')
                                    .replace('linea 0\n', 'linea 0\nOTRA COSA\n'), encoding='utf-8')
    (otra / 'updates' / 'manifest.json').write_text('{"v": 9}\n', encoding='utf-8')
    _g(otra, 'commit', '-q', '-am', 'choque')
    _g(otra, 'push', '-q', 'origin', 'HEAD:main')
    rep = _Rep()
    with pytest.raises(Fallo, match='a mano'):
        publish.push(fab, base, rep, espera=0)
    assert any('CRITICO' in a for a in rep.avisos)
    assert rep.main_sin_subir is True   # el cierre de la tanda lo convierte en aviso
    assert _g(fab, 'status', '--porcelain').strip() == ''
    assert _g(origin, 'log', '-1', '--format=%s', 'main').strip() == 'choque'
