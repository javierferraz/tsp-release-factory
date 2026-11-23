"""pruebas_windows.py — informe de pruebas del build de Windows (S-1275, PAA2 P6-1).

Lo escribe el build-*-windows.yml al final, cuando todas sus pruebas han pasado (si una falla,
el job para y no hay informe ni Release), y se sube a la Release como
<app>-v<version>-windows-pruebas.json. publish.py lo lee para la ficha de release.

    python pruebas_windows.py --app snappy-wasp --version 1.7.0 --tag v1.7.0 --commit <sha> \
        --ejecucion <url del run> --tests-en linux --instalador inno \
        --anterior anterior.json [--actualizacion update.json] \
        --herramienta python=3.12.7 --herramienta inno=6.7.3 --out <fichero>
"""
import argparse
import json
import re
import sys
from pathlib import Path

ESQUEMA = 1


def nombre(app, version):
    return f'{app}-v{version}-windows-pruebas.json'


def construir(app, version, tag, commit, ejecucion, tests_en, instalador, anterior, actualizacion,
              herramientas):
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise ValueError(f'commit no valido: {commit!r}')
    if instalador not in ('inno', 'nsis'):
        raise ValueError(f'instalador no valido: {instalador!r}')
    smoke = 'smoke-inno.ps1' if instalador == 'inno' else 'smoke-nsis.ps1'
    if actualizacion and actualizacion.get('resultado') == 'ok':
        act = actualizacion['texto']
    elif anterior and anterior.get('no_aplica'):
        act = f"no_aplica: {anterior['no_aplica']}"
    else:
        raise ValueError('sin resultado de la prueba de actualizacion ni motivo para no hacerla')
    pruebas = {
        'funcionales': f'ok: tests de la app en el build ({tests_en})',
        'instalacion_limpia': f'ok: {smoke} en runner limpio (instala en silencio, registro, '
                              'version, firmas, acceso del menu Inicio)',
        'arranque': f'ok: {smoke} (abre y sigue vivo sin crash)',
        'actualizacion': act,
        'desinstalacion': f'ok: {smoke}' + (' y smoke-update.ps1' if actualizacion else '')
                          + ' (desinstala en silencio sin dejar registro, .exe ni acceso)',
    }
    return {'esquema': ESQUEMA, 'app': app, 'version': version, 'tag': tag, 'commit': commit,
            'plataforma': 'windows-x64', 'ejecucion': ejecucion, 'instalador': instalador,
            'herramientas': herramientas or {}, 'pruebas': pruebas,
            'detalle': {'anterior': anterior, 'actualizacion': actualizacion}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for a in ('app', 'version', 'tag', 'commit', 'ejecucion', 'tests-en', 'instalador', 'anterior', 'out'):
        ap.add_argument(f'--{a}', required=True)
    ap.add_argument('--actualizacion')
    ap.add_argument('--herramienta', action='append', default=[], help='nombre=version')
    a = ap.parse_args(argv)

    def leer(p):
        return json.loads(Path(p).read_text(encoding='utf-8-sig')) if p and Path(p).exists() else None
    try:
        data = construir(a.app, a.version, a.tag, a.commit, a.ejecucion, a.tests_en, a.instalador,
                         leer(a.anterior), leer(a.actualizacion),
                         dict(h.split('=', 1) for h in a.herramienta if '=' in h))
    except ValueError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(data['pruebas'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
