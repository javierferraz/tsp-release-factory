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
2. Saca el repo de la app en el tag **con saltos de línea LF** (`core.autocrlf false`; los
   runners de Windows traen `true` y sacarían CRLF). Así se construye igual que en el portátil
   y en Linux, y no rompe tests que leen su propio fuente: el `v1.0.0` de Bright Cardinal busca
   `"\n}\n"` en `include_str!("commands.rs")` y con CRLF fallaba (run 41340319888). Luego
   comprueba que `tauri.conf.json` y `package.json` llevan
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

## `build-pyside-windows.yml` — instalador de Windows de una app PySide6 (R4)

```
gh workflow run build-pyside-windows.yml -R javierferraz/tsp-release-factory -f app=snappy-wasp -f repo=javierferraz/2606-snappy-wasp -f tag=v1.6.2
```

Mismas entradas y mismas reglas que el de Tauri. Dos jobs, por coste (Windows factura el
doble): si el primero falla, el de Windows ni arranca.

**`checks` (ubuntu-24.04):**

1. Valida las entradas; comprueba que el token ve el repo y que la Release `<tag>` no tiene ya
   el instalador ni `SHA256SUMS.txt` (**no pisa nada**).
2. Saca la app en el tag y comprueba que `__version__` (`utils/version.py`, `version.py` o
   `main.py`), la sección `## [X.Y.Z]` del CHANGELOG y el `AppVersion` del `.iss` coinciden con
   el tag; que el `.iss` lleva `SignTool=stromboli`, `PrivilegesRequired=lowest` y
   `{localappdata}`; y lee el Python del build de la línea `Interprete: CPython X.Y.Z` de
   `requirements-build.txt`.
3. `pytest` con `QT_QPA_PLATFORM=offscreen` bajo `xvfb-run` (pynput se conecta a un servidor X
   al importarse; sin él, pytest 9 no salta el módulo y la recogida revienta), en un entorno
   nuevo con `requirements.txt` y pytest fijado. Los tests solo de Win32 se saltan solos
   (`skipif`) y salen listados en el resumen. **Excepción:** las apps de
   `config/tests-en-windows.txt` (slug y motivo) pasan pytest en el job de Windows, antes del
   build, porque sus tests no corren en Linux sin tocar la app. Hoy: Snappy Wasp (dos tests de
   `test_activity_notice.py` usan `_Win32Hotkey` sin `skipif`). Se saca de la lista en cuanto la
   app los marque.

**`windows` (windows-latest):**

4. Entorno nuevo **solo** con `requirements-build.txt`, con ese Python exacto, y comprobación de
   que `pip freeze` es el freeze, ni más ni menos (un entorno equivocado metió numpy en Spider y
   otra cryptography en Frog).
5. `pyinstaller <App>.spec` con el PATH reducido al entorno y a Windows (con el de Git delante,
   PyInstaller empaqueta su OpenSSL); falla si aparece `libssl-3-x64`, `libcrypto-3-x64` o una
   DLL de MSYS. Firma `dist\<App>\<App>.exe` con `signing/sign.cmd` y `signtool verify /pa`.
6. Inno Setup 6.7.3 (la versión del portátil) bajado de la Release de `jrsoftware/issrc`,
   comprobado por hash y por firma, instalado en el job. `ISCC /Sstromboli=<sign.cmd> $f`
   compila el `.iss` del repo de la app sin tocarlo: Inno firma el desinstalador, el `.tmp` que
   el setup extrae en `%TEMP%` (error 4551 de Smart App Control si no, S-573) y el instalador, y
   aborta si alguno sale sin firma.
7. `signtool verify /pa` del instalador y SHA256.
8. Smoke en el runner (`scripts/smoke-inno.ps1`): `/VERYSILENT`, clave `<AppId>_is1` en HKCU con
   la versión, carpeta bajo `%LOCALAPPDATA%`, `.exe` y `unins000.exe` firmados, acceso del menú
   Inicio `<Animal> - <Función>` apuntando al `.exe`, arranque (vivo 15 s), **instancia única**
   (un segundo arranque sale solo y el primero sigue vivo), `crash.log` vacío, y
   `unins000.exe /VERYSILENT` sin registro, `.exe` ni acceso. Si la app deja su autoarranque en
   `HKCU\Run` tras desinstalar, sale como aviso (no falla).
