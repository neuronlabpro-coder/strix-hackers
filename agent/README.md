# Agente de escaneo de Mind Guard Fenix Team

Un proceso que el cliente instala **dentro de su propia red** y que hace el trabajo pesado de
los escaneos de contenedores y de redes.

## Por qué existe esto y por qué no escanea el SaaS

El sandbox de Strix, en el VPS, vive en un bridge aislado cuyo cerco de salida permite DNS, HTTPS
y conexiones establecidas: `scripts/harden_runner_egress.sh` descarta en silencio el resto,
incluidos el 22, el 80 y el 8080. Ese cerco no se abre, porque es lo que impide que un agente
comprometido saque la clave del proveedor por un puerto alto.

La consecuencia es que **desde el SaaS no se alcanza la red privada de nadie**, y una base de
datos en un contenedor o un segmento interno no son accesibles desde Internet. Un motor de
pentesting que solo sabe atacar objetivos públicos no cobra por lo que el cliente quiere
comprar.

Así que el escaneo ocurre donde la red es alcanzable, y este agente es ese "dónde".

## El agente no lleva ninguna credencial de la plataforma

Es la propiedad más importante de este diseño, y conviene entender por qué es una consecuencia y
no una preferencia. Este proceso corre en la red del cliente, con los permisos del cliente, sobre
infraestructura del cliente: es el entorno **menos** confiable de todo el sistema desde el punto
de vista de la plataforma. Si la clave del proveedor estuviera aquí, comprometer la red de un
cliente comprometería la facturación de todos, y el radio de impacto de un `README` con
`curl http://169.254.169.254` sería la plataforma entera.

El agente recibe un token que solo puede hacer dos cosas: **pedir trabajo** y **devolver
resultados**. No habla con el proveedor de modelos, no ve la base de datos y no puede tocar nada
de otro cliente. Por eso escanea de forma determinista —descubre, mide puertos e inventar la
imagen— y devuelve **hechos**: el razonamiento ocurre en la plataforma, con la clave donde sí
debe estar.

## Qué escanea, y qué no

| Trabajo | Qué hace | Qué devuelve |
| :--- | :--- | :--- |
| `CONTAINER_SCAN` | Habla con la API de distribución OCI: manifiesto, configuración y la capa que lleva la base de datos de paquetes | Digest, sistema operativo, y los paquetes de `apk` y `dpkg` que se pudieron leer |
| `NETWORK_SCAN` | Recorre el CIDR y prueba los puertos de la lista en cada host vivo | Hosts que respondieron, con sus puertos y lo que se vio al conectar |

Dos límites que conviene tener claros antes de usarlo:

- **`rpm` no se lee.** La base de datos de paquetes de RHEL, Fedora y Rocky está en un formato
  binario propio, y un lector aproximado inventaría versiones, que es peor que no informar. La
  imagen sale con el inventario de lo que sí se pudo leer y con un motivo que lo explica.
- **No hay vulnerabilidades por paquete.** El proyecto tiene `cve_records`, pero esa tabla no
  guarda qué paquetes afecta cada CVE, así que no se puede cruzar el inventario con la base de
  datos local. Lo que se reporta es el inventario, no un número de CVEs que no se puede calcular.

## Instalación

### 1. Registrar el agente en el panel

En **Ajustes > Agentes de escaneo**, pulsa "Registrar agente" y ponle un nombre que lo
identifique: el de la máquina o el del segmento donde va a estar. La respuesta trae un `token` que
**solo se muestra una vez**. Cópialo.

### 2. Configurar el agente

Crea el fichero de configuración en el servidor donde va a correr:

```ini
# /etc/fenix-agent/fenix-agent.ini
[plataforma]
url = https://api.example.com
token = mgf_agent_...
intervalo_sondeo = 15
```

El `intervalo_sondeo` son segundos entre peticiones de trabajo. Con más de una decena de agentes
conviene subirlo: cada sondeo es una petición HTTP aunque no haya nada que hacer, y el valor por
defecto está pensado para un handful de agentes.

### 3. Ejecutar

```bash
python -m fenix_agent --config /etc/fenix-agent/fenix-agent.ini
```

Como servicio, con `systemd`:

```ini
[Unit]
Description=Agente de escaneo de Mind Guard Fenix Team
After=network-online.target

[Service]
Type=simple
User=fenix-agent
ExecStart=/usr/bin/python3 -m fenix_agent --config /etc/fenix-agent/fenix-agent.ini
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Qué necesita el agente

- **Python 3.11 o superior.** Nada más: el módulo usa solo la biblioteca estándar. No hay
  `requirements.txt` porque no hay dependencias, y esa es una decisión para que instalarlo en
  una red segmentada no conlleve resolver paquetes de fuera.
- **Salida HTTPS** hacia la plataforma. Y solo eso. No necesita puertos entrantes, que es lo que
  hace que se pueda instalar en una red de producción sin pedir una excepción al cortafuegos.
- **Permisos de red** para poder sondear el CIDR que se le pida. Nada más.

## Por qué no necesita el socket de Docker

Porque no habla con Docker. Habla con el **registro** por su API HTTP, que es lo mismo que hace
`docker pull` pero sin el demonio. Un `FROM scratch` o una imagen de un registro privado se
inventarían igual.

La contrapartida es que no ve contenedores **en ejecución**: solo la imagen. El inventario de lo
que está corriendo en un contenedor concreto —procesos, montajes, variables— requiere el socket,
y un socket de Docker en el proceso que corre en la red del cliente es una decisión que no se
toma en el cliente de otro. Si algún día hace falta, es un agente distinto con su propia
justificación.

## Si el agente se cae

No pasa nada, y esa es la razón de que exista el **alquiler**. Un trabajo que un agente toma queda
reservado con un plazo. Si el agente no devuelve nada antes de que venza, la plataforma lo
devuelve a la cola y otro agente lo recoge. Sin eso, un agente que se apaga a mitad dejaría un
escaneo en curso para siempre, que es un trabajo perdido y un hueco en la postura que nadie sabría
leer.

## Diagnóstico

El agente escribe un log con formato estructurado. Lo que más se va a necesitar:

- `agente registrado` con su identificador: si no aparece, el token está mal o el agente no
  arranca.
- `sin trabajo disponible`: normal, es la respuesta más frecuente.
- `trabajo fallido` con el motivo: el escaneo no pudo hacerse, y el motivo va también a la base
  de datos, donde se ve en la pantalla del cliente.
