# FASE 6 — Auditoría de Seguridad End-to-End, Hardening Dokploy & Despliegue

> **Documento de especificación ejecutable.** Define las pruebas adversariales completas, la verificación forense de la política Zero Data (R5), el endurecimiento de red y proxy inverso en Dokploy, las pruebas de estrés bajo concurrencia real y la puesta en marcha de producción con subdominios definitivos y cobros en vivo.
>
> **Estado:** `[ ]` Pendiente de ejecución  
> **Dependencias previas:** Todas las fases anteriores (1 a 5) completadas, probadas y firmadas en `ROADMAP.md`.  
> **Autoridades que rigen esta fase:** `ARCHITECTURE.md` (§1, §5, §9) y `AGENTS.md` (Las 6 Reglas de Oro).

---

## 1. Objetivo de la Fase

Ejecutar una auditoría de seguridad integral y exhaustiva sobre toda la superficie del sistema (API REST, servidor MCP, orquestador Celery, base de datos relacional y runners Docker de Strix). 

El objetivo es cazar vulnerabilidades que solo emergen al cruzar múltiples capas: validar que ninguna organización pueda acceder a datos o escaneos de otra bajo ataques deliberados, certificar forensemente que ningún fragmento de código fuente privado queda en disco tras los análisis (Zero Data R5), verificar con `nmap` que los servicios internos (PostgreSQL y Redis) permanecen invisibles desde Internet (R6), aplicar cabeceras de seguridad estrictas en Traefik (grado SSL A+) y activar la plataforma en producción con dominios definitivos y `STRIPE_LIVE_MODE=true`.

---

## 2. Decisiones de Arquitectura e Infraestructura

1. **Aislamiento Multi-tenant Certificado (R3 Estricta):**
   * Se ejecuta una batería de pruebas de penetración cruzadas (Cross-Tenant Pentest): inyección de IDs ajenos en rutas, encabezados manipulados, tokens falsificados y ataques en el plano de ejecución de Docker.
   * Ningún runner debe tener visibilidad sobre redes de otros contenedores ni acceso al socket de Docker del host (`/var/run/docker.sock`).
2. **Certificación Forense Zero Data (R5 Estricta):**
   * Tras la ejecución de un escaneo (tanto si finaliza con éxito como si es cancelado o crashea), se realiza una inspección forense en el sistema de archivos del VPS para validar que el directorio temporal `/tmp/strix_workspaces/<job_id>` y `/tmp/pr_workspaces/<job_id>` se han eliminado sin dejar residuos.
   * En PostgreSQL, se audita que no existan tablas o campos donde se haya filtrado código fuente de clientes.
3. **Exposición Cero en Red (R6 Estricta):**
   * Los puertos de base de datos (`5432`) y de caché (`6379`) deben responder única y exclusivamente a través de la interfaz privada de Tailscale (`100.x.y.z`).
   * Escaneos externos contra la IP pública del VPS deben certificar que los puertos están completamente filtrados o cerrados.
4. **Hardening de Traefik y Conexiones Seguras:**
   * Certificados SSL automáticos mediante Let's Encrypt para todos los subdominios públicos.
   * Configuración de cabeceras HTTP de seguridad de grado bancario (HSTS con preload, CSP estricto, mitigación de clickjacking y protección contra MIME sniffing).

---

## 3. Desglose de Tareas de Implementación

### Tarea 6.1 · Batería de Pruebas Adversariales Multi-Tenant
Crear la suite de pruebas de penetración interna en `backend/tests/security/test_adversarial_isolation.py`:

1. **Ataques en API y Autorización (BOLA / IDOR):**
   * El Usuario A (Org A) intenta consultar `/api/v1/vulnerabilities/{id_de_org_b}`. Debe responder invariablemente `HTTP 404 Not Found` o `HTTP 403 Forbidden`.
   * El Usuario A intenta actualizar el estado de un issue de Org B mediante `PATCH /api/v1/issues/{id}`. La transacción debe ser rechazada.
   * El Usuario A intenta abortar un pentest en curso de Org B (`POST /api/v1/pentests/{id}/abort`). Debe fallar sin detener el runner.
