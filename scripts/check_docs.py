"""Check repository text hygiene, Markdown links, and hidden content in files
an agent or a hook reads as instructions."""

import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
AGENT_FILES = ("CLAUDE.md",)
# Files an agent or CI executes or reads as instructions, anywhere in the
# tree. Hidden characters and comments here are invisible in review but read
# by the model, so every file under these paths is scanned whatever its name.
INSTRUCTION_DIRS = (".claude", ".codex", ".gemini", ".github/workflows", ".githooks")
INSTRUCTION_NAMES = AGENT_FILES + ("AGENTS.md", "GEMINI.md", ".mcp.json")
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".sh",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".ts",
    ".tsx",
    ".jsx",
    ".js",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".astro",
    ".css",
    ".html",
    ".xml",
    ".plist",
    ".kt",
    ".kts",
    ".gradle",
    ".swift",
    ".dart",
    ".sql",
    ".txt",
    ".markdown",
    ".mdx",
}
SPECIAL_NAMES = {"justfile", ".editorconfig", ".gitignore", ".gitattributes"}
MARKDOWN = (".md", ".markdown", ".mdx")
# Unicode that can hide or reorder text: controls, format characters (bidi,
# zero-width, joiners, tags), private use, surrogates, unassigned, line and
# paragraph separators, variation selectors, and the wide invisible fillers.
HIDDEN_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"}
HIDDEN_POINTS = {0x00A0, 0x115F, 0x1160, 0x3164, 0xFFA0}
HIDDEN_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))
ALLOWED_CONTROLS = {"\t", "\n"}
ALLOWED_COMMENTS = {"<!-- shared-core:begin -->", "<!-- shared-core:end -->"}
# Every link-reference definition contains "]:" and renders as nothing, in any
# nesting or wrapping CommonMark allows, so the bare token is refused.
REFERENCE_COMMENT = re.compile(r"\]:")
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/_-]{76,}")


def is_hidden(ch: str) -> bool:
    """True for a character that can hide, reorder or smuggle text."""
    if ch in ALLOWED_CONTROLS:
        return False
    code = ord(ch)
    if code in HIDDEN_POINTS or any(a <= code <= b for a, b in HIDDEN_RANGES):
        return True
    return unicodedata.category(ch) in HIDDEN_CATEGORIES


def is_instruction_file(name: str) -> bool:
    """Agent files by basename, .mcp.json, and anything under an instruction
    directory at any depth (agents load nested .claude/ folders too)."""
    lowered = name.lower()
    base = lowered.rsplit("/", 1)[-1]
    padded = "/" + lowered + "/"
    return base in {n.lower() for n in INSTRUCTION_NAMES} or any(
        "/" + d + "/" in padded for d in INSTRUCTION_DIRS
    )


def check_hidden_characters(name: str, content: str, errors: list[str]) -> None:
    """Refuse hidden Unicode in any scanned file. Split on newline only:
    splitlines() would swallow form feeds and Unicode line separators."""
    for number, line in enumerate(content.split("\n"), 1):
        bad = sorted({f"U+{ord(c):04X}" for c in line if is_hidden(c)})
        if bad:
            errors.append(f"{name}:{number}: hidden character {' '.join(bad)}")


def check_instruction_content(name: str, content: str, errors: list[str]) -> None:
    """Refuse comments and blobs that could carry instructions a reviewer never sees."""
    for number, line in enumerate(content.splitlines(), 1):
        if BASE64_BLOB.search(line):
            errors.append(f"{name}:{number}: long base64-like blob")
        if name.lower().endswith(MARKDOWN) and REFERENCE_COMMENT.search(line):
            errors.append(f"{name}:{number}: reference-style comment")
    stripped = content
    for allowed in ALLOWED_COMMENTS:
        stripped = stripped.replace(allowed, "")
    if "<!--" in stripped:
        errors.append(f"{name}: HTML comment outside the shared-core markers")


def main() -> int:
    """Validate tracked and non-ignored new text files, returning a process status."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    names = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    errors: list[str] = []
    checked = 0
    for name in names:
        path = ROOT / name
        instruction = is_instruction_file(name)
        if path.is_symlink():
            if instruction:
                errors.append(f"{name}: symlink in an instruction path")
            continue
        if not path.exists():
            continue
        is_text = (
            path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in SPECIAL_NAMES
        )
        if not (is_text or instruction):
            continue
        checked += 1
        try:
            content = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{name}: must be UTF-8")
            continue
        if content and not content.endswith("\n"):
            errors.append(f"{name}: missing final newline")
        if "\r" in content:
            errors.append(f"{name}: use LF line endings")
        for number, line in enumerate(content.splitlines(), 1):
            if line.rstrip() != line:
                errors.append(f"{name}:{number}: trailing whitespace")
        check_hidden_characters(name, content, errors)
        if path.suffix.lower() in MARKDOWN:
            for target in re.findall(r"\]\(([^\s)]+)\)", content):
                if ":" in target or target.startswith(("#", "/")):
                    continue
                relative = unquote(target.split("#", 1)[0])
                if relative and not (path.parent / relative).exists():
                    errors.append(f"{name}: missing linked file {relative}")
        if instruction:
            check_instruction_content(name, content, errors)
    for error in errors:
        print(error)
    print(f"Checked {checked} text files; {len(errors)} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
