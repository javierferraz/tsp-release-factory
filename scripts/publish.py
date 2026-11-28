"""publish.py — publica una tanda de Releases ya construidas por la fabrica (R6, S-1069).

Convierte la Release vX.Y.Z de cada app (instalador firmado, .sig del actualizador de Tauri,
SHA256SUMS, AppImage y .deb) en una publicacion completa, con el mismo resultado que el flujo
manual de tsp-create-dist §3 y tsp-linux-build §5, usando los scripts del repo central:

  1. Entradas y Release: la Release existe, no es borrador y trae los ficheros esperados.
  2. Descarga y comprobacion: tamano, SHA256 (contra SHA256SUMS de la Release o contra el
     manifest si esa version ya estaba publicada), Authenticode del instalador (firmante
     esperado y cadena de Microsoft, con osslsigncode) y, en Tauri, el .sig contra la pubkey de tauri.conf.json.
  3. Entrada del manifest (Windows estable, Linux experimental, notas en ingles de
     release-notes.json) y esquema validado con validate_manifest.validate_schema.
  4-5. ftp_release.py --fabrica (sube binarios + SHA256SUMS + manifest firmado por
     sign-manifest, sin poda ni archivo) y comprobacion en una sesion FTP NUEVA.
  6. <app>-latest.json de Tauri, sync_fallback_hrefs.py, entrada en news/, commit y push
     del central, deploy_site.py desde ese commit y audit_releases.py --quick --sin-respaldo.
  7. Release de prerelease a definitiva y resumen (Markdown + JSON para Telegram).

Por defecto es EN SECO: FTP y sign-manifest simulados en 127.0.0.1 (mock_servers.py), sin
push, sin deploy real y sin tocar la Release. Publicar de verdad exige --publicar.

Uso:
  python publish.py --central DIR --work DIR --apps '<json>' [--publicar]
                    [--resumen FICHERO.md] [--resultado FICHERO.json] [--inicio-job EPOCH]
                    [--autorizacion JSON]
                    [--notas slug=FICHERO]... [--entrada-previa slug=REV]...   (solo en seco)

--apps: [{"app": "bright-cardinal", "repo": "javierferraz/2610-bright-cardinal",
          "version": "1.0.0", "platforms": ["windows", "linux"]}, ...]

Contrato con R7 (quien prepara la tanda): la Release lleva un fichero release-notes.json con
{"windows": "...", "linux": "..."}: una o dos frases en ingles para quien usa la app, sin
numeros de tarjeta ni nombres de ficheros. "windows" va al manifest, a <app>-latest.json y a
news/; "linux", a la entrada linux-x64 del manifest.

Ficha de release (S-1275, PAA2 P6-1): por cada app se arma la ficha (release_ficha.py del
central) con el commit del tag, la construccion, las pruebas por plataforma (de
<app>-v<ver>-windows-pruebas.json, que deja el build de Windows en la Release), el informe
visual (<app>-v<ver>-informe-visual.json, que deja el render real del build de Windows, P6-2;
solo vale para Windows), las notas y el «si»
(--autorizacion). Si no es publicable segun docs/distribucion/ficha-obligatoria.json, la app
se excluye (en seco: «en real esto pararia»). Se pasa a ftp_release.py --fabrica --ficha, el
hash de cada binario se comprueba descargandolo de nuevo por FTP y la ficha final se sube a
la Release como <app>-v<ver>-ficha.json (en seco solo queda en el artefacto).

--apps admite "tag" (solo en seco) para probar con una Release de pruebas vX.Y.Z-sufijo.
--autorizacion: {"quien", "cuando", "texto", "ejecucion"} — el «si» que lanzo la publicacion.
"""
import argparse
import datetime
import hashlib
import html
import importlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from web_json import NoEsJSON, leer_json as leer_json_web

FACTORY = Path(__file__).resolve().parent.parent
SITE = 'https://thestromboliproject.com'
UPDATES_PREFIX = f'{SITE}/updates/'
EXPECTED_SIGNER = 'Francisco Javier Ferraz Rives'
MS_ROOT = 'CN=Microsoft Identity Verification Root Certificate Authority 2020'
# Huella SHA256 de esa raiz: la de mscerts y la embebida en los instaladores firmados coinciden (2026-11-11)
MS_ROOT_SHA256 = '5367f20c7ade0e2bca790915056d086b720c33c1fa2a2661acf787e3292e1270'
SLUG_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
REPO_RE = re.compile(r'^javierferraz/[A-Za-z0-9._-]+$')
VER_RE = re.compile(r'^\d+\.\d+\.\d+$')
LABELS_TAURI = {'deb': 'Ubuntu 24.04+ / Debian 13+', 'AppImage': 'Other distros / Ubuntu 22.04'}
LABELS_PYSIDE = {'deb': 'Ubuntu / Debian', 'AppImage': 'Other distros'}
MOCK_FTP_PORT = 21
SPANISH_HINTS = (' el ', ' la ', ' los ', ' las ', ' que ', ' para ', ' con ', ' una ', ' del ', ' ahora ')
# Ficha de release (S-1275)
PRUEBAS_FICHA = ('funcionales', 'instalacion_limpia', 'actualizacion', 'desinstalacion')
SIN_INFORME_VISUAL = ('sin_medida: la Release no trae informe visual (la app no tiene inventario de '
                      'pantallas o el render real no lo dejo)')
SIN_INFORME_VISUAL_LINUX = 'sin_medida: el render real (nivel 2) solo corre en el runner de Windows'
SIN_PRUEBAS_WINDOWS = 'no_ejecutada: la Release no trae un informe de pruebas valido de la fabrica'
SIN_PRUEBAS_LINUX = 'no_ejecutada: la fabrica aun no deja informe de pruebas de Linux'
VERIFICACION_FTP = 'tamaño y SHA256 (publish.py lo descarga de nuevo en una sesion FTP nueva)'


class Fallo(Exception):
    """Un paso no se puede dar. El mensaje es lo que sale en el resumen."""


# ───────────────────────────── utilidades ─────────────────────────────

def run(cmd, cwd=None, env=None, check=True, timeout=1800, log=None, label=None):
    """Ejecuta sin consola visible. Devuelve CompletedProcess; con log, vuelca ahi la salida."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout, creationflags=flags)
    if log:
        with open(log, 'a', encoding='utf-8') as fh:
            fh.write(f'$ {" ".join(str(c) for c in cmd[:6])}{" ..." if len(cmd) > 6 else ""}\n')
            fh.write(p.stdout or '')
            fh.write(p.stderr or '')
            fh.write(f'[exit {p.returncode}]\n\n')
    if check and p.returncode != 0:
        tail = ((p.stdout or '') + (p.stderr or '')).strip().splitlines()[-6:]
        name = label or Path(str(cmd[1] if len(cmd) > 1 else cmd[0])).name
        raise Fallo(f'{name} salio con {p.returncode}: ' + ' | '.join(tail))
    return p


def gh(*args, check=True):
    return run(['gh', *args], check=check, timeout=600)


def git(central, *args, check=True):
    return run(['git', '-C', str(central), *args], check=check, timeout=600)


def git_auth(central, *args, check=True):
    """git con las credenciales de gh (GH_TOKEN en el runner) solo para esta orden."""
    return git(central, '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
               *args, check=check)


def semver(v):
    return tuple(int(x) for x in v.split('.'))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def dump_json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def write_json(path, data):
    Path(path).write_bytes(dump_json(data).encode('utf-8'))


def url_release(slug, version, name):
    return f'{SITE}/releases/{slug}/v{version}/{name}'


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


# ───────────────────────────── entradas ─────────────────────────────

def parse_apps(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise Fallo(f'--apps no es JSON valido: {e}')
    if not isinstance(data, list) or not data:
        raise Fallo('--apps tiene que ser una lista no vacia')
    seen, out = set(), []
    for item in data:
        if (not isinstance(item, dict) or not {'app', 'repo', 'version', 'platforms'} <= set(item)
                or not set(item) <= {'app', 'repo', 'version', 'platforms', 'tag'}):
            raise Fallo(f'entrada de --apps con claves inesperadas: {item!r}')
        app, repo, ver, plats = item['app'], item['repo'], item['version'], item['platforms']
        if not isinstance(app, str) or not SLUG_RE.match(app):
            raise Fallo(f'app no valida: {app!r}')
        if not isinstance(repo, str) or not REPO_RE.match(repo):
            raise Fallo(f'repo no valido: {repo!r}')
        if not isinstance(ver, str) or not VER_RE.match(ver):
            raise Fallo(f'version no valida: {ver!r} (X.Y.Z)')
        if (not isinstance(plats, list) or not plats or len(set(plats)) != len(plats)
                or not set(plats) <= {'windows', 'linux'}):
            raise Fallo(f'platforms no valido para {app}: {plats!r}')
        tag = item.get('tag', f'v{ver}')
        if not isinstance(tag, str) or not re.fullmatch(rf'v{re.escape(ver)}(-[0-9A-Za-z.-]+)?', tag):
            raise Fallo(f'tag no valido para {app}: {tag!r} (v{ver} o v{ver}-sufijo)')
        if app in seen:
            raise Fallo(f'{app} aparece dos veces')
        seen.add(app)
        out.append({'app': app, 'repo': repo, 'version': ver, 'platforms': sorted(plats, reverse=True),
                    'tag': tag})
    return out


def parse_autorizacion(text):
    """El «si» que lanzo la publicacion, para la ficha. Vacio = {} (la ficha dira que falta)."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise Fallo(f'--autorizacion no es JSON valido: {e}')
    if (not isinstance(data, dict) or not set(data) <= {'quien', 'cuando', 'texto', 'ejecucion'}
            or not all(isinstance(v, str) for v in data.values())):
        raise Fallo(f'--autorizacion: se esperan textos quien, cuando, texto y ejecucion: {data!r}')
    return data


