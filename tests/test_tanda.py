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


def test_sellar_tauri_con_package_lock_desfasado(tmp_path):
    """S-1327: la tanda del 26-11 dejó fuera Clever Cat (lock en 1.4.8, app en 1.8.6) y Tiny Chicken
    (lock en 1.7.1, app en 1.8.0) con «package-lock.json: esperaba 2 versión(es) que cambiar y hay 0».
    La versión propia del lock es solo un dato (npm la reescribe): se pone la nueva sea cual sea la vieja."""
    escribir(tmp_path, 'src-tauri/tauri.conf.json', '{\n  "productName": "Clever Cat",\n  "version": "1.8.6",\n  "identifier": "x"\n}\n')
    escribir(tmp_path, 'package.json', '{\n  "name": "clever-cat",\n  "version": "1.8.6",\n  "dependencies": {"a": "1.4.8"}\n}\n')
    escribir(tmp_path, 'package-lock.json', '{\r\n  "name": "clever-cat",\r\n  "version": "1.4.8",\r\n  "lockfileVersion": 3,\r\n  "packages": {\r\n    "": {\r\n      "name": "clever-cat",\r\n      "version": "1.4.8"\r\n    },\r\n    "node_modules/a": {\r\n      "version": "1.4.8"\r\n    }\r\n  }\r\n}\r\n')
    escribir(tmp_path, 'src-tauri/Cargo.toml', '[package]\nname = "clever-cat"\nversion = "1.8.6"\n')
    assert 'package-lock.json' in tanda.subir_version(tmp_path, 'tauri', '1.9.0')
    lock = json.loads((tmp_path / 'package-lock.json').read_text())
    assert (lock['version'], lock['packages']['']['version'], lock['packages']['node_modules/a']['version']) == ('1.9.0', '1.9.0', '1.4.8')
    assert b'\r\n' in (tmp_path / 'package-lock.json').read_bytes()           # respeta los finales de línea


def test_sellar_tauri_lock_sin_su_nombre_sigue_fallando(tmp_path):
    """Lo desfasado se tolera; un lock que no es de esta app (otro nombre) sigue parando la tanda."""
    escribir(tmp_path, 'src-tauri/tauri.conf.json', '{\n  "version": "1.0.0"\n}\n')
    escribir(tmp_path, 'package.json', '{\n  "name": "bright-cardinal",\n  "version": "1.0.0"\n}\n')
    escribir(tmp_path, 'package-lock.json', '{\n  "name": "otra-app",\n  "version": "1.0.0",\n  "packages": {\n    "": {\n      "name": "otra-app",\n      "version": "1.0.0"\n    }\n  }\n}\n')
    escribir(tmp_path, 'src-tauri/Cargo.toml', '[package]\nname = "bright-cardinal"\nversion = "1.0.0"\n')
    with pytest.raises(tanda.Fallo, match='package-lock.json'):
        tanda.subir_version(tmp_path, 'tauri', '1.1.0')


EXTRA = [{'seccion': 'Fixed', 'linea': '- **Otra cosa** arreglada.'},
         {'seccion': 'Added', 'linea': '- **Nueva** función.'}]


def test_sellar_anade_lineas_del_vigia_antes_de_sellar(tmp_path):
    """S-1307: la línea va al final de su sección; si la sección falta, se crea."""
    escribir(tmp_path, 'CHANGELOG.md', CHANGELOG)
    assert tanda.sellar_changelog(tmp_path, '1.6.3', '2026-11-16', EXTRA)
    cl = (tmp_path / 'CHANGELOG.md').read_bytes().decode()
    assert cl == ('# Changelog\r\n\r\n## [Unreleased]\r\n\r\n## [1.6.3] — 2026-11-16\r\n\r\n'
                  '### Fixed\r\n- Algo.\r\n- **Otra cosa** arreglada.\r\n\r\n'
                  '### Added\r\n- **Nueva** función.\r\n\r\n## [1.6.2] — 2026-10-15\r\n')


