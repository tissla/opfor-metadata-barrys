#!/usr/bin/env python3

import argparse
import json
import re
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[1]
HEX_HASH = re.compile(r"[0-9A-Fa-f]{8}")
ID = re.compile(r"onepace:[a-z0-9-]+:[a-z0-9-]+(?::[a-z0-9-]+)*")
RANGES = r"\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*"
OVERRIDE_FIELDS = {"id", "episode_id", "variant", "aliases"}


def without_brackets(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", text)).strip()


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode()
    text = text.lower().replace("'", "")
    result = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if not result:
        raise ValueError(f"Cannot derive an identity from {text!r}")
    return result


def xml_root(path: Path, expected: str) -> ET.Element:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"Cannot read {path.name}: {error}") from error
    if root.tag != expected:
        raise ValueError(f"{path.name}: expected <{
                         expected}>, found <{root.tag}>")
    return root


def required_text(root: ET.Element, tag: str) -> str:
    value = (root.findtext(tag) or "").strip()
    if not value:
        raise ValueError(f"Missing <{tag}>")
    return value


def number(root: ET.Element, tag: str) -> int:
    value = required_text(root, tag)
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"Invalid <{tag}>: {value!r}")
    return int(value)


def release_hash(stem: str) -> str | None:
    hashes = {token.upper() for token in re.findall(r"\[([^\]]+)\]", stem)
              if HEX_HASH.fullmatch(token)}
    if len(hashes) > 1:
        raise ValueError(f"Conflicting release hashes in {stem!r}")
    return next(iter(hashes), None)