def parse_pairs(values, what):
    out = {}
    for v in values or []:
        k, sep, rest = v.partition('=')
        if not sep or not SLUG_RE.match(k) or not rest:
            raise Fallo(f'{what} espera slug=valor: {v!r}')
        out[k] = rest
    return out


def parse_sums(text):
    """{fichero: sha256} de un SHA256SUMS ('hash  nombre' o 'hash *nombre')."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]):
            out[parts[1].lstrip('*')] = parts[0].lower()
    return out


def check_notes(notes, platforms):
    """Valida release-notes.json. Devuelve {'windows': str, 'linux': str} (las que hagan falta)."""
    if not isinstance(notes, dict):
        raise Fallo('release-notes.json no es un objeto')
    out = {}
    for plat in platforms:
        text = notes.get(plat)
        if not isinstance(text, str) or not text.strip():
            raise Fallo(f'release-notes.json no trae "{plat}"')
        text = ' '.join(text.split())
        if re.search(r'\bS-\d{2,5}\b', text):
            raise Fallo(f'las notas de {plat} citan una tarjeta (S-NNN): son texto para el usuario')
        low = f' {text.lower()} '
        if sum(1 for w in SPANISH_HINTS if w in low) >= 3:
            raise Fallo(f'las notas de {plat} parecen estar en castellano: van en ingles')
        out[plat] = text
    return out


# ───────────────────────────── central ─────────────────────────────

def display_name(central, slug):
    text = (central / 'site' / 'assets' / 'catalog.js').read_text(encoding='utf-8')
    m = re.search(r'id:\s*"' + re.escape(slug) + r'"\s*,(.{0,1500}?)\bname:\s*"([^"]+)"', text, re.S)
    if not m or re.search(r'\bid:\s*"', m.group(1)):
        raise Fallo(f'{slug} no esta en site/assets/catalog.js: sin nombre para el manifest')
    return m.group(2)


def downloads_hidden(central, slug):
    text = (central / 'site' / 'assets' / 'catalog.js').read_text(encoding='utf-8')
    m = re.search(r'const\s+DOWNLOADS_HIDDEN\s*=\s*\[([^\]]*)\]', text)
    return bool(m) and f'"{slug}"' in m.group(1)


def load_central_module(central, name):
    scripts = str(central / 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


def credentials_host(central):
    path = central / 'credentials.json'
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8')).get('_ftp_host')


# ───────────────────────────── manifest ─────────────────────────────

def platform_state(cur_ver, cur_shas, new_ver, new_shas, label):
    """'nueva' o 'igual'; Fallo si la version baja o si se pisaria una URL publicada."""
    if not cur_ver:
        return 'nueva'
    if semver(new_ver) > semver(cur_ver):
        return 'nueva'
    if semver(new_ver) < semver(cur_ver):
        raise Fallo(f'{label}: {new_ver} es anterior a la publicada {cur_ver}')
    if cur_shas == new_shas:
        return 'igual'
    raise Fallo(f'{label}: {new_ver} ya esta publicada con otros ficheros (SHA256 distinto); '
                'una URL publicada no se pisa nunca')


def windows_current(entry):
    win = (entry or {}).get('platforms', {}).get('windows-x64') or {}
    if not win.get('installer_url'):
        return None, None
    return win.get('version') or entry.get('version'), win


def linux_current(entry):
    lin = (entry or {}).get('platforms', {}).get('linux-x64') or {}
    if not lin.get('installer_url'):
        return None, None
    return lin.get('version'), lin


def update_ordered(prev, values, order):
    new = dict(prev or {})
    for k in order:
        new[k] = values[k]
    return new


def windows_block(prev, slug, version, setup, portable, fmt, notes):
    """setup/portable: {'name', 'size', 'sha256'} (portable puede ser None)."""
    values = {
        'status': 'stable',
        'version': version,
        'installer_url': url_release(slug, version, setup['name']),
        'portable_url': url_release(slug, version, portable['name']) if portable else None,
        'sha256': setup['sha256'],
        'format': fmt,
        'installer_size': setup['size'],
        'portable_size': portable['size'] if portable else None,
        'notes': notes,
    }
    return update_ordered(prev, values, list(values))


def linux_block(prev, slug, version, appimage, deb, tauri, notes):
    labels = dict(LABELS_TAURI if tauri else LABELS_PYSIDE)
    for a in (prev or {}).get('assets') or []:
        if isinstance(a, dict) and a.get('format') in labels and a.get('label'):
            labels[a['format']] = a['label']

    def asset(fmt, f):
        return {'format': fmt, 'label': labels[fmt], 'url': url_release(slug, version, f['name']),
                'sha256': f['sha256'], 'size': f['size'], 'status': 'experimental'}

    values = {
        'status': 'experimental',
        'version': version,
        'assets': [asset('deb', deb), asset('AppImage', appimage)],
        'installer_url': url_release(slug, version, appimage['name']),
        'portable_url': None,
        'sha256': appimage['sha256'],
        'format': 'AppImage',
        'notes': notes,
        'installer_size': appimage['size'],
        'portable_size': None,
    }
    return update_ordered(prev, values, list(values))


def planned_block(fmt):
    return {'status': 'planned', 'installer_url': None, 'portable_url': None, 'sha256': None,
            'format': fmt, 'notes': None, 'installer_size': None, 'portable_size': None}


def entrada_de_partida(actual, vieja, plats):
    """(en seco, --entrada-previa) La entrada actual con SOLO las plataformas de la tanda como
    estaban en `vieja`. Las demas se quedan como estan publicadas: si volvieran atras,
    manifest_guard.py pararia la subida simulada por ir por detras del manifest en vivo (S-1275)."""
    out = json.loads(json.dumps(actual))
    for p in plats:
        key = {'windows': 'windows-x64', 'linux': 'linux-x64'}[p]
        if key in (vieja.get('platforms') or {}):
            out['platforms'][key] = json.loads(json.dumps(vieja['platforms'][key]))
        else:
            out['platforms'].pop(key, None)
    if 'windows' in plats:      # apply_plan las vuelve a poner con la version nueva
        for k in ('version', 'release_date'):
            if k in vieja:
                out[k] = vieja[k]
    return out


def apply_plan(manifest, plan, today):
    """Aplica a manifest (en memoria) lo que el plan publica. Devuelve la entrada resultante."""
    tools = manifest.setdefault('tools', [])
    entry = next((t for t in tools if t.get('tool') == plan['app']), None)
    if entry is None:
        entry = {'tool': plan['app'], 'display_name': plan['display_name'], 'version': plan['version'],
                 'channel': 'stable', 'release_date': today, 'download_page': f"{SITE}/tools/{plan['app']}/",
                 'notes_url': plan['notes_url'],
                 'platforms': {'windows-x64': planned_block(plan['windows_format']),
                               'linux-x64': planned_block('AppImage'),
                               'macos-arm64': planned_block('dmg')}}
        tools.append(entry)
    if plan.get('windows_block'):
        entry['platforms']['windows-x64'] = plan['windows_block']
        entry['version'] = plan['version']
        entry['release_date'] = today
        if entry.get('channel') != 'stable':   # la entrada de reserva de F0-F3 pasa a publicada
            entry.update(channel='stable', display_name=plan['display_name'], notes_url=plan['notes_url'])
    if plan.get('linux_block'):
        entry['platforms']['linux-x64'] = plan['linux_block']
    return entry


def latest_json(version, notes, pub_date, signature, url):
    return {'version': version, 'notes': notes, 'pub_date': pub_date,
            'platforms': {'windows-x86_64': {'signature': signature, 'url': url}}}


# ───────────────────────────── novedades ─────────────────────────────

NEWS_ANCHOR = '<div class="news-list">'
# Diseño v2 de la web (2026-11-28): la lista es <div class="stack"> dentro de la sección
# id="releases", y cada entrada un <div class="card"> en una sola línea.
NEWS_V2_SECTION = 'id="releases"'
NEWS_V2_ANCHOR = '<div class="stack">'


def news_has(text, slug, version):
    for art in re.findall(r'<article class="news-item">(.*?)</article>', text, re.S):
        if f'href="/tools/{slug}/"' in art and f'<p class="version">Version {version}</p>' in art:
            return True
    for card in re.findall(r'<div class="card">(.*?)</div>', text, re.S):
        if f'href="/tools/{slug}/"' in card and f'<p>Version {version}</p>' in card:
            return True
    return False


def news_insert(text, date, name, version, notes, slug):
    """Devuelve el HTML con la entrada nueva arriba del todo, o el mismo si ya estaba."""
    if news_has(text, slug, version):
        return text
    i = text.find(NEWS_ANCHOR)
    if i < 0:
        sec = text.find(NEWS_V2_SECTION)
        k = text.find(NEWS_V2_ANCHOR, sec) if sec >= 0 else -1
        if k < 0:
            raise Fallo('site/news/index.html sin <div class="news-list"> ni <div class="stack"> en id="releases"')
        k += len(NEWS_V2_ANCHOR)
        card = (f'<div class="card"><time datetime="{date}">{date}</time>'
                f'<h3>{html.escape(name, quote=False)}</h3><p>Version {version}</p>'
                f'<p>{html.escape(notes, quote=False)}</p>'
                f'<a href="/tools/{slug}/">Tool page →</a></div>')
        return text[:k] + card + text[k:]
    nl = '\r\n' if '\r\n' in text else '\n'
    j = text.index('\n', i) + 1
    block = [
        '',
        '          <article class="news-item">',
        f'            <time datetime="{date}">{date}</time>',
        f'            <h3>{html.escape(name, quote=False)}</h3>',
        f'            <p class="version">Version {version}</p>',
        f'            <p>{html.escape(notes, quote=False)}</p>',
        f'            <a href="/tools/{slug}/">Tool page →</a>',
        '          </article>',
    ]
    return text[:j] + nl.join(block) + nl + text[j:]


# ───────────────────────────── firmas ─────────────────────────────

def ms_root_pem(work):
    """La raiz de Microsoft, sacada de mscerts y comprobada por huella, como unico ancla.

    Con solo ella en -CAfile y -TSA-CAfile, una verificacion ok de osslsigncode implica que la
    cadena del firmante y la del sello de tiempo terminan ahi."""
    import mscerts
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    import warnings
    pems = re.findall(rb'-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----',
                      Path(mscerts.where()).read_bytes(), re.S)
    for pem in pems:   # uno a uno: el bundle trae alguno con serie negativa que cryptography acabara rechazando
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                cert = x509.load_pem_x509_certificate(pem)
        except ValueError:
            continue
        if cert.fingerprint(hashes.SHA256()).hex() == MS_ROOT_SHA256:
            path = work / 'ms-root.pem'
            path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            return path
    raise Fallo(f'mscerts no trae {MS_ROOT} con la huella esperada')


def parse_osslsigncode(out):
    """Firmante (CN) de la salida de 'osslsigncode verify' (2.9). Fallo si algo no es ok."""
    total = re.search(r'^Number of verified signatures: (\d+)$', out, re.M)
    oks = re.findall(r'^Signature verification: ok$', out, re.M)
    if not total or int(total.group(1)) < 1 or len(oks) != int(total.group(1)):
        raise Fallo('alguna firma no verifica (o el digest del fichero no coincide)')
    if re.search(r'^Signature verification: failed$', out, re.M) or not re.search(r'^Succeeded$', out, re.M):
        raise Fallo('osslsigncode no da Succeeded')
    signers = re.findall(r"^Signer's certificate:\n\t-+\n\tSigner #0:\n\t\tSubject: (.*)$", out, re.M)
    if len(signers) != len(oks):
        raise Fallo('no se encuentra el firmante en la salida de osslsigncode')
    for subject in signers:
        cn = re.search(r'/CN=([^/]+)', subject)
        if not cn or cn.group(1) != EXPECTED_SIGNER:
            raise Fallo(f'firmado por {subject}, no por CN={EXPECTED_SIGNER}')
    return EXPECTED_SIGNER


MS_RFC3161 = '1.3.6.1.4.1.311.3.3.1'   # sello de tiempo RFC 3161 anidado de Authenticode


def pe_signature(path):
    """La firma PKCS#7 (DER) de la tabla de certificados del PE."""
    data = Path(path).read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], 'little')
    if data[pe:pe + 4] != b'PE\0\0':
        raise Fallo('no es un ejecutable PE')
    opt = pe + 24
    magic = int.from_bytes(data[opt:opt + 2], 'little')
    off, size = struct.unpack_from('<II', data, opt + (96 if magic == 0x10b else 112) + 4 * 8)
    if not off or not size or off + 8 > len(data):
        raise Fallo('el ejecutable no lleva firma Authenticode')
    length = struct.unpack_from('<I', data, off)[0]
    return data[off + 8:off + length]


