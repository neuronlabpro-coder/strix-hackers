"""API pública de Mind Guard Fenix Team: credenciales de servicio y catálogo de scopes.

La app se llama `api_access` y no `auth` porque `auth` ya existe y cubre la identidad de
personas: JWT, invitaciones, verificación de correo. Esto es otro sujeto: un sistema, no
un usuario, y con un modelo de permisos distinto — scopes explícitos en vez de un rol
dentro de un tenant. Mezclarlos en un solo módulo haría que una decisión sobre uno
acabara tomando la del otro; revocar un token de API terminaría cambiando lo que un
usuario web puede ver, y ese error no lo detecta ningún test de los dos por separado.
"""
