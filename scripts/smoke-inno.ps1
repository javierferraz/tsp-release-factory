# Smoke del instalador Inno Setup (app PySide6, instalacion por usuario) en el runner, que es
# una maquina limpia y desechable: instala en silencio, comprueba registro, version, carpeta,
# firmas del .exe instalado y del desinstalador y el acceso del menu Inicio, arranca la app,
# comprueba la instancia unica (un segundo arranque debe salir solo), mira el crash.log y
# desinstala. Es la version Inno de smoke-nsis.ps1.
# Cada comprobacion se anade como fila a $Report (Markdown); el script falla en la primera
# que no pasa, despues de intentar dejar la maquina desinstalada.
param(
    [Parameter(Mandatory)] [string] $Installer,
    [Parameter(Mandatory)] [string] $AppId,        # AppId del .iss ya sin escapar: {GUID} o texto
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $ExeName,      # p. ej. SnappyWasp.exe
    [Parameter(Mandatory)] [string] $SignTool,
    [Parameter(Mandatory)] [string] $Report,
    [int] $AliveSeconds = 15
)
$ErrorActionPreference = 'Stop'

$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($AppId)_is1"

function Add-Row([string] $check, [string] $result) {
    Add-Content -Path $Report -Value "| $check | $result |" -Encoding utf8
    Write-Output "$check -> $result"
}
function Fail([string] $check, [string] $why) {
    Add-Row $check "FALLA: $why"
    throw "$check : $why"
}
function Get-UninstallEntry { Get-ItemProperty -LiteralPath $uninstallKey -ErrorAction SilentlyContinue }
function Get-LinksTo([string] $target) {
    $shell = New-Object -ComObject WScript.Shell
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    Get-ChildItem -Path $programs -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $shell.CreateShortcut($_.FullName).TargetPath -eq $target }
}
function Test-Signature([string] $file) {
    & $SignTool verify /pa /q $file *> $null
    return ($LASTEXITCODE -eq 0)
}
function Get-RunEntries([string] $exePath) {
    $run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    if (-not $run) { return @() }
    @($run.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' -and "$($_.Value)" -like "*$exePath*" } |
        ForEach-Object { $_.Name })
}

if (Get-UninstallEntry) { Fail 'Maquina limpia' "ya habia una instalacion con AppId $AppId en el runner" }

# 1. Instalar en silencio. [Run] postinstall lleva skipifsilent: no arranca la app.
$log = Join-Path $env:RUNNER_TEMP 'inno-install.log'
$p = Start-Process -FilePath $Installer -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/LOG=`"$log`"" -PassThru
$null = $p.Handle   # sin esto, ExitCode sale vacio al no usar -Wait
# Mientras instala: el setup extrae y ejecuta un .tmp en %TEMP%\is-*.tmp\. Sin firma, Smart App
# Control lo bloquea con el error 4551 (S-573); SignTool=stromboli en el .iss lo evita.
$tmpSig = $null
while (-not $p.HasExited) {
    if (-not $tmpSig) {
        $tmp = Get-ChildItem -Path $env:TEMP -Filter 'is-*.tmp' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ChildItem -Path $_.FullName -Filter '*.tmp' -File -ErrorAction SilentlyContinue } |
            Select-Object -First 1
        if ($tmp) {
            try { $tmpSig = Get-AuthenticodeSignature -FilePath $tmp.FullName } catch { $tmpSig = $null }
        }
    }
    Start-Sleep -Milliseconds 100
}
$p.WaitForExit()
if ($p.ExitCode -ne 0) { Fail 'Instalar (/VERYSILENT)' "codigo de salida $($p.ExitCode); log en $log" }
Add-Row 'Instalar (/VERYSILENT)' 'codigo de salida 0'
if (-not $tmpSig) {
    Add-Row 'Firma del .tmp interno (aviso)' 'no se llego a ver el .tmp mientras instalaba; sin comprobar'
} elseif ($tmpSig.Status -ne 'Valid') {
    Fail 'Firma del .tmp interno' "$($tmpSig.Status): el setup ejecuta un .tmp sin firma valida (error 4551 con Smart App Control, S-573)"
} else {
    Add-Row 'Firma del .tmp interno' "valida; firmante $($tmpSig.SignerCertificate.Subject -replace '^CN=([^,]+).*', '$1')"
}

