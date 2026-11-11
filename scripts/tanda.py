#!/usr/bin/env python3
"""tanda.py — lleva a cabo la tanda que Javier aprueba (S-1069, R8). Lo usa .github/workflows/tanda.yml.

La tanda la prepara el Vigía Releases del central (R7) en docs/distribucion/tandas/<fecha>.json.
Al pulsar «Sí, publica la tanda» en Telegram, Javier abre este workflow en GitHub y lo lanza: ese
clic es el «sí». Este script:

  plan     lee la tanda del central (la de la fecha pedida o la última), comprueba que sigue en
           «propuesta» y no ha caducado (24 h), y por cada app decide qué falta: crear el tag
           (subir la versión y sellar el CHANGELOG), construir Windows y/o Linux (lo que la
           Release aún no tenga) o nada si ya está publicada en el manifest.
  sellar   en una copia del repo de la app: sube la versión en los ficheros de su stack y sella
           el [Unreleased] del CHANGELOG como «## [X.Y.Z] — fecha». El commit, el tag y el push los
           hace el workflow (y solo de verdad, no en el ensayo).
  notas    escribe el release-notes.json de una app (lo que publish.py lee de la Release).
  notas-seco  las notas de todas las apps del plan en un JSON {slug: {...}} para publish.yml en seco.

Uso:
  python scripts/tanda.py plan [--fecha AAAA-MM-DD] --out DIR
  python scripts/tanda.py sellar --dir DIR --version X.Y.Z --stack tauri|pyside [--fecha AAAA-MM-DD]
  python scripts/tanda.py notas --plan FICHERO --app SLUG --out FICHERO
  python scripts/tanda.py notas-seco --plan FICHERO
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

CENTRAL = 'javierferraz/2606-thestromboliproject'
TANDAS = 'docs/distribucion/tandas'


class Fallo(Exception):
    """La tanda no se puede llevar a cabo; el mensaje sale en el resumen."""


# ----------------------------- GitHub -----------------------------------------

def gh(*args, check=True):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, encoding='utf-8')
    if check and r.returncode != 0:
        raise Fallo(f'gh {" ".join(args[:3])}…: {(r.stderr or r.stdout).strip()[:300]}')
    return r


def leer_central(path):
    return gh('api', '-H', 'Accept: application/vnd.github.raw', f'repos/{CENTRAL}/contents/{path}').stdout


def ultima_tanda():
    r = gh('api', f'repos/{CENTRAL}/contents/{TANDAS}', '--jq', '.[].name', check=False)
    nombres = sorted(n for n in r.stdout.split() if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', n))
    if r.returncode != 0 or not nombres:
        raise Fallo(f'no hay ninguna tanda en {TANDAS} del central')
    return nombres[-1][:-5]


def release(repo, tag):
    """(existe el tag, nombres de los ficheros de la Release)."""
    tag_ok = gh('api', f'repos/{repo}/git/ref/tags/{tag}', check=False).returncode == 0
    r = gh('release', 'view', tag, '-R', repo, '--json', 'assets', '--jq', '.assets[].name', check=False)
    return tag_ok, (set(r.stdout.split()) if r.returncode == 0 else set())


# ----------------------------- plan -------------------------------------------

def validar(tanda, ahora):
    if tanda.get('estado') != 'propuesta':
        raise Fallo(f'la tanda del {tanda.get("fecha")} está en «{tanda.get("estado")}», no en «propuesta»')
    if not tanda.get('apps'):
        raise Fallo(f'la tanda del {tanda.get("fecha")} no tiene apps listas')
    caduca = datetime.datetime.fromisoformat(tanda['caduca'])
    if ahora > caduca:
        raise Fallo(f'la tanda del {tanda["fecha"]} caducó el {caduca:%Y-%m-%d %H:%M} UTC: '
                    'sin «sí» en 24 h no sale; espera a la siguiente')


def publicada(manifest, slug):
    t = next((t for t in manifest.get('tools', []) if t.get('tool') == slug), None) or {}
    return (t.get('platforms', {}).get('windows-x64') or {}).get('version') or t.get('version')


def plan_app(app, manifest, tag_existe, assets):
    slug, ver = app['app'], app['version']
    p = {k: app[k] for k in ('app', 'repo', 'version', 'platforms', 'stack', 'notas')}
    p['tag'] = f'v{ver}'
    if publicada(manifest, slug) == ver:
        p.update(accion='nada', motivo=f'la {ver} ya está publicada en el manifest')
        return p
    p['crear_tag'] = not tag_existe
    p['build_windows'] = 'windows' in app['platforms'] and f'{slug}-v{ver}-windows-x64-setup.exe' not in assets
    p['build_linux'] = 'linux' in app['platforms'] and f'{slug}-v{ver}-linux-x64.AppImage' not in assets
    p['accion'] = 'publicar'
    return p


def plan(tanda, manifest, consultar_release, ahora):
    validar(tanda, ahora)
    return {'fecha': tanda['fecha'], 'caduca': tanda['caduca'],
            'apps': [plan_app(a, manifest, *consultar_release(a['repo'], f'v{a["version"]}'))
                     for a in tanda['apps']]}


# ----------------------------- sellar -----------------------------------------

def _sub_una(path, patron, nuevo, cuantas=1):
    texto = path.read_bytes().decode('utf-8')
    hecho, n = re.subn(patron, nuevo, texto, count=cuantas, flags=re.M)
    if n != cuantas:
        raise Fallo(f'{path.name}: esperaba {cuantas} versión(es) que cambiar y hay {n}')
    path.write_bytes(hecho.encode('utf-8'))


def version_actual(d, stack):
    if stack == 'tauri':
        return json.loads((d / 'src-tauri/tauri.conf.json').read_text(encoding='utf-8'))['version']
    for f in ('utils/version.py', 'version.py', 'main.py'):
        m = (d / f).is_file() and re.search(r'^__version__\s*=\s*"([^"]+)"', (d / f).read_text(encoding='utf-8'), re.M)
        if m:
            return m.group(1)
    raise Fallo('no hay __version__ = "X.Y.Z" en utils/version.py, version.py ni main.py')


def subir_version(d, stack, nueva):
    """Cambia la versión en los ficheros del stack. Devuelve los que tocó."""
    vieja = version_actual(d, stack)
    if vieja == nueva:
        return []
    v = re.escape(vieja)
    tocados = []
    if stack == 'tauri':
        _sub_una(d / 'src-tauri/tauri.conf.json', rf'^(\s*"version":\s*"){v}"', rf'\g<1>{nueva}"')
        _sub_una(d / 'package.json', rf'^(\s*"version":\s*"){v}"', rf'\g<1>{nueva}"')
        tocados += ['src-tauri/tauri.conf.json', 'package.json']
        lock = d / 'package-lock.json'
        if lock.is_file():                                  # la raíz y packages[""]
            _sub_una(lock, rf'^(\s*"version":\s*"){v}"', rf'\g<1>{nueva}"', cuantas=2)
            tocados.append('package-lock.json')
        cargo = d / 'src-tauri/Cargo.toml'
        crate = re.search(r'^name\s*=\s*"([^"]+)"', cargo.read_text(encoding='utf-8'), re.M).group(1)
        _sub_una(cargo, rf'^(version\s*=\s*"){v}"', rf'\g<1>{nueva}"')
        tocados.append('src-tauri/Cargo.toml')
        clock = d / 'src-tauri/Cargo.lock'
        if clock.is_file():
            _sub_una(clock, rf'^(name = "{re.escape(crate)}"\r?\nversion = "){v}"', rf'\g<1>{nueva}"')
            tocados.append('src-tauri/Cargo.lock')
    else:
        vf = next(f for f in ('utils/version.py', 'version.py', 'main.py')
                  if (d / f).is_file() and re.search(r'^__version__\s*=', (d / f).read_text(encoding='utf-8'), re.M))
        _sub_una(d / vf, rf'^(__version__\s*=\s*"){v}"', rf'\g<1>{nueva}"')
        tocados.append(vf)
        isses = sorted(list(d.glob('packaging/windows/*.iss')) + list(d.glob('packaging/*.iss')))
        if len(isses) != 1:
            raise Fallo(f'esperaba 1 .iss en packaging/ y hay {len(isses)}')
        texto = isses[0].read_text(encoding='utf-8')
        if re.search(r'^#define\s+AppVersion\s+"', texto, re.M):
            _sub_una(isses[0], rf'^(#define\s+AppVersion\s+"){v}"', rf'\g<1>{nueva}"')
        else:
            _sub_una(isses[0], rf'^(AppVersion=){v}(\s*)$', rf'\g<1>{nueva}\g<2>')
        tocados.append(isses[0].relative_to(d).as_posix())
    return tocados


def sellar_changelog(d, version, fecha):
    """«## [Unreleased]» → «## [Unreleased]» vacío + «## [X.Y.Z] — fecha». False si ya estaba."""
    cl = d / 'CHANGELOG.md'
    texto = cl.read_bytes().decode('utf-8')
    if re.search(rf'^## \[{re.escape(version)}\]', texto, re.M):
        return False
    nl = '\r\n' if '\r\n' in texto else '\n'
    m = re.search(r'^## \[Unreleased\][^\r\n]*', texto, re.M)
    if not m:
        raise Fallo('CHANGELOG.md no tiene sección «## [Unreleased]»')
    nuevo = texto[:m.end()] + f'{nl}{nl}## [{version}] — {fecha}' + texto[m.end():]
    cl.write_bytes(nuevo.encode('utf-8'))
    return True


