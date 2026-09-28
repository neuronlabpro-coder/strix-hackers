# Premisas del informe que no se sostienen

Este documento no es un informe de estado: es el registro de tres diagnos receivedidos que, al
comprobarse contra el codigo, resultaron falsos. Se deja escrito porque el diagnostico original
llego con evidencia aparente —nombres de archivo, mensajes de error, rutas concretas— y esa
evidencia es precisamente lo que lo hacia convincente.

## 1. "Los routers de chat, supply chain, admin/tickets y admin/audit no estan incluidos"

Es cierto que los cuatro estan incluidos. Lo que es cierto tambien es que `app.routes` **no los
muestra**, y ahi empieza el error de lectura.

En esta version de FastAPI los routers incluidos no se aplanan: `app.routes` contiene 22 objetos
de tipo `_IncludedRouter`, y las rutas de cada uno viven dentro, en un atributo. Contando los
`APIRoute` de primer nivel salen **dos**: `GET /` y `GET /health`. Un diagnostico hecho mirando
`app.routes` concluye razonablemente que no hay ningun router montado, porque es exactamente lo
que se ve.

La fuente de verdad es el esquema OpenAPI, que es lo que ve el cliente, y ahi hay **86 rutas**:

    get,post   /api/v1/chat/conversations
    post       /api/v1/chat/conversations/{conversation_id}/messages
    get        /api/v1/supply-chain/packages
    get        /api/v1/supply-chain/summary
    post       /api/v1/supply-chain/repositories/{repository_id}/sync
    get        /api/v1/admin/tickets
    get        /api/v1/admin/audit

Que un `404` fuera real de todos modos es otra historia, y no la explica este
documento: un `404` de este tipo se produce tambien con el router montado, y sus
que no reencamina, un backend que no esta levantado, o una peticion a un puerto distinto del que
escucha. Ninguna se arregla incluyendo un router que ya esta incluido.

## 2. "`tenants.members` devuelve un objeto en vez de una cadena"

El error de i18next ese es real y tiene una causa exacta: `t('tenants.members')` **sin** `count`
devuelve el objeto, porque la clave tiene las variantes `one` y `other` de un plural.

Pero el codigo no hace eso:

    backend/...: AdminTenantsPage.tsx:273
    {t('tenants.members', { count: tenant.member_count })}

Y la clave esta bien formada:

    "members": { "one": "{{count}} miembro", "other": "{{count}} miembros" }

Ademas hay dos claves distintas que podrian confundirse al leer: `tenants.columns.members` es el
**titulo** de la columna, un string; `tenants.members` es el **conteo**, un objeto con plural. El
diagnostico las mezclo. Ambas son correctas y estan en sitio.

Ademas, el fichero `TenantsTab.tsx` que se cita como lugar del fallo **no existe**. La pantalla se
llama `AdminTenantsPage.tsx`, igual que `TokensTab.tsx` en el encargo anterior: tambien era un
nombre de archivo que no esta en el repositorio.

## 3. "Aparece la cadena literal `{ACTIVE} ACTIVOS DE {TOTAL}`"

No aparece. No hay ninguna coincidencia de `ACTIVOS DE` ni de `ACTIVE` en `frontend/src`, ni en
componentes, ni en los ficheros de traduccion.

Lo que si hay, y probablemente es lo que se vio, es un **placeholder de pluralizacion** en un
fichero de traduccion, que en pantalla aparece con la forma `{count}` antes de que i18next lo
sustituya:

    "one": "{{count}} activo"
    "other": "{{count}} activos"

Eso es correcto y es como se escribe un plural. Si aparecio con las llaves, el problema estaria en
que la llamada no pasa `count`, no en el texto.

## Por que se documenta en vez de descartarse

Porque la diferencia entre "este sintoma no existe" y "este sintoma no lo he encontrado" es la que
decide si se arregla algo. Descartarlos sin dejar constancia hace que la misma persona los
represente en la proxima revision, y la proxima revision empieza por el mismo sitio.

Lo que si procede es medir de verdad, que es lo que hace el resto del trabajo: levantar el
entorno, abrir el navegador, y mirar la consola.
