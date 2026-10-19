import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import sounddevice as sd
from tkinterdnd2 import DND_FILES, TkinterDnD


APP_NAME = "Game Soundboard"
SAMPLE_RATE = 48000
BLOCK_SIZE = 960
SUPPORTED_FILES = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".opus")
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
KEY_CODES = {
    **{str(number): 0x30 + number for number in range(10)},
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
    **{f"Num{number}": 0x60 + number for number in range(10)},
    "Space": 0x20,
}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    root = Path(os.environ.get("APPDATA", app_dir())) / "GameSoundboard"
    root.mkdir(parents=True, exist_ok=True)
    return root


def library_dir() -> Path:
    root = app_dir() / "music"
    root.mkdir(parents=True, exist_ok=True)
    return root


def ffmpeg_path() -> str:
    bundled = app_dir() / "ffmpeg.exe"
    return str(bundled) if bundled.exists() else "ffmpeg"


class AudioEngine:
    def __init__(self):
        self.input_stream = None
        self.output_stream = None
        self.monitor_stream = None
        self.lock = threading.Lock()
        self.mic_blocks = queue.Queue(maxsize=8)
        self.monitor_blocks = queue.Queue(maxsize=4)
        self.track = None
        self.position = 0
        self.loop = False
        self.mic_gain = 1.0
        self.sound_gain = 0.8
        self.mic_muted = False
        self.duck_mic = False
        self.monitor_gain = 0.8
        self.monitor_mic = False
        self.monitor_sound = True
        self.events = queue.Queue()

    @property
    def running(self):
        return (
            self.input_stream is not None
            and self.output_stream is not None
            and self.input_stream.active
            and self.output_stream.active
        )

    def start(self, input_device, output_device, monitor_device=None):
        self.stop_stream()
        self.mic_blocks = queue.Queue(maxsize=8)
        self.monitor_blocks = queue.Queue(maxsize=4)
        self.input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            device=input_device,
            channels=1,
            dtype="float32",
            latency="low",
            callback=self._input_callback,
        )
        self.output_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            device=output_device,
            channels=1,
            dtype="float32",
            latency="low",
            callback=self._output_callback,
        )
        if monitor_device is not None:
            self.monitor_stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                device=monitor_device,
                channels=1,
                dtype="float32",
                latency="low",
                callback=self._monitor_callback,
            )
        try:
            self.input_stream.start()
            self.output_stream.start()
            if self.monitor_stream is not None:
                self.monitor_stream.start()
        except Exception:
            self.stop_stream()
            raise

    def stop_stream(self):
        for stream_name in ("input_stream", "output_stream", "monitor_stream"):
            stream = getattr(self, stream_name)
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            finally:
                setattr(self, stream_name, None)

    def play(self, samples):
        with self.lock:
            self.track = samples
            self.position = 0

    def stop_sound(self):
        with self.lock:
            self.track = None
            self.position = 0

    def _input_callback(self, indata, _frames, _time_info, status):
        if status:
            self.events.put(("warning", str(status)))
        block = indata[:, 0].copy()
        try:
            self.mic_blocks.put_nowait(block)
        except queue.Full:
            try:
                self.mic_blocks.get_nowait()
                self.mic_blocks.put_nowait(block)
            except queue.Empty:
                pass

    def _output_callback(self, outdata, frames, _time_info, status):
        if status:
            self.events.put(("warning", str(status)))
        try:
            mic = self.mic_blocks.get_nowait()
            if len(mic) != frames:
                mic = np.resize(mic, frames)
        except queue.Empty:
            mic = np.zeros(frames, dtype=np.float32)
        mic_level = 0.0 if self.mic_muted else self.mic_gain
        mic_result = mic * mic_level
        sound_result = np.zeros(frames, dtype=np.float32)

        with self.lock:
            if self.track is not None:
                if self.duck_mic:
                    mic_result *= 0.35
                written = 0
                while written < frames and self.track is not None:
                    available = len(self.track) - self.position
                    count = min(frames - written, available)
                    sound_result[written:written + count] += (
                        self.track[self.position:self.position + count] * self.sound_gain
                    )
                    self.position += count
                    written += count
                    if self.position >= len(self.track):
                        if self.loop:
                            self.position = 0
                        else:
                            self.track = None
                            self.position = 0
                            self.events.put(("finished", None))

        result = mic_result + sound_result
        np.clip(result, -0.98, 0.98, out=result)
        outdata[:, 0] = result
        if self.monitor_stream is not None:
            monitor_block = np.zeros(frames, dtype=np.float32)
            if self.monitor_mic:
                monitor_block += mic_result
            if self.monitor_sound:
                monitor_block += sound_result
            np.clip(monitor_block, -0.98, 0.98, out=monitor_block)
            try:
                self.monitor_blocks.put_nowait(monitor_block)
            except queue.Full:
                try:
                    self.monitor_blocks.get_nowait()
                    self.monitor_blocks.put_nowait(monitor_block)
                except queue.Empty:
                    pass

    def _monitor_callback(self, outdata, frames, _time_info, status):
        if status:
            self.events.put(("warning", f"监听：{status}"))
        try:
            block = self.monitor_blocks.get_nowait()
            if len(block) != frames:
                block = np.resize(block, frames)
        except queue.Empty:
            block = np.zeros(frames, dtype=np.float32)
        outdata[:, 0] = block * self.monitor_gain


