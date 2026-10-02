import io, re
R = "frontend/src/features/admin/AdminPricingPage.tsx"
t = io.open(R, encoding="utf-8", newline="").read()
# Cada `<input ... />` con su clase, para ver cuales se quedan sin `.text-input`.
for m in re.finditer(r"<input\b[^>]*?/?>", t, re.S):
    frag = " ".join(m.group(0).split())
    linea = t[:m.start()].count("\n") + 1
    tiene = "text-input" in frag
    print("  L%-5d %-14s %s" % (linea, "CON clase" if tiene else "SIN text-input", frag[:96]))
