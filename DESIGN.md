---
name: Mind Guard Fenix Team
description: Panel de seguridad ofensiva continua en modo code-first: densidad alta, un solo acento y cero decoración.
colors:
  neutral-bg: "#1C1C1C"
  surface: "#2A2A2A"
  primary: "#EDEDED"
  secondary: "#8A8F8A"
  caption-on-surface: "#B2B2B2"
  accent: "#17A163"
  on-accent: "#1C1C1C"
typography:
  display:
    fontFamily: "Inter, sans-serif"
    fontSize: "2.2rem"
    fontWeight: 500
    lineHeight: 1.15
  headline:
    fontFamily: "Inter, sans-serif"
    fontSize: "1.8rem"
    fontWeight: 500
    lineHeight: 1.2
  title:
    fontFamily: "Inter, sans-serif"
    fontSize: "1.1rem"
    fontWeight: 500
    lineHeight: 1.3
  body:
    fontFamily: "Inter, sans-serif"
    fontSize: "0.96rem"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "JetBrains Mono, monospace"
    fontSize: "0.72rem"
    fontWeight: 500
    letterSpacing: "0.04em"
rounded:
  sm: "4px"
  md: "6px"
  lg: "10px"
  pill: "999px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "32px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.md}"
    padding: "10px 16px"
    height: "38px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: "10px 16px"
    height: "38px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: "24px"
  input:
    backgroundColor: "{colors.neutral-bg}"
    textColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: "11px 12px"
    height: "44px"
  toggle-on:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.pill}"
---

# Design System: Mind Guard Fenix Team

## 1. Overview

**Creative North Star: "La sala de operaciones nocturna"**

Una mesa de trabajo de seguridad a las 03:00: la luz de la pantalla es lo único que ilumina el escritorio, el operador conoce cada pantalla de memoria y no tolera que nada se mueva sin motivo. El sistema está construido para esa escena. La base es un negro cálido (`#1C1C1C`) que no cansa la vista, las superficies (`#2A2A2A`) separan por tono y no por sombra, y el texto principal (`#EDEDED`) tiene un contraste de 14.56:1 sobre el fondo para que se lea de reojo. Sobre ese grisalto entra un único verde esmeralda (`#17a163`) que significa exactamente una cosa: "esta es la acción".

La densidad es deliberada. Una tabla de repositorios con proveedor, estado, severidad y fecha de última auditoría pertenece en una sola pantalla sin desplazamiento horizontal innecesario. JetBrains Mono no es estética: está reservado para lo que un ingeniero necesita leer como código o como medida, hashes, ramas, endpoints, severidades y puntuaciones. La prosa se escribe en Inter, y los rótulos en mono versalita funcionan como el "eyebrow" de un terminal.

El sistema rechaza explícitamente la ciberseguridad de escaparate (fósforo, calaveras, retículas de hacker), el SaaS de seguridad genérico (degradados, glassmorphism, rejillas de tarjeta idénticas) y el terminal como estética (el mono para todo). También rechaza los degradados: el tema es plano por diseño y la profundidad se consigue con tono.

**Key Characteristics:**
- Fondo y superficies como única forma de profundidad; sin sombras salvo en la tarjeta de autenticación, que flota sobre la pantalla completa.
- Un solo acento que actúa, nunca decora.
- Rótulos en mono versalita como ancla de escaneo en cada bloque.
- Todo dato del panel es rastreable al backend; los estados vacíos enseñan en lugar de rellenar.

## 2. Colors

Paleta de cuatro neutros cálidos y un único acento; la severidad se codifica con opacidad del color primary, nunca con una paleta paralela.

