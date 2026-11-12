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

## `build-tauri-linux.yml` — AppImage y `.deb` de una app Tauri (R5)

```
gh workflow run build-tauri-linux.yml -R javierferraz/tsp-release-factory -f app=bright-cardinal -f repo=javierferraz/2610-bright-cardinal -f tag=v1.0.0
```

Mismas entradas y mismas reglas que los de Windows. Un job en `ubuntu-24.04` (fijo: con
`ubuntu-latest` sale glibc 2.43 y no arranca en Ubuntu 24.04 ni Mint 22, S-930):

1. Valida las entradas; comprueba que el token ve el repo y que la Release `<tag>` no tiene ya
   el AppImage, el `.deb` ni `linux-SHA256SUMS.txt` (**no pisa nada**).
2. Saca la app en el tag y comprueba que `tauri.conf.json` y `package.json` llevan la versión del
   tag y que existe `src-tauri/tauri.linux.conf.json` (targets `appimage` y `deb`).
3. Dependencias del sistema (las del `linux-build.yml` de las apps, `patchelf` incluido), Node 22,
   Rust estable y caché de Rust por app.
4. **Tests de la app, aquí y una sola vez:** `npm test` (si la app tiene ese script; Chicken, Lion
   y Tiger no lo tienen) y `cargo test --lib`, que además prueba la capa `platform/linux.rs`.
5. `tauri build --bundles appimage deb` sin ficheros del actualizador (`createUpdaterArtifacts`
   a `false` por `--config`, sin tocar el repo: Linux no los usa y no hace falta la clave).
6. Artefactos filtrados por `_<versión>_` (incidente Clever Cat 1.3.6), renombrados a
   `<slug>-v<X.Y.Z>-linux-x64.{AppImage,deb}`; el `.deb` declara esa versión y `amd64`.
7. Abre el AppImage (`--appimage-extract`) y el `.deb` (`dpkg-deb -x`) y comprueba con `objdump -T`
   que **ningún** binario que se entrega pide glibc > 2.39 (los workflows de las apps solo miraban
   el ejecutable o el AppDir), y que el runtime del AppImage no enlaza `libfuse` (sin el runtime
   estático no arranca sin `libfuse2`). SHA256 en `linux-SHA256SUMS.txt` (formato `sha256sum`).
8. Sube los tres ficheros a la Release `<tag>` sin `--clobber`; si no existe, la crea como
   prerelease, y si el job de Windows la crea a la vez, sigue con la suya.

Sirve para las 6 Tauri con `linux-build.yml` (Cat, Chicken, Lion, Tiger, Cardinal, Greenjay) y
para Parrot y Bluejay, que no lo tienen pero sí `tauri.linux.conf.json`: el build es el mismo que
hacen los `build-<slug>.sh` (`npm install` + `tauri build` + copiar por versión). No pasa el
`check-farewell.py` del central (tsp-linux-build §3.0); tampoco lo pasan los de Windows.

## `build-pyside-linux.yml` — AppImage y `.deb` de una app PySide6 (R5)

```
gh workflow run build-pyside-linux.yml -R javierferraz/tsp-release-factory -f app=snappy-wasp -f repo=javierferraz/2606-snappy-wasp -f tag=v1.6.2
```

Las 7 PySide6 con `linux-build.yml` (Wasp, Beaver, Raccoon, Frog, Tappy, Ant, Bison) construyen
igual: `packaging/linux/build-appimage.sh` (PyInstaller onedir, AppDir, `appimagetool`) y
`build-packages.sh` (`fpm`), con `__version__` en `utils/version.py`. La fábrica ejecuta esos
scripts del repo de la app y pone alrededor lo mismo que en Tauri: validación, Release sin esos
ficheros, versión del `.deb` (`X.Y.Z-1` por la iteración de fpm), glibc ≤ 2.39 en todo lo que se
entrega, runtime del AppImage sin `libfuse`, SHA256 y subida sin pisar.

- **Python y PyInstaller:** si `requirements-build.txt` declara `Interprete: CPython X.Y.Z` y fija
  `pyinstaller`, se usan esos (los mismos que en Windows y que en el pytest de Linux); si no,
  Python 3.12 y la última PyInstaller, como los `linux-build.yml` de las apps. Hoy solo Wasp los
  declara (CPython 3.11.9; su `linux-build.yml` usaba 3.12).
- **fpm 1.18.0 fijado** (las apps instalaban el último).
- **Paquetes de apt extra por app** en `config/linux-apt-extra.txt`: Bison y Ant instalaban los de
  Qt offscreen y PulseAudio para sus tests; se mantienen para que PyInstaller empaquete lo mismo
  que antes, y las demás no los llevan para no cambiar su bundle.
- **No pasa pytest:** en la tanda lo pasa una vez el job `checks` de `build-pyside-windows.yml`,
  que ya corre en Linux. Lanzado solo, este workflow no prueba nada de la app.