def validate_filename(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError(
            "Filename aliases must be nonempty strings without surrounding whitespace")
    if "/" in name or "\\" in name or PureWindowsPath(name).drive or name in {".", ".."}:
        raise ValueError(f"Expected a filename stem, not a path: {name!r}")
    if name.lower().endswith((".mkv", ".mp4", ".nfo")):
        raise ValueError(
            f"Filename aliases must omit the file extension: {name!r}")
    return name


def variant_of(stem: str) -> str:
    name = without_brackets(stem)
    if re.search(r"\bExtended$", name, re.I):
        return "extended"
    alternate = re.search(r"\bAlternate\b(.*)$", name, re.I)
    if alternate:
        return slug("alternate " + alternate[1])
    return "standard"


def source_episode(stem: str, fallback: int) -> int:
    name = without_brackets(stem)
    if re.match(r"Chapters?\b", name, re.I):
        # Dressrosa's older filenames contain only manga chapters.
        return fallback
    match = re.search(
        r"\b(\d+)(?:\s+(?:Extended|Alternate\b.*))?$", name, re.I)
    return int(match[1]) if match else fallback


def chapter_info(stem: str, root: ET.Element, arc: ET.Element) -> dict:
    candidates = []
    match = re.match(r"\[One Pace\]\[([^\]]+)\]", stem, re.I)
    if match:
        candidates.append((match[1], "filename"))
    match = re.match(
        rf"Chapters?\s+({RANGES})(?!\w)", without_brackets(stem), re.I)
    if match:
        candidates.append((match[1], "filename"))
    match = re.match(r"\[([^\]]+)\]", root.findtext("title") or "")
    if match:
        candidates.append((match[1], "title"))
    original = (root.findtext("originaltitle") or "").strip()
    # Anchoring excludes novel/theatre chapters, which are not manga coverage.
    match = re.match(rf"Chapters?\s+({RANGES})(?!\w)", original, re.I)
    if match:
        candidates.append((match[1], "originaltitle"))
    cover = any(re.search(r"\bCover(?:s| Story)?\b", value, re.I) for value in
                (stem, root.findtext("title") or "", arc.findtext("title") or "", original))
    for raw, source in candidates:
        raw = raw.replace("–", "-").replace("—", "-")
        raw = re.sub(r"\bCover(?:s| Story)?\b", "", raw, flags=re.I).strip()
        if not re.fullmatch(RANGES, raw):
            continue
        ranges = []
        for part in raw.split(","):
            ends = [int(n.strip()) for n in part.split("-")]
            start, end = ends[0], ends[-1]
            if start > end:
                break
            ranges.append([start, end])
        else:
            return {"kind": "cover_story" if cover else "manga", "ranges": ranges, "source": source}
    return {"kind": None, "ranges": [], "source": None}


def filenames_for(stem: str, root: ET.Element, overrides: dict) -> list[str]:
    names = {validate_filename(stem)}
    known_hash = release_hash(stem)
    for poster in root.findall("./art/poster"):
        value = (poster.text or "").strip()
        if not value or "://" in value:
            continue
        name = PureWindowsPath(value).name
        match = re.fullmatch(r"(.+)-thumb\.(?:png|jpe?g|webp)", name, re.I)
        if not match or match[1] == stem:
            continue
        alias = validate_filename(match[1])
        if not known_hash or release_hash(alias) != known_hash:
            raise ValueError(
                "poster alias differs from the NFO filename without an agreeing release hash; review the NFO")
        names.add(alias)
    aliases = overrides.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("Override aliases must be a list")
    for alias in aliases:
        names.add(validate_filename(alias))
    return sorted(names)


def build_index(library: Path, overrides: dict) -> dict:
    library = library.resolve()
    xml_root(library / "tvshow.nfo", "tvshow")
    if not isinstance(overrides, dict):
        raise ValueError(
            "Overrides must be an object keyed by relative NFO path")
    arcs, entries, by_filename, by_hash = {}, {}, {}, {}
    warnings = []
    seen_paths = set()

    def warning(code, path, message):
        warnings.append({"code": code, "path": path, "message": message})

    def insert_lookup(lookup, key, entry_id, kind):
        previous = lookup.get(key)
        if previous is not None and previous != entry_id:
            raise ValueError(f"Conflicting {kind} {key!r}: {
                             previous} and {entry_id}")
        lookup[key] = entry_id

    for directory in sorted(library.iterdir()):
        if not directory.name.startswith("[One Pace]") or not directory.is_dir():
            continue
        if directory.is_symlink():
            raise ValueError(f"Symlinked arc directory: {directory.name}")
        arc_path = directory / "season.nfo"
        if arc_path.is_symlink():
            raise ValueError(f"Symlinked season.nfo: {directory.name}")
        arc_root = xml_root(arc_path, "season")
        arc_id = slug(without_brackets(directory.name))
        arc_id = {"whiskey-peak": "whisky-peak",
                  "alabasta": "arabasta"}.get(arc_id, arc_id)
        if arc_id in arcs:
            raise ValueError(f"Duplicate arc identity: {arc_id}")
        kind = "special" if arc_id == "specials" else "episode"
        arcs[arc_id] = {
            "name": without_brackets(required_text(arc_root, "title")),
            "folder": directory.name,
            "season_number": number(arc_root, "seasonnumber"),
            "kind": kind,
        }
        for path in sorted(directory.glob("*.nfo")):
            if path.name == "season.nfo":
                continue
            relative = path.relative_to(library).as_posix()
            if path.is_symlink() or "\\" in relative:
                raise ValueError(f"NFO path is not portable: {relative}")
            seen_paths.add(relative)
            override = overrides.get(relative, {})
            try:
                if not isinstance(override, dict) or set(override) - OVERRIDE_FIELDS:
                    raise ValueError(f"Supported override fields: {
                                     ', '.join(sorted(OVERRIDE_FIELDS))}")
                root = xml_root(path, "episodedetails")
                episode = number(root, "episode")
                season = number(root, "season")
                ordinal = None if kind == "special" else source_episode(
                    path.stem, episode)
                special_name = re.sub(
                    r"\s+(?:Extended|Alternate\b.*)$", "", without_brackets(path.stem), flags=re.I)
                natural_key = slug(special_name) if kind == "special" else f"{
                    ordinal:03d}"
                episode_id = override.get("episode_id", f"onepace:{
                                          arc_id}:{natural_key}")
                variant = override.get("variant", variant_of(path.stem))
                if not isinstance(variant, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", variant):
                    raise ValueError("Variant must be a lowercase slug")
                entry_id = override.get("id", f"{episode_id}:{variant}")
                if not isinstance(episode_id, str) or not ID.fullmatch(episode_id) or not isinstance(entry_id, str) or not ID.fullmatch(entry_id):
                    raise ValueError(
                        "IDs must use onepace:<arc>:<episode>[:<variant>] with lowercase slugs")
                if entry_id in entries:
                    raise ValueError(f"Duplicate entry ID {
                                     entry_id}; review the source numbering or add an explicit ID override")
                filenames = filenames_for(path.stem, root, override)
                hashes = sorted(
                    {h for name in filenames if (h := release_hash(name))})
                entry = {
                    "episode_id": episode_id,
                    "arc_id": arc_id,
                    "kind": kind,
                    "variant": variant,
                    "title": required_text(root, "title"),
                    "originaltitle": (root.findtext("originaltitle") or "").strip(),
                    "season_number": season,
                    "episode_number": episode,
                    "source_episode": ordinal,
                    "nfo_path": relative,
                    "filenames": filenames,
                    "release_hashes": hashes,
                    "chapters": chapter_info(path.stem, root, arc_root),
                }
                for name in filenames:
                    insert_lookup(by_filename, name, entry_id, "filename")
                for h in hashes:
                    insert_lookup(by_hash, h, entry_id, "hash")
                entries[entry_id] = entry
                last = re.search(r"\[([0-9A-Fa-f]+)\]$", path.stem)
                if last and not HEX_HASH.fullmatch(last[1]):
                    warning("invalid_release_hash", relative, f"Ignored non-eight-digit hash marker [{
                            last[1]}]; exact filename matching remains available")
                elif not hashes:
                    warning("missing_release_hash", relative,
                            "No release hash; exact filename matching remains available")
                if season != arcs[arc_id]["season_number"]:
                    warning("season_number_mismatch", relative, f"Episode season {
                            season} differs from arc season {arcs[arc_id]['season_number']}; source values retained")
            except ValueError as error:
                raise ValueError(f"{relative}: {error}") from error

    if not entries:
        raise ValueError("No One Pace episode NFOs found")
    stale = set(overrides) - seen_paths
    if stale:
        raise ValueError(
            f"Overrides reference missing/excluded NFOs: {', '.join(sorted(stale))}")
    return {
        "schema_version": 2,
        "source": "barrys-one-pace",
        "arcs": arcs,
        "entries": entries,
        "by_filename": by_filename,
        "by_hash": by_hash,
        "warnings": sorted(warnings, key=lambda w: (w["path"], w["code"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=ROOT / "One Pace")
    parser.add_argument("--overrides", type=Path,
                        default=ROOT / "scripts" / "index_overrides.json")
    parser.add_argument("--check", action="store_true",
                        help="Fail if the committed index differs; never write")
    args = parser.parse_args()
    output = args.library / "metadata-index.json"
    temporary = None
    try:
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))
        index = build_index(args.library, overrides)
        data = (json.dumps(index, ensure_ascii=False, indent=2,
                sort_keys=True) + "\n").encode("utf-8")
        current = output.read_bytes() if output.exists() else None
        if args.check and current != data:
            raise ValueError(
                f"{output}: index is missing or stale; run scripts/build_metadata_index.py")
        if not args.check and current != data:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".metadata-index-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            temporary.chmod(0o644)
            temporary.replace(output)
        for warning in index["warnings"]:
            print(f"WARNING [{warning['code']}] {warning['path']}: {
                  warning['message']}", file=sys.stderr)
        print(f"{'Verified' if args.check else 'Built'} {output}: {len(index['entries'])} entries, "
              f"{len(index['arcs'])} arcs, {len(index['by_filename'])} filenames, {len(index['by_hash'])} hashes")
        return 0
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
