# Evidencia TDD — Fase 5 — Bloque 5.1

## Alcance

Orquestador dinámico de LLMs con margen de rentabilidad, ledger de créditos inmutable con saldo
atómico, rechazo de escaneos sin saldo, scaffolding de Stripe (checkout y webhook) y consola de
SuperAdmin para la gestión de modelos.

## RED

Tres archivos de pruebas escritos antes de la implementación. Los tres fallaron en la recolección:

```
backend\tests\test_credit_ledger.py: ModuleNotFoundError: No module named 'backend.apps.billing'
backend\tests\test_llm_router.py:    ModuleNotFoundError: No module named 'backend.apps.audit'
backend\tests\test_billing_stripe.py: ModuleNotFoundError: No module named 'backend.apps.billing'
```

## GREEN

`39 passed` en los tres archivos nuevos; `257 passed, 2 skipped` en la suite completa.

| Módulo | Entregable | Pruebas |
| --- | --- | --- |
| `backend/apps/llm_router/` | Catálogo, cadena de resolución, clasificación de fallos y calculadora de margen | 17 |
| `backend/apps/billing/` | Ledger append-only, saldo atómico, `402` en pentests | 9 |
| `backend/apps/billing/stripe.py` + `router.py` | Checkout, webhook firmado, idempotencia | 13 |

## Decisiones que conviene revisar

### El margen es un recargo, no un margen sobre precio

La definición habitual de margen comercial es `(precio - coste) / precio`. Con esa definición, un
margen del 150 % es imposible: el precio tendría que ser negativo. La especificación del bloque
aclaraba en el paréntesis "150.00 para 150 % de recargo sobre coste base", así que la implementación
usa **recargo sobre coste**: `precio = coste × (1 + margen/100)`. Con 150 %, el cliente paga 2,5
veces el coste y el beneficio es 1,5 veces el coste.

El nombre del campo se mantiene por compatibilidad con la especificación, pero `ChargeBreakdown`
expone `markup_multiplier` y su docstring declara la semántica para que nadie lo lea al revés.
`test_compute_charge_applies_markup_over_base_cost` fija el comportamiento.

### El saldo pasó de `float` a `numeric`

`organizations.credit_balance` era `Float`. Un ledger de créditos con coma flotante deriva de centavo
y acaba sin cuadrar sin ninguna forma de explicar la diferencia. La migración
`a2b3c4d5e6f7` convierte la columna a `Numeric(18, 4)` y todas las monedas del ledger usan la misma
escala. Es un cambio de tipo sobre una columna existente, y por eso lo hago ahora y no al final de
la Fase 5: cambiarlo con datos reales sería más caro.

### Los créditos se descuentan antes de encolar

El orden importa. Si el descuento fuera posterior al encolado, un fallo de la cola de Celery
dejaría un escaneo registrado que nadie ha pagado y que nadie va a ejecutar. El flujo real es:
`flush` (asigna el `run.id`) → `apply_credit_delta` con el `run_id` como referencia → `commit` →
despachar. Si el despacho falla, `_refund_failed_dispatch` escribe un asiento compensatorio con
referencia `{run_id}:refund`, y el saldo vuelve exactamente a su valor anterior.
`test_failed_dispatch_refunds_the_reserved_credits` lo verifica asiento por asiento.

### `profit_margin_pct` no es el margen real

El catálogo declara un margen configurado y `llm_usage_events` registra el consumo real. Son cosas
distintas: el margen declarado es una intención comercial y el efectivo depende de los tokens que el
motor queme de verdad. Por eso la consola muestra ambos, y por eso existe la tabla: sin ella, el
margen del catálogo sería una promesa sin respaldo.

### Los precios de los paquetes viven en el backend

`CREDIT_PACKS` está en `backend/apps/billing/schemas.py`, no en el frontend. R1 prohíbe precios en
el código, y un importe de recarga que el panel pudiera alterar sería un importe que el backend no
conoce. El panel no puede inventar un paquete: solo puede pedir uno del catálogo.

## R4: el ledger es append-only