Sneaky Spider no entra todavía: su versión está en `config.py` (`APP_VERSION`), no en
`utils/version.py` (tampoco entra en `build-pyside-windows`). Orderly Owl sí cumple el contrato
aunque no tenga `linux-build.yml`. Crafty Fox no tiene `packaging/linux/`.

## `publish.yml` — publicación de una tanda ya construida (R6)

```
gh workflow run publish.yml -R javierferraz/tsp-release-factory -f apps='[{"app":"bright-cardinal","repo":"javierferraz/2610-bright-cardinal","version":"1.0.0","platforms":["windows","linux"]}]'
```

**En seco por defecto** (`dry_run: true`): el FTPS y el `sign-manifest` se simulan en
`127.0.0.1` (`scripts/mock_servers.py`), no hay push, ni deploy, ni se toca la Release, y los
secretos del FTP y de la firma no llegan al paso. De verdad, además `-f dry_run=false`.

Todo lo hace `scripts/publish.py` con los scripts del repo central, como el flujo manual
(tsp-create-dist §3 y tsp-linux-build §5):

1. Valida la tanda y saca el central (`main`) con `APPS_TOKEN`.
2. Descarga los ficheros de la Release y comprueba tamaño, SHA256 (contra `SHA256SUMS` o contra
   el manifest si esa versión ya estaba publicada), Authenticode (firmante, y firma y sello de tiempo
   hasta la raíz de Microsoft, con esa raíz de `mscerts` fijada por huella como único ancla: el
   sello lo verifica `openssl cms` y la firma `osslsigncode` 2.9 (el `.deb` de Ubuntu de
   `vendor/`, con su SHA256) a la hora del sello y sin CRL, porque el certificado de Azure Trusted
   Signing dura 3 días y osslsigncode no sabe leer el sello de Microsoft; la 2.8 de noble no sirve:
   sin `-ignore-crl`, falla siempre) y, en Tauri, el `.sig` con la pubkey de `tauri.conf.json`.
3. Entrada del manifest: Windows estable, Linux experimental, notas en inglés de
   `release-notes.json` de la Release; esquema con `validate_manifest.validate_schema`.
4. `ftp_release.py --fabrica`: sube binarios, `SHA256SUMS` y el manifest firmado, **sin poda,
   archivo en D: ni Sheets** (eso sigue siendo del portátil), y lo comprueba en una sesión FTP nueva.
5. `<app>-latest.json` de Tauri, `sync_fallback_hrefs.py`, entrada en `news/`, commit y push del
   central, `deploy_site.py` desde ese commit y `audit_releases.py --quick --sin-respaldo` de puerta.
6. Release de prerelease a definitiva y resumen (Markdown en el run, JSON con texto para Telegram
   en los artefactos).

Una app que no pasa las comprobaciones se excluye y las demás siguen. Si algo falla después de
subir al FTP, lo ya publicado se commitea en `main` (si no, la siguiente subida del manifest lo
revertiría) y el resumen dice hasta dónde llegó. Repetir una tanda ya publicada no sube nada.
`DOWNLOADS_HIDDEN` de `catalog.js` no se toca: se avisa.

**Contrato con R7:** la Release lleva `release-notes.json` con `{"windows": "...", "linux": "..."}`,
una o dos frases en inglés para quien usa la app, sin números de tarjeta. Sin él, en real no se
publica (en seco sale como «En real esto pararía la publicación»).

## `tanda.yml` — el «sí» de la tanda (R8)

Lo abre el botón «Sí, publica la tanda» del aviso [RELEASES]. «Run workflow» con «Ensayo»
desmarcado publica; por defecto es ensayo.

1. `scripts/tanda.py plan` lee del central `docs/distribucion/tandas/<fecha>.json` (vacío = la
   última) y la rechaza si caducó (24 h), ya se publicó o no tiene apps.
2. Por app: si falta el tag, sube la versión, sella el CHANGELOG, commit y tag `vX.Y.Z`; construye
   Windows y después Linux lo que la Release no tenga; sube `release-notes.json`. La que falla se
   queda fuera y las demás siguen.
3. Lanza `publish.yml` con las que llegaron y espera su resultado.

En ensayo no hay push (solo `git push --dry-run`), ni tags, ni builds, y `publish.yml` corre en seco
con las notas de la tanda (`notas_seco`). Ensayo verde: run 43125982189 (Bright Cardinal 1.0.0).

## Dónde corre cada test

Cada test, una sola vez por tanda y en Linux (la mitad de precio que Windows):