def authenticode_timestamp(path, root_pem, work, log=None):
    """Hora del sello de tiempo de la firma principal, verificado con openssl cms hasta la raiz.

    Hace falta aparte: osslsigncode (2.8 y 2.9) exige al sello un signingTime que los de Microsoft no
    llevan, lo da por «not available» y verifica a la hora actual, cuando el certificado de
    Azure Trusted Signing (3 dias) ya ha caducado (run 42970816333)."""
    from asn1crypto import cms, tsp
    signer = cms.ContentInfo.load(pe_signature(path), strict=False)['content']['signer_infos'][0]
    token = next((a['values'][0] for a in (signer['unsigned_attrs'] or []) if a['type'].dotted == MS_RFC3161), None)
    if token is None:
        raise Fallo('la firma no lleva sello de tiempo RFC 3161')
    claimed = token['content']['encap_content_info']['content'].parsed['gen_time'].native
    tok, out = work / 'ts-token.der', work / 'ts-tstinfo.der'
    tok.write_bytes(token.dump())
    p = run(['openssl', 'cms', '-verify', '-inform', 'DER', '-in', str(tok), '-CAfile', str(root_pem),
             '-purpose', 'timestampsign', '-attime', str(int(claimed.timestamp())), '-binary', '-out', str(out)],
            check=False, log=log)
    if p.returncode != 0:
        raise Fallo('el sello de tiempo no verifica hasta la raiz de Microsoft')
    tst = tsp.TSTInfo.load(out.read_bytes())   # lo que openssl ha verificado, no lo que dice el token
    alg = tst['message_imprint']['hash_algorithm']['algorithm'].native
    if hashlib.new(alg, signer['signature'].native).digest() != tst['message_imprint']['hashed_message'].native:
        raise Fallo('el sello de tiempo no corresponde a esta firma')
    if tst['gen_time'].native != claimed:
        raise Fallo('la hora del sello verificado no es la del token')
    return tst['gen_time'].native


def verify_authenticode(path, root_pem, work, log=None):
    """Sello de tiempo con openssl y firma con osslsigncode 2.9 a la hora del sello, contra la
    raiz de Microsoft. Devuelve (firmante, hora del sello)."""
    if not shutil.which('osslsigncode'):
        raise Fallo('falta osslsigncode para comprobar Authenticode')
    try:
        when = authenticode_timestamp(path, root_pem, work, log)
    except Fallo as e:
        raise Fallo(f'Authenticode de {Path(path).name}: {e}')
    # -ignore-timestamp + -time: el sello ya esta verificado arriba. -ignore-crl (2.9+): sin CRL, como
    # el flujo manual. La CRL se comprueba a la hora actual, con el certificado de 3 dias ya caducado,
    # y en 2.8 -ignore-cdp sin -CRLfile da «failed» siempre (run 43044044917).
    p = run(['osslsigncode', 'verify', '-in', str(path), '-CAfile', str(root_pem), '-ignore-timestamp',
             '-time', str(int(when.timestamp())), '-ignore-crl'], check=False, log=log)
    try:
        return parse_osslsigncode((p.stdout or '').replace('\r\n', '\n')), when
    except Fallo as e:
        raise Fallo(f'Authenticode de {Path(path).name}: {e} (osslsigncode salio con {p.returncode})')


def verify_minisign(path, sig, pubkey):
    run([sys.executable, '-I', str(FACTORY / 'scripts' / 'verify_minisign.py'), str(path), str(sig), pubkey])


# ───────────────────────────── informe ─────────────────────────────

class Informe:
    def __init__(self, seco):
        self.seco = seco
        self.apps = {}
        self.pasos = []      # (paso, estado, detalle)
        self.avisos = []
        self.real_fallaria = []
        self.hasta = 'inicio'
        self.main_sin_subir = False   # FTP publicado y main con el manifest viejo

    def app(self, slug, **kw):
        self.apps.setdefault(slug, {'app': slug, 'estado': 'pendiente', 'comprobaciones': []}).update(kw)

    def check(self, slug, texto):
        self.app(slug)
        self.apps[slug]['comprobaciones'].append(texto)
        print(f'  [{slug}] {texto}', flush=True)

    def paso(self, paso, estado, detalle=''):
        self.pasos.append((paso, estado, detalle))
        if estado == 'ok' and paso[0].isdigit():   # el rescate no es un paso de la publicacion
            self.hasta = paso
        print(f'== {paso}: {estado} {detalle}', flush=True)

    def fallaria(self, slug, texto):
        """En seco se sigue, pero en real esto pararia: cuenta como fallo del dry-run."""
        if not self.seco:
            raise Fallo(texto)
        self.real_fallaria.append(f'{slug}: {texto}')
        self.check(slug, f'AVISO (en real fallaria): {texto}')


# ───────────────────────────── fase 1: preparar ─────────────────────────────

def release_info(repo, tag):
    p = gh('release', 'view', tag, '-R', repo, '--json', 'tagName,isDraft,isPrerelease,name,assets,url',
           check=False)
    if p.returncode != 0:
        raise Fallo(f'no se puede leer la Release {tag} de {repo}: {(p.stderr or p.stdout).strip()[:200]}')
    return json.loads(p.stdout)


def repo_file(repo, tag, path):
    p = gh('api', '-H', 'Accept: application/vnd.github.raw', f'repos/{repo}/contents/{path}?ref={tag}',
           check=False)
    if p.returncode != 0:
        if 'HTTP 404' in (p.stderr or ''):
            return None
        raise Fallo(f'no se puede leer {path} de {repo}@{tag}: {(p.stderr or "").strip()[:200]}')
    return p.stdout


def tauri_conf(repo, tag, version):
    text = repo_file(repo, tag, 'src-tauri/tauri.conf.json')
    if text is None:
        return None
    conf = json.loads(text)
    conf_ver = conf.get('version')
    if conf_ver and not VER_RE.match(str(conf_ver)):
        pkg = repo_file(repo, tag, 'package.json')
        conf_ver = json.loads(pkg).get('version') if pkg else None
    if conf_ver != version:
        raise Fallo(f'tauri.conf.json dice {conf_ver!r} y se publica {version}')
    return conf


