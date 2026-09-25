"""Parser y aplicador mínimo de parches unified diff para autofix."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


class AutofixPatchError(ValueError):
    """Patch de remediación inválido o inseguro."""


@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatchFile:
    path: str
    hunks: tuple[PatchHunk, ...]
    is_binary: bool = False
    is_delete: bool = False


@dataclass(frozen=True, slots=True)
class PatchChange:
    path: str
    content: str | None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def validate_patch_path(path: str) -> str:
    """Acepta solo rutas POSIX relativas y sin traversal."""

    if not path or "\x00" in path or "\\" in path:
        raise AutofixPatchError("El patch contiene una ruta insegura")
    pure_path = PurePosixPath(path)
    if (
        pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or pure_path.as_posix() != path
    ):
        raise AutofixPatchError("El patch contiene una ruta insegura")
    return path


def _strip_prefix(path: str) -> str:
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return validate_patch_path(path[2:])
    raise AutofixPatchError("El patch contiene una ruta no soportada")


def parse_patch(patch: str) -> tuple[PatchFile, ...]:
    """Parsea hunks sin ejecutar código ni invocar un shell."""

    if not isinstance(patch, str) or not patch.strip() or "\x00" in patch:
        raise AutofixPatchError("El diff de autofix está vacío o corrupto")
    lines = patch.replace("\r\n", "\n").splitlines()
    files: list[PatchFile] = []
    current_path: str | None = None
    current_hunks: list[PatchHunk] = []
    current_lines: list[str] | None = None
    old_start = new_start = old_count = new_count = 0
    binary = False
    delete_file = False

    def finish_hunk() -> None:
        nonlocal current_lines
        if current_lines is None:
            return
        current_hunks.append(
            PatchHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(current_lines),
            )
        )
        current_lines = None

    def finish_file() -> None:
        nonlocal current_path, current_hunks, binary, delete_file
        finish_hunk()
        if current_path is not None:
            if not current_hunks and not binary:
                raise AutofixPatchError("El diff no contiene hunks aplicables")
            files.append(
                PatchFile(
                    current_path,
                    tuple(current_hunks),
                    binary,
                    delete_file,
                )
            )
        current_path = None
        current_hunks = []
        binary = False
        delete_file = False

    for line in lines:
        if line.startswith("diff --git "):
            finish_file()
            header = line.removeprefix("diff --git ")
            if " b/" not in header:
                raise AutofixPatchError("El encabezado del diff es inválido")
            _old_header, new_header = header.rsplit(" b/", 1)
            current_path = _strip_prefix(f"b/{new_header}")
            continue
        if line.startswith("GIT binary patch"):
            binary = True
            continue
        if line.startswith("--- "):
            finish_hunk()
            header_path = _strip_prefix(line[4:].split("\t", 1)[0])
            if current_path is None:
                current_path = header_path
            continue
        if line.startswith("+++ "):
            new_path = line[4:].split("\t", 1)[0]
            if new_path == "/dev/null":
                delete_file = True
            else:
                new_path = _strip_prefix(new_path)
                if current_path is None:
                    current_path = new_path
            continue
        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            finish_hunk()
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2) or "1")
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4) or "1")
            current_lines = []
            continue
        if current_lines is not None:
            if line.startswith("\\ No newline at end of file"):
                continue
            if not line or line[0] not in " +-":
                raise AutofixPatchError("El diff contiene una línea no soportada")
            current_lines.append(line)
    finish_file()
    if not files:
        raise AutofixPatchError("El diff no contiene archivos")
    paths = [file.path for file in files]
    if len(paths) != len(set(paths)):
        raise AutofixPatchError("El diff contiene archivos duplicados")
    return tuple(files)


def _line_content(line: str) -> str:
    return line[1:]


def _with_newline(content: str) -> str:
    return content if not content or content.endswith("\n") else f"{content}\n"


def apply_patch(original_files: dict[str, str | None], patch: str) -> tuple[PatchChange, ...]:
    """Aplica un diff a contenidos base mapeados por ruta."""

    parsed_files = parse_patch(patch)
    changes: list[PatchChange] = []
    for patch_file in parsed_files:
        if patch_file.is_binary:
            raise AutofixPatchError("Los parches binarios no están soportados")
        original = original_files.get(patch_file.path)
        if patch_file.is_delete:
            if patch_file.path not in original_files:
                raise AutofixPatchError("El diff intenta borrar un archivo inexistente")
            changes.append(PatchChange(patch_file.path, None))
            continue
        if original is None:
            original = ""
        original_lines = original.splitlines(keepends=True)
        output: list[str] = []
        cursor = 0
        for hunk in patch_file.hunks:
            start = max(hunk.old_start - 1, 0)
            if start < cursor or start > len(original_lines):
                raise AutofixPatchError("El diff tiene hunks fuera de rango")
            output.extend(original_lines[cursor:start])
            cursor = start
            for line in hunk.lines:
                if not line:
                    raise AutofixPatchError("El diff contiene una línea vacía")
                marker, content = line[0], _line_content(line)
                if marker == " ":
                    if cursor >= len(original_lines) or original_lines[cursor].rstrip(
                        "\r\n"
                    ) != content:
                        raise AutofixPatchError("El diff no aplica sobre el archivo base")
                    output.append(original_lines[cursor])
                    cursor += 1
                elif marker == "-":
                    if cursor >= len(original_lines) or original_lines[cursor].rstrip(
                        "\r\n"
                    ) != content:
                        raise AutofixPatchError("El diff no aplica sobre el archivo base")
                    cursor += 1
                elif marker == "+":
                    output.append(_with_newline(content))
                else:
                    raise AutofixPatchError("El diff contiene una operación no soportada")
            expected_old_end = start + hunk.old_count
            if cursor != expected_old_end:
                raise AutofixPatchError("El diff tiene un número de líneas inconsistente")
        output.extend(original_lines[cursor:])
        changes.append(PatchChange(patch_file.path, "".join(output)))
    return tuple(changes)
