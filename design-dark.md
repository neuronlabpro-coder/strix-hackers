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

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EDEDED`):** Headlines and core text.
- **Secondary (`#8A8F8A`):** Borders, captions, and metadata.
- **Tertiary (`#17a163`):** The sole driver for interaction. Reserve it.
- **Neutral (`#1C1C1C`):** The page foundation.

## Typography

- **display:** Inter 4.5rem
- **h1:** Inter 2.2rem
- **body:** Inter 0.96rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
