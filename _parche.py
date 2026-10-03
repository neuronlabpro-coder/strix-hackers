import pathlib

RAIZ = pathlib.Path(__file__).resolve().parent
objetivo = RAIZ / "backend" / "apps" / "repositories" / "router.py"
texto = objetivo.read_text(encoding="utf-8")

INICIO = "async def _exigir_credencial_vigente("
FIN = "\n\n@asynccontextmanager\nasync def _open_client("

i = texto.index(INICIO)
j = texto.index(FIN, i)
viejo = texto[i:j]
print("=== VIEJO ===")
print(viejo)

nuevo = pathlib.Path(RAIZ / "_parche_router.py.txt").read_text(encoding="utf-8")
objetivo.write_text(texto[:i] + nuevo.rstrip("\n") + texto[j:], encoding="utf-8")
print("=== ESCRITO ===")
