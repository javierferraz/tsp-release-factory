"""Tests de scripts/informe_visual.py (S-1276, PAA2 P6-2). Sin red ni runner."""
import json
import sys
import zipfile
from pathlib import Path

import pytest

FACTORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(FACTORY / 'scripts'))

import informe_visual  # noqa: E402

COMMIT = 'b' * 40


def pasada(tmp_path, nombre, resultado='OK', ok=4, cortes=0, no_medido=0, unicos=(), capturas=('a.png',), extra=None):
    d = tmp_path / nombre
    (d / 'capturas').mkdir(parents=True)
    for c in capturas:
        (d / 'capturas' / c).write_bytes(b'\x89PNG')
    (d / 'informe.md').write_text('# informe', encoding='utf-8')
    inf = {'resultado': resultado, 'modo': nombre, 'navegador': 'Edg/157', 'pantallas': 3,
           'combos': ['normal_en_defecto_claro'], 'cuenta': {'ok': ok, 'cortes': cortes, 'no-medido': no_medido},
           'unicos': list(unicos), 'sinDeclarar': [], 'medidasDetalle': [], **(extra or {})}
    (d / 'informe.json').write_text(json.dumps(inf), encoding='utf-8')
    return str(d)


def construir(carpetas):
    return informe_visual.construir('clear-parrot', '1.4.0', 'v1.4.0', COMMIT, 'https://run', carpetas)


def test_verde_si_las_dos_pasadas_dicen_ok(tmp_path):
    inf = construir({'medida': pasada(tmp_path, 'medida', ok=64), 'instalada': pasada(tmp_path, 'instalada', ok=3)})
    assert inf['estado'] == 'verde'
    assert inf['medidas'] == 67
    assert inf['commit'] == COMMIT and inf['plataforma'] == 'windows-x64'


def test_rojo_si_alguna_tiene_cortes(tmp_path):
    corte = {'pantalla': 'ajustes', 'tipo': 'recortado-ancho', 'selector': 'select', 'texto': 'Auto', 'pxMax': 86, 'combos': ['x']}
    inf = construir({'medida': pasada(tmp_path, 'medida', 'CORTES', ok=34, cortes=30, unicos=[corte]),
                     'instalada': pasada(tmp_path, 'instalada', ok=3)})
    assert inf['estado'] == 'rojo'
    assert inf['pasadas']['medida']['cortes_total'] == 1
    assert inf['pasadas']['medida']['cortes'][0]['pxMax'] == 86


@pytest.mark.parametrize('falta', ['medida', 'instalada'])
def test_no_medido_si_falta_una_pasada(tmp_path, falta):
    carpetas = {n: pasada(tmp_path, n) for n in informe_visual.PASADAS if n != falta}
    inf = construir(carpetas)
    assert inf['estado'] == 'no_medido'
    assert 'no dejó informe' in inf['pasadas'][falta]['motivo']


def test_no_medido_si_una_pasada_no_mide(tmp_path):
    inf = construir({'medida': pasada(tmp_path, 'medida'),
                     'instalada': pasada(tmp_path, 'instalada', 'NO MEDIDO', ok=0, no_medido=3)})
    assert inf['estado'] == 'no_medido'


def test_ok_sin_medidas_no_es_verde(tmp_path):
    inf = construir({'medida': pasada(tmp_path, 'medida'), 'instalada': pasada(tmp_path, 'instalada', ok=0)})
    assert inf['estado'] == 'no_medido'


def test_commit_no_valido(tmp_path):
    with pytest.raises(ValueError):
        informe_visual.construir('clear-parrot', '1.4.0', 'v1.4.0', 'abc', 'run', {})


def test_main_escribe_json_zip_y_report(tmp_path):
    med = pasada(tmp_path, 'medida', capturas=('bienvenida__normal.png', 'ajustes__normal.png'))
    ins = pasada(tmp_path, 'instalada', capturas=('instalada__ajustes__ventana.png',),
                 extra={'ventanaReal': [{'pantalla': 'ajustes', 'ok': True, 'tamano': '1100x760'}]})
    out = tmp_path / 'out'
    report = tmp_path / 'report.md'
    assert informe_visual.main(['--app', 'clear-parrot', '--version', '1.4.0', '--tag', 'v1.4.0', '--commit', COMMIT,
                                '--ejecucion', 'https://run', '--medida', med, '--instalada', ins,
                                '--out-dir', str(out), '--report', str(report)]) == 0
    inf = json.loads((out / 'clear-parrot-v1.4.0-informe-visual.json').read_text(encoding='utf-8'))
    assert inf['estado'] == 'verde' and inf['capturas'] == {'fichero': 'clear-parrot-v1.4.0-capturas.zip', 'cuantas': 3}
    assert inf['pasadas']['instalada']['ventana_real'][0]['tamano'] == '1100x760'
    with zipfile.ZipFile(out / 'clear-parrot-v1.4.0-capturas.zip') as z:
        assert sorted(z.namelist()) == ['instalada/capturas/instalada__ajustes__ventana.png', 'instalada/informe.md',
                                        'medida/capturas/ajustes__normal.png', 'medida/capturas/bienvenida__normal.png',
                                        'medida/informe.md']
    assert '**verde**' in report.read_text(encoding='utf-8')


def test_lo_lee_publish_como_espera_la_ficha(tmp_path):
    """El JSON que deja informe_visual.py es el que publish.informe_visual() convierte para la ficha."""
    import publish
    inf = construir({'medida': pasada(tmp_path, 'medida'), 'instalada': pasada(tmp_path, 'instalada')})
    v = publish.informe_visual(inf, 'clear-parrot-v1.4.0-informe-visual.json')
    assert v == {'estado': 'verde', 'commit': COMMIT, 'medidas': 8, 'fuente': 'clear-parrot-v1.4.0-informe-visual.json'}
