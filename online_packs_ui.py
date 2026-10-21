"""Tk window for the online sound-pack library; all networking runs off the UI thread."""

from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from online_packs import (
    DownloadCancelled, PackError, cached_catalog, download_and_install,
    fetch_catalog, format_size, pack_state, save_catalog,
)


class OnlinePacksDialog(tk.Toplevel):
    def __init__(self, parent, library, data_directory, bundled_catalog, on_installed):
        super().__init__(parent)
        self.title("在线音效包")
        self.geometry("760x490")
        self.minsize(640, 420)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.library = Path(library)
        self.data_directory = Path(data_directory)
        self.cache = self.data_directory / "online-catalog.json"
        self.bundled_catalog = Path(bundled_catalog)
        self.on_installed = on_installed
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.close_callback = None
        self.packs = {}
        self.message = tk.StringVar(value="正在读取在线列表…")
        self.description = tk.StringVar(value="选择音效包查看详情，下载后可离线使用。")
        self.build_ui()
        initial = cached_catalog(self.cache, self.bundled_catalog)
        if initial:
            self.populate(initial)
        self.poll_id = self.after(100, self.poll)
        self.refresh()

    def build_ui(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="在线音效包", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(outer, text="按需下载，自动加入本地素材库。", foreground="#555555").pack(anchor="w", pady=(4, 12))
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("version", "count", "size", "state"), selectmode="browse")
        self.tree.heading("#0", text="音效包")
        self.tree.column("#0", width=200, minwidth=130)
        for key, title, width in (("version", "版本", 70), ("count", "音频数", 70),
                                  ("size", "下载大小", 100), ("state", "状态", 100)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=65, anchor="center", stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.selection_changed())
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        detail = ttk.Label(outer, textvariable=self.description, wraplength=700, justify="left")
        detail.pack(fill="x", pady=(12, 8))
        detail.bind("<Configure>", lambda event: detail.configure(wraplength=max(200, event.width)))
        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        ttk.Label(outer, textvariable=self.message, wraplength=700).pack(fill="x", pady=(0, 12))
        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        self.refresh_button = ttk.Button(actions, text="刷新列表", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.download_button = ttk.Button(actions, text="下载", command=self.download, state="disabled")
        self.download_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(actions, text="取消下载", command=self.cancel_download, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Button(actions, text="关闭", command=self.close).pack(side="right")

    def selected_pack(self):
        selection = self.tree.selection()
        return self.packs.get(selection[0]) if selection else None

    def populate(self, packs):
        selected = self.selected_pack()
        self.tree.delete(*self.tree.get_children())
        self.packs = {pack.id: pack for pack in packs}
        for pack in packs:
            self.tree.insert("", "end", iid=pack.id, text=pack.title, values=(
                pack.version, pack.file_count, format_size(pack.size), pack_state(pack, self.library),
            ))
        if packs:
            key = selected.id if selected and selected.id in self.packs else packs[0].id
            self.tree.selection_set(key)
        self.selection_changed()

    def selection_changed(self):
        pack = self.selected_pack()
        if not pack:
            self.download_button.configure(state="disabled")
            return
        state = pack_state(pack, self.library)
        self.description.set(pack.description or pack.title)
        label = {"已安装": "已安装", "可更新": "更新", "需修复": "重新下载", "本地已有": "同步音效包"}.get(state, "下载")
        self.download_button.configure(text=label, state="disabled" if self.busy or state == "已安装" else "normal")

    def set_busy(self, busy, downloading=False):
        self.busy = busy
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy and downloading else "disabled")
        self.selection_changed()

    def refresh(self):
        if self.busy:
            return
        self.cancel.clear()
        self.set_busy(True)
        self.message.set("正在更新在线列表…")
        self.progress.configure(value=0)
        threading.Thread(target=self.catalog_worker, daemon=True).start()

    def catalog_worker(self):
        try:
            packs, data = fetch_catalog(cancel=self.cancel)
            try:
                save_catalog(self.cache, data)
            except OSError:
                pass
            self.events.put(("catalog", packs, f"共 {len(packs)} 个在线音效包，下载后可离线使用。"))
        except DownloadCancelled:
            pass
        except Exception as exc:
            packs = cached_catalog(self.cache, self.bundled_catalog)
            message = str(exc) if isinstance(exc, PackError) else "暂时无法获取在线列表"
            if packs:
                message += "；当前显示缓存列表，仍可尝试下载。"
            self.events.put(("catalog", packs, message))
        finally:
            self.events.put(("done",))

    def download(self):
        pack = self.selected_pack()
        if self.busy or pack is None:
            return
        self.cancel.clear()
        self.set_busy(True, downloading=True)
        self.progress.configure(value=0)
        self.message.set(f"正在下载“{pack.title}”…")
        threading.Thread(target=self.download_worker, args=(pack,), daemon=True).start()

    def download_worker(self, pack):
        try:
            last = -1

            def progress(downloaded, total):
                nonlocal last
                percent = int(downloaded * 100 / total)
                if percent != last:
                    self.events.put(("progress", pack.title, downloaded, total))
                    last = percent

            result = download_and_install(pack, self.library, self.data_directory / "downloads",
                                          progress=progress, cancel=self.cancel)
            self.events.put(("installed", pack, result))
        except DownloadCancelled:
            self.events.put(("cancelled",))
        except Exception as exc:
            message = str(exc) if isinstance(exc, PackError) else "下载或安装失败，请检查网络、磁盘空间及素材库目录权限。"
            self.events.put(("error", message))
        finally:
            self.events.put(("done",))

    def cancel_download(self):
        self.cancel.set()
        self.cancel_button.configure(state="disabled")
        self.message.set("正在取消，等待当前文件操作结束…")

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "catalog":
                    self.populate(event[1])
                    self.message.set(event[2])
                elif kind == "progress":
                    _, title, downloaded, total = event
                    self.progress.configure(value=downloaded * 100 / total)
                    self.message.set(f"{title}：{format_size(downloaded)} / {format_size(total)}"
                                     if downloaded < total else f"“{title}”下载完成，正在校验和安装…")
                elif kind == "installed":
                    _, pack, result = event
                    self.on_installed(pack)
                    self.populate(list(self.packs.values()))
                    self.progress.configure(value=100)
                    text = f"“{pack.title}”已安装，已加入本地素材库。"
                    if result["preserved"]:
                        text += " 同名本地音频已保留，在线音频已另存。"
                    self.message.set(text)
                elif kind == "cancelled":
                    self.progress.configure(value=0)
                    self.message.set("已取消下载，本地音效保持不变。")
                elif kind == "error":
                    self.message.set(event[1])
                    if not self.closing:
                        messagebox.showerror("在线音效包", event[1], parent=self)
                elif kind == "done":
                    self.set_busy(False)
                    if self.closing:
                        self.finish_close()
                        return
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self.poll)

    def close(self, callback=None):
        if callback is not None:
            self.close_callback = callback
        if self.busy:
            self.closing = True
            self.cancel_download()
        else:
            self.finish_close()

    def finish_close(self):
        callback = self.close_callback
        self.after_cancel(self.poll_id)
        self.destroy()
        if callback is not None:
            callback()
