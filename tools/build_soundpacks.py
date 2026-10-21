"""Build deterministic ZIPs from tracked audio and refresh the static catalog."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from online_packs import AUDIO_EXTENSIONS, parse_catalog, safe_component, safe_relative


def main():
    definitions = json.loads((ROOT / "soundpacks" / "packs.json").read_text(encoding="utf-8"))
    raw = subprocess.check_output(["git", "-c", "safe.directory=" + ROOT.as_posix(),
                                   "ls-files", "-z", "music"], cwd=ROOT)
    tracked = [Path(p) for p in raw.decode("utf-8").split("\0") if p]
    output = ROOT / "dist" / "soundpacks"
    output.mkdir(parents=True, exist_ok=True)
    catalog_path = ROOT / "soundpacks" / "catalog.json"
    old = {pack.id: pack for pack in parse_catalog(catalog_path.read_bytes())} if catalog_path.exists() else {}
    entries = []
    for definition in definitions:
        pack_id = safe_component(definition["id"])
        version = safe_component(definition["version"])
        folder = Path("music") / safe_component(definition["name"])
        files = sorted((p for p in tracked if p.is_relative_to(folder)
                        and p.suffix.lower() in AUDIO_EXTENSIONS), key=lambda p: p.as_posix())
        if not files:
            raise ValueError(f"No tracked audio in {folder}")
        filename = f"{pack_id}-{version}.zip"
        destination = output / filename
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                relative = str(safe_relative(path.relative_to(folder).as_posix()))
                info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (ROOT / path).read_bytes())
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        previous = old.get(pack_id)
        if previous and previous.version == version and previous.sha256 != digest:
            raise ValueError(f"Bump the version of {pack_id} before changing its files")
        entries.append({**definition,
            "url": "https://github.com/DuskOri/Game-Soundboard/releases/download/soundpacks-v1/" + filename,
            "size": destination.stat().st_size, "sha256": digest, "file_count": len(files)})
    data = json.dumps({"schema_version": 1, "packs": entries}, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    parse_catalog(data)
    catalog_path.write_bytes(data)
    print(json.dumps({"catalog": str(catalog_path), "assets": [Path(e["url"]).name for e in entries],
                      "audio_files": sum(e["file_count"] for e in entries)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
