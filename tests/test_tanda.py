"""tanda.py (S-1069, R8): plan de la tanda aprobada, subida de versión y sellado del CHANGELOG.

    python -m pytest tests/test_tanda.py -q
"""
import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tanda  # noqa: E402

UTC = datetime.timezone.utc
NOTAS = {'windows': 'Faster startup.', 'linux': 'Faster startup.'}


def tanda_de(**kw):
    t = {'fecha': '2026-11-16', 'caduca': '2026-11-17T05:00:00+00:00', 'estado': 'propuesta',
         'apps': [{'app': 'snappy-wasp', 'repo': 'javierferraz/2606-snappy-wasp', 'version': '1.6.3',
                   'platforms': ['windows', 'linux'], 'stack': 'pyside', 'notas': NOTAS}]}
    t.update(kw)
    return t


MANIFEST = {'tools': [{'tool': 'snappy-wasp', 'platforms': {'windows-x64': {'version': '1.6.2'}}}]}
LUNES = datetime.datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def test_plan_crea_tag_y_construye_todo():
    p = tanda.plan(tanda_de(), MANIFEST, lambda repo, tag: (False, set()), LUNES)
    x = p['apps'][0]
    assert (x['accion'], x['tag'], x['crear_tag'], x['build_windows'], x['build_linux']) == \
        ('publicar', 'v1.6.3', True, True, True)


def test_plan_no_reconstruye_lo_que_la_release_ya_tiene():
    assets = {'snappy-wasp-v1.6.3-windows-x64-setup.exe', 'snappy-wasp-v1.6.3-linux-x64.AppImage'}
    x = tanda.plan(tanda_de(), MANIFEST, lambda r, t: (True, assets), LUNES)['apps'][0]
    assert (x['crear_tag'], x['build_windows'], x['build_linux']) == (False, False, False)


def test_plan_salta_lo_ya_publicado():
    m = {'tools': [{'tool': 'snappy-wasp', 'platforms': {'windows-x64': {'version': '1.6.3'}}}]}
    x = tanda.plan(tanda_de(), m, lambda r, t: (True, set()), LUNES)['apps'][0]
    assert x['accion'] == 'nada' and 'ya está publicada' in x['motivo']


@pytest.mark.parametrize('kw,ahora,texto', [
    ({}, datetime.datetime(2026, 11, 17, 5, 1, tzinfo=UTC), 'caducó'),
    ({'estado': 'publicada'}, LUNES, '«publicada»'),
    ({'apps': []}, LUNES, 'no tiene apps'),
])
def test_plan_rechaza(kw, ahora, texto):
    with pytest.raises(tanda.Fallo, match=texto):
        tanda.plan(tanda_de(**kw), MANIFEST, lambda r, t: (False, set()), ahora)


def escribir(d, rel, texto):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(texto.encode('utf-8'))


CHANGELOG = '# Changelog\r\n\r\n## [Unreleased]\r\n\r\n### Fixed\r\n- Algo.\r\n\r\n## [1.6.2] — 2026-10-15\r\n'


def test_sellar_pyside(tmp_path):
    escribir(tmp_path, 'utils/version.py', '"""v."""\n__version__ = "1.6.2"\n')
    escribir(tmp_path, 'packaging/SnappyWasp.iss', '#define AppName "Snappy Wasp"\n#define AppVersion "1.6.2"\n')
    escribir(tmp_path, 'CHANGELOG.md', CHANGELOG)
    assert tanda.main(['sellar', '--dir', str(tmp_path), '--version', '1.6.3', '--stack', 'pyside',
                       '--fecha', '2026-11-16']) == 0
    assert '__version__ = "1.6.3"' in (tmp_path / 'utils/version.py').read_text()
    assert '#define AppVersion "1.6.3"' in (tmp_path / 'packaging/SnappyWasp.iss').read_text()
    cl = (tmp_path / 'CHANGELOG.md').read_bytes().decode()
    assert '## [Unreleased]\r\n\r\n## [1.6.3] — 2026-11-16\r\n\r\n### Fixed' in cl
    assert not tanda.sellar_changelog(tmp_path, '1.6.3', '2026-11-16')          # ya sellado