def tag_commit(repo, tag):
    sha = gh('api', f'repos/{repo}/commits/{tag}', '--jq', '.sha').stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise Fallo(f'no se puede leer el commit del tag {tag} de {repo}: {sha[:80]!r}')
    return sha


def leer_json(path):
    """Un JSON de la Release; si esta roto devuelve 'ilegible' (no un dict), para que esa app
    quede fuera con su motivo en vez de tumbar la tanda entera (revision S-1275)."""
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeDecodeError):
        return 'ilegible'


def pruebas_validas(data, slug, ver, commit):
    """Por que el informe de pruebas de Windows de la Release no vale; [] si vale."""
    if not isinstance(data, dict) or not isinstance(data.get('pruebas'), dict):
        return ['no es un informe de pruebas']
    out = [f'{k} es {data.get(k)!r}, no {v!r}' for k, v in
           (('app', slug), ('version', ver), ('commit', commit), ('plataforma', 'windows-x64'))
           if data.get(k) != v]
    out += [f'sin la prueba «{k}»' for k in PRUEBAS_FICHA if not data['pruebas'].get(k)]
    return out


def informe_visual(data, fuente):
    """El informe visual de la Release, en la forma que lee release_ficha.publicable()."""
    if data is None:
        return SIN_INFORME_VISUAL
    if not isinstance(data, dict):     # existe pero no se puede leer: bloquea, no pasa por «sin medida»
        return {'estado': 'ilegible', 'commit': '', 'medidas': 0, 'fuente': fuente}
    medidas = data.get('medidas')
    return {'estado': str(data.get('estado') or ''), 'commit': str(data.get('commit') or ''),
            'medidas': medidas if isinstance(medidas, int) else 0, 'fuente': fuente}


def ficha_datos(slug, repo, ver, tag, commit, plats, pruebas_win, visual, firma, notas, autorizacion):
    """Lo que ftp_release.py --ficha pone en la ficha de release de esta subida (release_ficha.py).

    pruebas_win: el informe del build de Windows ya validado, o None. Lo que la fabrica no
    comprueba se declara con su motivo («no_ejecutada: …»), nunca se da por bueno."""
    por_plat = {}
    if 'windows' in plats:
        p = dict(pruebas_win['pruebas']) if pruebas_win else {k: SIN_PRUEBAS_WINDOWS for k in PRUEBAS_FICHA}
        por_plat['windows-x64'] = {**p, 'informe_visual': visual}
    if 'linux' in plats:
        por_plat['linux-x64'] = {**{k: SIN_PRUEBAS_LINUX for k in PRUEBAS_FICHA},
                                 'informe_visual': SIN_INFORME_VISUAL_LINUX}
    herramientas = (pruebas_win or {}).get('herramientas') or {'runner': 'GitHub Actions (fabrica)'}
    return {
        'origen': {'repo': repo, 'commit': commit, 'tag': tag, 'ramas': [], 'tareas': []},
        'construccion': {'donde': 'fabrica', 'ejecucion': (pruebas_win or {}).get('ejecucion', ''),
                         'herramientas': herramientas, 'firma_verificada': firma},
        'pruebas_por_plataforma': por_plat,
        'contenido': {'changelog_contrastado': 'no_ejecutada: la fabrica solo comprueba que el CHANGELOG '
                                               'esta sellado con esta version',
                      'notas_en': notas or '',
                      'ficha_web_comprobada': 'no_ejecutada: la fabrica no revisa la ficha de la web'},
        'autorizacion': autorizacion or {},
    }


def prepare(spec, manifest, central, work, rep, notes_override, notes_url_branch=None, autorizacion=None):
    slug, repo, ver, plats = spec['app'], spec['repo'], spec['version'], spec['platforms']
    tag = spec.get('tag') or f'v{ver}'
    rel = release_info(repo, tag)
    if rel.get('isDraft'):
        raise Fallo(f'la Release {tag} es un borrador')
    sizes = {a['name']: a['size'] for a in rel.get('assets', [])}
    rep.check(slug, f"Release {tag}: {'prerelease' if rel.get('isPrerelease') else 'definitiva'}, "
                    f'{len(sizes)} ficheros')

    conf = tauri_conf(repo, tag, ver)
    tauri = conf is not None
    want, optional = [], []
    names = {}
    if 'windows' in plats:
        names['setup'] = f'{slug}-v{ver}-windows-x64-setup.exe'
        want.append(names['setup'])
        if tauri:
            want.append(names['setup'] + '.sig')
        optional.append(f'{slug}-v{ver}-windows-x64-portable.zip')
    if 'linux' in plats:
        names['appimage'] = f'{slug}-v{ver}-linux-x64.AppImage'
        names['deb'] = f'{slug}-v{ver}-linux-x64.deb'
        want += [names['appimage'], names['deb']]
    missing = [n for n in want if n not in sizes]
    if missing:
        raise Fallo(f'a la Release {tag} le faltan: {", ".join(missing)}')
    names['pruebas'] = f'{slug}-v{ver}-windows-pruebas.json'
    names['visual'] = f'{slug}-v{ver}-informe-visual.json'
    names['ficha'] = f'{slug}-v{ver}-ficha.json'
    extras = [n for n in optional + ['SHA256SUMS.txt', 'linux-SHA256SUMS.txt', 'release-notes.json',
                                     names['pruebas'], names['visual'], names['ficha']]
              if n in sizes]
    portable_name = next((n for n in optional if n in sizes), None)

    dest = work / 'descargas' / slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    pattern_args = []
    for n in want + extras:
        pattern_args += ['-p', n]
    gh('release', 'download', tag, '-R', repo, '-D', str(dest), *pattern_args)
    for n in want + extras:
        got = (dest / n).stat().st_size if (dest / n).exists() else None
        if got != sizes[n]:
            raise Fallo(f'{n}: descargado {got} bytes, la Release dice {sizes[n]}')
    rep.check(slug, f'descargados {len(want) + len(extras)} ficheros, tamanos iguales a la Release')

    sums = {}
    for f in ('SHA256SUMS.txt', 'linux-SHA256SUMS.txt'):
        if (dest / f).exists():
            sums.update(parse_sums((dest / f).read_text(encoding='utf-8', errors='replace')))

    entry = next((t for t in manifest.get('tools', []) if t.get('tool') == slug), None)
    win_ver, win_prev = windows_current(entry)
    lin_ver, lin_prev = linux_current(entry)
    published_ref = {}
    if win_prev and win_ver == ver:
        published_ref[names.get('setup')] = win_prev.get('sha256')
    if lin_prev and lin_ver == ver:
        for a in lin_prev.get('assets') or []:
            published_ref[os.path.basename(a.get('url') or '')] = a.get('sha256')

    files = {}
    for n in [x for x in want + ([portable_name] if portable_name else []) if not x.endswith('.sig')]:
        digest = sha256(dest / n)
        ref = sums.get(n) or published_ref.get(n)
        if ref and ref.lower() != digest:
            raise Fallo(f'{n}: SHA256 {digest[:12]}... no coincide con la referencia {ref[:12]}...')
        if ref:
            src = 'SHA256SUMS' if n in sums else 'manifest publicado'
            rep.check(slug, f'{n}: SHA256 coincide con {src}')
        files[n] = {'name': n, 'size': sizes[n], 'sha256': digest, 'path': str(dest / n), 'ref': bool(ref)}

    plan = {'app': slug, 'repo': repo, 'version': ver, 'tag': tag, 'platforms': plats, 'tauri': tauri,
            'release': rel, 'files': {}, 'latest': {}, 'windows_format': 'nsis' if tauri else 'inno'}

    if 'windows' in plats:
        signer, when = verify_authenticode(dest / names['setup'], ms_root_pem(work), work, log=work / f'{slug}.log')
        rep.check(slug, f'Authenticode del instalador valido (firmante {signer}; sello de tiempo del '
                        f'{when:%Y-%m-%d %H:%M} UTC con openssl y firma a esa hora con osslsigncode, '
                        'las dos hasta la raiz de Microsoft)')
        if tauri:
            pubkey = conf.get('plugins', {}).get('updater', {}).get('pubkey')
            if not pubkey:
                raise Fallo('tauri.conf.json sin plugins.updater.pubkey')
            verify_minisign(dest / names['setup'], dest / (names['setup'] + '.sig'), pubkey)
            rep.check(slug, '.sig del actualizador verificado con la pubkey de tauri.conf.json')

    # Notas
    needed = [p for p in plats]
    if slug in notes_override:
        notes = check_notes(json.loads(Path(notes_override[slug]).read_text(encoding='utf-8')), needed)
        rep.check(slug, f'notas de {notes_override[slug]} (solo en seco)')
    elif (dest / 'release-notes.json').exists():
        notes = check_notes(json.loads((dest / 'release-notes.json').read_text(encoding='utf-8')), needed)
        rep.check(slug, 'notas en ingles de release-notes.json')
    else:
        notes = {}

    # Windows
    if 'windows' in plats:
        setup = files[names['setup']]
        portable = files.get(portable_name) if portable_name else None
        state = platform_state(win_ver, (win_prev or {}).get('sha256'), ver, setup['sha256'], 'Windows')
        if state == 'igual':
            rep.check(slug, f'Windows {ver} ya publicado con estos mismos ficheros: no se vuelve a subir')
            plan['windows_igual'] = True
        else:
            wnotes = notes.get('windows')
            if not wnotes:
                rep.fallaria(slug, 'la Release no trae release-notes.json con las notas de Windows')
                wnotes = '[en seco: sin release-notes.json]'
            plan['windows_block'] = windows_block(win_prev, slug, ver, setup, portable,
                                                  plan['windows_format'], wnotes)
            plan['files'][setup['name']] = setup
            if portable:
                plan['files'][portable['name']] = portable
        plan['windows_notes'] = (plan.get('windows_block') or win_prev or {}).get('notes')
        # *-latest.json: solo Tauri con endpoint propio en el dominio
        if tauri:
            sig_text = (dest / (names['setup'] + '.sig')).read_text(encoding='utf-8').strip()
            for ep in conf.get('plugins', {}).get('updater', {}).get('endpoints') or []:
                if ep.startswith(UPDATES_PREFIX) and ep.endswith('-latest.json') and '/' not in ep[len(UPDATES_PREFIX):]:
                    plan['latest'][ep[len(UPDATES_PREFIX):]] = {
                        'signature': sig_text, 'url': url_release(slug, ver, setup['name'])}
            if not plan['latest']:
                rep.check(slug, 'Tauri sin endpoint propio en el dominio: no hay *-latest.json que tocar')

    # Linux
    if 'linux' in plats:
        appimage, deb = files[names['appimage']], files[names['deb']]
        cur = None
        if lin_prev:
            cur = {os.path.basename(a.get('url') or ''): a.get('sha256') for a in lin_prev.get('assets') or []}
            cur = (cur.get(names['appimage'], lin_prev.get('sha256')), cur.get(names['deb']))
        state = platform_state(lin_ver, cur, ver, (appimage['sha256'], deb['sha256']), 'Linux')
        if state == 'igual':
            rep.check(slug, f'Linux {ver} ya publicado con estos mismos ficheros: no se vuelve a subir')
        else:
            lnotes = notes.get('linux')
            if not lnotes:
                rep.fallaria(slug, 'la Release no trae release-notes.json con las notas de Linux')
                lnotes = '[en seco: sin release-notes.json]'
            plan['linux_block'] = linux_block(lin_prev, slug, ver, appimage, deb, tauri, lnotes)
            plan['files'][appimage['name']] = appimage
            plan['files'][deb['name']] = deb

    # Solo lo que se va a subir necesita referencia: sin ella no hay prueba de que es lo construido.
    for n, f in plan['files'].items():
        if not f['ref']:
            rep.fallaria(slug, f'{n} sin SHA256 de referencia (ni SHA256SUMS en la Release ni manifest)')

    # Ficha de release (S-1275): sin ficha publicable no se sube nada de esta app.
    if plan['files']:
        commit = tag_commit(repo, tag)
        pruebas_win = None
        if 'windows' in plats:
            if (dest / names['pruebas']).exists():
                pruebas_win = leer_json(dest / names['pruebas'])
                malas = pruebas_validas(pruebas_win, slug, ver, commit)
                if malas:
                    rep.fallaria(slug, f"{names['pruebas']} no vale: {'; '.join(malas)}")
                    pruebas_win = None
                else:
                    rep.check(slug, f"informe de pruebas de Windows del commit {commit[:12]} "
                                    f"({pruebas_win.get('ejecucion', '')})")
            else:
                rep.check(slug, f"la Release no trae {names['pruebas']}")
        visual_data = None
        if (dest / names['visual']).exists():
            visual_data = leer_json(dest / names['visual'])
        firma = ('Authenticode del instalador' + (' y .sig de Tauri' if tauri else '')
                 + ('; los paquetes de Linux no van firmados' if 'linux' in plats else '')
                 if 'windows' in plats else 'no_aplica: los paquetes de Linux no van firmados')
        datos = ficha_datos(slug, repo, ver, tag, commit, plats, pruebas_win,
                            informe_visual(visual_data, names['visual']), firma,
                            plan.get('windows_notes') or notes.get('linux'), autorizacion)
        release_ficha = load_central_module(central, 'release_ficha')
        politica = release_ficha.cargar_politica(str(central / 'docs' / 'distribucion' / 'ficha-obligatoria.json'))
        subir = sorted({release_ficha.plataforma(n) for n in plan['files']})
        motivos = [f'{p}: {m}' for p in subir for m in release_ficha.publicable(datos, slug, p, politica)]
        for m in motivos:
            rep.fallaria(slug, f'ficha no publicable ({m})')
        if not motivos:
            rep.check(slug, f"ficha publicable para {', '.join(subir)} (commit {commit[:12]}, "
                            f"autorizacion de {datos['autorizacion'].get('quien')})")
        plan['ficha_datos'] = work / 'fichas' / f'{slug}-datos.json'
        plan['ficha_datos'].parent.mkdir(parents=True, exist_ok=True)
        write_json(plan['ficha_datos'], datos)
        plan['ficha_previa'] = dest / names['ficha'] if (dest / names['ficha']).exists() else None
        plan['ficha_nombre'] = names['ficha']

    # Una entrada de reserva (canal dev, de F0-F3) cuenta como app nueva: nombre del catálogo y
    # canal stable. Quiet Panda 1.3.0 salió en dev y con el nombre de junio (2026-11-27).
    if entry is None or entry.get('channel') != 'stable':
        if 'windows_block' not in plan:
            raise Fallo('app nueva en el manifest sin Windows en la tanda')
        plan['display_name'] = display_name(central, slug)
        branch = notes_url_branch or json.loads(gh('api', f'repos/{repo}').stdout).get('default_branch', 'main')
        plan['notes_url'] = f'https://github.com/{repo}/blob/{branch}/CHANGELOG.md'
        rep.check(slug, f"app nueva en el manifest: \"{plan['display_name']}\"")
    else:
        plan['display_name'] = entry.get('display_name')
    return plan


