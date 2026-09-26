<#
.SYNOPSIS
    Instala el Stripe CLI oficial para Windows en tools/ del proyecto.

.DESCRIPTION
    Resuelve el último release publicado por stripe/stripe-cli, descarga el zip de
    Windows x86_64 y lo descomprime en tools/.

    La descarga se verifica contra `stripe-windows-checksums.txt`, que el propio
    proyecto publica junto al binario. Sin esa comprobación, este script sería un
    "descarga y ejecuta lo que devuelva la red": el binario termina con permiso para
    firmar peticiones contra la API de Stripe con la clave de la maquina del
    desarrollador, y eso no se instala sin comprobar de donde sale.

    No se fija la version a proposito. Se resuelve por API en cada ejecucion, asi que
    el script no envejece ni hay que acordarse de actualizarlo.

.PARAMETER Force
    Vuelve a descargar aunque tools\stripe.exe ya exista.

.PARAMETER Version
    Fija una version concreta, por ejemplo "v1.52.0". Sin este parametro usa la ultima.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-stripe-cli.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install-stripe-cli.ps1 -Version v1.52.0
#>

[CmdletBinding()]
param(
    [switch] $Force,
    [string] $Version
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $RepositoryRoot 'tools'
$Executable = Join-Path $ToolsDir 'stripe.exe'
$ApiBase = 'https://api.github.com/repos/stripe/stripe-cli/releases'
$UserAgent = 'fenix-stripe-cli-installer'

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string] $Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Fail { param([string] $Message) Write-Host "    $Message" -ForegroundColor Red }

if ((Test-Path $Executable) -and -not $Force) {
    Write-Step 'Stripe CLI ya instalado'
    Write-Ok $Executable
    & $Executable --version
    Write-Host ''
    Write-Host '    Usa -Force para reinstalar.' -ForegroundColor DarkGray
    exit 0
}

# --- Resolucion de la version -------------------------------------------------
if (-not $Version) {
    Write-Step 'Resolviendo el ultimo release oficial'
    $release = Invoke-RestMethod -Uri "$ApiBase/latest" -Headers @{ 'User-Agent' = $UserAgent } -TimeoutSec 30
    $Version = $release.tag_name
    Write-Ok "stripe/stripe-cli $Version  ($($release.published_at))"
} else {
    Write-Step "Version fijada por parametro"
    Write-Ok $Version
}

# --- Descarga -----------------------------------------------------------------
$stagingDir = Join-Path ([IO.Path]::GetTempPath()) ("stripe-cli-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

try {
    $archiveName = 'stripe_{0}_windows_x86_64.zip' -f ($Version.TrimStart('v'))
    $zipPath = Join-Path $stagingDir $archiveName
    $checksumsPath = Join-Path $stagingDir 'stripe-windows-checksums.txt'
    $base = "https://github.com/stripe/stripe-cli/releases/download/$Version"

    Write-Step "Descargando $archiveName"
    Invoke-WebRequest -Uri "$base/$archiveName" -OutFile $zipPath -TimeoutSec 300 -UseBasicParsing
    $sizeMb = [Math]::Round((Get-Item $zipPath).Length / 1MB, 2)
    Write-Ok "$sizeMb MB"

    Write-Step 'Descargando el manifiesto de checksums'
    Invoke-WebRequest -Uri "$base/stripe-windows-checksums.txt" -OutFile $checksumsPath -TimeoutSec 60 -UseBasicParsing
    Write-Ok 'OK'

    # --- Verificacion de integridad (obligatoria) ------------------------------
    Write-Step 'Verificando SHA-256'
    $expected = $null
    foreach ($line in Get-Content $checksumsPath) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+(\S+)\s*$' -and $Matches[2] -eq $archiveName) {
            $expected = $Matches[1].ToLower()
            break
        }
    }

    if (-not $expected) {
        throw "El manifiesto no publica un checksum para '$archiveName'. No se instala sin verificar."
    }

    $actual = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        throw "SHA-256 no coincide. esperado $expected, obtenido $actual. No se descomprime nada."
    }
    Write-Ok "SHA-256 verificado: $($actual.Substring(0,16))..."

    # --- Extraccion ------------------------------------------------------------
    Write-Step "Extrayendo en tools/"
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    Expand-Archive -Path $zipPath -DestinationPath $ToolsDir -Force

    if (-not (Test-Path $Executable)) {
        throw "El zip se extrajo pero no aparece stripe.exe en $ToolsDir. Contenido: $((Get-ChildItem $ToolsDir | ForEach-Object Name) -join ', ')"
    }

    # --- Comprobacion final ---------------------------------------------------
    Write-Step 'Comprobando el binario'
    $versionOutput = & $Executable --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "stripe.exe --version devolvio codigo $LASTEXITCODE. La descarga no es utilizable."
    }
    Write-Ok ($versionOutput | Out-String).Trim()

    Write-Host ''
    Write-Ok 'Instalacion completa.'
    Write-Host ''
    Write-Host '    Siguiente paso:' -ForegroundColor Cyan
    Write-Host '      powershell -ExecutionPolicy Bypass -File .\stripe-listen.ps1'
    Write-Host ''
    Write-Host '    El servidor FastAPI debe estar en marcha en el puerto 8000,' -ForegroundColor DarkGray
    Write-Host '    y la clave de Stripe CLI se guarda en tu perfil, no en el repositorio.' -ForegroundColor DarkGray
}
finally {
    Remove-Item -Recurse -Force $stagingDir -ErrorAction SilentlyContinue
}