$installDir = $null
$exe = $null
$proc = $null
$crashLogs = @()
$runEntries = @()
$started = Get-Date
$uninstallExit = $null
try {
    # 2. Registro HKCU (instalacion por usuario, PrivilegesRequired=lowest), version y carpeta
    $entry = Get-UninstallEntry
    if (-not $entry) { Fail 'Registro HKCU' "no hay $($AppId)_is1 en HKCU (instalo por maquina o con otro AppId)" }
    if ($entry.DisplayVersion -ne $Version) { Fail 'Version registrada' "'$($entry.DisplayVersion)', se esperaba '$Version'" }
    $installDir = $entry.'Inno Setup: App Path'
    if (-not $installDir) { $installDir = "$($entry.InstallLocation)".TrimEnd('\') }
    Add-Row 'Registro HKCU' "'$($entry.DisplayName)', DisplayVersion $($entry.DisplayVersion)"
    if (-not $installDir.StartsWith($env:LOCALAPPDATA + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'Carpeta de instalacion' "'$installDir' no esta bajo %LOCALAPPDATA%"
    }
    Add-Row 'Carpeta de instalacion' ($installDir -replace [regex]::Escape($env:LOCALAPPDATA), '%LOCALAPPDATA%')

    # 3. .exe instalado y desinstalador, firmados
    $exe = Join-Path $installDir $ExeName
    if (-not (Test-Path $exe)) { Fail '.exe instalado' "no existe $ExeName en la carpeta" }
    if (-not (Test-Signature $exe)) { Fail 'Firma del .exe instalado' "signtool verify /pa falla en $ExeName" }
    Add-Row 'Firma del .exe instalado' "${ExeName}: signtool verify /pa OK"
    $uninstaller = ($entry.UninstallString -replace '"', '').Trim()
    if (-not (Test-Path $uninstaller)) { Fail 'Desinstalador' "no existe '$uninstaller'" }
    if (-not (Test-Signature $uninstaller)) { Fail 'Firma del desinstalador' "signtool verify /pa falla en $(Split-Path -Leaf $uninstaller)" }
    Add-Row 'Firma del desinstalador' "$(Split-Path -Leaf $uninstaller): signtool verify /pa OK"

    # 4. Acceso del menu Inicio «Animal - Funcion» apuntando al .exe instalado
    $links = @(Get-LinksTo $exe)
    if ($links.Count -eq 0) { Fail 'Acceso del menu Inicio' "ningun .lnk del menu Inicio apunta a $ExeName" }
    $named = @($links | Where-Object { $_.BaseName -like '* - *' })
    if ($named.Count -eq 0) { Fail 'Acceso del menu Inicio' "'$($links[0].BaseName)' no sigue la convencion '<Animal> - <Funcion>'" }
    Add-Row 'Acceso del menu Inicio' "'$($named[0].BaseName)' -> $ExeName"

    # 5. Arranque: el runner no tiene a nadie mirando la pantalla (ni altavoces); la prueba es
    #    que el proceso arranca y sigue vivo $AliveSeconds s sin escribir un crash.log.
    $proc = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds $AliveSeconds
    $proc.Refresh()
    if ($proc.HasExited) { Fail 'Arranque' "el proceso salio a los $AliveSeconds s con codigo $($proc.ExitCode)" }
    $mem = [math]::Round($proc.WorkingSet64 / 1MB)
    Add-Row 'Arranque' "vivo a los $AliveSeconds s, $mem MB"

    # 6. Instancia unica: un segundo arranque avisa al primero y sale sin crear otra UI.
    $second = Start-Process -FilePath $exe -PassThru
    if (-not $second.WaitForExit(30000)) {
        $second.Kill()
        Fail 'Instancia unica' 'el segundo arranque sigue vivo a los 30 s (habria dos copias)'
    }
    $proc.Refresh()
    if ($proc.HasExited) { Fail 'Instancia unica' "el primer proceso murio tras el segundo arranque (codigo $($proc.ExitCode))" }
    Add-Row 'Instancia unica' "el segundo arranque sale solo (codigo $($second.ExitCode)); el primero sigue vivo"

    # 7. crash.log: las apps PySide6 de la suite lo dejan en su carpeta de configuracion.
    $crashLogs = @(foreach ($root in $env:APPDATA, $env:LOCALAPPDATA) {
        Get-ChildItem -Path $root -Filter 'crash.log' -Recurse -Depth 2 -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -ge $started -and $_.Length -gt 0 }
    })
    if ($crashLogs.Count) {
        Get-Content $crashLogs[0].FullName -TotalCount 40 | Write-Output
        Fail 'crash.log' "$($crashLogs[0].FullName) tiene $($crashLogs[0].Length) bytes (arriba, sus primeras lineas)"
    }
    Add-Row 'crash.log' 'vacio o sin crear'
    $runEntries = @(Get-RunEntries $exe)
}
finally {
    if ($proc -and -not $proc.HasExited) { & taskkill.exe /PID $proc.Id /T /F *> $null }
    if ($exe) { Get-Process | Where-Object { $_.Path -eq $exe } | Stop-Process -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    # 8. Desinstalar en silencio (tambien si algo fallo antes, para no dejar restos).
    $entry = Get-UninstallEntry
    if ($entry) {
        $uninstaller = ($entry.UninstallString -replace '"', '').Trim()
        if (Test-Path $uninstaller) {
            $u = Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' -Wait -PassThru
            $uninstallExit = $u.ExitCode
        }
    }
}

if ($uninstallExit -ne 0) { Fail 'Desinstalar (/VERYSILENT)' "codigo de salida $uninstallExit" }
# El desinstalador de Inno se copia a %TEMP% y se relanza desde alli: esperar.
$deadline = (Get-Date).AddSeconds(60)
do {
    $left = @()
    if (Get-UninstallEntry) { $left += 'registro' }
    if (Test-Path (Join-Path $installDir $ExeName)) { $left += $ExeName }
    if (Get-LinksTo $exe) { $left += 'acceso del menu Inicio' }
    if ($left.Count -eq 0) { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($left.Count -gt 0) { Fail 'Desinstalar (/VERYSILENT)' "a los 60 s quedan: $($left -join ', ')" }
$extra = if (Test-Path $installDir) { "; queda la carpeta con $(@(Get-ChildItem $installDir -Recurse -Force).Count) elementos" } else { '' }
Add-Row 'Desinstalar (/VERYSILENT)' "codigo de salida 0; sin registro, .exe ni acceso$extra"

# Aviso, no fallo: la app escribe su autoarranque en HKCU\Run y el desinstalador no lo quita.
$leftRun = @(Get-RunEntries $exe)
if ($leftRun.Count) {
    Add-Row 'Autoarranque (aviso)' "tras desinstalar sigue en HKCU\Run: $($leftRun -join ', ') (apunta a un .exe que ya no existe)"
} elseif ($runEntries.Count) {
    Add-Row 'Autoarranque' "la app se apunto en HKCU\Run ($($runEntries -join ', ')) y al desinstalar desaparece"
}
exit 0