# ───────────────────────────── FTP ─────────────────────────────

def ftp_connect(central):
    import ftplib
    c = json.loads((central / 'credentials.json').read_text(encoding='utf-8'))
    ftp = ftplib.FTP_TLS(c['_ftp_host'], timeout=60)
    ftp.login(c['_ftp_user'], c['_ftp_pass'])
    ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def ftp_read(ftp, path):
    import io
    buf = io.BytesIO()
    ftp.retrbinary(f'RETR {path}', buf.write)
    return buf.getvalue()


def ftp_write(ftp, path, data):
    import io
    ftp.storbinary(f'STOR {path}', io.BytesIO(data))


def verify_remote(central, slug, version, files, manifest_bytes, latest):
    """Sesion FTP NUEVA: los binarios siguen ahi con su tamano (LIST + SIZE) y su SHA256 (se
    descargan de nuevo, S-1275), el SHA256SUMS los lista con su hash, y manifest.json y
    *-latest.json son byte a byte los locales."""
    ftp = ftp_connect(central)
    try:
        folder = f'/releases/{slug}/v{version}'
        listing = []
        ftp.retrlines(f'LIST {folder}', listing.append)
        ftp.voidcmd('TYPE I')
        for name, f in files.items():
            if not any(line.split(None, 8)[-1] == name for line in listing if line.strip()):
                raise Fallo(f'LIST de {folder} no muestra {name}')
            size = ftp.size(f'{folder}/{name}')
            if size != f['size']:
                raise Fallo(f'{folder}/{name}: {size} bytes en el FTP, {f["size"]} en local')
            h = hashlib.sha256()
            ftp.retrbinary(f'RETR {folder}/{name}', h.update)
            if h.hexdigest() != f['sha256']:
                raise Fallo(f'{folder}/{name}: el SHA256 descargado del FTP no es el del binario comprobado')
        if files:
            sums = parse_sums(ftp_read(ftp, f'{folder}/{slug}-v{version}-SHA256SUMS.txt').decode('utf-8'))
            for name, f in files.items():
                if sums.get(name) != f['sha256']:
                    raise Fallo(f'{slug}-v{version}-SHA256SUMS.txt no lista {name} con su hash')
        if manifest_bytes is not None and ftp_read(ftp, '/updates/manifest.json') != manifest_bytes:
            raise Fallo('/updates/manifest.json del FTP no es el manifest local firmado')
        for fname, data in latest.items():
            if ftp_read(ftp, f'/updates/{fname}') != data:
                raise Fallo(f'/updates/{fname} del FTP no es el local')
        return len(listing)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


DRY_WRAPPER = r'''
import sys, urllib.parse
scripts, endpoint = sys.argv[1], sys.argv[2]
if urllib.parse.urlsplit(endpoint).hostname not in ("127.0.0.1", "localhost"):
    raise SystemExit("en seco el sign-manifest tiene que ser local")
sys.path.insert(0, scripts)
import ftp_release
ftp_release.SIGN_ENDPOINT = endpoint
sys.argv = [ftp_release.__file__] + sys.argv[3:]
ftp_release.main()
'''