### Primary
- **Blanco de instrumental (#EDEDED)**: titulares, cifras, texto de formulario y etiquetas de tabla. Sobre `#1C1C1C` da 14.56:1, sobre `#2A2A2A` da 12.26:1. Siempre AA, con margen AAA.

### Secondary
- **Gris de metadatos (#8A8F8A)**: bordes, rótulos en mono, pies de tarjeta y estados apagados. Da 5.17:1 sobre el fondo y **4.36:1 sobre la superficie**, por lo que se usa en texto pequeño solo cuando la base es el fondo.

### Neutral
- **Negro de sala (#1C1C1C)**: la página, los campos de formulario, las filas alternas y los elementos "hundidos". 1.0 de estructura.
- **Gris de panel (#2A2A2A)**: tarjetas, tablas, modales, la barra lateral. Una capa por encima del fondo, sin sombra.

### Derived
- **Gris de rótulo (#B2B2B2)**: `#EDEDED` al 70 % sobre la superficie. Es el token para todo texto secundario pequeño que vive *dentro* de una tarjeta, tabla o modal: sube el contraste sobre `#2A2A2A` de 4.36:1 a 6.77:1 sin introducir un tono nuevo.

### Tertiary
- **Verde esmeralda (#17A163)**: el acento. Botón primario, interruptor activo, badge de escaneo en curso y progreso del gauge. Con texto `#1C1C1C` da 5.12:1.

### Named Rules

**The One Accent Rule.** `#17a163` aparece en una sola acción primaria por pantalla. Si dos elementos compiten por el acento en la misma vista, uno de los dos es un error, no una decisión.

**The Severity-Ramp Rule.** `design-dark.md` admite una única escala cromática, la rampa de severidad (`critical #EF4444`, `high #F97316`, `medium #F59E0B`, `low #3B82F6`, `info #8A8F8A`), y solo para visualización de datos: swatches, anillos y distribuciones. Cualquier otro color nuevo sigue prohibido. Dos corolarios importan más que la regla misma: la rampa nunca actúa como color de acción (ahí manda `#17a163`) y los badges de estado de remediación permanecen monocrimos, porque si el estado también fuera cromático la misma celda comunicaría dos escalas a la vez.

**The Contrast Floor Rule.** Ningún texto menor de 19 px baja de 4.5:1 contra su fondo real. Sobre superficies, el texto secundario usa `--color-caption`, nunca `--color-secondary`.

## 3. Typography

**Display Font:** Inter (con `sans-serif`)
**Body Font:** Inter (con `sans-serif`)
**Label/Mono Font:** JetBrains Mono (con `monospace`)

**Character:** Una sola familia humanista para todo el lenguaje y una mono para todo dato. El contraste no viene de emparejar familias sino de cambiar de familia según el tipo de contenido: si es algo que se copia, se compara o se teclea, va en mono.

### Hierarchy
- **Display** (500, 2.2 rem, 1.15): el único `h1` de cada pantalla de shell.
- **Headline** (500, 1.8 rem): títulos de las tarjetas de autenticación, fuera del shell.
- **Title** (500, 1.1 rem): encabezados de tarjeta dentro del shell.
- **Body** (400, 0.96 rem, 1.55): prosa y contenido de formulario; línea de 65 a 75 caracteres.
- **Label** (500, 0.72 rem, 0.04 em, versalitas): eyebrows, cabeceras de tabla, badges, datos técnicos.
- **Data** (500, 2 rem, mono): cifras de KPI y contadores de tabla.

### Named Rules

**The Mono Means Machine Rule.** Solo entra JetBrains Mono lo que una máquina nombraría: hash, rama, endpoint, proveedor, severidad, score, fecha técnica. Cualquier frase en mono es un error.

**The Eyebrow Rule.** Cada bloque de contenido empieza con un `eyebrow` en mono versalita. Es el ancla de escaneo del bloque y sustituye a los iconos decorativos.

## 4. Elevation

El sistema es plano por defecto: la profundidad se consigue con el tono (`#1C1C1C` página, `#2A2A2A` superficie, `#1C1C1C` de nuevo para campos y filas hundidas) y con un borde de 1 px al 35-45 % de opacidad del gris de metadatos. La única sombra del sistema es la de la tarjeta de autenticación, que flota sobre una pantalla completa y necesita separarse del fondo. Los estados `hover` desplazan el color del borde al acento, nunca la posición ni la escala.

### Shadow Vocabulary
- **auth-float** (`box-shadow: 0 18px 48px rgba(28, 28, 28, 0.55)`): solo la tarjeta de login, registro, verificación e invitación.

### Named Rules

**The Flat-By-Default Rule.** Las superficies están planas en reposo. Si algo necesita sombra para entenderse, el problema es de jerarquía tonal, no de sombra.

## 5. Components

### Buttons
- **Shape:** esquinas de 6 px (`--radius-md`), altura mínima de 38 px, tipografía de 0.8 rem con peso 500.
- **Primary:** fondo `#17a163`, texto `#1C1C1C`, `padding: 10px 16px`. En formularios de autenticación ocupa el 100 % del ancho y sube a 44 px de alto.
- **Hover / Focus:** `filter: brightness(1.08)` en el primario; borde al acento en el secundario; el foco visible es un contorno de 2 px en acento con 3 px de separación, aplicado globalmente.
- **Secondary / Ghost:** fondo `#2A2A2A`, borde de 1 px en gris de metadatos, texto principal. Es el botón por defecto de cualquier acción que no sea la primaria de la pantalla.
- **Icon button:** 32 × 32 px, borde de 1 px, icono de 16-18 px. Cumple el mínimo de 24 × 24 px de WCAG 2.2, no el objetivo táctil de 44 px: es un control de escritorio denso.

### Chips (if used)
- **Style:** fondo `#1C1C1C`, borde de 1 px en gris de metadatos, tipografía mono de 0.68 rem, `padding: 3px 9px`, radio de 4 px.
- **State:** el estado activo o en curso invierte los papeles, fondo acento y texto `#1C1C1C`. El estado "pendiente de dato" usa borde discontinuo para comunicar ausencia de dato sin parecer desactivado.

### Cards / Containers
- **Corner Style:** radio de 10 px (`--radius-lg`).
- **Background:** `#2A2A2A`; el fondo de página es `#1C1C1C`.
- **Shadow Strategy:** ninguna sombra (ver Elevation).
- **Border:** 1 px de gris de metadatos; en la tarjeta de autenticación, 35 % de opacidad.
- **Internal Padding:** 24 px en tarjetas, 20 px en tarjetas de KPI, 32 px en la tarjeta de autenticación.

### Inputs / Fields
- **Style:** fondo `#1C1C1C` (hundido), borde de 1 px al 45 % del gris de metadatos, radio de 6 px, `padding: 11px 12px`, altura 44 px, texto de 0.92 rem.
- **Focus:** el contorno global de 2 px en acento; el borde sube a acento en hover.
- **Error / Disabled:** el error es texto secundario con `role="alert"`, nunca un borde rojo; los campos no tienen estado deshabilitado porque el panel no edita en línea.

### Navigation
- **Estilo:** barra lateral de 280 px en `#2A2A2A` con borde de 1 px, selector de organización como disparador, y enlaces de 38 px de alto con icono de 17 px, radio de 4 px y fondo `#1C1C1C` al estar activo.
- **Tipografía:** 0.84 rem en Inter; los grupos van precedidos por un `eyebrow` en mono.
- **Móvil:** por debajo de 760 px la barra lateral pasa a ser una cabecera y la navegación se convierte en una tira horizontal desplazable con las mismas secciones, para que ninguna ruta quede inalcanzable.

### Charts
- **Renderer:** Apache ECharts con `echarts/core` (gauge y anillo), cargado bajo demanda.
- **Paleta:** la misma del sistema, leída de las variables CSS en tiempo de ejecución para que no exista una segunda fuente de verdad.
- **Accesibilidad:** cada canvas expone `role="img"` con su `aria-label`, y los datos que dibujan también existen como texto en el DOM.

### Toggle
- **Pista:** 38 × 20 px, radio 999 px, fondo `#1C1C1C` con borde de 1 px en reposo; fondo acento cuando está activo.
- **Pomo:** 14 px, `#8A8F8A` en reposo, `#1C1C1C` cuando está activo, desplazado 18 px con `transform` (nunca `left`).
- **Semántica:** `role="switch"` con `aria-checked`, etiqueta accesible en la acción que provoca y estado legible en texto.

## 6. Do's and Don'ts

### Do:
- **Do** usar `#17a163` para una sola acción primaria por pantalla y reservar el resto de interacciones para botones secundarios con borde.
- **Do** escribir en JetBrains Mono los hashes, ramas, endpoints, proveedores, severidades y puntuaciones; escribir en Inter todo lo demás.
- **Do** asegurar 4.5:1 mínimo en texto pequeño, y usar `--color-caption` cuando el texto secundario vive sobre `#2A2A2A`.
- **Do** mantener un `eyebrow` en mono versalita al inicio de cada bloque, para que el escaneo tenga un ancla.
- **Do** mostrar estados vacíos que enseñen el siguiente paso, con la acción principal a la vista.
- **Do** respetar `prefers-reduced-motion`: sin giratorios ni transiciones cuando el sistema lo pide.

### Don't:
- **Don't** introducir degradados de ningún tipo, ni en fondos, ni en bordes, ni en el texto: el tema es plano por diseño.
- **Don't** usar texto con degradado (`background-clip: text`) ni glassmorphism como recurso de profundidad.
- **Don't** añadir una segunda paleta para severidad (rojo, naranja, amarillo) mientras `design-dark.md` no la admita; la severidad se codifica con opacidad de `#EDEDED`.
- **Don't** usar franjas de color de 2 px en el borde izquierdo o derecho de tarjetas, elementos de lista o avisos; el estado activo se marca con fondo y borde.
- **Don't** repetir rejillas de tarjetas idénticas con icono y párrafo; una tarjeta es un recurso, no el contenedor por defecto.
- **Don't** construir la jerarquía con un patrón de métrica gigante con degradado de acento; las cifras van en mono y el contexto en el pie de la tarjeta.
- **Don't** coreografiar la carga de una página con animaciones de entrada, ni animar propiedades de layout; el movimiento comunica estado.
- **Don't** usar el modal como primera opción: primero la vista en línea o el revelado progresivo; el modal queda solo para flujos de dos pasos con decisión irreversible.
- **Don't** llamar a la seguridad "de escaparate": nada de fósforo, calaveras, retículas ni `0xDEADBEEF` decorativo.
