"""Static sound-pack catalog, verified downloads and non-destructive installation."""

from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile


CATALOG_URL = "https://raw.githubusercontent.com/DuskOri/Game-Soundboard/main/soundpacks/catalog.json"
AUDIO_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus")
METADATA_NAME = ".soundboard-pack.json"
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_FILES = 10000
MAX_CATALOG = 1024 * 1024
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


class PackError(Exception):
    pass


class DownloadCancelled(PackError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("已取消下载")


def safe_component(value):
    if not isinstance(value, str) or not value or len(value) > 150:
        raise PackError("音效包文件名无效")
    if value in (".", "..") or value.endswith((".", " ")):
        raise PackError("音效包文件名无效")
    if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in value):
        raise PackError("音效包包含不支持的文件名")
    if value.split(".", 1)[0].rstrip(" ").upper() in RESERVED_NAMES:
        raise PackError("音效包包含系统保留的文件名")
    return value


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise PackError("音效包路径无效")
    parts = value.split("/")
    for part in parts:
        safe_component(part)
    return PurePosixPath(*parts)


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise PackError("下载地址无效")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise PackError("下载地址必须使用 HTTPS")
    return value


@dataclass(frozen=True)
class SoundPack:
    id: str
    name: str
    title: str
    version: str
    description: str
    url: str
    size: int
    sha256: str
    file_count: int


def parse_catalog(data):
    if len(data) > MAX_CATALOG:
        raise PackError("在线清单过大")
    try:
        catalog = json.loads(data)
        if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
            raise ValueError()
        entries = catalog["packs"]
        if not isinstance(entries, list) or len(entries) > 500:
            raise ValueError()
        packs = []
        ids, names = set(), set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError()
            pack_id = item["id"]
            if not isinstance(pack_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", pack_id):
                raise ValueError()
            name = safe_component(item["name"])
            title, version = item.get("title", name), item["version"]
            description = item.get("description", "")
            if not all(isinstance(value, str) for value in (title, version, description)):
                raise ValueError()
            if not title or len(title) > 100 or not version or len(version) > 50 or len(description) > 2000:
                raise ValueError()
            size, count = item["size"], item["file_count"]
            if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
                raise ValueError()
            if type(count) is not int or not 0 < count <= MAX_FILES:
                raise ValueError()
            digest = item["sha256"]
            if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest):
                raise ValueError()
            if pack_id in ids or name.casefold() in names:
                raise ValueError()
            ids.add(pack_id)
            names.add(name.casefold())
            packs.append(SoundPack(pack_id, name, title, version, description,
                                   https_url(item["url"]), size, digest, count))
        return packs
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise PackError("在线音效包清单格式无效") from exc


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_https(url):
    https_url(url)
    request = urllib.request.Request(url, headers={
        "User-Agent": "GameSoundboard-SoundPacks/1.0",
        "Accept-Encoding": "identity",
    })
    return urllib.request.build_opener(HTTPSRedirectHandler()).open(request, timeout=20)


def network_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"下载服务返回 HTTP {exc.code}，请稍后重试"
    return "网络连接失败，请检查网络后重试"


def fetch_catalog(url=CATALOG_URL, *, opener=open_https, cancel=None):
    check_cancel(cancel)
    try:
        with opener(https_url(url)) as response:
            data = response.read(MAX_CATALOG + 1)
        check_cancel(cancel)
        return parse_catalog(data), data
    except (urllib.error.URLError, OSError) as exc:
        raise PackError(network_error(exc)) from exc


def cached_catalog(cache, bundled):
    for path in (cache, bundled):
        try:
            return parse_catalog(Path(path).read_bytes())
        except (OSError, PackError):
            continue
    return []


def save_catalog(cache, data):
    parse_catalog(data)
    cache = Path(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, cache)