def run_ftp_release(central, slug, version, paths, sign_endpoint, log, ficha):
    """sign_endpoint: el del sign-manifest simulado (en seco) o None (el de verdad).
    ficha: los datos de la ficha de release; ftp_release.py --fabrica no sube sin ellos."""
    args = ['--fabrica', '--ficha', str(ficha), slug, version, *paths]
    if sign_endpoint:
        cmd = [sys.executable, '-c', DRY_WRAPPER, str(central / 'scripts'), sign_endpoint, *args]
    else:
        cmd = [sys.executable, str(central / 'scripts' / 'ftp_release.py'), *args]
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    run(cmd, cwd=str(central), env=env, log=log, label='ftp_release.py')


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Mock:
    """FTPS y sign-manifest simulados (mock_servers.py) y credentials.json que apunta a ellos."""

    def __init__(self, central, work):
        self.central, self.work = central, work
        self.root = work / 'ftp-simulado'
        self.state = work / 'mock'
        self.secret = secrets.token_hex(16)
        self.user, self.password = 'dryrun', secrets.token_hex(12)
        self.proc = None
        self.sign_port = free_port()   # el FTP va en el 21 (ftp_release no deja elegir); la firma, en uno libre
        self.sign_endpoint = f'http://127.0.0.1:{self.sign_port}/api/sign-manifest'

    def start(self):
        for d in (self.root, self.state):
            if d.exists():
                shutil.rmtree(d)
        self.state.mkdir(parents=True)
        (self.root / 'releases').mkdir(parents=True)   # audit_releases lista /releases aunque no se suba nada
        log = open(self.state / 'mock.log', 'w', encoding='utf-8')
        self.proc = subprocess.Popen(
            [sys.executable, str(FACTORY / 'scripts' / 'mock_servers.py'), '--root', str(self.root),
             '--state', str(self.state), '--user', self.user, '--password', self.password,
             '--secret', self.secret, '--ftp-port', str(MOCK_FTP_PORT), '--sign-port', str(self.sign_port)],
            stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(100):
            if (self.state / 'ready').exists():
                break
            if self.proc.poll() is not None:
                raise Fallo(f'los servidores simulados no arrancan: {(self.state / "mock.log").read_text()[-400:]}')
            time.sleep(0.2)
        else:
            raise Fallo('los servidores simulados no responden en 20 s')
        write_json(self.central / 'credentials.json', {
            '_ftp_host': '127.0.0.1', '_ftp_user': self.user, '_ftp_pass': self.password,
            '_release_signing_secret': self.secret})

    def seed(self, rel, data):
        p = self.root / rel.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def sign_requests(self):
        p = self.state / 'sign-requests.jsonl'
        return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        cred = self.central / 'credentials.json'
        if cred.exists() and credentials_host(self.central) == '127.0.0.1':
            cred.unlink()


# ───────────────────────────── fase 2: publicar una app ─────────────────────────────

def ficha_final(plan, ficha_path, work, seco, rep):
    """Apunta en la ficha que escribio ftp_release.py la comprobacion por hash y la sube a la
    Release (en seco solo queda en el artefacto, work/fichas/)."""
    slug = plan['app']
    if not ficha_path.exists():
        raise Fallo(f'ftp_release.py no dejo la ficha de release en {ficha_path}')
    ficha = json.loads(ficha_path.read_text(encoding='utf-8'))
    for bloque in (ficha.get('plataformas') or {}).values():
        if any(a.get('nombre') in plan['files'] for a in bloque.get('artefactos') or []):
            bloque.setdefault('publicacion', {})['verificacion_remota'] = VERIFICACION_FTP
    write_json(ficha_path, ficha)
    out = work / 'fichas' / plan['ficha_nombre']
    write_json(out, ficha)
    cmd = ['release', 'upload', plan['tag'], '-R', plan['repo'], str(out), '--clobber']
    if seco:
        rep.check(slug, f"ficha de release en el artefacto ({out.name}); (en seco) se subiria a la Release "
                        f"{plan['tag']}")
    elif gh(*cmd, check=False).returncode == 0:
        rep.check(slug, f"ficha de release subida a la Release {plan['tag']} ({out.name})")
    else:
        # Ya esta publicado: que no pare el resto (main, web, puerta). La ficha sigue en el artefacto.
        rep.avisos.append(f"{slug}: no se pudo subir {out.name} a la Release {plan['tag']}: "
                          'subela a mano desde el artefacto de este run')


def publish_app(plan, central, work, seco, rep, mock, today, now_iso):
    slug, ver = plan['app'], plan['version']
    manifest_path = central / 'updates' / 'manifest.json'
    log = work / f'{slug}.log'
    snapshot = manifest_path.read_bytes()
    manifest = json.loads(snapshot)
    validate_manifest = load_central_module(central, 'validate_manifest')

    entry = apply_plan(manifest, plan, today)
    errors = validate_manifest.validate_schema(entry)
    if errors:
        raise Fallo(f'esquema del manifest: {errors}')
    new_bytes = dump_json(manifest).encode('utf-8')
    changed_manifest = manifest != json.loads(snapshot)   # por contenido: el checkout puede venir en CRLF

    uploaded = False
    ficha_path = central / 'releases-archive' / slug / f'v{ver}' / 'ficha.json'
    if plan['files']:
        # La ficha de una subida anterior de esta version (p. ej. Windows antes que Linux):
        # ftp_release.py le añade el bloque de esta plataforma y conserva los demas.
        if plan.get('ficha_previa') and not ficha_path.exists():
            ficha_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plan['ficha_previa'], ficha_path)
        manifest_path.write_bytes(new_bytes)
        sign_before = len(mock.sign_requests()) if mock else 0
        try:
            run_ftp_release(central, slug, ver, [f['path'] for f in plan['files'].values()],
                            mock.sign_endpoint if seco else None, log, plan['ficha_datos'])
        except Fallo as e:
            manifest_path.write_bytes(snapshot)
            raise Fallo(f'ftp_release.py: {e} (manifest local restaurado; ver {log.name})')
        uploaded = True
        rep.app(slug, servidor=True)   # el manifest con esta app ya esta en el FTP
        signed = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not signed.get('sig') or signed['sig'] == json.loads(snapshot).get('sig'):
            raise Fallo('ftp_release.py no dejo una firma nueva en el manifest')
        if mock:
            reqs = mock.sign_requests()[sign_before:]
            payload = {k: v for k, v in signed.items() if k != 'sig'}
            from mock_servers import dryrun_sig
            if len(reqs) != 1 or not reqs[0]['secret_ok'] or reqs[0]['has_sig'] or signed['sig'] != dryrun_sig(payload):
                raise Fallo(f'firma simulada inesperada: {reqs}')
            rep.check(slug, 'manifest firmado (sign-manifest simulado: secreto enviado, sin "sig" en lo firmado, '
                            'firma = la del contenido subido)')
        else:
            rep.check(slug, 'manifest firmado por sign-manifest')
        rep.check(slug, f'ftp_release.py --fabrica: {len(plan["files"])} ficheros + SHA256SUMS + manifest '
                        '(sin poda ni archivo)')
    elif changed_manifest:
        raise Fallo('el manifest cambia sin ficheros que subir (no deberia pasar)')

    # *-latest.json (Tauri con endpoint propio)
    latest_bytes = {}
    for fname, info in plan['latest'].items():
        path = central / 'updates' / fname
        if plan.get('windows_igual') and path.exists():
            cur = json.loads(path.read_text(encoding='utf-8'))
            if cur.get('version') == ver and cur.get('platforms', {}).get('windows-x86_64', {}).get('signature') == info['signature']:
                rep.check(slug, f'{fname} ya apunta a {ver}')
                continue
        data = dump_json(latest_json(ver, plan['windows_notes'], now_iso, info['signature'], info['url'])).encode('utf-8')
        ftp = ftp_connect(central)
        try:
            ftp_write(ftp, f'/updates/{fname}', data)
        finally:
            ftp.quit()
        path.write_bytes(data)          # solo lo que ya esta en el FTP llega a main
        rep.app(slug, servidor=True)
        latest_bytes[fname] = data
        rep.check(slug, f'{fname} subido ({ver}, firma del .sig de la Release)')

    if uploaded or latest_bytes:
        n = verify_remote(central, slug, ver, plan['files'],
                          manifest_path.read_bytes() if uploaded else None, latest_bytes)
        rep.check(slug, f'sesion FTP nueva: LIST ({n} entradas), SIZE y SHA256 (descargado de nuevo) de '
                        'cada fichero, SHA256SUMS, manifest y latest iguales a los locales')

    if uploaded:
        ficha_final(plan, ficha_path, work, seco, rep)

    # Web: fallback de las fichas a medida y novedades
    if 'windows' in plan['platforms']:
        out = run([sys.executable, str(central / 'scripts' / 'sync_fallback_hrefs.py'), slug],
                  cwd=str(central), log=log).stdout.strip()
        rep.check(slug, f'sync_fallback_hrefs: {out.splitlines()[-1] if out else "sin salida"}')
        published_today = plan.get('windows_block') or entry.get('release_date') == today
        news = central / 'site' / 'news' / 'index.html'
        if published_today and plan['windows_notes']:
            text = news.read_bytes().decode('utf-8')
            new = news_insert(text, entry['release_date'], plan['display_name'], ver, plan['windows_notes'], slug)
            if new != text:
                news.write_bytes(new.encode('utf-8'))
                rep.check(slug, 'entrada nueva en site/news/index.html')
            else:
                rep.check(slug, 'site/news/index.html ya tenia esta version')
        if downloads_hidden(central, slug):
            rep.avisos.append(f'{slug} sigue en DOWNLOADS_HIDDEN de site/assets/catalog.js: sacarla '
                              'es manual (decision de Javier) y no sale en /downloads/ hasta entonces')
    rep.app(slug, estado='publicada' if (uploaded or latest_bytes) else 'ya publicada')


# ───────────────────────────── fase 3 ─────────────────────────────

