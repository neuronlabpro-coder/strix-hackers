# Stripe CLI en desarrollo

Stripe CLI es la herramienta que reenvía los webhooks de Stripe a la máquina local.
Sin ella, el flujo de compra de créditos del Bloque 5.1 no se puede probar contra la
red real: el backend y el panel están enteros, pero ningún evento llega nunca.

## Instalación

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-stripe-cli.ps1
```

El instalador no fija la versión: la resuelve por la API de GitHub en cada ejecución,
así que no envejece. Descarga el zip de Windows x86_64 y **verifica su SHA-256 contra
`stripe-windows-checksums.txt`**, que publica el propio proyecto Stripe junto al
binario. Si el hash no coincide no descomprime nada.

Esa verificación no es burocracia. El binario terminado tiene permiso para firmar
peticiones contra la API de Stripe con las claves de la máquina del desarrollador; sin
comprobar el origen, el script sería «descarga y ejecuta lo que devuelva la red».

Reinstalar con una versión concreta o forzada:

```powershell
.\scripts\install-stripe-cli.ps1 -Version v1.52.0
.\scripts\install-stripe-cli.ps1 -Force
```

El binario vive en `tools/` y está en `.gitignore`: son ~11 MB que el script
reconstruye en un minuto, y un binario en el historial no se puede enmendar.

## Autenticación

El script `stripe-listen.ps1` **no maneja claves**. La credencial se resuelve por
fuera, en el perfil del usuario, y así ningún fichero del repositorio la toca.

Las dos vías:

```powershell
# Interactiva: abre el navegador una vez y guarda la credencial en el perfil.
.\tools\stripe.exe login

# Sin navegador, pasando la clave por parámetro (no se escribe en disco).
powershell -ExecutionPolicy Bypass -File .\stripe-listen.ps1 -ApiKey sk_test_...
```

## Escuchar eventos

```powershell
powershell -ExecutionPolicy Bypass -File .\stripe-listen.ps1
```

Reenvía por defecto a `localhost:8000/api/v1/billing/webhooks` y se suscribe a
`checkout.session.completed,invoice.paid`, que son los dos eventos que la Fase 5
necesita: el crédito de la recarga puntual y el cobro de la suscripción recurrente.

```powershell
# Other events, or a different destination.
powershell -File .\stripe-listen.ps1 -Events payment_intent.succeeded
powershell -File .\stripe-listen.ps1 -ForwardTo localhost:8000/api/v1/billing/webhooks
```

`stripe listen` exige `--events` de forma explícita: sin él se niega a arrancar,
porque reenviar el histórico completo de una cuenta de pruebas genera un volumen que
nadie quiere recibir.

### No hay opción de puerto local

`stripe listen` **no expone ningún flag de puerto**. El puerto de escucha lo asigna el
servidor de Stripe y el CLI lo anuncia al arrancar. Por eso el script no expone un
`-Port`: traducirlo a un `--port` inexistente daría un error de flag en lugar de un
diagnóstico de «puerto ocupado», que es un mensaje mucho peor.

## Comprobaciones previas del script

Antes de escuchar, `stripe-listen.ps1` verifica dos cosas. Las dos están escritas
porque las dos versiones ingenuas mentían:

**Que el destino responde de verdad.** No comprueba que el puerto esté abierto, sino
que el endpoint conteste. Hace un `POST` sin firma y espera un `400`. Ese `400` prueba
que hay una ruta viva, montada y que rechaza lo que no viene firmado. Un chequeo de
«puerto abierto» da falso positivo con un servidor wedged —un proceso que escucha y
no acepta— y ese proceso se traga todos los webhooks en silencio: Stripe los marca
como entregados y nunca aparece una fila en `stripe_events`.

**Que hay credencial.** No se fía del código de salida de `stripe config --list`:
devuelve `0` aunque solo tenga `machine_uuid` y `project-name`, es decir sin ninguna
clave. El script busca una credencial real y, si no la encuentra, dice exactamente
qué hacer y sale con código `2`.

## Probar la entrega completa

Con Stripe CLI escuchando, `stripe trigger` reenvía un evento de prueba sin pasar por
el panel de Stripe:

```powershell
.\tools\stripe.exe trigger checkout.session.completed
```

El endpoint devuelve `400` a un payload sin firmar y procesa el evento firmado. Para
ver el resultado en la base:

```sql
SELECT event_id, event_type, processed_at FROM stripe_events ORDER BY received_at DESC LIMIT 5;
```

## Estado actual

**No hay claves de Stripe en este entorno.** El instalador y el script funcionan; lo
que no se ha ejecutado nunca es una llamada contra la API real de Stripe.

Todo el camino del Bloque 5.1 está probado con dobles y con el verificador de firma
real del SDK de `stripe` —firma válida, cuerpo manipulado y secreto ajeno—, pero la
creación de una sesión de Checkout real, la acreditación de créditos y el evento
`invoice.paid` de la suscripción siguen sin ejercitarse.

Este es el hueco que queda abierto en el DoD de la Fase 5, y cerrarlo requiere claves
de prueba. Son gratuitas: se crean en el panel de Stripe en modo test.