def test_sellar_con_plan_por_la_cli(tmp_path):
    escribir(tmp_path, 'utils/version.py', '__version__ = "1.6.2"\n')
    escribir(tmp_path, 'packaging/SnappyWasp.iss', '#define AppVersion "1.6.2"\n')
    escribir(tmp_path, 'CHANGELOG.md', '# Changelog\n\n## [Unreleased]\n\n## [1.6.2] — 2026-10-15\n')
    p = tanda.plan(tanda_de(apps=[{**tanda_de()['apps'][0], 'changelog_extra': EXTRA[:1]}]),
                   MANIFEST, lambda r, t: (False, set()), LUNES)
    assert p['apps'][0]['changelog_extra'] == EXTRA[:1]
    f = tmp_path / 'plan.json'
    f.write_text(json.dumps(p), encoding='utf-8')
    assert tanda.main(['sellar', '--dir', str(tmp_path), '--version', '1.6.3', '--stack', 'pyside',
                       '--fecha', '2026-11-16', '--plan', str(f), '--app', 'snappy-wasp']) == 0
    cl = (tmp_path / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert cl == ('# Changelog\n\n## [Unreleased]\n\n## [1.6.3] — 2026-11-16\n\n'
                  '### Fixed\n- **Otra cosa** arreglada.\n\n## [1.6.2] — 2026-10-15\n')


def test_plan_sin_crear_tag_no_lleva_lineas():
    app = {**tanda_de()['apps'][0], 'changelog_extra': EXTRA}
    x = tanda.plan(tanda_de(apps=[app]), MANIFEST, lambda r, t: (True, set()), LUNES)['apps'][0]
    assert 'changelog_extra' not in x


def test_sellar_no_duplica_ni_acepta_secciones_raras(tmp_path):
    escribir(tmp_path, 'CHANGELOG.md', CHANGELOG)
    texto = tanda.anotar_unreleased(CHANGELOG, [{'seccion': 'Fixed', 'linea': '- Algo.'}], '\r\n')
    assert texto == CHANGELOG
    with pytest.raises(tanda.Fallo, match='no válida'):
        tanda.sellar_changelog(tmp_path, '1.6.3', '2026-11-16', [{'seccion': 'Rotas', 'linea': '- x'}])


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


def test_plan_solo_linux_compara_con_linux_y_no_toca_windows():
    app = dict(tanda_de()['apps'][0], version='1.6.2', platforms=['linux'])
    m = {'tools': [{'tool': 'snappy-wasp', 'platforms': {'windows-x64': {'version': '1.6.2'},
                                                         'linux-x64': {'version': '1.6.0'}}}]}
    x = tanda.plan(tanda_de(apps=[app]), m, lambda r, t: (True, {'snappy-wasp-v1.6.2-windows-x64-setup.exe'}), LUNES)['apps'][0]
    assert (x['accion'], x['crear_tag'], x['build_windows'], x['build_linux']) == ('publicar', False, False, True)
    m['tools'][0]['platforms']['linux-x64']['version'] = '1.6.2'
    assert tanda.plan(tanda_de(apps=[app]), m, lambda r, t: (True, set()), LUNES)['apps'][0]['accion'] == 'nada'


# ----------------------------- R11: integrar ramas terminadas ------------------------------

def test_plan_arrastra_las_ramas_solo_si_crea_el_tag():
    t = tanda_de()
    t['apps'][0]['ramas'] = [{'rama': 's1200', 'sha': 'f' * 40, 'extra': 'x'}]
    x = tanda.plan(t, MANIFEST, lambda r, g: (False, set()), LUNES)['apps'][0]
    assert x['ramas'] == [{'rama': 's1200', 'sha': 'f' * 40}]
    x = tanda.plan(t, MANIFEST, lambda r, g: (True, set()), LUNES)['apps'][0]
    assert 'ramas' not in x
    assert 'ramas' not in tanda.plan(tanda_de(), MANIFEST, lambda r, g: (False, set()), LUNES)['apps'][0]


@pytest.fixture
def clon(tmp_path, monkeypatch):
    """Un repo con origin y las ramas: lista, notas (choca solo en el CHANGELOG) y choca (en código)."""
    import subprocess
    for k, v in {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                 'GIT_COMMITTER_EMAIL': 't@t', 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(k, v)

    def g(*args, cwd=None):
        r = subprocess.run(['git', *args], cwd=cwd or repo, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def commit(f, txt, msg):
        (repo / f).write_text(txt, encoding='utf-8')
        g('add', f)
        g('commit', '-q', '-m', msg)

    origin, repo = tmp_path / 'o.git', tmp_path / 'app'
    g('init', '-q', '--bare', '-b', 'main', str(origin), cwd=tmp_path)
    g('clone', '-q', str(origin), str(repo), cwd=tmp_path)
    g('checkout', '-q', '-b', 'main')
    commit('a.txt', 'uno\n', 'inicio')
    commit('CHANGELOG.md', '## [Unreleased]\n\n## [1.0.0]\n', 'notas')
    shas = {}
    for rama, (f, txt) in {'lista': ('b.txt', 'b\n'),
                           'notas': ('CHANGELOG.md', '## [Unreleased]\n- de la rama\n\n## [1.0.0]\n'),
                           'choca': ('a.txt', 'dos\n')}.items():
        g('checkout', '-q', '-b', rama, 'main')
        commit(f, txt, f'{rama}\n\nRama: terminada')
        shas[rama] = g('rev-parse', 'HEAD')
    g('checkout', '-q', 'main')
    commit('a.txt', 'tres\n', 'main avanza')
    commit('CHANGELOG.md', '## [Unreleased]\n- de main\n\n## [1.0.0]\n', 'nota de main')
    return repo, shas, g


def test_integrar_une_notas_y_deja_la_principal_lista(clon):
    repo, shas, g = clon
    hechas = tanda.integrar(repo, [{'rama': 'lista', 'sha': shas['lista']}, {'rama': 'notas', 'sha': shas['notas']}],
                            'tanda 2026-11-19')
    assert hechas == ['lista', 'notas']
    assert (repo / 'b.txt').exists()
    assert (repo / 'CHANGELOG.md').read_text(encoding='utf-8') == '## [Unreleased]\n- de main\n- de la rama\n\n## [1.0.0]\n'
    assert g('status', '--porcelain') == '' and g('log', '-1', '--format=%s') == 'Integra notas (tanda 2026-11-19)'
    assert tanda.integrar(repo, [{'rama': 'lista', 'sha': shas['lista']}], 'otra') == []   # ya dentro


def test_integrar_conflicto_de_codigo_deja_fuera_sin_tocar(clon):
    repo, shas, g = clon
    antes = g('rev-parse', 'HEAD')
    with pytest.raises(tanda.Fallo, match='choca con la principal en a.txt'):
        tanda.integrar(repo, [{'rama': 'choca', 'sha': shas['choca']}], 'tanda')
    assert g('rev-parse', 'HEAD') == antes and g('status', '--porcelain') == ''


@pytest.mark.parametrize('rama,sha,texto', [
    ('x; rm -rf /', 'f' * 40, 'no válida'),
    ('lista', 'zz', 'no válida'),
    ('lista', '0' * 40, 'ya no tiene el commit'),
])
def test_integrar_rechaza_entradas_malas(clon, rama, sha, texto):
    with pytest.raises(tanda.Fallo, match=texto):
        tanda.integrar(clon[0], [{'rama': rama, 'sha': sha}], 'tanda')