2. **Ataques de Falsificación de Tokens:**
   * Una Service Key generada para Org A intenta invocar herramientas en el servidor MCP enviando un payload dirigido a repositorios de Org B. El servidor MCP debe resolver la organización desde el token criptográfico y rechazar el target ajeno.
3. **Aislamiento en Redes Docker:**
   * Levantar simultáneamente dos contenedores sandbox de Strix para Org A y Org B.
   * Desde el contenedor A, ejecutar un escaneo de red (`arp-scan` o `nmap`) dentro de su subred de Docker. Verificar que no puede resolver ni alcanzar la IP ni los puertos del contenedor B.

---

### Tarea 6.2 · Certificación Forense Zero Data (R5)
Implementar el script de validación forense en `scripts/audit_zero_data.py`:

1. **Prueba de Ciclo de Vida del Sandbox:**
   * Lanzar un pentest contra un repositorio de prueba que contenga un archivo canario (`CANARY_SECRET_123456789`).
   * Monitorear el directorio `/tmp/strix_workspaces/` durante la ejecución para confirmar la presencia del volumen efímero.
2. **Inspección Post-Ejecución:**
   * Inmediatamente después de que el job marque `COMPLETED`, el script inspecciona:
     * El directorio `/tmp/` del host VPS: debe haber 0 coincidencias de la cadena canario.
     * Los logs de Celery y del sistema: certificar que el contenido de los archivos de código no fue volcado a stdout/stderr.
     * La base de datos PostgreSQL: ejecutar consulta `SELECT` sobre todas las columnas de texto buscando la cadena canario. El único lugar admisible para trazas de código es el diff en `vulnerabilities.autofix_patch_diff` y la línea del PoC.
3. **Prueba ante Aborto y Fallo Forzado:**
   * Repetir el análisis y matar el worker a mitad de escaneo (`SIGKILL`).
   * Validar que la rutina de limpieza o el reaper de Celery elimina el contenedor huérfano y purga el directorio temporal.

---

### Tarea 6.3 · Hardening de Dokploy, Traefik y Firewall VPS
1. **Auditoría de Puertos con `nmap`:**
   * Ejecutar desde una máquina externa fuera de la red Tailscale:
     ```bash
     nmap -Pn -p 22,80,443,3000,5050,5432,6379,8000,8080 <IP_PUBLICA_VPS>
     ```
   * **Criterio de Aceptación:**
     * Puerto 22 (SSH): Abierto (preferiblemente restringido por clave o movido a puerto no estándar).
     * Puertos 80 y 443 (Traefik): Abiertos.
     * Puertos 5432 (Postgres), 6379 (Redis), 5050 (pgAdmin), 3000 (Gitea interno): **Estrictamente cerrados (`closed` o `filtered`)**.
2. **Configuración de Cabeceras en Traefik (`infra/dokploy/traefik-security.yml`):**
   * Añadir el middleware de cabeceras seguras en la configuración de Dokploy:
     ```yaml
     http:
       middlewares:
         security-headers:
           headers:
             stsSeconds: 31536000
             stsIncludeSubdomains: true
             stsPreload: true
             forceSTSHeader: true
             frameDeny: true
             contentTypeNosniff: true
             browserXssFilter: true
             referrerPolicy: "strict-origin-when-cross-origin"
             contentSecurityPolicy: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'; connect-src 'self' https: wss:;"
     ```
3. **Protección de Cookies de Sesión:**
   * En producción, verificar que todas las cookies (`session_id`, `jwt_token`) lleven las flags obligatorias:
     * `Secure = True`
     * `HttpOnly = True`
     * `SameSite = Lax` o `Strict`

---

### Tarea 6.4 · Pruebas de Estrés y Límites Operativos
1. **Concurrencia Máxima de Runners:**
   * Simular la llegada masiva de 10 peticiones de pentest simultáneas.
   * Verificar que la cola de Celery respeta el límite de concurrencia configurado en el worker (ej. máximo 2 o 3 contenedores Strix activos a la vez según la RAM del VPS).
   * Confirmar que los jobs restantes permanecen ordenadamente en estado `QUEUED` sin disparar el OOM Killer de Linux (*Out-Of-Memory*).
