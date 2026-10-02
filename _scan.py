import io
R = "frontend/src/styles/console.css"
NL = "\r\n" if "\r\n" in io.open(R, encoding="utf-8", newline="").read() else "\n"
L = io.open(R, encoding="utf-8", newline="").read().replace("\r\n", "\n").split("\n")
for n, l in enumerate(L, 1):
    raros = [c for c in l if 0x4e00 <= ord(c) <= 0x9fff or 0x3040 <= ord(c) <= 0x30ff
             or 0xac00 <= ord(c) <= 0xd7af or c == "\ufffd"]
    if raros:
        print("%5d  %r" % (n, l.strip()))
        print("      raros:", "".join(raros))
