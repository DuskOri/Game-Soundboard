import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import online_packs as packs


def archive_bytes(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            # The Windows ZipInfo constructor otherwise normalizes backslashes.
            info.filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return result.getvalue()


def sound_pack(data, count=1, version="1.0"):
    return packs.SoundPack("example", "测试包", "测试包", version, "说明",
                           "https://example.test/pack.zip", len(data),
                           hashlib.sha256(data).hexdigest(), count)


def response(data):
    return lambda _url: io.BytesIO(data)


class OnlinePacksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.root / "music"
        self.cache = self.root / "cache"
        self.library.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, files, version="1.0", count=None):
        data = archive_bytes(files)
        pack = sound_pack(data, len(files) if count is None else count, version)
        result = packs.download_and_install(pack, self.library, self.cache, opener=response(data))
        return pack, result

    def test_fresh_install_and_missing_file_repair(self):
        pack, result = self.install({"a.mp3": b"audio", "nested/b.wav": b"wav"})
        directory = result["directory"]
        self.assertEqual((directory / "nested/b.wav").read_bytes(), b"wav")
        self.assertEqual(packs.pack_state(pack, self.library), "已安装")
        (directory / "a.mp3").unlink()
        self.assertEqual(packs.pack_state(pack, self.library), "需修复")
        self.install({"a.mp3": b"audio", "nested/b.wav": b"wav"})
        self.assertEqual((directory / "a.mp3").read_bytes(), b"audio")
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_matching_local_files_are_reused_and_custom_files_preserved(self):
        directory = self.library / "测试包"
        directory.mkdir()
        (directory / "same.mp3").write_bytes(b"same")
        (directory / "conflict.mp3").write_bytes(b"local")
        (directory / "custom.wav").write_bytes(b"custom")
        pack, result = self.install({"same.mp3": b"same", "conflict.mp3": b"online"})
        self.assertEqual(result["preserved"], 1)
        self.assertEqual((directory / "conflict.mp3").read_bytes(), b"local")
        self.assertEqual((directory / "conflict (在线).mp3").read_bytes(), b"online")
        self.assertEqual((directory / "custom.wav").read_bytes(), b"custom")
        self.assertEqual(len(list(directory.glob("same*"))), 1)
        self.assertEqual(packs.pack_state(pack, self.library), "已安装")

    def test_update_replaces_owned_audio_but_keeps_user_edits(self):
        _, result = self.install({"a.mp3": b"v1", "b.mp3": b"v1", "old.mp3": b"old"})
        directory = result["directory"]
        (directory / "b.mp3").write_bytes(b"user edit")
        (directory / "user.mp3").write_bytes(b"user file")
        data = archive_bytes({"a.mp3": b"v2", "b.mp3": b"v2"})
        updated = sound_pack(data, 2, "2.0")
        self.assertEqual(packs.pack_state(updated, self.library), "可更新")
        packs.download_and_install(updated, self.library, self.cache, opener=response(data))
        self.assertEqual((directory / "a.mp3").read_bytes(), b"v2")
        self.assertEqual((directory / "b.mp3").read_bytes(), b"user edit")
        self.assertEqual((directory / "b (在线).mp3").read_bytes(), b"v2")
        self.assertEqual((directory / "old.mp3").read_bytes(), b"old")
        self.assertEqual((directory / "user.mp3").read_bytes(), b"user file")
        self.assertEqual(packs.pack_state(updated, self.library), "已安装")
        self.assertEqual(packs.pack_state(sound_pack(data, 2, "1.0"), self.library), "已安装")

    def test_failed_metadata_commit_rolls_back_replacements(self):
        _, result = self.install({"a.mp3": b"v1"})
        directory = result["directory"]
        original_metadata = (directory / packs.METADATA_NAME).read_bytes()
        replace = packs.os.replace

        def fail_metadata(source, target):
            if Path(source).name == "metadata.json":
                raise OSError("simulated disk failure")
            return replace(source, target)

        with patch.object(packs.os, "replace", side_effect=fail_metadata):
            with self.assertRaises(OSError):
                self.install({"a.mp3": b"v2", "new.mp3": b"new"}, "2.0")
        self.assertEqual((directory / "a.mp3").read_bytes(), b"v1")
        self.assertFalse((directory / "new.mp3").exists())
        self.assertEqual((directory / packs.METADATA_NAME).read_bytes(), original_metadata)

    def test_untrusted_zip_entries_are_rejected_before_installation(self):
        for name in ("../escape.mp3", "/absolute.mp3", "C:/drive.mp3", "folder\\bad.mp3",
                     "CON.mp3", "a.mp3:stream", "installer.exe", "folder/../bad.mp3"):
            with self.subTest(name=name), self.assertRaises(packs.PackError):
                self.install({name: b"malicious"})
            self.assertEqual(list(self.library.iterdir()), [])

    def test_windows_duplicate_paths_and_symlinks_are_rejected(self):
        data = archive_bytes({"A.mp3": b"a", "a.mp3": b"b"})
        with self.assertRaises(packs.PackError):
            packs.download_and_install(sound_pack(data, 2), self.library, self.cache, opener=response(data))
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w") as archive:
            info = zipfile.ZipInfo("link.mp3")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
        data = result.getvalue()
        with self.assertRaises(packs.PackError):
            packs.download_and_install(sound_pack(data), self.library, self.cache, opener=response(data))
        self.assertEqual(list(self.library.iterdir()), [])

    def test_hash_mismatch_and_file_count_mismatch_do_not_install(self):
        data = archive_bytes({"a.mp3": b"audio"})
        bad = packs.SoundPack("example", "测试包", "测试包", "1.0", "", "https://example.test/a.zip",
                              len(data), "0" * 64, 1)
        for pack in (bad, sound_pack(data, 2)):
            with self.assertRaises(packs.PackError):
                packs.download_and_install(pack, self.library, self.cache, opener=response(data))
            self.assertEqual(list(self.library.iterdir()), [])
            self.assertEqual(list(self.cache.iterdir()), [])

    def test_download_cancellation_removes_partial_data(self):
        data = archive_bytes({"a.mp3": b"audio"})
        cancel = threading.Event()

        def progress(downloaded, total):
            if downloaded:
                cancel.set()

        with self.assertRaises(packs.DownloadCancelled):
            packs.download_and_install(sound_pack(data), self.library, self.cache,
                                       opener=response(data), cancel=cancel, progress=progress)
        self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_transient_network_failure_retries_and_reports_progress(self):
        data = archive_bytes({"a.mp3": b"audio"})
        calls, progress = [], []

        def opener(url):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary")
            return io.BytesIO(data)

        packs.download_and_install(sound_pack(data), self.library, self.cache, opener=opener,
                                   progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(progress[-1], (len(data), len(data)))

    def test_catalog_validation_and_cache_fallback(self):
        entry = vars(sound_pack(b"zip"))
        valid = json.dumps({"schema_version": 1, "packs": [entry]}).encode()
        cache, bundled = self.root / "catalog.json", self.root / "bundled.json"
        packs.save_catalog(cache, valid)
        self.assertEqual(packs.cached_catalog(cache, bundled)[0].id, "example")
        cache.write_bytes(b"corrupt")
        bundled.write_bytes(valid)
        self.assertEqual(packs.cached_catalog(cache, bundled)[0].id, "example")
        for change in ({"url": "http://example.test/a.zip"}, {"name": "../bad"},
                       {"size": -1}, {"sha256": "bad"}, {"file_count": 0}):
            data = json.dumps({"schema_version": 1, "packs": [{**entry, **change}]}).encode()
            with self.subTest(change=change), self.assertRaises(packs.PackError):
                packs.parse_catalog(data)

    def test_declared_size_and_extraction_limit_are_enforced(self):
        data = archive_bytes({"a.mp3": b"audio"})
        pack = sound_pack(data)
        with self.assertRaises(packs.PackError):
            packs.download_and_install(pack, self.library, self.cache, opener=response(data + b"extra"))
        with patch.object(packs, "MAX_EXPANDED", 1), self.assertRaises(packs.PackError):
            packs.download_and_install(pack, self.library, self.cache, opener=response(data))
        self.assertEqual(list(self.library.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
