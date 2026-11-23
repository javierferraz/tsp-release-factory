"""informe_visual.py — informe visual del build de Windows (S-1276, PAA2 P6-2).

Nivel 2 del laboratorio (docs/organizacion/03-calidad.md §3 del central): render real antes de cada
release. El build-tauri-windows.yml mide la app en el runner con check_ui_fit_tauri.mjs del central
en dos pasadas y este script las junta en <app>-v<version>-informe-visual.json, que publish.py pone
en la ficha de release (release_ficha.publicable() exige «verde», el mismo commit y medidas > 0
donde ficha-obligatoria.json lo pide), y mete las capturas en <app>-v<version>-capturas.zip.

  - «medida»: las pantallas del inventario en las combinaciones de riesgo (--riesgo), en Edge con
    ventana real sobre la interfaz construida en el tag (el método calibrado en P1-4).
  - «instalada»: la app instalada de verdad, con su WebView2 y su puente real (--instalada).

Estado: «verde» si las dos pasadas dicen OK; «rojo» si alguna encuentra cortes; si no, «no_medido».

    python informe_visual.py --app clear-parrot --version 1.4.0 --tag v1.4.0 --commit <sha> \
        --ejecucion <url del run> --medida <carpeta> --instalada <carpeta> --out-dir <carpeta>
"""
import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ESQUEMA = 1
PASADAS = ('medida', 'instalada')


def nombres(app, version):
    return f'{app}-v{version}-informe-visual.json', f'{app}-v{version}-capturas.zip'


def leer_pasada(carpeta):
    """El informe.json de una pasada de check_ui_fit_tauri.mjs, o None si no hay."""
    if not carpeta:
        return None
    f = Path(carpeta) / 'informe.json'
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def resumen_pasada(nombre, inf):
    if inf is None:
        return {'resultado': 'NO MEDIDO', 'motivo': f'la pasada «{nombre}» no dejó informe', 'medidas': 0}
    cuenta = inf.get('cuenta') or {}
    out = {
        'resultado': inf.get('resultado') or 'NO MEDIDO',
        'modo': inf.get('modo', ''),
        'navegador': inf.get('navegador', ''),
        'pantallas': inf.get('pantallas', 0),
        'combinaciones': inf.get('combos') or [],
        'medidas': cuenta.get('ok', 0) + cuenta.get('cortes', 0),
        'cuenta': cuenta,
        'duracion_s': inf.get('duracion_s'),
        'cortes': [{k: u.get(k) for k in ('pantalla', 'tipo', 'selector', 'texto', 'pxMax', 'combos')}
                   for u in (inf.get('unicos') or [])][:40],
        'cortes_total': len(inf.get('unicos') or []),
        'sin_declarar': inf.get('sinDeclarar') or [],
        'no_medidas': [f"{m.get('pantalla')} · {m.get('combo')}: {m.get('motivo')}"
                       for m in (inf.get('medidasDetalle') or []) if m.get('estado') == 'no-medido'][:20],
    }
    if inf.get('ventanaReal') is not None:
        out['ventana_real'] = inf['ventanaReal']
    return out


def estado(pasadas):
    resultados = [p['resultado'] for p in pasadas.values()]
    if 'CORTES' in resultados:
        return 'rojo'
    if all(r == 'OK' for r in resultados) and all(p['medidas'] > 0 for p in pasadas.values()):
        return 'verde'
    return 'no_medido'


def construir(app, version, tag, commit, ejecucion, carpetas):
    if not re.fullmatch(r'[0-9a-f]{40}', commit or ''):
        raise ValueError(f'commit no valido: {commit!r}')
    pasadas = {n: resumen_pasada(n, leer_pasada(carpetas.get(n))) for n in PASADAS}
    return {
        'esquema': ESQUEMA, 'app': app, 'version': version, 'tag': tag, 'commit': commit,
        'plataforma': 'windows-x64', 'ejecucion': ejecucion, 'nivel': 2,
        'estado': estado(pasadas),
        'medidas': sum(p['medidas'] for p in pasadas.values()),
        'pasadas': pasadas,
    }


def empaquetar_capturas(carpetas, destino):
    """Zip con las capturas de las dos pasadas (carpeta = nombre de la pasada) y sus informe.md."""
    n = 0
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_DEFLATED) as z:
        for nombre in PASADAS:
            base = carpetas.get(nombre)
            if not base or not Path(base).is_dir():
                continue
            for f in sorted(Path(base).rglob('*')):
                if f.is_file() and (f.suffix.lower() == '.png' or f.name == 'informe.md'):
                    z.write(f, f'{nombre}/{f.relative_to(base).as_posix()}')
                    n += f.suffix.lower() == '.png'
    return n


def resumen_md(inf, zip_nombre, capturas):
    filas = [f"| Informe visual (nivel 2) | **{inf['estado']}**, {inf['medidas']} medidas; "
             f"{capturas} capturas en ``{zip_nombre}`` |"]
    for n, p in inf['pasadas'].items():
        txt = f"{p['resultado']}: {p['medidas']} medidas"
        if p.get('cortes_total'):
            txt += f", {p['cortes_total']} cortes"
        if p.get('motivo'):
            txt += f" ({p['motivo']})"
        if p.get('no_medidas'):
            txt += f"; sin medir: {p['no_medidas'][0]}"
        filas.append(f'| Render · {n} | {txt} |')
    return '\n'.join(filas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for a in ('app', 'version', 'tag', 'commit', 'ejecucion', 'out-dir'):
        ap.add_argument(f'--{a}', required=True)
    for n in PASADAS:
        ap.add_argument(f'--{n}', help=f'carpeta de la pasada «{n}»')
    ap.add_argument('--report', help='Markdown del job al que añadir las filas')
    a = ap.parse_args(argv)
    carpetas = {n: getattr(a, n) for n in PASADAS}
    inf = construir(a.app, a.version, a.tag, a.commit, a.ejecucion, carpetas)
    json_nombre, zip_nombre = nombres(a.app, a.version)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    capturas = empaquetar_capturas(carpetas, out / zip_nombre)
    inf['capturas'] = {'fichero': zip_nombre, 'cuantas': capturas}
    (out / json_nombre).write_text(json.dumps(inf, ensure_ascii=False, indent=1), encoding='utf-8')
    if a.report:
        with open(a.report, 'a', encoding='utf-8') as f:
            f.write(resumen_md(inf, zip_nombre, capturas) + '\n')
    print(f"{json_nombre}: {inf['estado']}, {inf['medidas']} medidas, {capturas} capturas")
    return 0


if __name__ == '__main__':
    sys.exit(main())
