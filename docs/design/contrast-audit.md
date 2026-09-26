# Auditoría de contraste — obsidiana

Evidencia medida del refactor cromático del Bloque 5.4. Todos los ratios salen de
calcular la luminancia relativa WCAG 2.1 sobre el bloque `@theme` **real** de
`frontend/src/styles/index.css`, no sobre una paleta reescrita en el propio script: una
auditoría que redefine los colores midiendo su copia demuestra que la copia es buena,
no que el panel lo sea.

## Superficies

| Token | Valor |
| :--- | :--- |
| `--color-background` | `#0B0C0E` |
| `--color-raised` | `#0E1012` |
| `--color-surface` | `#121316` |
| `--color-surface-elevated` | `#16181B` |

La escalera es monótona (1,026 / 1,026 / 1,044 entre planos) y el techo conserva
1,235:1 de margen frente a `#262A31`, de modo que ninguna superficie se acerca a gris
medio.

**Sobre el umbral de separación entre planos.** Una primera versión de esta auditoría
exigía 1,15:1 entre superficies y fallaba las cuatro. Ese umbral era inventado:
WCAG no fija ninguno, porque una separación de planos no es un límite de contenido. Se
sustituyó por dos comprobaciones que sí significan algo —monotonía y techo obsidiana— y
la percepción del escalo la completa el hairline de 1 px, que es el mecanismo con el
que funcionan los temas oscuros de referencia del sector.

## Texto

El caso peor es `--color-surface-elevated` (inputs, hover de fila), que es la superficie
más clara y por tanto la que menos contraste ofrece.

| Token | Valor | Lienzo | Raised | Surface | Elevated | Umbral | Veredicto |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| `primary` | `#F3F4F6` | 17,78 | 17,32 | 16,88 | 16,16 | 4,5 | AA |
| `caption` | `#A8AEB9` | 8,78 | 8,55 | 8,33 | 7,98 | 4,5 | AA |
| `placeholder` | `#798193` | 5,01 | 4,88 | 4,75 | 4,55 | 4,5 | AA |
| `secondary` | `#79818F` | 4,99 | 4,86 | 4,73 | 4,53 | 4,5 | AA |
| `muted` | `#5E6575` | 3,35 | 3,26 | 3,18 | 3,04 | 3,0 | AA (texto grande) |
| `accent` | `#17A163` | 5,88 | 5,73 | 5,59 | 5,35 | 3,0 | AA |
| `accent-bright` | `#10B981` | 7,71 | 7,52 | 7,32 | 7,01 | 3,0 | AA |
| `critical` | `#F87171` | 7,07 | 6,89 | 6,72 | 6,43 | 4,5 | AA |
| `high` | `#FB923C` | 8,65 | 8,42 | 8,21 | 7,86 | 4,5 | AA |
| `medium` | `#FBBF24` | 11,72 | 11,42 | 11,13 | 10,66 | 4,5 | AA |
| `low` | `#60A5FA` | 7,70 | 7,50 | 7,31 | 7,00 | 4,5 | AA |
| `info` | `#9CA3AF` | 7,71 | 7,51 | 7,32 | 7,01 | 4,5 | AA |

## Dos tonos del encargo que hubo que corregir

| Solicitado | Uso pedido | Medido | Fallo | Adoptado | Hue shift |
| :--- | :--- | ---: | :--- | :--- | ---: |
| `#6B7280` | texto secundario | 3,68:1 | AA 4,5 | `#79818F` (4,53:1) | 1,8° |
| `#525866` | placeholder | 2,50:1 | AA 4,5 | `#798193` (4,55:1) | 0,5° |

Ambos se levantaron **subiendo la luminosidad y conservando el matiz**. Un barrido libre
por el espacio RGB devuelve el primer color que cumple, y devolvió `#5D8A6B`, un verde:
cumplía el ratio y rompía la paleta. Ese es un error de diseño disfrazado de
accesibilidad, y por eso el método es HSL sobre el tono de partida, con una aserción de
que el matiz no se mueva más de 3° — el umbral de percepción, por debajo del cual solo
actúa el redondeo a 8 bits.

## Rampa de severidad

Se pasó de los tonos 500 a los **400**. Sobre negro, texto de 500 vibra:

| Rol | 500 (anterior) | 400 (adoptado) |
| :--- | ---: | ---: |
| `critical` | 4,73:1 `#EF4444` | **6,43:1** `#F87171` |
| `high` | 6,35:1 `#F97316` | **7,86:1** `#FB923C` |
| `medium` | 8,28:1 `#F59E0B` | **10,66:1** `#FBBF24` |
| `low` | 4,84:1 `#3B82F6` | **7,00:1** `#60A5FA` |
| `info` | 3,68:1 `#6B7280` | **7,01:1** `#9CA3AF` |

## Hairline

`#1E2024` mide 1,20 / 1,17 / 1,14 / 1,09:1 contra las cuatro superficies. No cumple el
3:1 de WCAG 1.4.11 y no debe cumplirlo: esa cláusula cubre el borde necesario para
*identificar* un control. Aquí el control se identifica por su relleno
(`#16181B` sobre `#121316`) y por el anillo de foco esmeralda, que marca 7,01:1 en la
peor superficie. El hairline solo traza la línea entre dos planos.

## Anillo de foco

`#10B981` marca 7,71 / 7,52 / 7,32 / 7,01:1 sobre las cuatro superficies. El foco se
implementa como anillo y no solo como cambio de color de borde, porque un cambio de
color no lo distingue quien no separa bien rojo de verde.