def download_pack(pack, destination, *, progress=None, cancel=None, opener=open_https):
    destination = Path(destination)
    try:
        for attempt in range(3):
            check_cancel(cancel)
            digest, downloaded = hashlib.sha256(), 0
            try:
                if progress:
                    progress(0, pack.size)
                with opener(https_url(pack.url)) as response, destination.open("wb") as output:
                    while True:
                        check_cancel(cancel)
                        block = response.read(128 * 1024)
                        if not block:
                            break
                        downloaded += len(block)
                        if downloaded > pack.size or downloaded > MAX_DOWNLOAD:
                            raise PackError("下载文件大小与清单不一致，请刷新清单")
                        output.write(block)
                        digest.update(block)
                        if progress:
                            progress(downloaded, pack.size)
                if downloaded != pack.size:
                    raise OSError("incomplete download")
                if digest.hexdigest() != pack.sha256:
                    raise PackError("下载文件校验失败，未安装任何文件；请刷新后重试")
                check_cancel(cancel)
                return
            except (urllib.error.URLError, OSError, http.client.IncompleteRead) as exc:
                # Missing permissions and a full disk are not network failures.
                if isinstance(exc, OSError) and exc.errno in (13, 28):
                    raise PackError("无法保存下载文件，请检查目录权限和磁盘空间") from exc
                if attempt == 2:
                    raise PackError(network_error(exc)) from exc
                if cancel is not None:
                    if cancel.wait(attempt + 1):
                        check_cancel(cancel)
                else:
                    threading.Event().wait(attempt + 1)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inside(root, path):
    root, path = Path(root).resolve(), Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise PackError("音效包目录包含不安全的链接")
    return path


def installed_metadata(directory, pack_id):
    try:
        path = Path(directory) / METADATA_NAME
        if path.stat().st_size > MAX_CATALOG:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") != pack_id or not isinstance(data.get("files"), dict) or not 0 < len(data["files"]) <= MAX_FILES:
            return {}
        if not isinstance(data.get("version"), str) or not 0 < len(data["version"]) <= 50:
            return {}
        for key, value in data["files"].items():
            safe_relative(key)
            safe_relative(value["path"])
            if PurePosixPath(value["path"]).suffix.lower() not in AUDIO_EXTENSIONS:
                return {}
            if not re.fullmatch("[a-f0-9]{64}", value["sha256"]):
                return {}
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError, PackError):
        return {}


def pack_state(pack, library):
    directory = Path(library) / pack.name
    metadata = installed_metadata(directory, pack.id)
    if metadata:
        if any(not (directory / value["path"]).is_file() for value in metadata["files"].values()):
            return "需修复"
        installed_version = metadata.get("version", "")
        if installed_version == pack.version:
            return "已安装"
        if re.fullmatch(r"\d+(?:\.\d+)*", installed_version) and re.fullmatch(r"\d+(?:\.\d+)*", pack.version):
            installed_numbers = tuple(int(part) for part in installed_version.split("."))
            online_numbers = tuple(int(part) for part in pack.version.split("."))
            width = max(len(installed_numbers), len(online_numbers))
            if installed_numbers + (0,) * (width - len(installed_numbers)) >= online_numbers + (0,) * (width - len(online_numbers)):
                return "已安装"
        return "可更新"
    return "本地已有" if directory.exists() else "未安装"


def inspect_archive(archive, pack):
    entries, seen, total = [], set(), 0
    infos = archive.infolist()
    if len(infos) > MAX_FILES * 2:
        raise PackError("音效包文件数量超过限制")
    for info in infos:
        if info.orig_filename != info.filename:
            raise PackError("音效包包含被系统改写的文件路径")
        relative = safe_relative(info.filename.rstrip("/") if info.is_dir() else info.filename)
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
            raise PackError("音效包包含不支持的文件类型")
        if info.is_dir():
            continue
        if relative.suffix.lower() not in AUDIO_EXTENSIONS:
            raise PackError("音效包只允许包含支持的音频文件")
        key = str(relative).casefold()
        if key in seen:
            raise PackError("音效包包含重复文件名")
        seen.add(key)
        total += info.file_size
        if total > MAX_EXPANDED:
            raise PackError("音效包解压后的大小超过限制")
        entries.append((info, relative))
    if len(entries) != pack.file_count:
        raise PackError("音效数量与在线清单不一致")
    return entries