`test_credit_ledger_is_append_only` bloquea `UPDATE`, `DELETE` y `TRUNCATE` con un trigger
`FOR EACH STATEMENT`, y comprueba que la fila sobrevive. Es la misma garantía que el R4 aplica a las
evidencias de los hallazgos, extendida al registro que los documenta: un historial que se puede
reescribir no vale como libro contable.

Cada sentencia va en su propio savepoint. Sin ellos, la primera operación rechazada aborta la
transacción y las siguientes solo devolverían `current transaction is aborted`.

## Correcciones durante la implementación

1. **`op.execute` no admite parámetros.** El siembra de `knowledge_entries` (Bloque 4.3) ya lo había
   demostrado; el siembra de `llm_model_configs` se hizo directamente con `op.get_bind()` y
   SQLAlchemy `text()`.
2. **Deriva de `alembic check`.** Los modelos declaraban `default=` sin `server_default`, y la
   migración sí fijaba el valor por defecto. `alembic check` propuso borrar ambas tablas. Se
   añadió `server_default` a `use_case`, `prompt_tokens` y `completion_tokens`.
3. **`Organization.credit_balance` como `Float`.** La migración lo convirtió a `Numeric` pero el
   modelo seguía declarando `Float`, así que `alembic check` pedía revertirlo. Se actualizó el
   modelo, `OrganizationResponse` y las tres pruebas que creaban organizaciones con saldo.
4. **`CheckoutRateLimit` como alias de tipo.** `Annotated[None, Depends(...)]` en la lista
   `dependencies=[...]` provocaba
   `AttributeError: type object 'NoneType' has no attribute 'dependency'`. El router de
   repositorios ya usaba `Depends(...)` directo; se siguió ese patrón.
5. **El parche del cliente de Stripe apuntaba al módulo equivocado.** El router hace
   `from backend.apps.billing.stripe import get_stripe_client`, así que parchear el módulo de origen
   no sustituía la referencia ya importada. Las pruebas parchean `backend.apps.billing.router`.
6. **`Settings` es `frozen=True`.** `patch.object(settings, "stripe_webhook_secret", ...)` falla con
   `Instance is frozen`. La solución no fue relajar la inmutabilidad de la configuración, sino pasar
   el secreto al constructor de `StripeSDKClient` —lo cual además es mejor diseño, porque el secreto
   deja de releerse del estado global en cada llamada.
7. **La API de webhooks del SDK cambió.** En `stripe` 15.x, `StripeClient` ya no expone
   `webhooks`, y `WebhookSignature.verify_header` solo *verifica* (devuelve un booleano) en lugar de
   devolver el evento. La implementación verifica y luego parsea el mismo `bytes` con `json`, lo que
   además garantiza que el objeto verificado y el objeto procesado son el mismo.
8. **Un bug real en la ruta de error del encolado.** Tras `session.rollback()`, la instancia `run`
   queda expirada; mutarla dispara una recarga implícita que en SQLAlchemy asíncrono lanza
   `MissingGreenlet` *dentro del manejador del error*. Además `tenant.organization.id` sufría lo
   mismo. Se copiaron los identificadores a `UUID` planos antes de cualquier rollback y el run se
   vuelve a leer con una consulta explícita. Este bug no lo detectó ningún test previo porque la ruta
   no existía.
9. **`next_in_chain` comparaba por `id`.** Dos modelos aún sin persistir tienen ambos `id = None`, y
   la comparación `None == None` hacía que el método devolviera siempre el segundo elemento. Ahora
   compara por `model_id`, que es la identidad canónica de OpenRouter y existe antes del `flush`.
10. **`LLMModelResponse` exigía `usage` en la validación.** `model_validate(model)` fallaba porque
    `usage` es un agregado que no vive en el modelo. Se añadió un constructor explícito.

## Verificación de la firma: con el SDK real, no con un doble

Las pruebas del endpoint usan un cliente doble, lo cual está bien para la idempotencia y el reparto de
créditos. Para la **firma** no sirve: un doble que devuelve `True` demuestra que el router llama al
verificador, no que el verificador rechaza un payload manipulado. Hay tres pruebas que usan
`StripeSDKClient` real con un secreto de pruebas y HMAC calculado a mano:

- payload correctamente firmado → aceptado;
- firma válida con el cuerpo manipulado → `SignatureVerificationError`;
- payload firmado con el secreto de otro → `SignatureVerificationError`.

## Ajustes en pruebas existentes

La introducción del cobro cambió el comportamiento de creación de pentests, así que tres pruebas
previas fallaron con `402`. Sus organizaciones se crean ahora con saldo mediante el ledger, no
escribiendo `credit_balance` a mano: un saldo escrito directamente quedaría sin asiento que lo
respalde y `credit_balance_of` —que suma el ledger— no coincidiría con la columna. En producción el
saldo de partida es siempre un bono de alta.

Las pruebas de configuración de producción se reestructuraron con un helper `production_values()`
porque el nuevo bloque de Stripe se valida antes que el de SMTP y hacía que cada prueba de producción
tuviera que repetir las seis líneas de SMTP.

## Métricas de consumo

`llm_usage_events` registra tokens y coste por llamada. Nadie la escribe todavía: el worker de Strix
sigue inyectando `LLM_API_KEY` y `DEFAULT_STRIX_LLM` directamente en el contenedor, sin pasar por el
motor. Cablear el router al runner es trabajo de un bloque posterior, y hasta entonces la consola
muestra la métrica a cero, que es la respuesta honesta.

## Verificación

- Backend: `257 passed, 2 skipped`; `ruff check` sin hallazgos; `pyright --project
  backend/pyproject.toml` con 0 errores; `alembic upgrade head` aplicó `a2b3c4d5e6f7` y `alembic
  check` no detecta drift.
- Frontend: `typecheck` y `lint` sin errores ni advertencias; `build` con chunk principal de 473 kB
  (138 kB gzip) y ECharts de 453 kB (153 kB gzip).
- i18n: 426 claves usadas en catorce namespaces en paridad es/en; auditoría de literales visibles
  en JSX limpia.
- Backend en ejecución: las cinco rutas nuevas registradas; `POST /api/v1/billing/checkout-session`
  responde `401` sin token; `POST /api/v1/billing/webhooks` responde `400` a un payload sin firma,
  que es la respuesta correcta; `GET /api/v1/admin/llm/` responde `401` sin token y `403` para un
  administrador no superusuario.
- `engines/`, `saas-boilerplate/` y `strix/` sin modificaciones. Ningún `.env` nuevo en el árbol.

## Límites y riesgos

- **Sin credenciales de Stripe en el entorno.** Todo el camino de cobro está probado con dobles y con
  el verificador de firma real, pero la creación de una sesión de Checkout contra la API de Stripe
  no se ha ejecutado nunca. Es el primer paso al tener claves de test.
- **El worker de Strix no consulta el catálogo.** Como se explica arriba, el orquestador resuelve y
  clasifica correctamente pero todavía no es quien elige el modelo del contenedor. El Bloque 5.1
  construye la capacidad; falta conectarla al runner.
- **La tasa de conversión de USD a créditos es una decisión comercial abierta.**
  `CREDITS_PER_USD = 1` implica que un crédito equivale a un dólar de coste con margen. Con el
  catálogo sembrado, un pentest profundo de 500 k tokens de entrada y 200 k de salida en Sonnet
  costaría al cliente unos 2,2 USD, muy por debajo de los 10 créditos cobrados por escaneo. Esa
  discrepancia es intencional si el escaneo cubre más que una llamada, pero conviene revisarla
  cuando sepamos cuántas llamadas hace un pentest real.
- **El reembolso de un escaneo fallido solo cubre el fallo de encolado.** Si el contenedor arranca y
  el escaneo falla después, el crédito se consume. Es defendible (se consumieron tokens) pero no está
  instrumentado: no hay forma de saber hoy cuánto cuesta un pentest fallido.
- **`GET /api/v1/billing/credits/ledger` no está paginado más allá de `limit`.** Suficiente para el
  panel; insuficiente para un tenant con decenas de miles de asientos.
- **Verificación de superusuario en cliente.** `StoredUser.is_superuser` viaja en `sessionStorage`;
  no es una frontera de seguridad. El backend valida en cada petición.
