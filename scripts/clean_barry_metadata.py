#!/usr/bin/env python3
"""Copy One Pace metadata, removing local library state without rewriting artwork."""

import argparse
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from xml.parsers import expat


# Only direct children of the NFO root are removed. In particular, everything
# inside <art> is retained, including absolute paths and release hash markers.
LOCAL_FIELDS = frozenset({
    "fileinfo", "lockdata", "lockedfields", "watched", "playcount",
    "lastplayed", "resume", "userrating",
})
ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def clean_nfo(data: bytes) -> tuple[bytes, Counter]:
    """Remove selected XML elements while preserving all other bytes verbatim."""
    data.decode("utf-8-sig")
    parser = expat.ParserCreate()
    depth = 0
    start = None
    spans = []
    removed = Counter()

    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace("-", "") not in {"utf8", "ascii"}:
            raise ValueError("NFO must use UTF-8 encoding")

    def element_start(name, attributes):
        nonlocal depth, start
        if depth == 0 and name not in {"tvshow", "season", "episodedetails"}:
            raise ValueError(f"Unsupported NFO root: {name}")
        if depth == 1 and name in LOCAL_FIELDS:
            start = parser.CurrentByteIndex
        depth += 1

    def element_end(name):
        nonlocal depth, start
        depth -= 1
        if depth != 1 or name not in LOCAL_FIELDS:
            return
        end = parser.CurrentByteIndex
        # For <tag/> Expat points after the element; for <tag>...</tag> it
        # points at the opening '<' of the closing tag.
        if data.startswith(b"</" + name.encode("ascii"), end):
            end = data.index(b">", end) + 1
        line_start = data.rfind(b"\n", 0, start) + 1
        if not data[line_start:start].strip(b" \t"):
            start = line_start
        following_whitespace = re.match(rb"[ \t]*(?:\r?\n|$)", data[end:])
        if following_whitespace:
            end += following_whitespace.end()
        spans.append((start, end))
        removed[name] += 1
        start = None

    parser.XmlDeclHandler = declaration
    parser.StartElementHandler = element_start
    parser.EndElementHandler = element_end
    parser.Parse(data, True)

    for start, end in reversed(spans):
        data = data[:start] + data[end:]
    return data, removed


def prepare(source: Path, destination: Path):
    """Validate every selected NFO before creating any output."""
    if not source.is_dir() or not (source / "tvshow.nfo").is_file():
        raise ValueError("Source must be the 'One Pace' directory containing tvshow.nfo")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must not overlap")
    if destination.exists():
        raise ValueError("Destination already exists; choose a new directory")

    planned = []
    removed = Counter()
    skipped = Counter()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if len(relative.parts) > 1 and not relative.parts[0].startswith("[One Pace]"):
            if path.is_file():
                skipped[relative.parts[0]] += 1
            continue
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported: {relative}")
        if not path.is_file():
            continue
        if path.suffix.lower() == ".nfo":
            try:
                cleaned, counts = clean_nfo(path.read_bytes())
            except (ValueError, expat.ExpatError) as error:
                raise ValueError(f"Invalid NFO {relative}: {error}") from error
            removed.update(counts)
            planned.append((relative, cleaned))
        elif path.suffix.lower() in ASSET_EXTENSIONS:
            planned.append((relative, None))
        else:
            skipped[relative.as_posix()] += 1
    return planned, removed, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Barry's One Pace directory (with tvshow.nfo)")
    parser.add_argument("destination", type=Path, help="New directory for cleaned metadata and artwork")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing files")
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    try:
        planned, removed, skipped = prepare(source, destination)
        if not args.dry_run:
            destination.mkdir(parents=True, exist_ok=False)
            for relative, cleaned in planned:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if cleaned is None:
                    shutil.copy2(source / relative, target)
                else:
                    target.write_bytes(cleaned)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    nfos = sum(cleaned is not None for _, cleaned in planned)
    action = "Dry run" if args.dry_run else "Copied"
    print(f"{action}: {nfos} NFO files and {len(planned) - nfos} artwork files")
    for tag, count in sorted(removed.items()):
        print(f"  {'Would remove' if args.dry_run else 'Removed'} <{tag}>: {count}")
    for name, count in sorted(skipped.items()):
        print(f"  Skipped {name}: {count} files")
    print("Preserved <art>, <originaltitle>, <dateadded>, filenames and content metadata.")
    print("Source files unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
