#!/usr/bin/env bash
# S-1327: vigila un run de esta fábrica recién lanzado y sale con 0 solo si acabó en «success».
#
# `gh workflow run` devuelve la URL del run antes de que la API lo conozca: en la tanda del 26-11
# `gh run watch` recibió «HTTP 404: Not Found» al instante y la tanda dejó fuera Handy Raccoon por
# «falló el build de windows», con el build (run 45842981252) acabando en verde seis minutos después.
# Por eso: primero espera a que el run exista, y el veredicto es la conclusión que guarda GitHub,
# no el código de salida de `gh run watch` (que también falla por un corte de red).
#
# Uso: GH_TOKEN=... vigilar_run.sh <id> <owner/repo>
set -u
id=$1 repo=$2
intentos=${VIGILAR_INTENTOS:-12} pausa=${VIGILAR_PAUSA:-10}

for ((n = 1; ; n++)); do
  gh run view "$id" -R "$repo" --json status > /dev/null 2>&1 && break
  if [ "$n" -ge "$intentos" ]; then
    echo "el run $id no aparece en la API tras $((intentos * pausa)) s"
    exit 1
  fi
  sleep "$pausa"
done

for ((n = 1; n <= 3; n++)); do
  gh run watch "$id" -R "$repo" --interval 30 > /dev/null && break
  sleep "$pausa"
done

estado=$(gh run view "$id" -R "$repo" --json status,conclusion --jq '.status + " " + .conclusion' 2>/dev/null)
[ "$estado" = "completed success" ] && exit 0
echo "el run $id acabó como: ${estado:-desconocido}"
exit 1
