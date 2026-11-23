"""Tests de scripts/anterior.py y scripts/pruebas_windows.py (S-1275, PAA2 P6-1). Sin red."""
import json
import sys
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY / 'scripts'))

import anterior  # noqa: E402
import pruebas_windows  # noqa: E402

APP = 'snappy-wasp'
COMMIT = 'a' * 40


def manifest(ver, url='https://thestromboliproject.com/releases/x/setup.exe', sha='AB' * 32):
    return {'tools': [{'tool': APP, 'version': ver,
                       'platforms': {'windows-x64': {'version': ver, 'installer_url': url, 'sha256': sha}}}]}


# ── anterior.py ──

def test_desde_manifest_menor_que_la_que_se_construye():
    assert anterior.desde_manifest(manifest('1.6.0'), APP, '1.7.0') == (
        '1.6.0', 'https://thestromboliproject.com/releases/x/setup.exe', 'ab' * 32)


@pytest.mark.parametrize('m', [manifest('1.7.0'), manifest('1.8.0'), manifest('1.6.0', sha=''),
                               {'tools': []}])
def test_desde_manifest_no_sirve(m):
    # igual o mayor (prueba de la misma versión), sin hash o app que no está: a GitHub
    assert anterior.desde_manifest(m, APP, '1.7.0') is None


def rel(tag, *names, draft=False, digest=None):
    return {'tag_name': tag, 'draft': draft,
            'assets': [{'name': n, **({'digest': digest} if digest else {})} for n in names]}


def test_candidatas_github_ordenadas_y_filtradas():
    releases = [
        rel('v1.5.0', f'{APP}-v1.5.0-windows-x64-setup.exe'),
        rel('v1.7.0-factory-test', f'{APP}-v1.7.0-windows-x64-setup.exe'),   # no es vX.Y.Z
        rel('v1.7.0', f'{APP}-v1.7.0-windows-x64-setup.exe'),                # no es menor
        rel('v1.6.2', 'SnappyWasp-1.6.2-setup.exe', 'SHA256SUMS.txt'),       # nombre viejo, único setup
        rel('v1.6.3', f'{APP}-v1.6.3-windows-x64-setup.exe', draft=True),    # borrador
        rel('v1.6.4', f'{APP}-v1.6.4-linux-x64.AppImage'),                    # sin instalador
        rel('v1.6.5', 'a-setup.exe', 'b-setup.exe'),                          # ambiguo
    ]
    got = [(v, s['name']) for v, _, s, _ in anterior.candidatas_github(releases, APP, '1.7.0')]
    assert got == [('1.6.2', 'SnappyWasp-1.6.2-setup.exe'), ('1.5.0', f'{APP}-v1.5.0-windows-x64-setup.exe')]


def test_hay_anteriores_distingue_primera_version():
    # Revision S-1275: «no encontre instalador» no puede pasar por «primera version».
    rels = [{'tag_name': 'v1.6.3'}, {'tag_name': 'v1.7.0'}, {'tag_name': 'v1.5.0', 'draft': True}]
    assert anterior.hay_anteriores({'tools': []}, rels, APP, '1.7.0') == '1.6.3'
    assert anterior.hay_anteriores(manifest('1.6.0'), [], APP, '1.7.0') == '1.6.0'
    assert anterior.hay_anteriores(manifest('1.7.0'), [{'tag_name': 'v1.7.0'}], APP, '1.7.0') == ''
    assert anterior.hay_anteriores({'tools': []}, [], APP, '1.0.0') == ''


def test_hash_esperado_prefiere_sha256sums_y_si_no_el_digest():
    setup = {'name': 'x-setup.exe', 'digest': 'sha256:' + 'C' * 64}
    sums = f"{'d' * 64}  x-setup.exe\n{'e' * 64}  otro.zip\n"
    assert anterior.hash_esperado(setup, [{'name': 'SHA256SUMS.txt'}], lambda: sums) == 'd' * 64
    assert anterior.hash_esperado(setup, [], lambda: pytest.fail('no hay SHA256SUMS')) == 'c' * 64
    assert anterior.hash_esperado({'name': 'x-setup.exe'}, [], lambda: '') is None


# ── pruebas_windows.py ──

UPDATE_OK = {'resultado': 'ok', 'texto': 'ok: v1.6.0 -> v1.7.0 en el runner (inno); 3 fichero(s) conservados'}


def construir(**kw):
    base = dict(app=APP, version='1.7.0', tag='v1.7.0', commit=COMMIT, ejecucion='https://run',
                tests_en='linux', instalador='inno', anterior={'version': '1.6.0'},
                actualizacion=UPDATE_OK, herramientas={'python': '3.12'})
    base.update(kw)
    return pruebas_windows.construir(**base)


def test_construir_con_actualizacion():
    d = construir()
    assert d['commit'] == COMMIT and d['plataforma'] == 'windows-x64'
    assert d['pruebas']['actualizacion'] == UPDATE_OK['texto']
    assert all(v.startswith('ok') for v in d['pruebas'].values())
    assert 'smoke-update.ps1' in d['pruebas']['desinstalacion']


def test_construir_sin_version_anterior_es_no_aplica():
    d = construir(anterior={'no_aplica': 'primera version'}, actualizacion=None, instalador='nsis')
    assert d['pruebas']['actualizacion'] == 'no_aplica: primera version'
    assert 'smoke-nsis.ps1' in d['pruebas']['instalacion_limpia']


@pytest.mark.parametrize('kw', [
    {'commit': 'abc'}, {'instalador': 'msi'},
    {'actualizacion': None},                                   # había anterior y no hay resultado
    {'actualizacion': {'resultado': 'fallo', 'texto': 'x'}},
])
def test_construir_rechaza(kw):
    with pytest.raises(ValueError):
        construir(**kw)


def test_cli_escribe_el_json(tmp_path):
    ant, upd, out = tmp_path / 'a.json', tmp_path / 'u.json', tmp_path / 'p.json'
    ant.write_text(json.dumps({'version': '1.6.0'}), encoding='utf-8-sig')   # PowerShell escribe con BOM
    upd.write_text(json.dumps(UPDATE_OK), encoding='utf-8')
    rc = pruebas_windows.main(['--app', APP, '--version', '1.7.0', '--tag', 'v1.7.0', '--commit', COMMIT,
                               '--ejecucion', 'https://run', '--tests-en', 'linux', '--instalador', 'inno',
                               '--anterior', str(ant), '--actualizacion', str(upd), '--out', str(out),
                               '--herramienta', 'python=3.12.7', '--herramienta', 'inno-setup=6.7.3'])
    assert rc == 0
    d = json.loads(out.read_text(encoding='utf-8'))
    assert d['herramientas'] == {'python': '3.12.7', 'inno-setup': '6.7.3'}
    assert pruebas_windows.nombre(APP, '1.7.0') == f'{APP}-v1.7.0-windows-pruebas.json'
