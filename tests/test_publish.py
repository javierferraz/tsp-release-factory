"""Tests de scripts/publish.py y scripts/mock_servers.py (R6, S-1069).

Las funciones puras se prueban sin red. La prueba de integracion levanta el FTPS y el
sign-manifest simulados y corre el ftp_release.py --fabrica del repo central sobre una
COPIA de sus scripts (su credentials.json lo busca junto a los scripts, asi que nunca ve
el de verdad). Necesita el central (TSP_CENTRAL o la carpeta hermana) y el puerto 21 libre.
"""
import hashlib
import json
import os
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
    assert out == [dict(app(), platforms=['windows', 'linux'])]


@pytest.mark.parametrize('bad', [
    app(app='Bright Cardinal'),
    app(repo='otro/2610-bright-cardinal'),
    app(version='1.0'),
    app(platforms=['macos']),
    app(platforms=[]),
    dict(app(), extra=1),
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


def test_dryrun_sig_ignora_el_orden_de_las_claves():
    assert dryrun_sig({'a': 1, 'b': [1, 2]}) == dryrun_sig({'b': [1, 2], 'a': 1})
    assert dryrun_sig({'a': 1}) != dryrun_sig({'a': 2})


def test_dry_wrapper_solo_firma_en_local():
    code = publish.DRY_WRAPPER
    assert '"127.0.0.1", "localhost"' in code and 'SIGN_ENDPOINT = endpoint' in code


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
def test_ftp_release_fabrica_sin_poda_y_suma_las_sumas_previas(tmp_path):
    pytest.importorskip('pyftpdlib')
    central = tmp_path / 'central'
    shutil.copytree(_central() / 'scripts', central / 'scripts',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
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

        publish.run_ftp_release(central, 'demo', '1.0.0', [str(setup)], mock.sign_endpoint, tmp_path / 'ftp.log')

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
    finally:
        mock.stop()
    assert not (central / 'credentials.json').exists()
