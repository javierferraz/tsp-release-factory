"""anterior.py — busca el instalador de Windows de la versión anterior a la que se construye
(S-1275, PAA2 P6-1), para que la fábrica pruebe la actualización (smoke-update.ps1).

    python anterior.py --app snappy-wasp --repo javierferraz/2606-snappy-wasp \
                       --version 1.7.1 --dest DIR --out anterior.json

Por orden:
  1. la que está publicada en el manifest público, si es menor que --version: es la que tienen
     instalada quienes van a actualizar. Se descarga de thestromboliproject.com y su SHA256
     tiene que ser el del manifest;
  2. si no (p. ej. una prueba de la misma versión), la Release de GitHub de la app con el tag
     vX.Y.Z más alto por debajo de --version que tenga instalador de Windows, comprobado contra
     el SHA256SUMS de esa Release o el digest que da GitHub del fichero.

Escribe en --out {"version", "ruta", "origen", "sha256"} o, si es la primera versión de la app,
{"no_aplica": "motivo"}. Sale con 1 si una descarga no cuadra con su hash, o si hay versiones
anteriores pero ningún instalador comprobable: eso no es «no hay anterior», es un fallo.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

MANIFEST_URL = 'https://thestromboliproject.com/updates/manifest.json'
TAG_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')


class Fallo(Exception):
    pass


def semver(v):
    return tuple(int(x) for x in v.split('.'))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def desde_manifest(manifest, app, version):
    """(version, url, sha256) de Windows publicada si es menor que `version`; si no, None."""
    entry = next((t for t in manifest.get('tools', []) if t.get('tool') == app), None)
    win = ((entry or {}).get('platforms') or {}).get('windows-x64') or {}
    ver, url, sha = win.get('version') or (entry or {}).get('version'), win.get('installer_url'), win.get('sha256')
    if ver and url and sha and re.fullmatch(r'\d+\.\d+\.\d+', ver) and semver(ver) < semver(version):
        return ver, url, sha.lower()
    return None


def _instalador(assets, app, ver):
    exacto = f'{app}-v{ver}-windows-x64-setup.exe'
    por_nombre = {a['name']: a for a in assets}
    if exacto in por_nombre:
        return por_nombre[exacto]
    setups = [a for a in assets if a['name'].lower().endswith('.exe') and 'setup' in a['name'].lower()]
    return setups[0] if len(setups) == 1 else None


def candidatas_github(releases, app, version):
    """Releases con tag vX.Y.Z < version y con instalador, de la más alta a la más baja."""
    out = []
    for r in releases:
        m = TAG_RE.match(r.get('tag_name') or '')
        if not m or r.get('draft'):
            continue
        ver = '.'.join(m.groups())
        if semver(ver) >= semver(version):
            continue
        setup = _instalador(r.get('assets') or [], app, ver)
        if setup:
            out.append((ver, r['tag_name'], setup, r.get('assets') or []))
    return sorted(out, key=lambda c: semver(c[0]), reverse=True)


def hash_esperado(setup, assets, leer_sums):
    """SHA256SUMS de la Release o, si no, el digest de GitHub. None si no hay ninguno."""
    if any(a['name'] == 'SHA256SUMS.txt' for a in assets):
        for line in leer_sums().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip('*') == setup['name'] and len(parts[0]) == 64:
                return parts[0].lower()
    digest = setup.get('digest') or ''
    if digest.startswith('sha256:'):
        return digest.split(':', 1)[1].lower()
    return None


def gh(*args):
    p = subprocess.run(['gh', *args], capture_output=True, text=True, encoding='utf-8',
                       errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if p.returncode != 0:
        raise Fallo(f"gh {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def buscar(app, repo, version, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(
            MANIFEST_URL, headers={'User-Agent': 'tsp-release-factory'}), timeout=60) as r:
        manifest = json.loads(r.read().decode('utf-8'))
    pub = desde_manifest(manifest, app, version)
    if pub:
        ver, url, sha = pub
        ruta = dest / url.rsplit('/', 1)[1]
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'tsp-release-factory'}),
                                    timeout=600) as r, open(ruta, 'wb') as fh:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
        got = sha256(ruta)
        if got != sha:
            raise Fallo(f'{ruta.name}: SHA256 {got[:12]}... no es el del manifest {sha[:12]}...')
        return {'version': ver, 'ruta': str(ruta), 'origen': f'manifest publicado ({url})', 'sha256': got}

    releases = json.loads(gh('api', f'repos/{repo}/releases?per_page=100'))
    for ver, tag, setup, assets in candidatas_github(releases, app, version):
        sub = dest / tag
        def leer_sums(tag=tag, sub=sub):
            gh('release', 'download', tag, '-R', repo, '-p', 'SHA256SUMS.txt', '-D', str(sub), '--clobber')
            return (sub / 'SHA256SUMS.txt').read_text(encoding='utf-8', errors='replace')
        want = hash_esperado(setup, assets, leer_sums)
        if not want:
            print(f'{tag}: {setup["name"]} sin SHA256 de referencia; se salta', flush=True)
            continue
        gh('release', 'download', tag, '-R', repo, '-p', setup['name'], '-D', str(sub), '--clobber')
        ruta = sub / setup['name']
        got = sha256(ruta)
        if got != want:
            raise Fallo(f'{tag}/{setup["name"]}: SHA256 {got[:12]}... no es el de referencia {want[:12]}...')
        return {'version': ver, 'ruta': str(ruta), 'origen': f'GitHub Release {tag} de {repo}', 'sha256': got}
    hay = hay_anteriores(manifest, releases, app, version)
    if hay:
        raise Fallo(f'hay versiones anteriores ({hay}) pero ningun instalador con SHA256 comprobable: '
                    'la actualizacion no se puede probar')
    return {'no_aplica': f'primera version: no hay ninguna anterior a {version} publicada ni en las Releases de {repo}'}


def hay_anteriores(manifest, releases, app, version):
    """Versiones anteriores a `version` que existen (publicada o con tag); '' si es la primera.
    Sin esto, «no encontre el instalador» pasaria por «no hay anterior» y la prueba exigida
    de actualizacion quedaria en no_aplica sin que nadie lo note (revision S-1275)."""
    entry = next((t for t in manifest.get('tools', []) if t.get('tool') == app), None)
    pub = (((entry or {}).get('platforms') or {}).get('windows-x64') or {}).get('version') or ''
    vers = {pub} if re.fullmatch(r'\d+\.\d+\.\d+', pub) and semver(pub) < semver(version) else set()
    for r in releases:
        m = TAG_RE.match(r.get('tag_name') or '')
        if m and not r.get('draft') and semver('.'.join(m.groups())) < semver(version):
            vers.add('.'.join(m.groups()))
    return ', '.join(sorted(vers, key=semver, reverse=True)[:3])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--app', required=True)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--version', required=True)
    ap.add_argument('--dest', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    try:
        res = buscar(a.app, a.repo, a.version, Path(a.dest))
    except Fallo as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
