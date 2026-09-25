# Infraestructura Dokploy de Fenix

`docker-compose.yml` despliega PostgreSQL 16 y Redis 7 para el stack aislado de Fenix. El despliegue debe ejecutarse en el VPS con `TAILSCALE_BIND_IP` configurada a la interfaz privada de Tailscale; ambos puertos se publican únicamente en esa interfaz (`5433` y `6380`).

## Variables requeridas

- `TAILSCALE_BIND_IP`: dirección privada `100.x.y.z` del VPS.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
- `REDIS_PASSWORD`.

Los secretos se inyectan desde el entorno de Dokploy y no se almacenan en Git.

## Verificación de exposición

Antes de cerrar la Fase 1, ejecutar desde una red externa al VPS:

```text
Test-NetConnection -ComputerName <IP_PUBLICA_VPS> -Port 5433
Test-NetConnection -ComputerName <IP_PUBLICA_VPS> -Port 6380
nmap -Pn -p 5433,6380 <IP_PUBLICA_VPS>
```

El resultado esperado es `closed` o `filtered` para ambos puertos. La evidencia debe adjuntarse al checkpoint de la fase; no se debe marcar la fase como completada sin ella.