9. Sube `<slug>-v<X.Y.Z>-windows-x64-setup.exe` y `SHA256SUMS.txt` a la Release `<tag>` (la crea
   como prerelease si no existe). No hay `.sig`: el actualizador de las apps PySide6 confía en
   el manifest firmado, que es R6.

No genera el `portable.zip` que algunas releases publicaron a mano.

## Coste medido

GitHub factura cada job redondeando al minuto, y los de Windows cuentan doble. La API de
`timing` del run devuelve 0 en este repo, así que los facturables se calculan con esa regla.

| Workflow | App | Run | Job | Duración | Facturables |
|---|---|---|---|---|---|
| `build-tauri-windows` | Bright Cardinal 1.0.0 | 41341539373 | build (Windows) | 18 min 2 s | ~38 |
| `build-pyside-windows` | Snappy Wasp 1.6.2 | 41360434848 | checks (ubuntu-24.04) | 10 s | 1 |
| | | | windows (con pytest, caché de pip caliente) | 3 min 21 s | 8 |

En el de Snappy Wasp el job de Windows se reparte así: PyInstaller y firma del `.exe` 44 s,
ISCC con las tres firmas 39 s, smoke 26 s, pytest 18 s, entorno del build 17 s, el resto
(checkouts, Python, Inno, subida) ~1 min. Una app PySide6 cuyos tests van en Linux se ahorra
el paso de pytest en Windows.

`pytest` va con `-p no:unraisableexception` en los dos jobs: ese plugin fuerza un
`gc.collect()` al cerrar la sesión que destruye objetos de Qt en mal orden. Con el checkout
recién sacado (sin `.pyc`) los 71 tests de Snappy Wasp v1.6.2 pasan y el proceso muere con
`0xC0000374` (heap corruption) antes del resumen, en el runner y en el portátil, siempre; sin el
plugin sale 0. Solo se pierde el aviso de excepciones *unraisable*, que pytest no cuenta como
fallo.

## Secretos

| Secreto | Para qué |
|---|---|
| `APPS_TOKEN` | PAT de grano fino con *Contents: read and write* en los repos de las apps (checkout y Releases) |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Firma Authenticode (Azure Trusted Signing, cuenta `kokoawebsign`, perfil `echocontrol-public`) |
| `TAURI_KEY_<SLUG>` | Clave privada del actualizador de cada app Tauri (contenido del `<slug>-signing.key`, sin contraseña). El nombre sale del slug en mayúsculas con `_`: `bright-cardinal` → `TAURI_KEY_BRIGHT_CARDINAL` |
| `RELEASE_SIGNING_SECRET`, `FTP_HOST`, `FTP_USER`, `FTP_PASS` | Para la publicación (R6); este workflow no los usa |

Añadir una app Tauri nueva = darle acceso a `APPS_TOKEN` sobre su repo y grabar su
`TAURI_KEY_<SLUG>` (`gh secret set TAURI_KEY_<SLUG> -R javierferraz/tsp-release-factory < <slug>-signing.key`).

Añadir una app PySide6 nueva = darle acceso a `APPS_TOKEN` sobre su repo (no tiene clave propia)
y que el repo cumpla lo que lista la cabecera de `build-pyside-windows.yml`; si sus tests no
corren en Linux, una línea con su slug y el motivo en `config/tests-en-windows.txt`.

## Seguridad

- `permissions: contents: read` (solo para leer esta fábrica); el repo de la app va con `APPS_TOKEN`.
- Solo `workflow_dispatch` y solo desde `main`: no corre en pull requests ni en ramas.
- Las entradas se validan y se usan por variable de entorno, nunca interpoladas en el script.
- Acciones fijadas por SHA; el cliente de Trusted Signing, por hash.
- La clave del actualizador solo existe en el paso que firma, en un fichero temporal del runner.
