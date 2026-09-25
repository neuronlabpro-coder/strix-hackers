# Product

## Register

product

## Users

Equipos de seguridad ofensiva y sus responsables de aplicación (AppSec) que trabajan dentro de una organización cliente con datos aislados por tenant. Vienen de dos sitios: (a) un ingeniero que revisa un Pull Request entre commits y necesita un veredicto en minutos; (b) un responsable que audita postura a diario, abre pentests y gestiona vulnerabilidades con clientes. Ambos leen en sesiones cortas, a veces de madrugada, sobre pantallas de portátil o de monitor grande en un puesto de trabajo con poca luz ambiente.

El trabajo que hay que hacer bien: **saber qué está roto antes de que un cliente lo descubra**. Todo lo que el panel muestra es evidencia (un score, una severidad, un commit status, un PoC), nunca decoración.

## Product Purpose

Plataforma SaaS de pentesting autónomo, DevSecOps y evaluación ofensiva continua. Conecta repositorios Git, ejecuta escaneos en contenedores efímeros, devuelve hallazgos con prueba de concepto y abre ramas de remediación. El éxito es que un PR con SQLi no se fusione, y que el equipo de seguridad tenga la postura de su producto a la vista sin exportar nada a una hoja de cálculo.

Restricciones que mandan sobre el diseño: cero datos de cliente en la plataforma, aislamiento estricto por organización, y una estética de densidad alta donde el espacio negativo y la precisión tipográfica son la decoración.

## Brand Personality

Preciso, sobrio, técnico. Tres palabras: **auditable, contenido, veraz**. La interfaz se parece a un terminal de operaciones bien diseñado, no a un producto de seguridad que quiere impresionar. Se comunica en español por defecto con inglés disponible, y usa el idioma para hablar de lo que hace, no de lo que vende.

## Anti-references

- **Ciberseguridad de escaparate**: negro y verde fósforo, escáneres animados, calaveras, cadenas de `0xDEADBEEF` decorativas, retículas de hacker en el fondo.
- **SaaS de seguridad genérico**: degradados morados y azules, glassmorphism, tarjetas con icono + titular + párrafo repetido en rejilla, sombras difusas que no significan nada.
- **Terminal como estética**: monoespaciado como estética principal. El mono es para los datos (hashes, ramas, scores, severidades), nunca para la prosa.
- **Hype de IA en el panel**: nada de gradientes en el texto, nada de "✨", nada de gradientes radiales que comuniquen "moderno", nada de animaciones que coreografien la carga de la página.

## Design Principles

1. **La evidencia es la interfaz.** Cada número, badge o estado que aparece en pantalla tiene que ser rastreable a un dato del backend. Si no existe el dato, se muestra un estado vacío honesto, nunca un cero inventado.
2. **Un acento, una acción.** `#17a163` es el único color de la paleta que actúa y se usa para la acción primaria de cada pantalla. Su escasez es lo que lo hace legible.
3. **Densidad con jerarquía.** Mucha información por pantalla, pero el ojo tiene que saber dónde mirar primero: escala y peso, no color.
4. **Aislamiento visible.** Nada en la interfaz puede sugerir que un usuario ve datos de otro tenant: un 404 genérico es parte del diseño.
5. **Honestidad operacional.** Estados vacíos que enseñan, errores que dicen qué hacer, y nada de eufemismos: si una función aún no existe, la interfaz lo admite.

## Accessibility & Inclusion

Objetivo WCAG 2.1 AA. Requisitos concretos: contraste mínimo 4.5:1 en todo el texto pequeño (incluidos los que viven sobre superficies elevadas), foco visible en todo elemento interactivo, navegación completa por teclado en diálogos, `prefers-reduced-motion` respetado, y el atributo `lang` del documento sincronizado con el idioma activo. Se asume pantalla de alta densidad y uso compartido en puestos de trabajo de seguridad, donde el contraste bajo y el texto minúsculo se pagan caro.
