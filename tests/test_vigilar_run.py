"""scripts/vigilar_run.sh (S-1327): un run recién lanzado que la API aún no conoce no es un fallo.

    python -m pytest tests/test_vigilar_run.py -q

Usa un `gh` falso en el PATH que contesta 404 las primeras veces, como el 26-11.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vigilar_run.sh'
BASH = shutil.which('bash')
pytestmark = pytest.mark.skipif(not BASH or 'system32' in BASH.lower(), reason='hace falta bash (Git Bash o Linux)')

GH_FALSO = r'''#!/usr/bin/env bash
d=$(dirname "$0")
echo "$*" >> "$d/llamadas"
case "$*" in
  "run view"*"--json status") # ¿existe el run?
    n=$(cat "$d/vistas" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$d/vistas"
    if [ "$n" -le "${GH_404:-0}" ]; then echo "failed to get run: HTTP 404: Not Found" >&2; exit 1; fi
    exit 0 ;;
  "run watch"*) exit "${GH_WATCH_RC:-0}" ;;
  "run view"*"status,conclusion"*) echo "${GH_ESTADO:-completed success}" ;;
esac
'''


def correr(tmp_path, **env):
    gh = tmp_path / 'gh'
    gh.write_bytes(GH_FALSO.encode())
    gh.chmod(0o755)
    entorno = {**os.environ, 'VIGILAR_PAUSA': '0', 'VIGILAR_INTENTOS': '5',
               'PATH': f'{tmp_path}{os.pathsep}{os.environ["PATH"]}', **env}
    r = subprocess.run([BASH, str(SCRIPT), '45842981252', 'javierferraz/tsp-release-factory'],
                       capture_output=True, text=True, env=entorno)
    return r.returncode, r.stdout + r.stderr


def test_404_al_principio_y_build_verde_es_verde(tmp_path):
    """El caso de Handy Raccoon del 26-11: 404 justo tras lanzarlo, el build acaba bien."""
    rc, out = correr(tmp_path, GH_404='2')
    assert rc == 0, out


def test_build_rojo_sigue_siendo_rojo(tmp_path):
    rc, out = correr(tmp_path, GH_ESTADO='completed failure')
    assert rc == 1 and 'completed failure' in out


def test_run_que_nunca_aparece_es_fallo(tmp_path):
    rc, out = correr(tmp_path, GH_404='99')
    assert rc == 1 and 'no aparece' in out


def test_watch_que_se_corta_no_decide(tmp_path):
    """Si `gh run watch` falla (red), manda la conclusión guardada en GitHub."""
    rc, out = correr(tmp_path, GH_WATCH_RC='1')
    assert rc == 0, out
    rc, out = correr(tmp_path, GH_WATCH_RC='1', GH_ESTADO='in_progress ')
    assert rc == 1