2. **Resiliencia ante Fallos de Proveedores LLM:**
   * Simular una caída de OpenRouter/OpenAI inyectando una clave de API inválida o bloqueando el tráfico saliente al proveedor.
   * Comprobar que Strix falla controladamente, el runner captura la excepción en menos de 60 segundos, el contenedor se destruye y el estado en el dashboard se marca como `FAILED` con un mensaje claro al usuario (sin congelarse en `RUNNING`).

---

### Tarea 6.5 · Despliegue Definitivo y Puesta en Producción
1. **Configuración de Variables de Entorno en Dokploy:**
   * `ENVIRONMENT = "production"`
   * `DEBUG = False`
   * `SECRET_KEY = "<clave_aleatoria_64_bytes>"`
   * `STRIPE_LIVE_MODE = True` (claves `sk_live_...` y `whsec_live_...`)
   * `DEFAULT_STRIX_LLM = "openrouter/z-ai/glm-5.3"` (o modelo aprobado para producción)
2. **Mapeo de Subdominios Públicos:**
   * Panel Web: `https://app.tu-dominio.com` (Frontend React estático servido por Nginx/Traefik).
   * API Gateway: `https://api.tu-dominio.com` (Backend FastAPI / Django).
   * Servidor MCP: `https://mcp.tu-dominio.com` (Endpoint SSE para clientes de IA).
3. **Verificación de Certificados SSL:**
   * Analizar los subdominios en SSL Labs (`ssllabs.com/ssltest/`) y confirmar calificación **A** o **A+**.

---

## 4. Definition of Done (DoD) — Criterios de Aceptación Final

Para dar por concluida la Fase 6 y autorizar el lanzamiento comercial, se deben validar y marcar todas las casillas siguientes:

- [ ] **Auditoría de Aislamiento Aprobada:** El 100% de los tests adversariales cross-tenant (`test_adversarial_isolation.py`) pasan limpiamente sin una sola fuga de datos.
- [ ] **Certificación Forense Zero Data:** El script `audit_zero_data.py` valida que el código fuente de repositorios privados se purga al 100% de disco y memoria tras cada escaneo.
- [ ] **Escaneo de Puertos Limpio:** El comando `nmap` contra la IP pública del VPS confirma que PostgreSQL (`5432`) y Redis (`6379`) están cerrados a Internet.
- [ ] **Grado SSL A+:** Los subdominios de producción (`app.`, `api.`, `mcp.`) cuentan con certificados válidos y cabeceras HSTS/CSP verificadas.
- [ ] **Resiliencia bajo Carga:** La cola de Celery gestiona ráfagas de escaneos sin saturar la CPU ni la memoria del VPS de Dokploy.
- [ ] **Flujo Comercial Completo Validado End-to-End:**
  1. Registro de usuario y creación de organización en `app.tu-dominio.com`.
  2. Suscripción al plan Pro y compra de créditos procesada en vivo por Stripe.
  3. Vinculación de un repositorio de GitHub mediante la App oficial.
  4. Apertura de un Pull Request: escaneo automático en CI, reporte de status check, comentario con comando PoC y bloqueo de merge.
  5. Aprobación y aplicación del parche *autofix*.
  6. Conexión de un asistente externo (Cursor / Claude Code) al servidor MCP remoto y ejecución de herramientas de escaneo con éxito.
- [ ] **Todas las fases anteriores (1 a 5) firmadas y completadas en `ROADMAP.md`.**

---

## 5. Protocolo de Revisión Especializada (Paso 8 de AGENTS.md)

| Especialista | Verificación Obligatoria |
| :--- | :--- |
| **Security Reviewer** | Comprobar que no existen credenciales de prueba, tokens de desarrollo ni contraseñas por defecto en ninguna variable de entorno de Dokploy. Realizar barrido final de secretos en el historial Git. |
| **Database Reviewer** | Validar la activación de respaldos periódicos (*automated backups*) de PostgreSQL en Dokploy con retención configurada y prueba de restauración satisfactoria. |
| **Silent Failure Hunter** | Verificar la monitorización centralizada de errores (Sentry o similar) en backend y frontend para capturar cualquier excepción no controlada en producción. |
| **Frontend Reviewer** | Comprobar que el build de producción de Vite genera chunks optimizados con compresión gzip/brotli y que no se imprimen trazas de `console.log` en el navegador del cliente. |