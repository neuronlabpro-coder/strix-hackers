<#
.SYNOPSIS
    Escucha webhooks de Stripe y los reenvia al backend local.

.DESCRIPTION
    Ejecuta `tools\stripe.exe listen` apuntando al endpoint de webhooks del backend.

    La sesion de Stripe CLI se autentica una vez con `stripe login` o
    `stripe listen --api-key ...` y guarda la credencial en el perfil del usuario, de
    forma que este script nunca toca una clave. En un entorno de desarrollo sin claves
    de Stripe es normal que `listen` falle al autenticar: eso es Informativo y este
    script lo dice antes de devolver el codigo de error, en lugar de fallar en silencio.

.PARAMETER ForwardTo
    URL de destino. Por defecto el endpoint de webhooks del backend local.

.PARAMETER ApiKey
    Clave de API para entorno de pruebas. Se pasa por parametro, nunca se escribe en
    disco, para que no acabe en el historial de PowerShell mas de lo inevitable ni en
    ningun fichero del repositorio.

.PARAMETER Events
    Eventos a reenviar, separados por coma. Stripe CLI los exige de forma explicita:
    sin esta opcion se niega a arrancar, porque reenviar el historico completo de una
    cuenta de pruebas genera un volumen que nadie quiere recibir. Por defecto, los dos
    que la Fase 5 necesita: el credito de la recarga unica y el cobro de la
    suscripcion recurrente.

.PARAMETER SkipLoginCheck
    Omite la comprobacion previa de sesion. Útil en CI, donde la credencial llega por
    variable de entorno y no por login interactivo.

.NOTES
    No hay parametro de puerto local, y es a proposito: `stripe listen` no expone
    ningun flag de puerto. El puerto de escucha lo asigna el servidor de Stripe y el
    CLI lo anuncia al arrancar. Anadir un `-Port` que se tradujera a un `--port`
    inexistente daria un error de flag en vez de un fallo de puerto ocupado, que es
    un diagnostico peor.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\stripe-listen.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\stripe-listen.ps1 -ApiKey sk_test_...

.EXAMPLE
    powershell -File .\stripe-listen.ps1 -Events checkout.session.completed,payment_intent.succeeded
#>

[CmdletBinding()]
param(
    [string] $ForwardTo = 'localhost:8000/api/v1/billing/webhooks',
    [string] $ApiKey,
    [string] $Events = 'checkout.session.completed,invoice.paid',
    [switch] $SkipLoginCheck
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StripeExe = Join-Path $RepositoryRoot 'tools\stripe.exe'
$WebhookPath = '/api/v1/billing/webhooks'

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string] $Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string] $Message) Write-Host "    $Message" -ForegroundColor Yellow }

if (-not (Test-Path $StripeExe)) {
    Write-Host "No se encuentra Stripe CLI en $StripeExe" -ForegroundColor Red
    Write-Host ''
    Write-Host '    Instalalo con:' -ForegroundColor Cyan
    Write-Host '      powershell -ExecutionPolicy Bypass -File .\scripts\install-stripe-cli.ps1'
    exit 1
}

Write-Ok (& $StripeExe --version)

# --- Comprobacion de que el destino responde de verdad ------------------------
# No se comprueba que el puerto este abierto, sino que el endpoint conteste. Un
# proceso que escucha y no acepta (un servidor wedged, un proceso zombi que mantiene
# el socket) pasa la prueba de TCP y falla todos los webhooks en silencio: Stripe los
# marca como entregados y nunca aparece una fila en `stripe_events`.
#
# La peticion es un POST sin firma, y la respuesta esperada es `400`. Ese 400 es la
# prueba de que hay una ruta viva, que esta montada y que rechaza lo que no viene
# firmado. Un `401` o un `405` tambien indican que hay algo escuchando, pero
# significarian que el endpoint no es el que se espera, y eso tambien hay que avisar.
$forwardUri = if ($ForwardTo -match '^https?://') { $ForwardTo } else { "http://$ForwardTo" }

Write-Step "Comprobando el destino $forwardUri"
$health = 'desconocido'
try {
    $request = [Net.HttpWebRequest]::Create($forwardUri)
    $request.Method = 'POST'
    $request.ContentType = 'application/json'
    $request.Timeout = 4000
    $request.ReadWriteTimeout = 4000
    $payload = [Text.Encoding]::UTF8.GetBytes('{}')
    $request.ContentLength = $payload.Length
    $stream = $request.GetRequestStream()
    $stream.Write($payload, 0, $payload.Length)
    $stream.Close()
    $response = $request.GetResponse()
    $health = "vivo (HTTP $([int]$response.StatusCode))"
    $response.Close()
} catch [Net.WebException] {
    $webResponse = $_.Exception.Response
    if ($webResponse) {
        $code = [int]$webResponse.StatusCode
        $health = switch ($code) {
            400 { "vivo (HTTP 400, rechaza la peticion sin firma: correcto)" }
            405 { "responde pero el endpoint no acepta POST (HTTP 405)" }
            401 { "responde pero exige autenticacion (HTTP 401)" }
            default { "responde HTTP $code" }
        }
        $webResponse.Close()
    } else {
        $health = "sin respuesta: $($_.Exception.Message)"
    }
} catch {
    $health = "sin respuesta: $($_.Exception.Message)"
}

