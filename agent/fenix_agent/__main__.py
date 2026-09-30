"""Ejecuta el agente: `python -m fenix_agent --config <ruta>`."""

from __future__ import annotations

import sys

from fenix_agent.bucle import main

if __name__ == "__main__":
    # `raise SystemExit(main())` en vez de `main()` a secas, porque el código de salida del
    # proceso **es** la señal que usa `systemd` para decidir si hay que reiniciar. Un agente que
    # termina con 0 porque el token estaba revocado parecería sano.
    sys.exit(main())
