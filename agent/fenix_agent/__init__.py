"""Agente de escaneo de contenedores y redes de Mind Guard Fenix Team.

Este proceso se instala **dentro de la red del cliente** y hace la parte determinista de los
escaneos: descubrir, medir puertos e inventariar la imagen de un contenedor. El razonamiento
ocurre en la plataforma, que es donde está la clave del proveedor de modelos.

La razón de que sea un agente y no un escaneo desde el servidor está en el cerco de salida del
sandbox: `scripts/harden_runner_egress.sh` deja pasar DNS, HTTPS y conexiones establecidas, y
descarta el resto. Desde el VPS no se alcanza la red privada de nadie, y una base de datos en un
contenedor no es accesible desde Internet.

Y este agente **no lleva ninguna credencial de la plataforma**: su token solo puede pedir
trabajo y devolver resultados. Corre en la red del cliente, con los permisos del cliente, que es
el entorno menos confiable de todo el sistema para la plataforma. Que el razonamiento no ocurra
ahí no es una preferencia: es lo que evita que un `README` malicioso en un escaneo convierta la
facturación de todos los clientes en el radio de impacto de un `curl`.
"""

__version__ = "1.0.0"