| Stack | Tests | Los pasa | Comentario |
|---|---|---|---|
| Tauri | `npm test`, `cargo test --lib` | `build-tauri-linux` | **Hoy también `build-tauri-windows`.** Propuesta: quitar de ese job los pasos «Tests (npm test)» y «Tests de Rust (cargo test --lib)» (el `npm run build` lo hace ya `tauri build`); ahorra unos 5 min de Windows ≈ 10 facturables por app. Requiere que la tanda lance siempre los dos workflows. Sin aplicar: el fichero es de R4. |
| PySide6 | `pytest` | job `checks` de `build-pyside-windows` (Linux), o su job de Windows para las de `config/tests-en-windows.txt` | `build-pyside-linux` no los repite. |

## Coste medido

GitHub factura cada job redondeando al minuto, y los de Windows cuentan doble. La API de
`timing` del run devuelve 0 en este repo, así que los facturables se calculan con esa regla.

| Workflow | App | Run | Job | Duración | Facturables |
|---|---|---|---|---|---|
| `build-tauri-windows` | Bright Cardinal 1.0.0 | 41341539373 | build (Windows) | 18 min 2 s | ~38 |
| `build-pyside-windows` | Snappy Wasp 1.6.2 | 41360434848 | checks (ubuntu-24.04) | 10 s | 1 |
| | | | windows (con pytest, caché de pip caliente) | 3 min 21 s | 8 |
| `build-tauri-linux` | Bright Cardinal 1.0.0 | 42912725701 | build (ubuntu-24.04, con npm test y cargo test) | 18 min 43 s | 19 |
| `build-pyside-linux` | Snappy Wasp 1.6.2 | 41408254413 | build (ubuntu-24.04, sin pytest) | 2 min 31 s | 3 |

En el de Bright Cardinal: `tauri build` 11 min 33 s, `cargo test --lib` 4 min 35 s (perfil
`test`, aparte del `release`), `npm test` 37 s, apt 33 s, guardar la caché de Rust 1 min; la
caché (segunda pasada) apenas acorta. Quitar los tests del job de Windows (ver «Dónde corre cada
test») compensa con creces: unos 5 min de Windows son ~10 facturables y aquí cuestan ~5.

Pilotos comparados con lo publicado (2026-11-02 y 2026-11-10; tag `-factory-test` en el mismo
commit, Release de prueba borrada después):

- **Bright Cardinal:** AppImage 104,04 MB frente a 104,05 MB, mismos 605 ficheros; `.deb`
  16,59 MB con los mismos 5 ficheros; glibc más alta 2.39 en los dos. Solo cambian el `.desktop`
  (`Categories=Office;` y `%F`) y el binario por unos bytes: el AppImage y el `.deb` de la Release
  `v1.0.0` se reconstruyeron el 2026-11-05 desde `main` con S-1116, después del tag. La primera
  pasada salió sin 9 ficheros de GStreamer (`gstreamer1.0-x` y `-gl`) por instalar con
  `--no-install-recommends`; corregido (run 41407709372 → 42912725701).
- **Snappy Wasp:** AppImage 90,7 MB frente a 93,6 MB, `.deb` 97,8 frente a 101,2 MB; mismos
  ficheros salvo los nombres y los módulos de la biblioteca estándar de Python, porque la
  fábrica usa el CPython 3.11.9 de `requirements-build.txt` y el `linux-build.yml` de la app
  usaba 3.12. glibc más alta 2.38 en los dos.

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
| `RELEASE_SIGNING_SECRET`, `FTP_HOST`, `FTP_USER`, `FTP_PASS` | Solo `publish.yml` en real (R6): firma del manifest y FTP. Además, `APPS_TOKEN` tiene que poder leer y escribir en `javierferraz/2606-thestromboliproject` (push del manifest) |

Añadir una app Tauri nueva = darle acceso a `APPS_TOKEN` sobre su repo y grabar su
`TAURI_KEY_<SLUG>` (`gh secret set TAURI_KEY_<SLUG> -R javierferraz/tsp-release-factory < <slug>-signing.key`).

Añadir una app PySide6 nueva = darle acceso a `APPS_TOKEN` sobre su repo (no tiene clave propia)
y que el repo cumpla lo que lista la cabecera de `build-pyside-windows.yml`; si sus tests no
corren en Linux, una línea con su slug y el motivo en `config/tests-en-windows.txt`.

Los dos workflows de Linux solo usan `APPS_TOKEN`: nada se firma (el manifest de Linux lleva
solo SHA256 y no hay `.sig` del actualizador).

## Seguridad

- `permissions: contents: read` (solo para leer esta fábrica); el repo de la app va con `APPS_TOKEN`.
- Solo `workflow_dispatch` y solo desde `main`: no corre en pull requests ni en ramas.
- Las entradas se validan y se usan por variable de entorno, nunca interpoladas en el script.
- Acciones fijadas por SHA; el cliente de Trusted Signing, por hash.
- La clave del actualizador solo existe en el paso que firma, en un fichero temporal del runner.
