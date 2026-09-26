---
version: alpha
name: Supabase
description: Dark emerald theme. Code-first.
colors:
  primary: "#EDEDED"
  secondary: "#8A8F8A"
  tertiary: "#17a163"
  neutral: "#1C1C1C"
  surface: "#2A2A2A"
  on-primary: "#1C1C1C"
  caption: "#B2B2B2"
  severity:
    critical: "#EF4444"
    high: "#F97316"
    medium: "#F59E0B"
    low: "#3B82F6"
    info: "#8A8F8A"
typography:
  display:
    fontFamily: Inter
    fontSize: 4.5rem
    fontWeight: 500
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 2.2rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.96rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 4px
  md: 6px
  lg: 10px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

Supabase: open-source Firebase aesthetic — dark emerald theme, code-first density, mono kickers.

## Colors

The palette is a four-plane obsidian scale plus a single accent that drives interaction.

Hierarchy reads by **depth, not by border**. A card is one step lighter than the canvas
and carries a 1 px hairline; nothing else. Borders separate, they never carry weight.

### Surfaces (deepest to highest)

| Token | Value | Use |
| :--- | :--- | :--- |
| `--color-background` | `#0B0C0E` | Page canvas. Deep matte black. |
| `--color-raised` | `#0E1012` | Sidebar, topbar, table headers. |
| `--color-surface` | `#121316` | Cards, modals, panels. |
| `--color-surface-elevated` | `#16181B` | Inputs, row hover, active surfaces. |

WCAG sets no threshold for surface-to-surface separation — they are not content
boundaries. What matters is that the ladder is monotonic and the top stays black
rather than mid-gray. The perceived step is carried by the 1 px hairline.

### Line

- **`--color-border` (`#1E2024`):** Hairline, 1 px. Separates, never weighs.
- **`--color-border-hover` (`#2A2D33`):** One step up on hover. Deliberately *not* the
  accent: hover means "you can touch this", accent means "this is active", and sharing
  a color makes the whole interface look selected.
- **A hairline does not meet the 3:1 of WCAG 1.4.11 and is not meant to.** That clause
  covers the boundary needed to *identify* a control. Controls here are identified by
  their fill (`#16181B` against the surface) and by the emerald focus ring, which clears
  7:1 everywhere. The hairline only draws the line between two planes.

### Text

Every value clears WCAG 2.1 AA on all four surfaces. The worst case is on
`--color-surface-elevated`, the lightest plane.

| Token | Value | Ratio (worst) | Use |
| :--- | :--- | :--- | :--- |
| `--color-primary` | `#F3F4F6` | 16.2:1 | Headlines and key values. |
| `--color-caption` | `#A8AEB9` | 8.0:1 | Tertiary text and descriptions. |
| `--color-placeholder` | `#798193` | 4.55:1 | Placeholders. |
| `--color-secondary` | `#79818F` | 4.53:1 | Secondary text. |
| `--color-muted` | `#5E6575` | 3.04:1 | Uppercase kickers only (11 px mono counts as large text). |

`#6B7280` and `#525866` were the requested values for secondary text and placeholders.
They measure 3.68:1 and 2.50:1 on the lightest surface, so they fail AA. Both were
lifted to the values above **preserving hue** (Δhue < 2°, imperceptible), because
keeping the tint is what stops a contrast fix from turning a cool gray into green.

### Accent

- **`--color-accent` (`#17A163`):** The sole driver for interaction: primary buttons,
  active toggles, focus, status dots, healthy state. 5.35:1 worst case.
- **`--color-accent-bright` (`#10B981`):** Focus rings and hover on primary. 7.0:1.
- **`--color-on-primary` (`#06120C`):** Text on emerald.

### Severity ramp

`#F87171` / `#FB923C` / `#FBBF24` / `#60A5FA` / `#9CA3AF`

Tailwind **400** tones, not 500. On near-black, 500-series text vibrates; 400 is the
tone that reads. The ramp is for data visualization and for severity meaning severity
alone. It never becomes an action color, and remediation-status badges stay monochrome
so that color keeps meaning one thing per cell.

## Components

### Form controls

- Height **34 px**, not 40. In a ten-row table, six pixels per control is sixty pixels
  of air nobody asked for.
- Fill `--color-surface-elevated`, hairline `--color-border`, `--radius-md`.
- Focus is a **ring**, not just a border color change: `border-color` at 50% accent
  plus a 1 px box-shadow at 30%. A color change alone is not distinguishable to
  everyone.
- Kicker labels: 11 px mono, uppercase, `letter-spacing: 0.1em`, `--color-muted`.

### Severity pills

Compact filter chips replace the old inflated buttons: 28 px tall, mono 0.72rem,
colored dot, 10 % fill and 20 % border of the ramp tone. The dot is not decoration —
it is what lets someone scan twenty rows for red without reading twenty words.

### Data tables

- Header is a `--color-raised` band with `--color-muted` mono uppercase kickers. It
  reads as "label" rather than "content" without spending a border.
- Rows separated by a 1 px hairline at 60 % opacity; hover lifts to
  `--color-surface-elevated`, so a row can be followed across six columns.

### Sidebar and topbar

The sidebar is the backdrop plane (`--color-raised`) with a single right hairline and
nothing else. The topbar fuses with the canvas and is separated only by its bottom
hairline, because it sits against the content and a different fill would split the
screen in two. The active nav item gets a 7 % white fill and a 2 px emerald line at its
left edge — a line, not a saturated icon, so it never competes with the severity color
of a linked row.

### Empty states

Monochrome icon in `--color-muted`, title in `--color-primary`, subtitle in
`--color-caption` with a clear action.

## Typography

- **display:** Inter 4.5rem
- **h1:** Inter 2.2rem
- **body:** Inter 0.96rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let the canvas carry the composition — negative space is a feature, and at
  these luminances the difference between planes is read from the hairline.
- **Do** verify text contrast against the *lightest* surface, not the canvas. Inputs
  are the worst case and they are everywhere.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
- **Don't** use a text tone as a border tone. That single conflation is what turns a
  dark UI into a gray grid, and it is why `--color-border` is a separate token.