# ----------------------------- notas ------------------------------------------

def notas_de(p):
    return {plat: p['notas'][plat] for plat in p['platforms']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='orden', required=True)
    a = sub.add_parser('plan')
    a.add_argument('--fecha', default='')
    a.add_argument('--out', type=Path, required=True)
    s = sub.add_parser('sellar')
    s.add_argument('--dir', type=Path, required=True)
    s.add_argument('--version', required=True)
    s.add_argument('--stack', choices=('tauri', 'pyside'), required=True)
    s.add_argument('--fecha', default=datetime.date.today().isoformat())
    n = sub.add_parser('notas')
    n.add_argument('--plan', type=Path, required=True)
    n.add_argument('--app', required=True)
    n.add_argument('--out', type=Path, required=True)
    ns = sub.add_parser('notas-seco')
    ns.add_argument('--plan', type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        if args.orden == 'plan':
            fecha = args.fecha or ultima_tanda()
            tanda = json.loads(leer_central(f'{TANDAS}/{fecha}.json'))
            manifest = json.loads(leer_central('updates/manifest.json'))
            p = plan(tanda, manifest, release, datetime.datetime.now(datetime.timezone.utc))
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / 'plan.json').write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding='utf-8')
            for x in p['apps']:
                que = x.get('motivo') or ', '.join(k for k in ('crear_tag', 'build_windows', 'build_linux') if x.get(k)) or 'solo publicar'
                print(f'{x["app"]} {x["version"]}: {que}')
        elif args.orden == 'sellar':
            tocados = subir_version(args.dir, args.stack, args.version)
            if sellar_changelog(args.dir, args.version, args.fecha):
                tocados.append('CHANGELOG.md')
            print(' '.join(tocados))
        elif args.orden == 'notas':
            p = next(x for x in json.loads(args.plan.read_text(encoding='utf-8'))['apps'] if x['app'] == args.app)
            args.out.write_text(json.dumps(notas_de(p), ensure_ascii=False) + '\n', encoding='utf-8')
        else:
            apps = json.loads(args.plan.read_text(encoding='utf-8'))['apps']
            print(json.dumps({x['app']: notas_de(x) for x in apps if x['accion'] == 'publicar'}, ensure_ascii=False))
    except Fallo as e:
        print(f'::error::{e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
