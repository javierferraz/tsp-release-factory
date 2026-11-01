# tsp-release-factory

Fábrica de builds de las apps de The Stromboli Project. Construye, firma y prueba los
instaladores en runners de GitHub, sin depender del portátil. Plan completo en el repo central:
`docs/distribucion/releases-automaticas.md` (S-1069).

**Nada aquí se dispara solo ni publica nada.** Todos los workflows son `workflow_dispatch`: la
puerta humana es el disparo (el «sí» de la tanda). Los ficheros quedan como prerelease en la
Release del repo de la app; FTP, manifest y web son otro paso (R6).

## `build-tauri-windows.yml` — instalador de Windows de una app Tauri (R4)

```
gh workflow run build-tauri-windows.yml -R javierferraz/tsp-release-factory -f app=bright-cardinal -f repo=javierferraz/2610-bright-cardinal -f tag=v1.0.0
```

Entradas: `app` (slug), `repo` (`javierferraz/<repo>`), `tag` (`vX.Y.Z`; admite sufijo de
pruebas, `v1.0.0-factory-test`, y entonces compara solo `X.Y.Z` con la versión).

Qué hace, en `windows-latest`:

1. Comprueba que el token ve el repo de la app y que la Release `<tag>` no tiene ya los ficheros
   (si los tiene, falla antes de construir: **no pisa nada**).
2. Saca el repo de la app en el tag y comprueba que `tauri.conf.json` y `package.json` llevan
   la versión del tag.
3. Node 22 + Rust estable, `npm ci`, `npm test` y `cargo test --lib`.
4. `tauri build --bundles nsis` con el `signCommand` de esta fábrica (`signing/sign.cmd`,
   inyectado con `--config`): Azure Trusted Signing con `signtool` del Windows SDK del runner y
   el cliente `Microsoft.Trusted.Signing.Client` 1.0.95 bajado de NuGet y comprobado por hash.
   El `signCommand` del repo de la app (rutas del portátil) se ignora; el repo no se toca.
5. `signtool verify /pa` del instalador; `.sig` del actualizador con `tauri signer sign` y la
   clave de la app (a un fichero temporal que se borra), verificado con la clave pública de
   `tauri.conf.json` (`scripts/verify_minisign.py`); SHA256.
6. Smoke en el runner (`scripts/smoke-nsis.ps1`): instalar con `/S`, registro HKCU y versión,
   `.exe` instalado y `uninstall.exe` firmados, acceso del menú Inicio apuntando al `.exe`,
   arranque (vivo 10 s con su WebView2: el runner no tiene a nadie mirando la pantalla) y
   `uninstall.exe /S` sin restos.
7. Sube `<slug>-v<X.Y.Z>-windows-x64-setup.exe`, su `.sig` y `SHA256SUMS.txt` a la Release
   `<tag>` (la crea como prerelease si no existe).
8. Resumen del job con cada comprobación (sin secretos).

## Secretos

| Secreto | Para qué |
|---|---|
| `APPS_TOKEN` | PAT de grano fino con *Contents: read and write* en los repos de las apps (checkout y Releases) |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Firma Authenticode (Azure Trusted Signing, cuenta `kokoawebsign`, perfil `echocontrol-public`) |
| `TAURI_KEY_<SLUG>` | Clave privada del actualizador de cada app Tauri (contenido del `<slug>-signing.key`, sin contraseña). El nombre sale del slug en mayúsculas con `_`: `bright-cardinal` → `TAURI_KEY_BRIGHT_CARDINAL` |
| `RELEASE_SIGNING_SECRET`, `FTP_HOST`, `FTP_USER`, `FTP_PASS` | Para la publicación (R6); este workflow no los usa |

Añadir una app Tauri nueva = darle acceso a `APPS_TOKEN` sobre su repo y grabar su
`TAURI_KEY_<SLUG>` (`gh secret set TAURI_KEY_<SLUG> -R javierferraz/tsp-release-factory < <slug>-signing.key`).

## Seguridad

- `permissions: contents: read` (solo para leer esta fábrica); el repo de la app va con `APPS_TOKEN`.
- Solo `workflow_dispatch` y solo desde `main`: no corre en pull requests ni en ramas.
- Las entradas se validan y se usan por variable de entorno, nunca interpoladas en el script.
- Acciones fijadas por SHA; el cliente de Trusted Signing, por hash.
- La clave del actualizador solo existe en el paso que firma, en un fichero temporal del runner.