def test_sellar_tauri(tmp_path):
    escribir(tmp_path, 'src-tauri/tauri.conf.json', '{\n  "productName": "Bright Cardinal",\n  "version": "1.0.0",\n  "identifier": "x"\n}\n')
    escribir(tmp_path, 'package.json', '{\n  "name": "bright-cardinal",\n  "version": "1.0.0",\n  "dependencies": {"a": "1.0.0"}\n}\n')
    escribir(tmp_path, 'package-lock.json', '{\n  "name": "bright-cardinal",\n  "version": "1.0.0",\n  "packages": {\n    "": {\n      "name": "bright-cardinal",\n      "version": "1.0.0"\n    },\n    "node_modules/a": {\n      "version": "1.0.0"\n    }\n  }\n}\n')
    escribir(tmp_path, 'src-tauri/Cargo.toml', '[package]\nname = "bright-cardinal"\nversion = "1.0.0"\n\n[dependencies]\nx = { version = "1.0.0" }\n')
    escribir(tmp_path, 'src-tauri/Cargo.lock', '[[package]]\nname = "aaa"\nversion = "1.0.0"\n\n[[package]]\nname = "bright-cardinal"\nversion = "1.0.0"\n')
    escribir(tmp_path, 'CHANGELOG.md', '# Changelog\n\n## [Unreleased]\n\n- Algo.\n\n## [1.0.0] — 2026-11-01\n')
    tocados = tanda.subir_version(tmp_path, 'tauri', '1.1.0')
    assert set(tocados) == {'src-tauri/tauri.conf.json', 'package.json', 'package-lock.json',
                            'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock'}
    assert json.loads((tmp_path / 'src-tauri/tauri.conf.json').read_text())['version'] == '1.1.0'
    lock = json.loads((tmp_path / 'package-lock.json').read_text())
    assert (lock['version'], lock['packages']['']['version'], lock['packages']['node_modules/a']['version']) == ('1.1.0', '1.1.0', '1.0.0')
    assert json.loads((tmp_path / 'package.json').read_text())['dependencies'] == {'a': '1.0.0'}
    cargo = (tmp_path / 'src-tauri/Cargo.toml').read_text()
    assert 'version = "1.1.0"' in cargo and 'x = { version = "1.0.0" }' in cargo
    clock = (tmp_path / 'src-tauri/Cargo.lock').read_text()
    assert 'name = "aaa"\nversion = "1.0.0"' in clock and 'name = "bright-cardinal"\nversion = "1.1.0"' in clock
    assert tanda.subir_version(tmp_path, 'tauri', '1.1.0') == []                 # ya está


def test_sellar_sin_unreleased_falla(tmp_path):
    escribir(tmp_path, 'CHANGELOG.md', '# Changelog\n\n## [1.0.0]\n')
    with pytest.raises(tanda.Fallo, match='Unreleased'):
        tanda.sellar_changelog(tmp_path, '1.0.1', '2026-11-16')


def test_notas(tmp_path, capsys):
    p = tanda.plan(tanda_de(), MANIFEST, lambda r, t: (False, set()), LUNES)
    f = tmp_path / 'plan.json'
    f.write_text(json.dumps(p), encoding='utf-8')
    assert tanda.main(['notas', '--plan', str(f), '--app', 'snappy-wasp', '--out', str(tmp_path / 'rn.json')]) == 0
    assert json.loads((tmp_path / 'rn.json').read_text()) == NOTAS
    assert tanda.main(['notas-seco', '--plan', str(f)]) == 0
    assert json.loads(capsys.readouterr().out) == {'snappy-wasp': NOTAS}
