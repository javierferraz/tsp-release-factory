# Smoke del instalador NSIS (Tauri, instalacion por usuario) en el runner, que es una
# maquina limpia y desechable: instala en silencio, comprueba registro, version, firma del
# .exe instalado y acceso del menu Inicio, arranca la app unos segundos y desinstala.
# Es la receta del smoke de Bright Cardinal del 2026-10-27 (CONTEXT.md de la app, fila F6).
# Cada comprobacion se anade como fila a $Report (Markdown); el script falla en la primera
# que no pasa, despues de intentar dejar la maquina desinstalada.
param(
    [Parameter(Mandatory)] [string] $Installer,
    [Parameter(Mandatory)] [string] $ProductName,
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $SignTool,
    [Parameter(Mandatory)] [string] $Report,
    [int] $AliveSeconds = 10
)
$ErrorActionPreference = 'Stop'

function Add-Row([string] $check, [string] $result) {
    Add-Content -Path $Report -Value "| $check | $result |" -Encoding utf8
    Write-Output "$check -> $result"
}
function Fail([string] $check, [string] $why) {
    Add-Row $check "FALLA: $why"
    throw "$check : $why"
}
function Get-UninstallEntry {
    Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ItemProperty $_.PSPath } |
        Where-Object { $_.DisplayName -eq $ProductName }
}
function Get-StartMenuLinks {
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    Get-ChildItem -Path $programs -Filter "$ProductName*.lnk" -Recurse -ErrorAction SilentlyContinue
}
function Test-Signature([string] $file) {
    & $SignTool verify /pa /q $file *> $null
    return ($LASTEXITCODE -eq 0)
}

if (Get-UninstallEntry) { Fail 'Maquina limpia' "$ProductName ya estaba instalado en el runner" }

# 1. Instalar en silencio
$p = Start-Process -FilePath $Installer -ArgumentList '/S' -Wait -PassThru
if ($p.ExitCode -ne 0) { Fail 'Instalar (/S)' "codigo de salida $($p.ExitCode)" }
Add-Row 'Instalar (/S)' 'codigo de salida 0'

$installDir = $null
$exe = $null
$proc = $null
$uninstallExit = $null
try {
    # 2. Registro HKCU y version
    $entry = Get-UninstallEntry
    if (-not $entry) { Fail 'Registro HKCU' "no hay entrada de desinstalacion con DisplayName '$ProductName'" }
    if ($entry.DisplayVersion -ne $Version) { Fail 'Version registrada' "'$($entry.DisplayVersion)', se esperaba '$Version'" }
    $uninstaller = ($entry.UninstallString -replace '"', '').Trim()
    $installDir = Split-Path -Parent $uninstaller
    Add-Row 'Registro HKCU' "entrada '$ProductName', DisplayVersion $($entry.DisplayVersion)"
    Add-Row 'Carpeta de instalacion' ($installDir -replace [regex]::Escape($env:LOCALAPPDATA), '%LOCALAPPDATA%')

    # 3. .exe instalado y firmado
    $exes = @(Get-ChildItem -Path $installDir -Filter '*.exe' | Where-Object { $_.Name -ne 'uninstall.exe' })
    if ($exes.Count -ne 1) { Fail '.exe instalado' "se esperaba 1 .exe ademas de uninstall.exe, hay $($exes.Count)" }
    $exe = $exes[0].FullName
    if (-not (Test-Signature $exe)) { Fail 'Firma del .exe instalado' "signtool verify /pa falla en $($exes[0].Name)" }
    Add-Row 'Firma del .exe instalado' "$($exes[0].Name): signtool verify /pa OK"
    if (-not (Test-Signature $uninstaller)) { Fail 'Firma de uninstall.exe' 'signtool verify /pa falla' }
    Add-Row 'Firma de uninstall.exe' 'signtool verify /pa OK'

    # 4. Acceso del menu Inicio apuntando al .exe instalado
    $links = @(Get-StartMenuLinks)
    if ($links.Count -eq 0) { Fail 'Acceso del menu Inicio' "no hay ningun '$ProductName*.lnk'" }
    $shell = New-Object -ComObject WScript.Shell
    $target = $shell.CreateShortcut($links[0].FullName).TargetPath
    if ($target -ne $exe) { Fail 'Acceso del menu Inicio' "'$($links[0].Name)' apunta a '$target'" }
    Add-Row 'Acceso del menu Inicio' "'$($links[0].BaseName)' -> $($exes[0].Name)"

    # 5. Arranque: el runner no tiene a nadie mirando la pantalla, asi que la prueba es que
    #    el proceso arranca, levanta WebView2 y sigue vivo $AliveSeconds segundos.
    $proc = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds $AliveSeconds
    $proc.Refresh()
    if ($proc.HasExited) { Fail 'Arranque' "el proceso salio a los $AliveSeconds s con codigo $($proc.ExitCode)" }
    $webviews = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" |
        Where-Object { $_.Name -eq 'msedgewebview2.exe' }).Count
    $title = if ($proc.MainWindowTitle) { "ventana '$($proc.MainWindowTitle)'" } else { 'sin titulo de ventana visible' }
    $mem = [math]::Round($proc.WorkingSet64 / 1MB)
    Add-Row 'Arranque' "vivo a los $AliveSeconds s, $mem MB, $webviews procesos WebView2 hijos, $title"
}
finally {
    if ($proc -and -not $proc.HasExited) { & taskkill.exe /PID $proc.Id /T /F *> $null }
    if ($exe) { Get-Process | Where-Object { $_.Path -eq $exe } | Stop-Process -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    # 6. Desinstalar en silencio (tambien si algo fallo antes, para no dejar restos).
    if ($installDir -and (Test-Path (Join-Path $installDir 'uninstall.exe'))) {
        $u = Start-Process -FilePath (Join-Path $installDir 'uninstall.exe') -ArgumentList '/S' -Wait -PassThru
        $uninstallExit = $u.ExitCode
    }
}

if ($uninstallExit -ne 0) { Fail 'Desinstalar (/S)' "codigo de salida $uninstallExit" }
# El desinstalador de NSIS se relanza desde %TEMP% y el original vuelve enseguida: esperar.
$deadline = (Get-Date).AddSeconds(60)
do {
    $left = @()
    if (Get-UninstallEntry) { $left += 'registro' }
    if (Test-Path $installDir) { $left += 'carpeta' }
    if (Get-StartMenuLinks) { $left += 'acceso del menu Inicio' }
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($left.Count -gt 0) { Fail 'Desinstalar (/S)' "a los 60 s quedan: $($left -join ', ')" }
Add-Row 'Desinstalar (/S)' 'codigo de salida 0; sin registro, carpeta ni acceso'
exit 0