def unique_destination(path, reserved):
    candidate, number = path, 1
    while candidate.exists() or str(candidate).casefold() in reserved:
        suffix = " (在线)" if number == 1 else f" (在线{number})"
        candidate = path.with_name(path.stem + suffix + path.suffix)
        number += 1
    return candidate


def install_pack(pack, archive_path, library, *, cancel=None):
    library = Path(library)
    library.mkdir(parents=True, exist_ok=True)
    directory = inside(library, library / pack.name)
    if directory.exists() and not directory.is_dir():
        raise PackError("同名路径不是语音包文件夹")
    old = installed_metadata(directory, pack.id)
    created_directory = not directory.exists()
    # Staging sits outside music/, so refreshing the library never shows it.
    with tempfile.TemporaryDirectory(prefix=".soundpack-", dir=library.parent) as temp:
        staging = Path(temp)
        with zipfile.ZipFile(archive_path) as archive:
            entries = inspect_archive(archive, pack)
            for index, (info, relative) in enumerate(entries):
                check_cancel(cancel)
                with archive.open(info) as source, (staging / str(index)).open("wb") as output:
                    shutil.copyfileobj(source, output, length=128 * 1024)
        check_cancel(cancel)
        plan, files, reserved, preserved = [], {}, set(), 0
        for index, (_info, relative) in enumerate(entries):
            source = staging / str(index)
            digest = sha256_file(source)
            previous = old.get("files", {}).get(str(relative), {})
            target = inside(directory, directory / previous.get("path", str(relative)))
            replaceable = False
            if target.exists():
                if not target.is_file():
                    target = unique_destination(directory / relative, reserved)
                    preserved += 1
                else:
                    existing_digest = sha256_file(target)
                    if existing_digest == digest:
                        source = None
                    elif previous and existing_digest == previous.get("sha256"):
                        replaceable = True
                    else:
                        target = unique_destination(directory / relative, reserved)
                        preserved += 1
            if str(target).casefold() in reserved:
                raise PackError("音效包文件路径相互冲突")
            inside(directory, target)
            reserved.add(str(target).casefold())
            files[str(relative)] = {"path": target.relative_to(directory).as_posix(), "sha256": digest}
            plan.append((source, target, previous.get("sha256") if replaceable else None))
        check_cancel(cancel)
        metadata = staging / "metadata.json"
        metadata.write_text(json.dumps({"schema_version": 1, "id": pack.id,
            "version": pack.version, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
        journal = []
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Commit is short and deliberately cannot be cancelled halfway.
            for index, (source, target, previous_digest) in enumerate(plan):
                if source is None:
                    continue
                inside(directory, target)
                if target.exists() and (previous_digest is None or sha256_file(target) != previous_digest):
                    raise PackError("安装期间本地音频发生变化，请重试")
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = staging / f"backup-{index}" if target.exists() else None
                if backup is not None:
                    shutil.copy2(target, backup)
                journal.append((target, backup))
                os.replace(source, target)
            os.replace(metadata, inside(directory, directory / METADATA_NAME))
        except Exception:
            for target, backup in reversed(journal):
                if backup is not None:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            if created_directory and directory.exists():
                shutil.rmtree(directory)
            raise
    return {"directory": directory, "preserved": preserved, "files": len(files)}


def download_and_install(pack, library, cache_directory, *, progress=None, cancel=None, opener=open_https):
    cache_directory = Path(cache_directory)
    cache_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache_directory) as temp:
        archive = Path(temp) / "pack.zip"
        download_pack(pack, archive, progress=progress, cancel=cancel, opener=opener)
        check_cancel(cancel)
        try:
            return install_pack(pack, archive, library, cancel=cancel)
        except zipfile.BadZipFile as exc:
            raise PackError("下载文件不是有效的音效包，未完成安装") from exc


def format_size(size):
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