def changed_paths(central):
    out = git(central, 'status', '--porcelain', '--', 'updates', 'site').stdout
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def commit(central, apps_done, partial):
    paths = changed_paths(central)
    if not paths:
        return None
    if any(Path(p).name == 'credentials.json' for p in paths):
        raise Fallo('credentials.json aparece entre los cambios: no se commitea')
    git(central, 'add', '--', *paths)
    what = ', '.join(f"{p['app']} v{p['version']} ({' y '.join(p['platforms'])})" for p in apps_done)
    msg = (f'release: {what} desde la fabrica'
           + (' (PARCIAL: la publicacion se paro, ver el resumen del run)' if partial else '')
           + ' (S-1069)\n\nmanifest firmado, *-latest.json, fallback de las fichas y novedades. '
             'Publicado por tsp-release-factory/publish.yml.')
    git(central, '-c', 'user.name=tsp-release-factory',
        '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'commit', '-q', '-m', msg)
    return paths


PUSH_INTENTOS = 6


def push(central, base, rep, intentos=PUSH_INTENTOS, espera=10):
    """Sube el commit de la tanda a main aunque otras sesiones esten subiendo a la vez.

    Con el FTP ya publicado, quedarse sin subir deja main con el manifest viejo y la siguiente
    publicacion lo revertiria (2026-11-16: el rebase choco y la tanda paro). Por eso reintenta:
    rebase sobre main fresco y, si choca, rehace el commit aplicando el parche a 3 vias.
    """
    msg = git(central, 'log', '-1', '--format=%B').stdout
    parche = Path(central).parent / f'tanda-push-{os.getpid()}.diff'
    # En bytes: el modo texto de run() cambia los \r\n y el parche de un fichero CRLF no aplicaria
    parche.write_bytes(subprocess.run(['git', '-C', str(central), 'diff', '--binary', base, 'HEAD'],
                                      capture_output=True, check=True).stdout)
    avisado = False
    ultimo = ''
    for intento in range(intentos):
        p = git_auth(central, 'push', 'origin', 'HEAD:main', check=False)
        if p.returncode == 0:
            if intento:
                rep.avisos.append(f'el push a main entro al intento {intento + 1} (otras sesiones subian a la vez)')
            return
        ultimo = ((p.stdout or '') + (p.stderr or '')).strip().splitlines()[-1:] or ['']
        time.sleep(espera * (intento + 1))
        git_auth(central, 'fetch', 'origin', 'main')
        if not avisado and git(central, 'diff', '--quiet', base, 'FETCH_HEAD', '--', 'updates/manifest.json',
                               check=False).returncode != 0:
            avisado = True
            rep.avisos.append('CRITICO: otra sesion cambio updates/manifest.json en main mientras se publicaba; '
                              'el manifest del FTP no lleva ese cambio: hay que volver a firmarlo y subirlo')
        if git(central, 'rebase', 'FETCH_HEAD', check=False).returncode == 0:
            continue
        git(central, 'rebase', '--abort', check=False)
        git(central, 'reset', '-q', '--hard', 'FETCH_HEAD')
        a = git(central, 'apply', '--3way', '--index', str(parche), check=False)
        if a.returncode != 0:
            git(central, 'reset', '-q', '--hard', 'FETCH_HEAD')
            rep.main_sin_subir = True
            raise Fallo('el push a main se rechazo y el parche no aplica sobre main ni a 3 vias: hay que subir '
                        'el commit a mano (el parche va en los artefactos del run)')
        git(central, '-c', 'user.name=tsp-release-factory',
            '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'commit', '-q', '-m', msg)
    rep.main_sin_subir = True
    raise Fallo(f'el push a main se rechazo {intentos} veces ({ultimo[0]}): hay que subir el commit a mano '
                '(el parche va en los artefactos del run)')


def seed_site_manifest(central, mock):
    deploy_site = load_central_module(central, 'deploy_site')
    files = deploy_site.collect_local_files(str(central / 'site'))
    data = json.dumps({k: v[1] for k, v in files.items()}, ensure_ascii=False, indent=2).encode('utf-8')
    mock.seed('/site-manifest.json', data)
    # S-1352: las fichas de las apps tal como están en main (= lo desplegado, en seco). La puerta
    # (audit_releases) las lee por FTP; sin ellas todas salían «sin desplegar».
    for ficha in sorted((central / 'site' / 'tools').glob('*/index.html')):
        mock.seed(f'/tools/{ficha.parent.name}/index.html', ficha.read_bytes())


def simular_deploy(central, mock, salida):
    """En seco, lo que deploy_site.py --dry-run subiría se copia al FTP simulado, como ya se hace
    con los binarios en 4-6a. Sin esto, la puerta (audit_releases lee la ficha por FTP) veía las
    fichas de la tanda «sin desplegar» y todo ensayo con versión nueva acababa en rojo (S-1352)."""
    listed = re.findall(r'^\s+(/\S+)\s+\(', salida.split('[DRY-RUN] Ficheros que se subir', 1)[-1], re.M)
    site = (central / 'site').resolve()
    for rel in listed:
        src = (site / rel.lstrip('/')).resolve()
        if src.is_relative_to(site) and src.is_file():
            mock.seed(rel, src.read_bytes())
    return listed


def http_json(url, intentos=4, pausa=20):
    """S-1352: con reintentos; desde los runners el hosting sirve a veces HTML en vez del JSON."""
    return leer_json_web(url, 'tsp-release-factory/1.0', intentos=intentos, pausa=pausa)


def latest_en_vivo(fname, version, rep, leer=http_json):
    """Puerta 6d: *-latest.json en vivo sirve `version`. Si el hosting solo da HTML (protección
    antibots), queda como aviso: el paso 4-6a ya comprobó por FTP que latest es el local. Una versión
    distinta, o un JSON roto o un error que no es esa página, siguen parando."""
    url = f'{UPDATES_PREFIX}{fname}'
    try:
        live = leer(url)
    except NoEsJSON as e:
        # Solo la página de espera de la protección del hosting es aviso; otra página HTML
        # (una caída, un 404 servido con 200) sigue parando.
        if not (e.html and 'One moment, please' in str(e)):
            raise Fallo(f'{url} no da JSON: {e}') from None
        rep.avisos.append(f'{url} no se pudo comprobar en vivo: el hosting sirvio HTML en todos los '
                          f'intentos ({e}). El FTP ya lo comprobo igual al local.')
        return
    except OSError as e:
        raise Fallo(f'{url} no se pudo leer: {e}') from None
    if not isinstance(live, dict):
        raise Fallo(f'{url} no da JSON de latest: {json.dumps(live)[:80]}')
    if live.get('version') != version:
        raise Fallo(f'{url} sirve {live.get("version")}, no {version}')


# ───────────────────────────── resumen ─────────────────────────────

def write_summary(rep, plans, args, t0, md_path, json_path):
    elapsed = (time.time() - t0) / 60
    job_min = (time.time() - args.inicio_job) / 60 if args.inicio_job else None
    lines = [f"## Publicacion {'EN SECO' if rep.seco else 'REAL'}", '']
    lines.append(f'Llego hasta: **{rep.hasta}**. publish.py: {elapsed:.1f} min'
                 + (f'; job hasta aqui: {job_min:.1f} min (~{int(job_min) + 1} facturables de Linux)' if job_min else ''))
    lines += ['', '| Paso | Estado | Detalle |', '|---|---|---|']
    lines += [f'| {p} | {e} | {d} |' for p, e, d in rep.pasos]
    result = {'modo': 'seco' if rep.seco else 'real', 'hasta': rep.hasta, 'minutos_publish': round(elapsed, 1),
              'minutos_job': round(job_min, 1) if job_min else None, 'apps': [], 'avisos': rep.avisos,
              'en_real_fallaria': rep.real_fallaria, 'main_sin_subir': rep.main_sin_subir}
    for slug, a in rep.apps.items():
        plan = plans.get(slug, {})
        lines += ['', f"### {slug} {plan.get('version', '')}: {a['estado']}"]
        if a.get('motivo'):
            lines.append(f"**Motivo:** {a['motivo']}")
        lines += [f'- {c}' for c in a['comprobaciones']]
        ficheros = [{'nombre': f['name'], 'bytes': f['size'], 'mb': round(f['size'] / 1048576, 1),
                     'url': url_release(slug, plan['version'], f['name'])} for f in plan.get('files', {}).values()]
        if ficheros:
            lines.append('')
            lines += [f"- [{f['nombre']}]({f['url']}): {f['mb']} MB" for f in ficheros]
        result['apps'].append({'app': slug, 'version': plan.get('version'), 'plataformas': plan.get('platforms'),
                               'estado': a['estado'], 'motivo': a.get('motivo'), 'ficheros': ficheros,
                               'latest': list(plan.get('latest', {})), 'release_final': a.get('release_final', False)})
    if rep.avisos:
        lines += ['', '### Avisos'] + [f'- {x}' for x in rep.avisos]
    if rep.real_fallaria:
        lines += ['', '### En real esto pararia la publicacion'] + [f'- {x}' for x in rep.real_fallaria]
    tg = [f"{'[EN SECO] ' if rep.seco else ''}Publicacion de la tanda: {rep.hasta}"]
    for a in result['apps']:
        mb = sum(f['mb'] for f in a['ficheros'])
        tg.append(f"- {a['app']} {a['version'] or ''}: {a['estado']}"
                  + (f" ({len(a['ficheros'])} ficheros, {mb:.0f} MB)" if a['ficheros'] else '')
                  + (f" — {a['motivo']}" if a['motivo'] else ''))
    tg += [f'Aviso: {x}' for x in rep.avisos]
    tg.append(f"{elapsed:.0f} min" + (f" (job ~{int(job_min) + 1} min)" if job_min else ''))
    result['telegram'] = '\n'.join(tg)
    if md_path:
        Path(md_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if json_path:
        Path(json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('\n'.join(lines))


# ───────────────────────────── main ─────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--central', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--apps', required=True)
    ap.add_argument('--publicar', action='store_true', help='publica de verdad (sin esto, en seco)')
    ap.add_argument('--resumen')
    ap.add_argument('--resultado')
    ap.add_argument('--inicio-job', type=float)
    ap.add_argument('--notas', action='append')
    ap.add_argument('--entrada-previa', action='append')
    ap.add_argument('--autorizacion', help='JSON {"quien","cuando","texto","ejecucion"}: el «si» para la ficha')
    args = ap.parse_args(argv)
    t0 = time.time()
    seco = not args.publicar
    rep = Informe(seco)
    central = Path(args.central).resolve()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    plans, done, mock = {}, [], None
    try:
        apps = parse_apps(args.apps)
        notes_override = parse_pairs(args.notas, '--notas')
        previous = parse_pairs(args.entrada_previa, '--entrada-previa')
        if not seco and (notes_override or previous):
            raise Fallo('--notas y --entrada-previa son solo para el modo en seco')
        if not seco and any(s['tag'] != f"v{s['version']}" for s in apps):
            raise Fallo('un tag con sufijo (Release de pruebas) solo se usa en seco')
        autorizacion = parse_autorizacion(args.autorizacion)
        host = credentials_host(central)
        if seco and host not in (None, '127.0.0.1'):
            raise Fallo('en seco no se usa un checkout con credentials.json real: usa un clon aparte')
        if not seco and host in (None, '127.0.0.1'):
            raise Fallo('falta credentials.json con el FTP real en el checkout del central')
        if git(central, 'status', '--porcelain', '--', 'updates', 'site').stdout.strip():
            raise Fallo('el checkout del central tiene cambios sin commitear en updates/ o site/')
        base = git(central, 'rev-parse', 'HEAD').stdout.strip()
        rep.paso('1. Entradas', 'ok', f"{len(apps)} app(s); central en {base[:8]}")

        manifest_path = central / 'updates' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for slug, rev in previous.items():
            if git(central, 'cat-file', '-e', f'{rev}^{{commit}}', check=False).returncode != 0:
                git_auth(central, 'fetch', '-q', '--depth=1', 'origin', rev)   # el checkout es de profundidad 1
            old = json.loads(git(central, 'show', f'{rev}:updates/manifest.json').stdout)
            old_entry = next(t for t in old['tools'] if t['tool'] == slug)
            plats = next((s['platforms'] for s in apps if s['app'] == slug), [])
            manifest['tools'] = [entrada_de_partida(t, old_entry, plats) if t['tool'] == slug else t
                                 for t in manifest['tools']]
            write_json(manifest_path, manifest)
            git(central, '-c', 'user.name=dry-run', '-c', 'user.email=dry-run@localhost', 'commit', '-q',
                '-m', f'[en seco] {slug} como en {rev}', '--', 'updates/manifest.json')
            base = git(central, 'rev-parse', 'HEAD').stdout.strip()
            rep.check(slug, f'(en seco) entrada de partida del manifest = la de {rev}')

        for spec in apps:
            try:
                plans[spec['app']] = prepare(spec, manifest, central, work, rep, notes_override,
                                             autorizacion=autorizacion)
                rep.app(spec['app'], estado='lista')
            except Fallo as e:
                rep.app(spec['app'], estado='excluida', motivo=str(e))
                print(f"  [{spec['app']}] EXCLUIDA: {e}", flush=True)
        if not plans:
            raise Fallo('ninguna app de la tanda paso las comprobaciones')
        rep.paso('2-3. Descarga, comprobaciones y plan del manifest', 'ok',
                 f"{len(plans)} lista(s), {len(apps) - len(plans)} excluida(s)")

        if seco:
            mock = Mock(central, work)
            mock.start()
            mock.seed('/updates/manifest.json', manifest_path.read_bytes())
            seed_site_manifest(central, mock)
            sys.path.insert(0, str(FACTORY / 'scripts'))
        # Nadie ha tocado main desde el checkout: si no, se publicaria un manifest sin su cambio.
        git_auth(central, 'fetch', '-q', 'origin', 'main')
        remote = git(central, 'rev-parse', 'FETCH_HEAD').stdout.strip()
        if remote != base and not previous:
            raise Fallo(f'main se movio desde el checkout ({base[:8]} -> {remote[:8]}): relanza la publicacion')

        today = utc_now().strftime('%Y-%m-%d')
        now_iso = utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')
        for slug, plan in plans.items():
            try:
                publish_app(plan, central, work, seco, rep, mock, today, now_iso)
                done.append(plan)
            except Fallo as e:
                rep.app(slug, estado='fallo', motivo=str(e))
                raise Fallo(f'{slug}: {e}')
        rep.paso('4-6a. FTP, firma, latest, fichas y novedades', 'ok',
                 f"{len(done)} app(s){' contra el FTP simulado' if seco else ''}")
    except Fallo as e:
        rep.paso('PARADA', 'fallo', str(e))
        live = [plans[s] for s, a in rep.apps.items() if a.get('servidor') and s in plans]
        if live:
            # Lo que ya esta en el FTP tiene que quedar en main: si no, la siguiente subida del
            # manifest (desde cualquier sitio) lo revertiria en silencio. La web no se toca.
            git(central, 'restore', '--', 'site', check=False)
            try:
                commit(central, live, partial=True)
                if not seco:
                    push(central, base, rep)
                rep.paso('Rescate', 'ok', 'manifest y *-latest.json ya publicados quedan en main; web sin desplegar')
            except Fallo as e2:
                rep.paso('Rescate', 'fallo', f'{e2}: el FTP y main NO coinciden, hay que subir el commit a mano')
        finish(rep, plans, args, t0, mock, central, work)
        return 1

    try:
        paths = commit(central, done, partial=False)
        if paths is None:
            rep.paso('6b. Commit', 'ok', 'nada que commitear (todo estaba publicado)')
        else:
            (work / 'publish.diff').write_text(git(central, 'diff', base, 'HEAD').stdout, encoding='utf-8')
            stat = git(central, 'diff', '--stat', base, 'HEAD').stdout.strip().splitlines()[-1]
            if seco:
                rep.paso('6b. Commit', 'ok', f'local, sin push (en seco): {stat}; diff en publish.diff')
            else:
                push(central, base, rep)
                rep.paso('6b. Commit y push a main', 'ok', stat)

        # Deploy de site/ desde el commit de main, nunca desde un disco con trabajo ajeno
        if seco:
            out = run([sys.executable, str(central / 'scripts' / 'deploy_site.py'), '--dry-run'],
                      cwd=str(central), log=work / 'deploy.log').stdout
            m = re.search(r'(\d+) ficheros a subir', out)
            listed = simular_deploy(central, mock, out)
            rep.paso('6c. deploy_site.py --dry-run', 'ok',
                     f"{m.group(1) if m else '?'} fichero(s) a subir frente al FTP simulado (= main sin la tanda): "
                     + ', '.join(listed[:8]))
        else:
            head = git(central, 'rev-parse', 'HEAD').stdout.strip()
            git_auth(central, 'fetch', '-q', 'origin', 'main')
            if git(central, 'rev-parse', 'FETCH_HEAD').stdout.strip() != head:
                raise Fallo('HEAD no es origin/main tras el push: no se despliega')
            env = dict(os.environ, DEPLOY_ORIGEN='la fábrica de releases', PYTHONIOENCODING='utf-8')
            run([sys.executable, str(central / 'scripts' / 'deploy_site.py')], cwd=str(central), env=env,
                log=work / 'deploy.log')
            rep.paso('6c. deploy_site.py', 'ok', f'desde main {head[:8]}')

        # Puerta
        audit = run([sys.executable, str(central / 'scripts' / 'audit_releases.py'), '--quick', '--sin-respaldo'],
                    cwd=str(central), log=work / 'audit.log', check=False)
        if audit.returncode != 0:
            problems = [l.strip() for l in audit.stdout.splitlines() if l.strip().startswith('- ')]
            raise Fallo('audit_releases.py --quick --sin-respaldo: ' + '; '.join(problems[:6]))
        detail = 'audit_releases --quick --sin-respaldo OK'
        if seco:
            detail += ' (contra el FTP simulado; validate_manifest y latest en vivo no se pueden comprobar en seco)'
        else:
            for plan in done:
                run([sys.executable, str(central / 'scripts' / 'validate_manifest.py'), '--no-update', '--slug',
                     plan['app']], cwd=str(central), log=work / 'audit.log')
                for fname in plan['latest']:
                    latest_en_vivo(fname, plan['version'], rep)
            detail += '; validate_manifest --slug (HEAD y tamano en vivo) y *-latest.json en vivo OK'
        rep.paso('6d. Puerta', 'ok', detail)

        for plan in done:
            rel = plan['release']
            if not rel.get('isPrerelease'):
                continue
            cmd = ['release', 'edit', plan['tag'], '-R', plan['repo'], '--prerelease=false', '--latest']
            if seco:
                rep.check(plan['app'], f"(en seco) se haria: gh {' '.join(cmd)}")
            else:
                gh(*cmd)
                rep.app(plan['app'], release_final=True)
                rep.check(plan['app'], 'Release pasada a definitiva')
        rep.paso('7. Release definitiva', 'ok', 'en seco: no se toca' if seco else '')
    except Fallo as e:
        rep.paso('PARADA', 'fallo', f'{e} (FTP y main ya publicados; lo que falta, a mano)')
        finish(rep, plans, args, t0, mock, central, work)
        return 1
    finish(rep, plans, args, t0, mock, central, work)
    bad = [a for a in rep.apps.values() if a['estado'] in ('excluida', 'fallo')]
    return 1 if rep.real_fallaria or bad else 0


def finish(rep, plans, args, t0, mock, central, work):
    if mock:
        mock.stop()
    write_summary(rep, plans, args, t0, args.resumen, args.resultado)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