class SoundboardApp(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("820x590")
        self.minsize(700, 520)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.engine = AudioEngine()
        self.files = []
        self.input_devices = []
        self.output_devices = []
        self.decode_token = 0
        self.pack_names = []
        self.packs = {}
        self.current_pack = None
        self.saved_pack = None
        self.custom_hotkey_targets = {}
        self.custom_hotkeys = {}
        self.config_path = data_dir() / "settings.json"
        self.hotkey_stop = threading.Event()
        self.hotkey_thread = None
        self.hotkey_commands = queue.Queue()

        self.status_var = tk.StringVar(value="尚未启动音频")
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.monitor_var = tk.StringVar()
        self.mic_volume = tk.DoubleVar(value=100)
        self.sound_volume = tk.DoubleVar(value=80)
        self.monitor_volume = tk.DoubleVar(value=80)
        self.monitor_enabled = tk.BooleanVar(value=False)
        self.monitor_mic = tk.BooleanVar(value=False)
        self.monitor_sound = tk.BooleanVar(value=True)
        self.mic_muted = tk.BooleanVar(value=False)
        self.loop = tk.BooleanVar(value=False)
        self.duck_mic = tk.BooleanVar(value=False)

        self._build_ui()
        self.refresh_devices()
        self.load_config()
        self.refresh_library()
        self.start_hotkeys()
        self.after(100, self.poll_events)

    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        device_box = ttk.LabelFrame(outer, text="音频设备", padding=12)
        device_box.pack(fill="x")
        device_box.columnconfigure(1, weight=1)

        ttk.Label(device_box, text="真实麦克风").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.input_combo = ttk.Combobox(device_box, textvariable=self.input_var, state="readonly")
        self.input_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(device_box, text="混音输出").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.output_combo = ttk.Combobox(device_box, textvariable=self.output_var, state="readonly")
        self.output_combo.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(device_box, text="本机监听输出").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        self.monitor_combo = ttk.Combobox(device_box, textvariable=self.monitor_var, state="readonly")
        self.monitor_combo.grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(device_box, text="启用监听", variable=self.monitor_enabled).grid(row=2, column=2, sticky="w", padx=(10, 0))

        ttk.Button(device_box, text="刷新设备", command=self.refresh_devices).grid(row=0, column=2, rowspan=2, padx=(10, 0))
        self.start_button = ttk.Button(device_box, text="启动混音", command=self.toggle_stream)
        self.start_button.grid(row=0, column=3, rowspan=3, padx=(8, 0))

        controls = ttk.Frame(outer, padding=(0, 14, 0, 10))
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(4, weight=1)

        ttk.Label(controls, text="麦克风音量").grid(row=0, column=0, sticky="w")
        ttk.Scale(controls, from_=0, to=150, variable=self.mic_volume, command=self.update_levels).grid(row=0, column=1, sticky="ew", padx=(8, 16))
        ttk.Label(controls, text="素材音量").grid(row=0, column=3, sticky="w")
        ttk.Scale(controls, from_=0, to=150, variable=self.sound_volume, command=self.update_levels).grid(row=0, column=4, sticky="ew", padx=(8, 0))
        ttk.Label(controls, text="监听音量").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Scale(controls, from_=0, to=150, variable=self.monitor_volume, command=self.update_levels).grid(row=1, column=1, sticky="ew", padx=(8, 16), pady=(8, 0))

        checks = ttk.Frame(outer)
        checks.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(checks, text="麦克风静音", variable=self.mic_muted, command=self.update_levels).pack(side="left")
        ttk.Checkbutton(checks, text="循环播放", variable=self.loop, command=self.update_levels).pack(side="left", padx=18)
        ttk.Checkbutton(checks, text="播放时压低麦克风", variable=self.duck_mic, command=self.update_levels).pack(side="left")
        ttk.Checkbutton(checks, text="监听麦克风", variable=self.monitor_mic, command=self.update_levels).pack(side="left", padx=(18, 0))
        ttk.Checkbutton(checks, text="监听素材", variable=self.monitor_sound, command=self.update_levels).pack(side="left", padx=(12, 0))

        tabs = ttk.Frame(outer)
        tabs.pack(fill="x", pady=(0, 6))
        ttk.Button(tabs, text="‹", width=3, command=lambda: self.scroll_tabs(-1)).pack(side="left")
        self.tabs_canvas = tk.Canvas(tabs, height=34, highlightthickness=0)
        self.tabs_canvas.pack(side="left", fill="x", expand=True, padx=4)
        self.tabs_inner = ttk.Frame(self.tabs_canvas)
        self.tabs_window = self.tabs_canvas.create_window((0, 0), window=self.tabs_inner, anchor="nw")
        self.tabs_inner.bind("<Configure>", self.update_tab_scrollregion)
        self.tabs_canvas.bind("<Configure>", self.update_tab_view)
        ttk.Button(tabs, text="›", width=3, command=lambda: self.scroll_tabs(1)).pack(side="left")
        self.pack_menu_button = ttk.Menubutton(tabs, text="更多")
        self.pack_menu = tk.Menu(self.pack_menu_button, tearoff=False)
        self.pack_menu_button.configure(menu=self.pack_menu)
        self.pack_menu_button.pack(side="left", padx=(4, 0))

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, activestyle="dotbox", selectmode="browse", font=("Segoe UI", 10))
        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.listbox.bind("<Double-Button-1>", lambda _event: self.play_selected())
        self.listbox.drop_target_register(DND_FILES)
        self.listbox.dnd_bind("<<Drop>>", self.on_drop)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(outer, padding=(0, 10, 0, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="添加音频", command=self.add_files).pack(side="left")
        ttk.Button(buttons, text="刷新素材库", command=self.refresh_library).pack(side="left", padx=8)
        ttk.Button(buttons, text="打开素材文件夹", command=self.open_library).pack(side="left")
        ttk.Button(buttons, text="从素材库删除", command=self.remove_selected).pack(side="left", padx=(8, 14))
        ttk.Button(buttons, text="设置快捷键", command=self.show_hotkey_dialog).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="播放", command=self.play_selected).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="停止素材", command=self.stop_sound).pack(side="left")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="right")

        ttk.Label(
            outer,
            text="Num1～Num9 播放当前语音包前 9 条；Alt+←/→ 切换语音包；Num0 停止。",
            foreground="#555555",
        ).pack(fill="x", pady=(12, 0))

    def start_hotkeys(self):
        self.hotkey_thread = threading.Thread(target=self.hotkey_worker, daemon=True)
        self.hotkey_thread.start()

    def hotkey_worker(self):
        user32 = ctypes.windll.user32
        registered = []

        def register_all():
            for registered_id in registered:
                user32.UnregisterHotKey(None, registered_id)
            registered.clear()
            bindings = self.hotkey_bindings()
            for hotkey_id, modifiers, key_code in bindings:
                if user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, key_code):
                    registered.append(hotkey_id)
            if len(registered) != len(bindings):
                self.engine.events.put(("hotkey_warning", (len(registered), len(bindings))))

        register_all()

        message = wintypes.MSG()
        try:
            while not self.hotkey_stop.is_set():
                try:
                    while self.hotkey_commands.get_nowait() == "refresh":
                        register_all()
                except queue.Empty:
                    pass
                while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_HOTKEY:
                        self.engine.events.put(("hotkey", int(message.wParam)))
                    elif message.message == WM_QUIT:
                        return
                time.sleep(0.03)
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)

    def hotkey_bindings(self):
        bindings = [(100, 0, 0x60), (110, MOD_ALT, 0x25), (111, MOD_ALT, 0x27)]
        bindings.extend((100 + slot, 0, 0x60 + slot) for slot in range(1, 10))
        self.custom_hotkey_targets = {}
        all_files = [path for name in self.pack_names for path in self.packs.get(name, [])]
        for index, path in enumerate(all_files):
            binding = self.custom_hotkeys.get(self.relative_key(path))
            if binding and binding.get("key") in KEY_CODES:
                hotkey_id = 1000 + index
                self.custom_hotkey_targets[hotkey_id] = path
                bindings.append((hotkey_id, int(binding.get("modifiers", 0)), KEY_CODES[binding["key"]]))
        return bindings

    def handle_hotkey(self, hotkey_id):
        if hotkey_id == 100:
            self.stop_sound()
            return
        if hotkey_id == 110:
            self.change_pack(-1)
            return
        if hotkey_id == 111:
            self.change_pack(1)
            return
        if hotkey_id >= 1000:
            path = self.custom_hotkey_targets.get(hotkey_id)
            if path:
                pack_name = self.pack_for_path(path)
                if pack_name and pack_name != self.current_pack:
                    self.select_pack(pack_name)
                index = self.files.index(path) if path in self.files else -1
            else:
                index = -1
            if index >= 0:
                self.listbox.selection_clear(0, "end")
                self.listbox.selection_set(index)
                self.listbox.see(index)
                self.play_index(index, notify=False)
            return
        index = hotkey_id - 101
        if 0 <= index < len(self.files):
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(index)
            self.listbox.see(index)
            self.play_index(index, notify=False)

    def refresh_devices(self):
        try:
            devices = sd.query_devices()
            self.input_devices = [(i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
            self.output_devices = [(i, d["name"]) for i, d in enumerate(devices) if d["max_output_channels"] > 0]
            self.input_combo["values"] = [self.device_label(x) for x in self.input_devices]
            self.output_combo["values"] = [self.device_label(x) for x in self.output_devices]
            self.monitor_combo["values"] = [self.device_label(x) for x in self.output_devices]

            if not self.input_var.get() and self.input_devices:
                default_input = sd.default.device[0]
                default_device = self.find_device(self.input_devices, default_input)
                preferred_input = self.prefer_wasapi(self.input_devices, default_device[1]) or default_device
                self.input_var.set(self.device_label(preferred_input))
            if not self.output_var.get() and self.output_devices:
                cable_matches = [d for d in self.output_devices if "cable input" in d[1].lower()]
                cable = self.prefer_wasapi(cable_matches) if cable_matches else None
                default_output = sd.default.device[1]
                self.output_var.set(self.device_label(cable or self.find_device(self.output_devices, default_output)))
            if not self.monitor_var.get() and self.output_devices:
                default_output = sd.default.device[1]
                default_device = self.find_device(self.output_devices, default_output)
                preferred_monitor = self.prefer_wasapi(self.output_devices, default_device[1]) or default_device
                if "cable input" not in preferred_monitor[1].lower():
                    self.monitor_var.set(self.device_label(preferred_monitor))
            self.status_var.set("设备列表已刷新")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法读取音频设备：\n{exc}")

    @staticmethod
    def device_label(device):
        return f"[{device[0]}] {device[1]}"

    @staticmethod
    def find_device(devices, index):
        return next((d for d in devices if d[0] == index), devices[0])

    @staticmethod
    def prefer_wasapi(devices, exact_name=None):
        hostapis = sd.query_hostapis()
        candidates = [d for d in devices if exact_name is None or d[1] == exact_name]
        return next((d for d in candidates if "WASAPI" in hostapis[sd.query_devices(d[0])["hostapi"]]["name"]), None)

    @staticmethod
    def selected_index(label):
        return int(label.split("]", 1)[0][1:])

    def toggle_stream(self):
        if self.engine.running:
            self.engine.stop_stream()
            self.start_button.configure(text="启动混音")
            self.status_var.set("混音已停止")
            return
        if not self.input_var.get() or not self.output_var.get():
            messagebox.showwarning(APP_NAME, "请先选择麦克风和混音输出设备。")
            return
        try:
            self.update_levels()
            monitor_device = None
            if self.monitor_enabled.get():
                if not self.monitor_var.get():
                    messagebox.showwarning(APP_NAME, "启用监听前请选择本机的耳机或扬声器。")
                    return
                monitor_device = self.selected_index(self.monitor_var.get())
                if monitor_device == self.selected_index(self.output_var.get()):
                    messagebox.showwarning(APP_NAME, "监听输出不能与 CABLE Input 相同，请选择耳机或扬声器。")
                    return
            self.engine.start(
                self.selected_index(self.input_var.get()),
                self.selected_index(self.output_var.get()),
                monitor_device,
            )
            self.start_button.configure(text="停止混音")
            self.status_var.set("正在向虚拟声卡输出")
            self.save_config()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"启动音频失败：\n{exc}\n\n请确认输出选择的是 CABLE Input。")

    def update_levels(self, _value=None):
        self.engine.mic_gain = self.mic_volume.get() / 100.0
        self.engine.sound_gain = self.sound_volume.get() / 100.0
        self.engine.mic_muted = self.mic_muted.get()
        self.engine.loop = self.loop.get()
        self.engine.duck_mic = self.duck_mic.get()
        self.engine.monitor_gain = self.monitor_volume.get() / 100.0
        self.engine.monitor_mic = self.monitor_mic.get()
        self.engine.monitor_sound = self.monitor_sound.get()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="添加音频",
            filetypes=[("音频文件", " ".join(f"*{ext}" for ext in SUPPORTED_FILES)), ("所有文件", "*.*")],
        )
        if paths:
            self.import_paths(paths)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        self.import_paths(paths)
        return event.action

    def import_paths(self, paths):
        imported = 0
        skipped = 0
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_dir():
                pack_dir = library_dir() / path.name
                pack_dir.mkdir(parents=True, exist_ok=True)
                for source in (p for p in path.rglob("*") if self.is_supported(p)):
                    try:
                        relative_parent = source.parent.relative_to(path)
                        target_dir = pack_dir / relative_parent
                        target_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, self.available_destination(source.name, target_dir))
                        imported += 1
                    except OSError:
                        skipped += 1
            elif self.is_supported(path):
                target_dir = library_dir() / (self.current_pack or "默认语音包")
                target_dir.mkdir(parents=True, exist_ok=True)
                try:
                    if path.resolve().parent == target_dir.resolve():
                        skipped += 1
                    else:
                        shutil.copy2(path, self.available_destination(path.name, target_dir))
                        imported += 1
                except OSError:
                    skipped += 1

        self.refresh_library()
        if imported:
            self.status_var.set(f"已导入 {imported} 个音频到 music 素材库")
        elif skipped:
            self.status_var.set("未导入：文件已在素材库中或无法读取")
        else:
            messagebox.showinfo(APP_NAME, "没有找到支持的音频文件。")

    @staticmethod
    def is_supported(path):
        return path.is_file() and path.suffix.lower() in SUPPORTED_FILES

    @staticmethod
    def available_destination(filename, target_dir):
        source_name = Path(filename)
        destination = target_dir / source_name.name
        counter = 2
        while destination.exists():
            destination = target_dir / f"{source_name.stem} ({counter}){source_name.suffix}"
            counter += 1
        return destination

    def refresh_library(self):
        selected_path = None
        selected = self.listbox.curselection()
        if selected and selected[0] < len(self.files):
            selected_path = self.files[selected[0]]
        pack_dirs = sorted((path for path in library_dir().iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
        self.packs = {
            pack_dir.name: sorted(
                (str(path.resolve()) for path in pack_dir.rglob("*") if self.is_supported(path)),
                key=lambda value: str(Path(value).relative_to(pack_dir)).casefold(),
            )
            for pack_dir in pack_dirs
        }
        self.packs = {name: files for name, files in self.packs.items() if files}
        self.pack_names = list(self.packs)
        preferred = self.current_pack or self.saved_pack
        self.current_pack = preferred if preferred in self.packs else (self.pack_names[0] if self.pack_names else None)
        self.files = self.packs.get(self.current_pack, [])
        self.render_tabs()
        self.render_library()
        if self.hotkey_thread and self.hotkey_thread.is_alive():
            self.hotkey_commands.put("refresh")
        if selected_path in self.files:
            index = self.files.index(selected_path)
            self.listbox.selection_set(index)
            self.listbox.see(index)
        total = sum(len(files) for files in self.packs.values())
        self.status_var.set(f"{len(self.pack_names)} 个语音包，共 {total} 条音频")

    def render_library(self):
        selected = self.listbox.curselection()
        selected_index = selected[0] if selected else None
        self.listbox.delete(0, "end")
        for index, path in enumerate(self.files):
            label = str(Path(path).relative_to(library_dir() / self.current_pack))
            custom = self.custom_hotkeys.get(self.relative_key(path))
            if custom:
                label = f"[{self.format_hotkey(custom)}]  {label}"
            if index < 9:
                label = f"[Num{index + 1}]  {label}"
            self.listbox.insert("end", label)
        if selected_index is not None and selected_index < len(self.files):
            self.listbox.selection_set(selected_index)
            self.listbox.see(selected_index)
    def render_tabs(self):
        for child in self.tabs_inner.winfo_children():
            child.destroy()
        self.pack_menu.delete(0, "end")
        for name in self.pack_names:
            active = name == self.current_pack
            button = tk.Button(
                self.tabs_inner,
                text=name,
                command=lambda value=name: self.select_pack(value),
                relief="sunken" if active else "raised",
                bg="#d9eaff" if active else "SystemButtonFace",
                padx=12,
                pady=4,
            )
            button.pack(side="left", padx=(0, 3))
            self.pack_menu.add_command(label=name, command=lambda value=name: self.select_pack(value))
        self.update_idletasks()
        self.update_tab_scrollregion()

    def update_tab_scrollregion(self, _event=None):
        self.tabs_canvas.configure(scrollregion=self.tabs_canvas.bbox("all"))

    def update_tab_view(self, event=None):
        if event:
            self.tabs_canvas.itemconfigure(self.tabs_window, height=event.height)

    def scroll_tabs(self, direction):
        self.tabs_canvas.xview_scroll(direction * 3, "units")

    def select_pack(self, name):
        if name not in self.packs:
            return
        self.current_pack = name
        self.files = self.packs[name]
        self.render_tabs()
        self.render_library()
        self.status_var.set(f"当前语音包：{name}（{len(self.files)} 条）")
        self.save_config()

    def change_pack(self, delta):
        if not self.pack_names:
            return
        index = self.pack_names.index(self.current_pack) if self.current_pack in self.pack_names else 0
        self.select_pack(self.pack_names[(index + delta) % len(self.pack_names)])

    def pack_for_path(self, path):
        for name, files in self.packs.items():
            if path in files:
                return name
        return None

    @staticmethod
    def relative_key(path):
        return Path(path).resolve().relative_to(library_dir().resolve()).as_posix().casefold()

    @staticmethod
    def format_hotkey(binding):
        parts = []
        modifiers = int(binding.get("modifiers", 0))
        if modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if modifiers & MOD_ALT:
            parts.append("Alt")
        if modifiers & MOD_SHIFT:
            parts.append("Shift")
        parts.append(binding.get("key", "?"))
        return "+".join(parts)

    def show_hotkey_dialog(self):
        selected = self.listbox.curselection()
        if not selected:
            messagebox.showinfo(APP_NAME, "请先选择一个音频。")
            return
        path = self.files[selected[0]]
        relative = self.relative_key(path)
        current = self.custom_hotkeys.get(relative, {})

        dialog = tk.Toplevel(self)
        dialog.title("设置快捷键")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=Path(path).name, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))

        modifiers = int(current.get("modifiers", 0))
        ctrl_var = tk.BooleanVar(value=bool(modifiers & MOD_CONTROL))
        alt_var = tk.BooleanVar(value=bool(modifiers & MOD_ALT))
        shift_var = tk.BooleanVar(value=bool(modifiers & MOD_SHIFT))
        ttk.Checkbutton(body, text="Ctrl", variable=ctrl_var).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(body, text="Alt", variable=alt_var).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Checkbutton(body, text="Shift", variable=shift_var).grid(row=1, column=2, sticky="w")

        key_var = tk.StringVar(value=current.get("key", "F1"))
        key_combo = ttk.Combobox(body, textvariable=key_var, values=list(KEY_CODES), state="readonly", width=10)
        key_combo.grid(row=1, column=3, padx=(16, 0))

        def save_binding():
            modifier_value = 0
            if ctrl_var.get():
                modifier_value |= MOD_CONTROL
            if alt_var.get():
                modifier_value |= MOD_ALT
            if shift_var.get():
                modifier_value |= MOD_SHIFT
            candidate = {"modifiers": modifier_value, "key": key_var.get()}
            if modifier_value == 0 and key_var.get() in tuple(f"Num{n}" for n in range(10)):
                messagebox.showwarning(APP_NAME, "Num0～Num9 已用于停止和当前组播放，请选择其他组合。", parent=dialog)
                return
            for other_path, binding in self.custom_hotkeys.items():
                if other_path != relative and binding == candidate:
                    messagebox.showwarning(APP_NAME, f"该快捷键已分配给：\n{other_path}", parent=dialog)
                    return
            self.custom_hotkeys[relative] = candidate
            self.save_config()
            self.render_library()
            self.hotkey_commands.put("refresh")
            dialog.destroy()

        def clear_binding():
            self.custom_hotkeys.pop(relative, None)
            self.save_config()
            self.render_library()
            self.hotkey_commands.put("refresh")
            dialog.destroy()

        actions = ttk.Frame(body, padding=(0, 14, 0, 0))
        actions.grid(row=2, column=0, columnspan=4, sticky="e")
        ttk.Button(actions, text="清除自定义", command=clear_binding).pack(side="left")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=8)
        ttk.Button(actions, text="保存", command=save_binding).pack(side="left")

    def open_library(self):
        os.startfile(library_dir())

    def remove_selected(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        index = selected[0]
        path = Path(self.files[index])
        if not messagebox.askyesno(APP_NAME, f"确定要从素材库删除“{path.name}”吗？\n\n此操作会删除 music 文件夹中的文件。"):
            return
        try:
            path.unlink()
            self.refresh_library()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法删除文件：\n{exc}")

    def play_selected(self):
        selected = self.listbox.curselection()
        if not selected:
            messagebox.showinfo(APP_NAME, "请先从列表中选择一个音频。")
            return
        self.play_index(selected[0], notify=True)

    def play_index(self, index, notify=True):
        if not self.engine.running:
            if notify:
                messagebox.showinfo(APP_NAME, "请先点击“启动混音”。")
            else:
                self.status_var.set("快捷键未播放：请先启动混音")
            return
        path = self.files[index]
        if not Path(path).exists():
            messagebox.showerror(APP_NAME, "音频文件已经不存在。")
            return
        self.decode_token += 1
        token = self.decode_token
        self.status_var.set(f"正在加载：{Path(path).name}")
        threading.Thread(target=self.decode_and_play, args=(path, token), daemon=True).start()

    def decode_and_play(self, path, token):
        command = [
            ffmpeg_path(), "-v", "error", "-i", path,
            "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
        ]
        try:
            result = subprocess.run(command, capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            samples = np.frombuffer(result.stdout, dtype=np.float32).copy()
            if not len(samples):
                raise RuntimeError("音频中没有可播放的数据")
            self.after(0, self.finish_decode, samples, path, token)
        except Exception as exc:
            detail = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            self.after(0, lambda: messagebox.showerror(APP_NAME, f"无法读取音频：\n{detail}"))
            self.after(0, self.status_var.set, "加载失败")

    def finish_decode(self, samples, path, token):
        if token != self.decode_token:
            return
        self.engine.play(samples)
        self.status_var.set(f"正在播放：{Path(path).name}")

    def stop_sound(self):
        self.decode_token += 1
        self.engine.stop_sound()
        self.status_var.set("素材播放已停止")

    def poll_events(self):
        try:
            while True:
                kind, value = self.engine.events.get_nowait()
                if kind == "finished":
                    self.status_var.set("播放完成")
                elif kind == "warning":
                    self.status_var.set(f"音频警告：{value}")
                elif kind == "hotkey":
                    self.handle_hotkey(value)
                elif kind == "hotkey_warning":
                    succeeded, total = value
                    self.status_var.set(f"全局快捷键仅成功注册 {succeeded}/{total} 个")
        except queue.Empty:
            pass
        self.after(100, self.poll_events)

    def load_config(self):
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for variable, key in ((self.mic_volume, "mic_volume"), (self.sound_volume, "sound_volume"), (self.monitor_volume, "monitor_volume")):
            if key in config:
                variable.set(config[key])
        self.custom_hotkeys = {
            path: binding
            for path, binding in config.get("custom_hotkeys", {}).items()
            if not (
                int(binding.get("modifiers", 0)) == 0
                and binding.get("key") in tuple(f"Num{number}" for number in range(10))
            )
        }
        self.saved_pack = config.get("current_pack")
        self.restore_device(self.input_var, self.input_devices, config.get("input_device"))
        self.restore_device(self.output_var, self.output_devices, config.get("output_device"))
        self.restore_device(self.monitor_var, self.output_devices, config.get("monitor_device"))
        self.monitor_enabled.set(bool(config.get("monitor_enabled", False)))
        self.monitor_mic.set(bool(config.get("monitor_mic", False)))
        self.monitor_sound.set(bool(config.get("monitor_sound", True)))
        self.update_levels()

    def restore_device(self, variable, devices, saved_name):
        if not saved_name:
            return
        match = next((d for d in devices if d[1] == saved_name), None)
        if match:
            variable.set(self.device_label(match))

    def save_config(self):
        try:
            input_name = self.input_var.get().split("] ", 1)[1] if "] " in self.input_var.get() else ""
            output_name = self.output_var.get().split("] ", 1)[1] if "] " in self.output_var.get() else ""
            monitor_name = self.monitor_var.get().split("] ", 1)[1] if "] " in self.monitor_var.get() else ""
            config = {
                "input_device": input_name,
                "output_device": output_name,
                "monitor_device": monitor_name,
                "monitor_enabled": self.monitor_enabled.get(),
                "monitor_mic": self.monitor_mic.get(),
                "monitor_sound": self.monitor_sound.get(),
                "mic_volume": self.mic_volume.get(),
                "sound_volume": self.sound_volume.get(),
                "monitor_volume": self.monitor_volume.get(),
                "custom_hotkeys": self.custom_hotkeys,
                "current_pack": self.current_pack,
            }
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def on_close(self):
        self.hotkey_stop.set()
        self.save_config()
        self.engine.stop_stream()
        self.destroy()


if __name__ == "__main__":
    SoundboardApp().mainloop()