if ($health -like 'vivo*') {
    Write-Ok $health
} else {
    Write-Warn $health
    Write-Host ''
    Write-Host '    El backend tiene que estar en marcha y sano:' -ForegroundColor Cyan
    Write-Host '      uv run --project backend uvicorn backend.main:app --port 8000 --reload'
    Write-Host ''
    Write-Host '    Se continua de todos modos, pero cada evento se perdera: Stripe los da' -ForegroundColor DarkGray
    Write-Host '    por entregados y no llegara nada a `stripe_events`. Arranca el backend y' -ForegroundColor DarkGray
    Write-Host '    vuelve a ejecutar este script.' -ForegroundColor DarkGray
    Write-Host ''
}

# --- Comprobacion de credencial ------------------------------------------------
# No se usa el codigo de salida de `stripe config --list` como senal: devuelve 0
# aunque solo tenga `machine_uuid` y `project-name`, es decir sin ninguna credencial.
# Comprobando el codigo de salida, este script anunciaba "sesion activa" y el CLI
# fallaba un segundo despues con "You have not configured API keys yet".
#
# La senal fiable es que exista una credencial: la clave de pruebas por parametro o
# por entorno, o la que escribe `stripe login` en el fichero de configuracion del
# usuario.
if (-not $ApiKey -and -not $SkipLoginCheck) {
    Write-Step 'Comprobando la credencial de Stripe'

    $configOutput = (& $StripeExe config --list 2>&1) | Out-String
    $hasConfigKey = $configOutput -match '(?m)^\s*api_key\s*=\s*\S'
    $hasEnvKey = [bool] $env:STRIPE_API_KEY

    if ($hasEnvKey -or $hasConfigKey) {
        if ($hasEnvKey) { Write-Ok 'STRIPE_API_KEY definida en el entorno' }
        else { Write-Ok 'Credencial guardada por `stripe login`' }
    } else {
        Write-Warn 'No hay ninguna credencial de Stripe. `listen` no podra autenticarse.'
        Write-Host ''
        Write-Host '    Elige una de estas dos:' -ForegroundColor Cyan
        Write-Host '      1) Autenticacion interactiva, abre el navegador una vez:'
        Write-Host '           & .\tools\stripe.exe login'
        Write-Host '      2) Clave de pruebas, sin navegador ni escritura en disco:'
        Write-Host '           powershell -File .\stripe-listen.ps1 -ApiKey sk_test_...'
        Write-Host ''
        Write-Host '    Sin claves no hay nada que escuchar. Todo el camino de Stripe esta' -ForegroundColor DarkGray
        Write-Host '    probado con dobles y con el verificador de firma real del SDK,' -ForegroundColor DarkGray
        Write-Host '    pero ninguna llamada ha salido a la red de Stripe.' -ForegroundColor DarkGray
        Write-Host ''
        exit 2
    }
}

# --- Ejecucion ----------------------------------------------------------------
Write-Step "Escuchando y reenviando a $ForwardTo"
Write-Ok "Eventos: $Events"
Write-Host '    Ctrl+C para detener.' -ForegroundColor DarkGray
Write-Host ''

$arguments = @('listen', '--forward-to', $ForwardTo, '--events', $Events)
if ($ApiKey) { $arguments += @('--api-key', $ApiKey) }

& $StripeExe @arguments
$code = $LASTEXITCODE

Write-Host ''
if ($code -eq 0) {
    Write-Ok 'Stripe CLI se detuvo correctamente.'
} else {
    Write-Warn "Stripe CLI termino con codigo $code."
    Write-Host ''
    Write-Host '    Causas habituales, en este orden:' -ForegroundColor Cyan
    Write-Host '      1) Sesion no autenticada   -> & .\tools\stripe.exe login'
    Write-Host '      2) Sin salida a internet   -> el CLI necesita hablar con Stripe'
    Write-Host '      3) Backend caido           -> el destino de arriba debe responder'
    Write-Host ''
    Write-Host '    Secretos de pruebas para CI:' -ForegroundColor DarkGray
    Write-Host '      powershell -File .\stripe-listen.ps1 -ApiKey sk_test_...' -ForegroundColor DarkGray
}
exit $code
