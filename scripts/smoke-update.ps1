# Prueba de actualizacion en el runner (S-1275, PAA2 P6-1), para Inno (PySide6) y NSIS (Tauri):
# instala en silencio la version ANTERIOR publicada, la abre y la cierra, apunta los ajustes que
# ha guardado (ficheros nuevos en %APPDATA% / %LOCALAPPDATA% y claves nuevas en HKCU\Software),
# instala encima el instalador NUEVO, comprueba que los ajustes siguen ahi y sin tocar, abre y
# cierra la nueva, vuelve a comprobarlos y desinstala.
#
# Va ANTES de smoke-inno.ps1 / smoke-nsis.ps1 en el mismo job: al terminar (bien o mal) borra lo
# que ha creado desde su foto inicial (carpetas de datos, claves, accesos), para que el smoke de
# la instalacion limpia siga empezando en una maquina limpia.
#
# Cada paso se anade como fila a $Report (Markdown) y el resultado va a $Json:
#   {"resultado": "ok", "texto": "ok: v1.6.2 -> v1.7.0, ...", "anterior", "nueva", "pasos": [...],
#    "ajustes": {"ficheros": [...], "registro": [...]}}
# El script falla en el primer paso que no pasa.
param(
    [Parameter(Mandatory)] [ValidateSet('inno', 'nsis')] [string] $Kind,
    [Parameter(Mandatory)] [string] $Previous,         # instalador de la version anterior
    [Parameter(Mandatory)] [string] $PreviousVersion,
    [Parameter(Mandatory)] [string] $Installer,        # instalador nuevo
    [Parameter(Mandatory)] [string] $Version,
    [Parameter(Mandatory)] [string] $Report,
    [Parameter(Mandatory)] [string] $Json,
    [string] $AppId = '',          # inno: AppId del .iss sin escapar
    [string] $ExeName = '',        # inno: p. ej. SnappyWasp.exe
    [string] $ProductName = '',    # nsis: productName de tauri.conf.json
    [int] $AliveSeconds = 12
)
$ErrorActionPreference = 'Stop'
if ($Kind -eq 'inno' -and (-not $AppId -or -not $ExeName)) { throw 'inno necesita -AppId y -ExeName' }
if ($Kind -eq 'nsis' -and -not $ProductName) { throw 'nsis necesita -ProductName' }

$pasos = New-Object System.Collections.ArrayList
function Add-Row([string] $check, [string] $result) {
    Add-Content -Path $Report -Value "| Actualizacion: $check | $result |" -Encoding utf8
    [void] $pasos.Add([ordered]@{ paso = $check; resultado = $result })
    Write-Output "$check -> $result"
}
function Fail([string] $check, [string] $why) {
    Add-Row $check "FALLA: $why"
    throw "$check : $why"
}

