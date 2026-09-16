from __future__ import annotations

import re
from pathlib import Path

MAX_FILE_SIZE = 2**63 - 1
SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
TEXT_SUFFIXES = frozenset(
    {
        ".adoc",
        ".bash",
        ".bat",
        ".bib",
        ".c",
        ".cc",
        ".cfg",
        ".cjs",
        ".cmake",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".cxx",
        ".dart",
        ".fish",
        ".fs",
        ".fsx",
        ".go",
        ".gql",
        ".gradle",
        ".graphql",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".ipynb",
        ".java",
        ".jl",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".kt",
        ".kts",
        ".less",
        ".log",
        ".lua",
        ".markdown",
        ".md",
        ".mdx",
        ".mjs",
        ".ndjson",
        ".php",
        ".properties",
        ".proto",
        ".ps1",
        ".py",
        ".pyi",
        ".r",
        ".rb",
        ".rs",
        ".rst",
        ".sass",
        ".scss",
        ".sh",
        ".sql",
        ".svelte",
        ".svg",
        ".swift",
        ".tex",
        ".text",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".vb",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
TEXT_NAMES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".env",
        ".gitattributes",
        ".gitignore",
        ".gitmodules",
        ".npmrc",
        "authors",
        "cargo.lock",
        "changelog",
        "cmakelists.txt",
        "containerfile",
        "copying",
        "dockerfile",
        "gnumakefile",
        "license",
        "makefile",
        "poetry.lock",
        "readme",
        "uv.lock",
    }
)


def is_text_name(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name in TEXT_NAMES
        or name.startswith(".env.")
        or path.suffix.casefold() in TEXT_SUFFIXES
    )


def parse_file_size(value: object) -> int:
    if type(value) is int:
        if 0 <= value <= MAX_FILE_SIZE:
            return value
        raise ValueError("file size must be between 0 and 2^63-1 bytes")
    if not isinstance(value, str):
        raise TypeError("file size must be a byte integer or a string with a size unit")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)", value.strip())
    if match is None or match[2].casefold() not in SIZE_UNITS:
        raise ValueError("file size requires B, KB/MB/GB/TB or KiB/MiB/GiB/TiB")
    multiplier = SIZE_UNITS[match[2].casefold()]
    integer, _, fraction = match[1].partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if len(integer) > 19:
        raise ValueError("file size must be between 0 and 2^63-1 bytes")
    # TiB (2^40) needs at most 40 decimal places for an integral byte result;
    # all other supported units need fewer. Bound arithmetic after stripping
    # insignificant zeros, preserving legal arbitrarily padded input.
    if len(fraction) > 40:
        raise ValueError("file size must resolve to a whole number of bytes")
    size, remainder = divmod(int(integer + fraction) * multiplier, 10 ** len(fraction))
    if size > MAX_FILE_SIZE:
        raise ValueError("file size must be between 0 and 2^63-1 bytes")
    if remainder:
        raise ValueError("file size must resolve to a whole number of bytes")
    return size


IMAGE_SUFFIXES = frozenset(
    [
        ".jpg",
        ".jpeg",
        ".jfif",
        ".png",
        ".apng",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".avif",
        ".heic",
        ".heif",
        ".jxl",
        ".svg",
    ]
)
VIDEO_SUFFIXES = frozenset(
    [
        ".mp4",
        ".m4v",
        ".mkv",
        ".mov",
        ".avi",
        ".webm",
        ".mpg",
        ".mpeg",
        ".wmv",
        ".flv",
        ".3gp",
        ".3g2",
        ".mts",
        ".m2ts",
        ".vob",
        ".ogv",
    ]
)


def is_image_name(path: Path) -> bool:
    return path.suffix.casefold() in IMAGE_SUFFIXES


def is_video_name(path: Path) -> bool:
    return path.suffix.casefold() in VIDEO_SUFFIXES