# ── Instalacion: registro, instalar, desinstalar ──
function Get-UninstallEntries {
    if ($Kind -eq 'inno') {
        @(Get-ItemProperty -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($AppId)_is1" -ErrorAction SilentlyContinue)
    } else {
        @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DisplayName -eq $ProductName })
    }
}
function Install-Silent([string] $setup, [string] $label) {
    $args_ = if ($Kind -eq 'inno') {
        @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/LOG=`"$(Join-Path $env:RUNNER_TEMP "update-$label.log")`"")
    } else { @('/S') }
    $p = Start-Process -FilePath $setup -ArgumentList $args_ -Wait -PassThru
    return $p.ExitCode
}
function Get-InstalledExe($entry) {
    if ($Kind -eq 'inno') {
        $dir = $entry.'Inno Setup: App Path'
        if (-not $dir) { $dir = "$($entry.InstallLocation)".TrimEnd('\') }
        return @{ dir = $dir; exe = (Join-Path $dir $ExeName) }
    }
    $uninstaller = ($entry.UninstallString -replace '"', '').Trim()
    $dir = Split-Path -Parent $uninstaller
    $exes = @(Get-ChildItem -Path $dir -Filter '*.exe' | Where-Object { $_.Name -ne 'uninstall.exe' })
    if ($exes.Count -ne 1) { Fail '.exe instalado' "se esperaba 1 .exe ademas de uninstall.exe en '$dir', hay $($exes.Count)" }
    return @{ dir = $dir; exe = $exes[0].FullName }
}
function Assert-Installed([string] $want, [string] $label) {
    $entries = @(Get-UninstallEntries)
    if ($entries.Count -ne 1) { Fail "Registro tras $label" "hay $($entries.Count) entradas de desinstalacion (se esperaba 1)" }
    if ($entries[0].DisplayVersion -ne $want) { Fail "Registro tras $label" "DisplayVersion '$($entries[0].DisplayVersion)', se esperaba '$want'" }
    $i = Get-InstalledExe $entries[0]
    if (-not (Test-Path $i.exe)) { Fail "Registro tras $label" "no existe $($i.exe)" }
    return $i
}

# ── Abrir y cerrar como lo haria una persona ──
function Open-AndClose([string] $exe, [string] $label) {
    $proc = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds $AliveSeconds
    $proc.Refresh()
    if ($proc.HasExited) { Fail "Abrir $label" "el proceso salio a los $AliveSeconds s con codigo $($proc.ExitCode)" }
    Add-Row "Abrir $label" "vivo a los $AliveSeconds s, $([math]::Round($proc.WorkingSet64 / 1MB)) MB"
    # Primero como una persona (cerrar la ventana); si se queda en la bandeja, WM_CLOSE a todo el
    # arbol; solo al final, forzado. Se apunta como se cerro.
    $como = $null
    if ($proc.CloseMainWindow() -and $proc.WaitForExit(15000)) { $como = 'cerrando la ventana' }
    if (-not $como) {
        & taskkill.exe /PID $proc.Id /T *> $null
        if ($proc.WaitForExit(10000)) { $como = 'con WM_CLOSE (taskkill sin /F)' }
    }
    if (-not $como) {
        & taskkill.exe /PID $proc.Id /T /F *> $null
        [void] $proc.WaitForExit(10000)
        $como = 'forzado (taskkill /F): no cerro con la ventana ni con WM_CLOSE'
    }
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Add-Row "Cerrar $label" $como
}

# ── Foto de la maquina y ajustes ──
$dataRoots = @($env:APPDATA, $env:LOCALAPPDATA)
$noise = '\\(Temp|Microsoft|EBWebView|pip|npm-cache|NuGet|D3DSCache|CrashDumps|Packages)(\\|$)'
function Get-Dirs {
    $out = @{}
    foreach ($r in $dataRoots) {
        Get-ChildItem -LiteralPath $r -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
            $out[$_.FullName] = 1
            Get-ChildItem -LiteralPath $_.FullName -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object { $out[$_.FullName] = 1 }
        }
    }
    $out
}
function Get-Keys {
    $out = @{}
    Get-ChildItem 'HKCU:\Software' -ErrorAction SilentlyContinue | ForEach-Object {
        $out[$_.PSPath] = 1
        Get-ChildItem $_.PSPath -ErrorAction SilentlyContinue | ForEach-Object { $out[$_.PSPath] = 1 }
    }
    $out
}
function Get-Links {
    $programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $out = @{}
    Get-ChildItem -Path $programs -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $out[$_.FullName] = 1 }
    $out
}
$foto = @{ dirs = Get-Dirs; keys = Get-Keys; links = Get-Links }

function New-Since($now, $before) { @($now.Keys | Where-Object { -not $before.ContainsKey($_) } | Sort-Object) }
function Get-TopNew($paths) {
    # Quita las que cuelgan de otra nueva: borrar o recorrer la de arriba ya las incluye.
    $sorted = @($paths | Sort-Object Length)
    $top = New-Object System.Collections.ArrayList
    foreach ($p in $sorted) {
        if (-not ($top | Where-Object { $p.StartsWith("$_\", [StringComparison]::OrdinalIgnoreCase) })) { [void] $top.Add($p) }
    }
    @($top)
}

function Get-Settings([string] $installDir) {
    $files = @()
    foreach ($d in (Get-TopNew (New-Since (Get-Dirs) $foto.dirs))) {
        if ($d -match $noise) { continue }
        if ($installDir -and ($d -eq $installDir -or $d.StartsWith("$installDir\", [StringComparison]::OrdinalIgnoreCase) -or $installDir.StartsWith("$d\", [StringComparison]::OrdinalIgnoreCase))) { continue }
        $files += @(Get-ChildItem -LiteralPath $d -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch $noise } | ForEach-Object {
                [ordered]@{ ruta = $_.FullName; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower(); bytes = $_.Length }
            })
    }
    $reg = @()
    foreach ($k in (Get-TopNew (New-Since (Get-Keys) $foto.keys))) {
        if ($k -match '\\Microsoft\\|\\Classes\\') { continue }
        foreach ($key in @(Get-Item -LiteralPath $k -ErrorAction SilentlyContinue) + @(Get-ChildItem -LiteralPath $k -Recurse -ErrorAction SilentlyContinue)) {
            foreach ($name in $key.GetValueNames()) {
                $reg += [ordered]@{ clave = $key.Name; valor = $name; dato = "$($key.GetValue($name))" }
            }
        }
    }
    @{ ficheros = @($files); registro = @($reg) }
}
function Compare-Settings($before, [bool] $sameContent) {
    $lost = @(); $changed = 0
    foreach ($f in $before.ficheros) {
        if (-not (Test-Path -LiteralPath $f.ruta)) { $lost += $f.ruta; continue }
        if ((Get-FileHash -LiteralPath $f.ruta -Algorithm SHA256).Hash.ToLower() -ne $f.sha256) { $changed++ }
    }
    foreach ($r in $before.registro) {
        $k = Get-Item -LiteralPath "Registry::$($r.clave)" -ErrorAction SilentlyContinue
        if (-not $k -or -not ($k.GetValueNames() -contains $r.valor)) { $lost += "$($r.clave)\$($r.valor)"; continue }
        if ("$($k.GetValue($r.valor))" -ne $r.dato) { $changed++ }
    }
    if ($lost.Count) { return "se han perdido $($lost.Count): $(($lost | Select-Object -First 5) -join '; ')" }
    if ($sameContent -and $changed) { return "$changed ajuste(s) cambiados por el instalador sin abrir la app" }
    return $null
}
function Get-Short([string] $p) { $p -replace [regex]::Escape($env:LOCALAPPDATA), '%LOCALAPPDATA%' -replace [regex]::Escape($env:APPDATA), '%APPDATA%' }

# ── Limpieza final: deja la maquina como en la foto ──
function Restore-Machine {
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $script:installDir -and $_.Path.StartsWith("$script:installDir\", [StringComparison]::OrdinalIgnoreCase) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    foreach ($e in @(Get-UninstallEntries)) {
        $u = ($e.UninstallString -replace '"', '').Trim()
        if (Test-Path $u) {
            $a = if ($Kind -eq 'inno') { @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') } else { @('/S') }
            Start-Process -FilePath $u -ArgumentList $a -Wait | Out-Null
        }
    }
    $deadline = (Get-Date).AddSeconds(60)
    while (@(Get-UninstallEntries).Count -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    foreach ($d in (Get-TopNew (New-Since (Get-Dirs) $foto.dirs))) { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($k in (Get-TopNew (New-Since (Get-Keys) $foto.keys))) { Remove-Item -LiteralPath $k -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($l in (New-Since (Get-Links) $foto.links)) { Remove-Item -LiteralPath $l -Force -ErrorAction SilentlyContinue }
}

if (@(Get-UninstallEntries).Count) { Fail 'Maquina limpia' 'la app ya estaba instalada en el runner' }
$script:installDir = $null

$result = $null
try {
    # 1. Version anterior: instalar, abrir, cerrar
    $code = Install-Silent $Previous 'anterior'
    if ($code -ne 0) { Fail "Instalar v$PreviousVersion" "codigo de salida $code" }
    $old = Assert-Installed $PreviousVersion 'instalar la anterior'
    $script:installDir = $old.dir
    Add-Row "Instalar v$PreviousVersion" "codigo 0; registrada como $PreviousVersion"
    Open-AndClose $old.exe "v$PreviousVersion"

    # 2. Ajustes que ha guardado la anterior
    $ajustes = Get-Settings $old.dir
    $nf, $nr = $ajustes.ficheros.Count, $ajustes.registro.Count
    $where = @($ajustes.ficheros | ForEach-Object { Split-Path -Parent (Get-Short $_.ruta) } | Select-Object -Unique) +
             @($ajustes.registro | ForEach-Object { $_.clave } | Select-Object -Unique)
    Add-Row 'Ajustes de la anterior' $(if ($nf + $nr) { "$nf fichero(s) y $nr valor(es) de registro: $(($where | Select-Object -First 4) -join '; ')" } else { 'no guardo ninguno en su primer arranque' })

    # 3. Actualizar con el instalador nuevo, encima
    $code = Install-Silent $Installer 'nueva'
    if ($code -ne 0) { Fail "Actualizar a v$Version" "codigo de salida $code" }
    $new = Assert-Installed $Version 'actualizar'
    $script:installDir = $new.dir
    Add-Row "Actualizar a v$Version" "codigo 0; una sola entrada, registrada como $Version"
    $why = Compare-Settings $ajustes $true
    if ($why) { Fail 'Ajustes tras actualizar' $why }
    Add-Row 'Ajustes tras actualizar' $(if ($nf + $nr) { 'todos siguen ahi, sin cambios' } else { 'nada que comparar' })

    # 4. Abrir y cerrar la nueva; los ajustes siguen (la app puede reescribirlos al migrar)
    Open-AndClose $new.exe "v$Version"
    $why = Compare-Settings $ajustes $false
    if ($why) { Fail 'Ajustes tras abrir la nueva' $why }
    Add-Row 'Ajustes tras abrir la nueva' $(if ($nf + $nr) { 'siguen todos' } else { 'nada que comparar' })

    # 5. Desinstalar
    $entry = @(Get-UninstallEntries)[0]
    $u = ($entry.UninstallString -replace '"', '').Trim()
    $a = if ($Kind -eq 'inno') { @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') } else { @('/S') }
    $p = Start-Process -FilePath $u -ArgumentList $a -Wait -PassThru
    if ($p.ExitCode -ne 0) { Fail 'Desinstalar' "codigo de salida $($p.ExitCode)" }
    $deadline = (Get-Date).AddSeconds(60)
    while (((@(Get-UninstallEntries).Count) -or (Test-Path $new.exe)) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if (@(Get-UninstallEntries).Count -or (Test-Path $new.exe)) { Fail 'Desinstalar' 'a los 60 s siguen el registro o el .exe' }
    Add-Row 'Desinstalar' 'codigo 0; sin registro ni .exe'


    $detalle = if ($nf + $nr) { "$nf fichero(s) y $nr valor(es) de registro conservados" } else { 'la anterior no guardo ajustes en su primer arranque: no habia nada que comparar' }
    $result = [ordered]@{
        resultado = 'ok'
        texto = "ok: v$PreviousVersion -> v$Version en el runner ($Kind); $detalle; desinstala limpio"
        anterior = $PreviousVersion; nueva = $Version; instalador = $Kind
        pasos = @($pasos)
        ajustes = [ordered]@{ ficheros = @($ajustes.ficheros | ForEach-Object { [ordered]@{ ruta = Get-Short $_.ruta; bytes = $_.bytes } }); registro = @($ajustes.registro | ForEach-Object { [ordered]@{ clave = $_.clave; valor = $_.valor } }) }
    }
}
finally {
    Restore-Machine
}
if (@(Get-UninstallEntries).Count) { Fail 'Limpieza' 'la app sigue instalada tras la limpieza' }
[IO.File]::WriteAllText($Json, ($result | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
Add-Row 'Limpieza' 'maquina como al empezar (datos, claves y accesos nuevos borrados) para el smoke limpio'
exit 0
