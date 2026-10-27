"""努比亚N1 对讲机频道配置

依赖: bleak (蓝牙), Python 3.10+
设备操作必须收到匹配响应，写入和切换必须读回核验。
"""
from __future__ import annotations
import asyncio, json, queue, struct, threading, tkinter as tk, logging, traceback, configparser
import re
from decimal import Decimal, InvalidOperation
from dataclasses import replace
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = BleakScanner = None

SERVICE_UUID = "01ead4a5-7a67-4703-ad02-589886e00923"
DEFAULT_SERVICE = "00010203-0405-0607-0809-0A0B0C0D1912"
DEFAULT_CHAR = "00010203-0405-0607-0809-0A0B0C0D2B12"
N1_SERVICE = "0000febe-0000-1000-8000-00805f9b34fb"
N1_READ_CHAR = "0000bec9-0000-1000-8000-00805f9b34fb"
N1_WRITE_CHAR = "0000bec8-0000-1000-8000-00805f9b34fb"
N1_COMMAND_CHAR = "0000bec7-0000-1000-8000-00805f9b34fb"
N1_NOTIFY2_CHAR = "0000feab-0000-1000-8000-00805f9b34fb"
DATA_DIR = Path(__file__).with_name("radio_profiles")
DATA_DIR.mkdir(exist_ok=True)
LOG_FILE = Path(__file__).with_name("radio_debug.log")
CONFIG_INI = Path(__file__).with_name("config.ini")
logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
LOGGER = logging.getLogger("radio")

# 每类列表第 0 项是关闭；低 8 位是列表序号，数字亚音也不是八进制码值。
_TONE_DATA = json.loads(Path(__file__).with_name("apk_tone_lists.json").read_text(encoding="utf-8"))
CTCSS = [float(x[:-2]) for x in _TONE_DATA["analog"][1:]]
DCS_CODES = [x[1:4] for x in _TONE_DATA["digital_normal"][1:]]
TONE_OPTIONS = [("关闭", 0)]
for _key, _prefix, _base in [("analog", "模拟亚音", 256),
                             ("digital_normal", "数字亚音 正相", 512),
                             ("digital_inverse", "数字亚音 负相", 768)]:
    for _index, _label in enumerate(_TONE_DATA[_key]):
        TONE_OPTIONS.append((f"{_prefix} {_label}", _base + _index))
TONE_LABEL = {v:k for k,v in TONE_OPTIONS}
TONE_VALUE = {k:v for k,v in TONE_OPTIONS}

from n1_protocol import (Channel, N1Session, SERVICE, COMMAND, NOTIFICATIONS, fields, channel_matches,
                         validate_preset_change, validate_channel_changes)

class ToneSelector(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.mode = tk.IntVar(value=0)
        self.label = tk.StringVar()
        self.unknown = None
        self.lists = {1: _TONE_DATA["analog"], 2: _TONE_DATA["digital_normal"],
                      3: _TONE_DATA["digital_inverse"]}
        for i, text in enumerate(("关闭", "模拟亚音", "数字亚音 正相", "数字亚音 负相")):
            ttk.Radiobutton(self, text=text, variable=self.mode, value=i,
                            command=self.mode_changed).grid(row=i // 2, column=i % 2, sticky="w")
        self.box = ttk.Combobox(self, textvariable=self.label, state="disabled", width=24)
        self.box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=3)

    def mode_changed(self):
        self.unknown = None
        self._populate()

    def _populate(self, index=None):
        mode = self.mode.get()
        options = self.lists.get(mode, [])
        self.box.configure(values=options, state="readonly" if options else "disabled")
        self.label.set(options[index if index is not None else 1] if options else "")

    def set_value(self, value):
        self.unknown = None
        mode, index = value >> 8, value & 255
        if value == 0:
            self.mode.set(0); self._populate()
        elif mode in self.lists and index < len(self.lists[mode]):
            self.mode.set(mode); self._populate(index)
        else:
            self.unknown = value
            self.mode.set(-1)
            self.box.configure(values=(), state="disabled")
            self.label.set(f"未知亚音 ({value})")

    def get_value(self):
        if self.unknown is not None:
            return self.unknown
        mode = self.mode.get()
        if mode == 0:
            return 0
        return (mode << 8) + self.lists[mode].index(self.label.get())


def format_frequency(value):
    return f"{int(value):,}".replace(",", " ")


def parse_frequency(text):
    text = text.strip()
    if re.fullmatch(r"[0-9]{1,10}", text):
        value = int(text)
    elif re.fullmatch(r"[0-9]{1,4} [0-9]{3} [0-9]{3}", text):
        value = int(text.replace(" ", ""))
    elif re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?\s*MHz", text, re.IGNORECASE):
        value = int(Decimal(re.sub(r"\s*MHz$", "", text, flags=re.IGNORECASE)) * 1000000)
    else:
        return None
    return value if 0 < value <= 0xffffffff else None


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.popup, self.timer = widget, text, None, None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _=None):
        self.hide()
        self.timer = self.widget.after(500, self.show)

    def show(self):
        self.timer = None
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        self.popup = tk.Toplevel(self.widget)
        self.popup.overrideredirect(True)
        self.popup.geometry(f"+{self.widget.winfo_pointerx()+12}+{self.widget.winfo_pointery()+18}")
        ttk.Label(self.popup, text=text, padding=7, wraplength=300, relief="solid").pack()

    def hide(self, _=None):
        if self.timer:
            self.widget.after_cancel(self.timer); self.timer = None
        if self.popup:
            self.popup.destroy(); self.popup = None


class FrequencyInput(ttk.Frame):
    """Three digit groups, backed by the existing integer-Hz StringVar."""
    def __init__(self, parent, variable):
        super().__init__(parent)
        self.variable = variable
        self.parts = [tk.StringVar() for _ in range(3)]
        self.entries = []
        self.copy_button = None
        self.next_input = None
        self.previous_input = None
        self._updating = False
        self._input_state = "normal"
        # Windows Tk uses Mod1 (0x08) for Num Lock, not Alt.
        self._shortcut_mask = 0x20004 if self.tk.call("tk", "windowingsystem") == "win32" else 0x0c
        self.row = ttk.Frame(self)
        self.row.pack(anchor="w")
        for i, part in enumerate(self.parts):
            validate = self.register(lambda text, limit=4 if i == 0 else 3:
                                     text == "" or (text.isascii() and text.isdigit() and len(text) <= limit))
            entry = ttk.Entry(self.row, textvariable=part, width=4, justify="center",
                              validate="key", validatecommand=(validate, "%P"))
            entry.pack(side="left", padx=(0, 5) if i < 2 else 0)
            self.entries.append(entry)
            entry.bind("<KeyPress>", lambda event, index=i: self._overwrite(event, index))
            entry.bind("<<Paste>>", self.paste)
            part.trace_add("write", self._parts_changed)
        self.hint = ttk.Label(self, text="", foreground="#c62828")
        self.hint.pack(side="bottom", anchor="w")
        self.paste_button = ttk.Button(self.row, text="粘贴", width=4, command=self.paste)
        self.paste_button.pack(side="left", padx=(6,0))
        variable.trace_add("write", self._value_changed)
        self._value_changed()

    def _overwrite(self, event, index):
        entry = self.entries[index]
        cursor = entry.index("insert")
        if self._input_state != "disabled" and not event.state & self._shortcut_mask:
            if getattr(event, "keysym", "") in ("Left", "BackSpace") and cursor == 0 and not entry.selection_present():
                target = self.entries[index-1] if index else (self.previous_input.entries[-1] if self.previous_input else None)
                if target is not None:
                    target.focus_set(); target.selection_clear(); target.icursor("end")
                    return "break"
            if getattr(event, "keysym", "") == "Right" and cursor == len(entry.get()):
                target = self.entries[index+1] if index < 2 else (self.next_input.entries[0] if self.next_input else None)
                if target is not None:
                    target.focus_set(); target.selection_clear(); target.icursor(0)
                    return "break"
        if event.state & self._shortcut_mask or len(event.char) != 1 or event.char not in "0123456789":
            return
        if self._input_state == "disabled":
            return "break"
        entry = self.entries[index]
        text = self.parts[index].get()
        cursor = entry.index("sel.first") if entry.selection_present() else entry.index("insert")
        limit = 4 if index == 0 and len(text) == 4 else 3
        if cursor >= limit:
            return "break"
        self.parts[index].set(text[:cursor] + event.char + text[cursor+1:])
        entry.selection_clear()
        entry.icursor(cursor+1)
        if cursor+1 == limit:
            target = self.entries[index+1] if index < 2 else (
                self.next_input.entries[0] if self.next_input is not None else None)
            if target is not None and str(target["state"]) != "disabled":
                target.focus_set()
                target.selection_clear()
                target.icursor(0)
        return "break"

    def add_copy_button(self, text, source):
        self.copy_button = ttk.Button(self.row, text=text, command=lambda: self.variable.set(source.get()))
        self.copy_button.pack(side="left", padx=(8,0))

    def paste(self, _=None):
        if self._input_state != "disabled":
            try:
                value = parse_frequency(self.clipboard_get())
                if value is not None:
                    self.variable.set(str(value))
            except tk.TclError:
                pass
        return "break"

    def _value_changed(self, *_):
        if self._updating:
            return
        self._updating = True
        try:
            value = self.variable.get()
            digits = str(int(value)).zfill(9) if value.isascii() and value.isdigit() else ""
            groups = [digits[:-6], digits[-6:-3], digits[-3:]] if digits else ["", "", ""]
            for part, group in zip(self.parts, groups):
                part.set(group)
            self.hint.configure(text="")
        finally:
            self._updating = False

    def _parts_changed(self, *_):
        if self._updating:
            return
        groups = [part.get() for part in self.parts]
        self._updating = True
        try:
            valid = all(s.isascii() and s.isdigit() and (len(s) in (3,4) if i == 0 else len(s)==3) for i,s in enumerate(groups))
            self.variable.set(str(int(groups[0] + groups[1].zfill(3) + groups[2].zfill(3))) if valid else "")
            self.hint.configure(text="" if valid else "频率未填写完整")
        finally:
            self._updating = False

    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            self._input_state = kwargs.pop("state")
            for entry in self.entries:
                entry.configure(state=self._input_state)
            if self.copy_button is not None:
                self.copy_button.configure(state=self._input_state)
            self.paste_button.configure(state=self._input_state)
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)

    def __getitem__(self, key):
        return self._input_state if key == "state" else super().__getitem__(key)


class SettingChoices(ttk.Frame):
    def __init__(self, parent, variable, options):
        super().__init__(parent)
        self.buttons = []
        for value in options.values():
            button = ttk.Radiobutton(self, text=value, value=value, variable=variable)
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)

    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            state = kwargs.pop("state")
            for button in self.buttons:
                button.configure(state=state)
        if cnf is not None or kwargs:
            return super().configure(cnf, **kwargs)

class BleRadio:
    """One worker loop, one N1 session, no Tk calls on this thread."""
    def __init__(self, emit, state, disconnected):
        self.emit, self.state, self.on_disconnected = emit, state, disconnected
        self.loop = self.thread = self.client = self.session = None
        self.ready = threading.Event()
        self.start_lock = threading.Lock()
        self.device = None
        self.read_char = N1_READ_CHAR
        self.write_char = COMMAND

    def start(self):
        with self.start_lock:
            if not self.thread:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("BLE 后台事件循环启动超时")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.close()

    def call(self, coro):
        try:
            self.start()
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception:
            coro.close()
            raise

    async def scan(self):
        if not BleakScanner:
            raise RuntimeError("请安装 bleak")
        devices = await BleakScanner.discover(timeout=6)
        LOGGER.info("scan devices=%s", [(d.name, d.address) for d in devices])
        return [(d.name, d.address) for d in devices if (d.name or "").upper().startswith("N1")]

    async def connect(self, address):
        if not BleakClient:
            raise RuntimeError("请安装 bleak")
        await self.disconnect()
        def disconnected(client):
            if self.client is client:
                if self.session:
                    self.session.disconnected()
                self.on_disconnected()
        client = BleakClient(address, disconnected_callback=disconnected)
        self.client = client
        try:
            await client.connect()
            rows = [(s.uuid, c.uuid, ",".join(c.properties)) for s in client.services for c in s.characteristics]
            found = {c.uuid.lower(): c for s in client.services if s.uuid.lower() == SERVICE for c in s.characteristics}
            if COMMAND not in found or "write" not in found[COMMAND].properties:
                raise RuntimeError("此设备没有 N1 的 BEC7 命令特征，不能配置频道")
            if not any(u in found and "notify" in found[u].properties for u in NOTIFICATIONS):
                raise RuntimeError("缺少 N1 响应通知特征")
            self.session = N1Session(client, self.state, self.emit)
            await self.session.start()
            self.device = address
            return rows
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self):
        client, session = self.client, self.session
        # Unlink before disconnect to avoid stale callback resetting a newer session.
        self.client = self.session = None
        self.device = None
        if session:
            await session.close()
        if client and client.is_connected:
            await client.disconnect()

    async def read_channels(self, progress=None, kinds=(1,2)):
        if not self.session:
            raise RuntimeError("尚未连接")
        return await self.session.read_channels(progress, kinds)

    async def read_current(self):
        if not self.session:
            raise RuntimeError("尚未连接")
        return await self.session.read_current()

    async def switch(self, channel):
        if not self.session:
            raise RuntimeError("尚未连接")
        return await self.session.switch(channel)

    async def sync(self, desired, original, current_mapping=None, kinds=(1,2)):
        if not self.session:
            raise RuntimeError("尚未连接")
        return await self.session.sync(desired, original, current_mapping, kinds)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.config_data = configparser.ConfigParser(interpolation=None)
        self.config_data.read(CONFIG_INI, encoding="utf-8")
        if not CONFIG_INI.exists():
            self.config_data.read(Path(__file__).with_name("n1_devices.ini"), encoding="utf-8")
        self.loaded_kinds = set()
        self.editor_key = None
        self._loading_editor = False
        self._refreshing = False
        self._column_resizing = False
        self._layout_ready = False
        self.tooltips = []
        self.title("对讲机频道配置器 · 努比亚N1")
        geometry = self.config_data.get("window", "geometry", fallback="1280x720")
        self.geometry(geometry if re.fullmatch(r"\d+x\d+(?:[+-]\d+[+-]\d+)?", geometry) else "1280x720")
        self.minsize(950, 600)
        self.events = queue.Queue()
        self.closed = self.busy = False
        self.current_channel = None
        self.device_current_channel = None
        self.order_mapping = {}
        self.snapshot_complete = False
        self.original = []
        self.channels, self.devices, self.char_rows = [], [], []
        self.ble = BleRadio(self.log, lambda key: self.enqueue(lambda: self._state(key)),
                            lambda: self.enqueue(self._disconnected))
        self._build()
        for variable in self.vars.values():
            variable.trace_add("write", self._editor_changed)
        for selector in self.tones.values():
            selector.mode.trace_add("write", self._editor_changed)
            selector.label.trace_add("write", self._editor_changed)
        self.load_saved_devices()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(40, self._drain)
        self.update_buttons()

    def _build(self):
        style=ttk.Style(self); style.configure("Treeview", rowheight=28); style.configure("Title.TLabel",font=("Microsoft YaHei",14,"bold"))
        self.save_font = tkfont.Font(font=style.lookup("TButton", "font") or "TkDefaultFont")
        self.save_font.configure(weight="bold")
        style.configure("Save.TButton", font=self.save_font)
        style.map("Treeview", foreground=[], background=[("selected", "#dcecff")])
        top=ttk.Frame(self,padding=8); top.pack(fill="x"); ttk.Label(top,text="蓝牙频道配置器",style="Title.TLabel").pack(side="left")
        self.status=tk.StringVar(value="未连接")
        statusbar = ttk.Frame(self,padding=(8,4)); statusbar.pack(side="bottom",fill="x")
        self.status_label = ttk.Label(statusbar,textvariable=self.status,foreground="black")
        self.status_label.pack(side="left")
        self.scan_btn=ttk.Button(top,text="扫描蓝牙",command=self.scan); self.scan_btn.pack(side="right"); self.devvar=tk.StringVar(); self.devbox=ttk.Combobox(top,textvariable=self.devvar,width=30,state="readonly"); self.devbox.pack(side="right",padx=6); self.disconnect_btn=ttk.Button(top,text="断开连接",command=self.disconnect,state="disabled"); self.disconnect_btn.pack(side="right",padx=4); self.connect_btn=ttk.Button(top,text="连接",command=self.connect); self.connect_btn.pack(side="right")
        pan=ttk.PanedWindow(self,orient="horizontal"); self.pane = pan; pan.pack(fill="both",expand=True,padx=8,pady=4)
        left=ttk.Frame(pan); right=ttk.Frame(pan); pan.add(left,weight=7); pan.add(right,weight=3)
        pan.bind("<Map>", lambda _: self.after_idle(self.restore_layout), add="+")
        bar=ttk.Frame(left); bar.pack(fill="x"); self.read_btn=ttk.Button(bar,text="从设备读取",command=self.read_device); self.read_btn.pack(side="left"); self.write_btn=ttk.Button(bar,text="写入设备",command=self.write_device); self.write_btn.pack(side="left",padx=5)
        self.add_btn = ttk.Button(bar,text="新增自定义频道",command=self.add); self.add_btn.pack(side="left")
        self.delete_btn = ttk.Button(bar,text="删除频道",command=self.delete); self.delete_btn.pack(side="left")
        ttk.Button(bar,text="导入 JSON",command=self.import_json).pack(side="right"); ttk.Button(bar,text="导出 JSON",command=self.export_json).pack(side="right",padx=5)
        filters = ttk.Frame(left); filters.pack(fill="x", pady=4)
        ttk.Label(filters, text="查找频道").pack(side="left")
        self.search = tk.StringVar(); ttk.Entry(filters, textvariable=self.search, width=24).pack(side="left", padx=4)
        saved_filter = self.config_data.get("ui", "channel_filter", fallback="全部")
        self.channel_filter = tk.StringVar(value=saved_filter if saved_filter in ("全部","预设","自定义") else "全部")
        for name in ("全部","预设","自定义"):
            ttk.Radiobutton(filters,text=name,value=name,variable=self.channel_filter).pack(side="left")
        self.search.trace_add("write", lambda *_: self.refresh())
        self.channel_filter.trace_add("write", self.filter_changed)
        self.up_btn = ttk.Button(filters, text="向上", command=lambda: self.move_channel(-1)); self.up_btn.pack(side="left", padx=4)
        self.down_btn = ttk.Button(filters, text="向下", command=lambda: self.move_channel(1)); self.down_btn.pack(side="left")
        self.pending_text = tk.StringVar(value="待保存 0 个频道")
        ttk.Label(filters,textvariable=self.pending_text).pack(side="right")
        self.column_weights = self.load_column_weights()
        cols=("seq","kind","name","rx","tx","rxt","txt")
        table = ttk.Frame(left); table.pack(fill="both",expand=True,pady=6)
        self.tree=ttk.Treeview(table,columns=cols,show="headings",selectmode="browse")
        self.tree.grid(row=0,column=0,sticky="nsew")
        table.rowconfigure(0,weight=1); table.columnconfigure(0,weight=1)
        scrollbar = ttk.Scrollbar(table,orient="vertical",command=self.tree.yview)
        scrollbar.grid(row=0,column=1,sticky="ns"); self.tree.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(table,orient="horizontal",command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky="ew"); self.tree.configure(xscrollcommand=horizontal.set)
        heads=("序号","类型","频道名称","接收频率 Hz","发射频率 Hz","接收亚音","发射亚音")
        for c,h in zip(cols,heads):
            self.tree.heading(c,text=h); self.tree.column(c,width=1,minwidth=1,stretch=False,anchor="center")
        self.tree.tag_configure("current", foreground="#d71919", background="#ffe6e6")
        self.tree.bind("<<TreeviewSelect>>",self.select)
        self.tree.bind("<Double-1>", self.double_click_channel)
        self.tree.bind("<Configure>",self.resize_columns)
        self.tree.bind("<ButtonRelease-1>",self.remember_columns,add="+")
        tabs = ttk.Notebook(right); tabs.pack(fill="both", expand=True)
        editor = ttk.Frame(tabs); device = ttk.Frame(tabs); extras = ttk.Frame(tabs)
        tabs.add(editor, text="频道编辑"); tabs.add(device, text="设备设置"); tabs.add(extras, text="耳机 / 批量配置")
        form=ttk.Frame(editor,padding=12); form.pack(fill="x"); self.vars={}
        self.tones = {}
        self.editor_widgets = {}
        self.kind_buttons = []
        fields=[("seq","序号"),("kind","类型"),("name","频道名称"),("rx","接收频率 Hz"),("tx","发射频率 Hz"),("rxt","接收亚音"),("txt","发射亚音")]
        for i,(k,h) in enumerate(fields):
            ttk.Label(form,text=h).grid(row=i,column=0,sticky="nw" if k in ("rx","tx") else "w",pady=(6,3) if k in ("rx","tx") else 3,padx=(0,8))
            v=tk.StringVar(); self.vars[k]=v
            if k in ("rxt", "txt"):
                self.tones[k] = ToneSelector(form)
                self.tones[k].grid(row=i,column=1,sticky="ew",pady=3)
            elif k == "kind":
                choices = ttk.Frame(form); choices.grid(row=i,column=1,sticky="w")
                for label, value in [("预设", "1"), ("自定义", "2")]:
                    button = ttk.Radiobutton(choices,text=label,variable=v,value=value)
                    button.pack(side="left",padx=4); self.kind_buttons.append(button)
            elif k in ("rx", "tx"):
                widget = FrequencyInput(form, v)
                widget.grid(row=i, column=1, sticky="w", pady=3)
                self.editor_widgets[k] = widget
            else:
                widget = ttk.Entry(form,textvariable=v,width=30)
                widget.grid(row=i,column=1,sticky="ew",pady=3)
                self.editor_widgets[k] = widget
        self.editor_widgets["rx"].add_copy_button("同发射频率", self.vars["tx"])
        self.editor_widgets["tx"].add_copy_button("同接收频率", self.vars["rx"])
        self.editor_widgets["rx"].next_input = self.editor_widgets["tx"]
        self.editor_widgets["tx"].previous_input = self.editor_widgets["rx"]
        actions = ttk.Frame(form); actions.grid(row=len(fields),column=0,columnspan=2,pady=8)
        self.switch_btn = ttk.Button(actions,text="切换到此频道",command=self.switch_selected)
        self.switch_btn.pack(side="left")
        ttk.Button(actions,text="应用到列表",command=self.apply).pack(side="left",padx=4)
        self.save_one_btn = ttk.Button(actions,text="保存频道",command=self.save_one,style="Save.TButton")
        self.save_one_btn.pack(side="left")
        self.editor_hint = tk.StringVar()
        ttk.Label(form,textvariable=self.editor_hint,wraplength=350,foreground="#777777").grid(row=len(fields)+1,column=0,columnspan=2,sticky="w")
        self._build_device_settings(device)
        self._build_extras(extras)
        self.add_tooltips()

    def load_column_weights(self):
        defaults = dict(seq=7, kind=7, name=16, rx=13, tx=13, rxt=24, txt=24)
        result = {}
        for key, default in defaults.items():
            try:
                value = self.config_data.getfloat("column_widths", key, fallback=default)
                if not 0 < value < float("inf"):
                    raise ValueError("比例必须为正数")
                result[key] = value
            except ValueError:
                LOGGER.warning("Invalid column ratio for %s; using %s", key, default)
                result[key] = default
        return result

    def resize_columns(self, event):
        self._column_resizing = True
        total = sum(self.column_weights.values())
        width = max(1, event.width - 4)
        used = 0
        columns = list(self.column_weights)
        for i, key in enumerate(columns):
            pixels = max(1, round(width * self.column_weights[key] / total)) if i < len(columns)-1 else max(1, width-used)
            self.tree.column(key, width=pixels)
            used += pixels
        self._column_resizing = False

    def remember_columns(self, _=None):
        self.column_weights = {key:self.tree.column(key,"width") for key in self.column_weights}

    def restore_layout(self):
        if self._layout_ready:
            return
        try:
            ratio = self.config_data.getfloat("window","split",fallback=0.66)
        except ValueError:
            ratio = 0.66
        self.pane.sashpos(0,int(self.pane.winfo_width()*min(0.85,max(0.25,ratio))))
        self._layout_ready = True

    def save_config(self, layout=False):
        self.config_data["ui"] = {"channel_filter":self.channel_filter.get()}
        if layout and self._layout_ready:
            self.config_data["window"] = {"geometry":self.geometry(),"split":str(self.pane.sashpos(0)/max(1,self.pane.winfo_width()))}
            self.config_data["column_widths"] = {key:str(self.tree.column(key,"width")) for key in self.column_weights}
        temp = CONFIG_INI.with_suffix(".ini.tmp")
        with temp.open("w",encoding="utf-8") as stream:
            self.config_data.write(stream)
        temp.replace(CONFIG_INI)

    def filter_changed(self, *_):
        self.refresh()
        self.save_config()

    def filter_kinds(self):
        return {"全部":(1,2),"预设":(1,),"自定义":(2,)}[self.channel_filter.get()]

    def known_kinds(self):
        return self.loaded_kinds or ({c.kind for c in self.original} if self.snapshot_complete else set())

    def set_status(self, text, result=None):
        self.status.set(text)
        self.status_label.configure(foreground="#16803c" if result == "success" else "#c62828" if result == "error" else "black")

    def editor_differs(self, channel):
        if not channel or self.editor_key != channel.key:
            return False
        values = dict(seq=str(channel.seq),kind=str(channel.kind),name=channel.name,rx=str(channel.rx),tx=str(channel.tx))
        try:
            return any(self.vars[k].get()!=v for k,v in values.items()) or self.tones["rxt"].get_value()!=channel.rx_tone or self.tones["txt"].get_value()!=channel.tx_tone
        except (ValueError,KeyError):
            return True

    def dirty_keys(self):
        before = {c.key:c for c in self.original}
        after = {c.key:c for c in self.channels}
        dirty = {key for key in set(before)|set(after) if key not in before or key not in after or before[key].encode()!=after[key].encode()}
        if self.editor_key in after:
            reference = before.get(self.editor_key)
            if reference is None or self.editor_differs(reference):
                dirty.add(self.editor_key)
            elif not self.editor_differs(after[self.editor_key]):
                # A list edit still differs if the editor has not reverted it.
                pass
            else:
                dirty.discard(self.editor_key)
        dirty.update(self.order_mapping)
        return dirty

    def _editor_changed(self, *_):
        if self._loading_editor:
            return
        self.update_dirty_marks()
        self.update_buttons()

    def update_dirty_marks(self):
        dirty = self.dirty_keys()
        self.pending_text.set(f"待保存 {len(dirty)} 个频道")
        for c in self.channels:
            row = f"{c.kind}:{c.seq}"
            if self.tree.exists(row):
                name = c.display_name + (" *" if c.key in dirty else "")
                if self.tree.set(row,"name") != name:
                    self.tree.set(row,"name",name)

    def disabled_reason(self, action):
        selected = self.selected()
        if self.busy:
            return "正在与设备通信，请等待操作完成"
        if action in ("frequency","fixed","name"):
            return "请选择频道" if not selected else "预设频率不可修改" if selected.kind==1 and action=="frequency" else "预设频道名称不可修改" if action=="name" and selected.kind==1 else "已读频道的类型和序号固定" if action=="fixed" else ""
        if action in ("up","down"):
            return "仅自定义频道支持调整排序" if not selected or selected.kind==1 else "请先完整读取自定义频道" if 2 not in self.known_kinds() else "已到达排序边界"
        if action == "add" and (self.channel_filter.get()=="预设" or selected and selected.kind==1):
            return "设备不支持新增预设频道"
        if action == "delete" and selected and selected.kind==1:
            return "设备不支持删除预设频道"
        if not self.connected:
            return "请先连接 N1 设备"
        if action in ("save","delete","switch") and self.order_mapping:
            return "排序尚未写入，暂不能单独保存" if action=="save" else "请先写入排序，再进行此操作"
        if action in ("save","delete","switch") and not selected:
            return "请选择频道"
        if not self.snapshot_complete or action in ("save","delete","switch") and selected.kind not in self.known_kinds() or action=="add" and 2 not in self.known_kinds():
            return "请先读取对应类型的完整频道配置"
        if action=="write" and not set(self.filter_kinds()) & self.known_kinds():
            return "请先读取所选类型的完整频道配置"
        if action == "save":
            return "频道没有修改，无需保存"
        if action == "write":
            return "当前范围没有待保存修改"
        if action == "switch":
            return "该频道已经是当前频道"
        if action == "connect":
            return "设备已连接"
        return ""

    def add_tooltips(self):
        for widget,action in [(self.save_one_btn,"save"),(self.write_btn,"write"),(self.add_btn,"add"),(self.delete_btn,"delete"),(self.switch_btn,"switch"),(self.up_btn,"up"),(self.down_btn,"down"),(self.connect_btn,"connect")]:
            self.tooltips.append(Tooltip(widget,lambda w=widget,a=action: self.disabled_reason(a) if str(w["state"])=="disabled" else ""))
        for key,widget in self.editor_widgets.items():
            targets = widget.entries + [widget.paste_button,widget.copy_button] if isinstance(widget,FrequencyInput) else [widget]
            for target in targets:
                self.tooltips.append(Tooltip(target,lambda w=widget,k=key: self.disabled_reason("frequency" if k in ("rx","tx") else "name" if k=="name" else "fixed") if str(w["state"])=="disabled" else ""))
        for button in self.kind_buttons:
            self.tooltips.append(Tooltip(button,"N1 预设频道不可新增；已读频道的类型固定"))
        for widget,action in [(self.read_btn,"read"),(self.disconnect_btn,"disconnect"),(self.scan_btn,"scan")]:
            self.tooltips.append(Tooltip(widget,lambda w=widget,a=action: self.disabled_reason(a) if str(w["state"])=="disabled" else ""))
        for selector in self.tones.values():
            self.tooltips.append(Tooltip(selector.box,lambda s=selector: "关闭亚音时无需选择列表项" if s.mode.get()==0 else ""))
        for widget in self.setting_widgets.values():
            targets = widget.buttons if isinstance(widget,SettingChoices) else [widget]
            for target in targets:
                self.tooltips.append(Tooltip(target,lambda w=target: "请先连接并读取设备信息；设备未返回或无法识别的设置保持禁用" if str(w["state"])=="disabled" else ""))

    def _build_device_settings(self, parent):
        self.info_values = {}
        self.setting_values = {}
        self.setting_widgets = {}
        self.device_snapshot = None
        self.setting_options = {
            10: {0: "关闭", 1: "开启"},
            12: {0: "关闭", 1: "开启"},
            13: {1: "关闭", 2: "中文", 3: "英文"},
            15: {0: "关闭", 1: "开启"},
            "ch7": {0: "正常", 1: "灵敏"},
            "ch8": {0: "关闭", 1: "开启"},
            "ch12": {0: "宽频带", 1: "窄频带"},
            "ch13": {0: "关闭", 1: "开启"},
        }
        frame = ttk.Frame(parent, padding=10); frame.pack(fill="both", expand=True)
        row = 0
        for tag, name in [(4, "硬件版本（设备原值）"), (5, "固件版本（设备原值）"),
                          (6, "设备名称"), (8, "电量（设备原值）")]:
            self.info_values[tag] = tk.StringVar(value="未读取")
            ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Label(frame, textvariable=self.info_values[tag]).grid(row=row, column=1, sticky="w")
            row += 1
        for tag, name in [(7, "蓝牙名称"), (10, "提示音"), (12, "省电模式"),
                          (13, "语音播报"), (15, "自动锁频"),
                          ("ch8", "VOX 声控"), ("ch7", "静噪灵敏度"), ("ch12", "宽 / 窄频带"), ("ch13", "礼貌模式")]:
            var = tk.StringVar(); self.setting_values[tag] = var
            ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w", pady=4)
            if tag == 7:
                widget = ttk.Entry(frame, textvariable=var, width=26)
            else:
                widget = SettingChoices(frame, var, self.setting_options[tag])
            widget.grid(row=row, column=1, sticky="ew")
            widget.configure(state="disabled")
            self.setting_widgets[tag] = widget
            row += 1
        buttons = ttk.Frame(frame); buttons.grid(row=row, column=0, columnspan=2, pady=10)
        ttk.Button(buttons, text="刷新设备信息", command=self.read_info).pack(side="left")
        ttk.Button(buttons, text="写入设置并核验", command=self.write_info).pack(side="left", padx=8)
        ttk.Label(frame, text="设备未返回的设置保持禁用。频道表无需写入即可修改这些设置。", wraplength=360).grid(row=row+1, column=0, columnspan=2)

    def _build_extras(self, parent):
        self.headsets = {}
        self.headset_var = tk.StringVar()
        self.headset_status = tk.StringVar(value="未查询耳机状态")
        ear = ttk.LabelFrame(parent, text="对讲机连接蓝牙耳机", padding=10); ear.pack(fill="x", padx=8, pady=8)
        ttk.Label(ear, textvariable=self.headset_status, wraplength=350).pack(fill="x")
        self.headset_box = ttk.Combobox(ear, textvariable=self.headset_var, state="readonly", width=38); self.headset_box.pack(fill="x", pady=6)
        row = ttk.Frame(ear); row.pack(fill="x")
        for label, command in [("扫描耳机", lambda: self.ear_scan(0)), ("暂停", lambda: self.ear_scan(1)),
                               ("停止", lambda: self.ear_scan(2)), ("刷新状态", lambda: self.ear_state(0))]:
            ttk.Button(row, text=label, command=command).pack(side="left")
        row = ttk.Frame(ear); row.pack(fill="x", pady=6)
        ttk.Button(row, text="连接选中耳机", command=lambda: self.ear_state(1)).pack(side="left")
        ttk.Button(row, text="断开耳机", command=lambda: self.ear_state(2)).pack(side="left", padx=6)
        batch = ttk.LabelFrame(parent, text="批量配置 / 备份", padding=10); batch.pack(fill="x", padx=8, pady=8)
        ttk.Label(batch, text="先完成频道写入，再让对讲机开始批量配置。\n该功能对应 APK 的 startBatchConfig / stopBatchConfig。", wraplength=350).pack(fill="x")
        row = ttk.Frame(batch); row.pack(fill="x", pady=6)
        ttk.Button(row, text="开始批量配置", command=lambda: self.broadcast(True)).pack(side="left")
        ttk.Button(row, text="停止批量配置", command=lambda: self.broadcast(False)).pack(side="left", padx=6)
        row = ttk.Frame(batch); row.pack(fill="x")
        ttk.Button(row, text="备份设备频道", command=self.backup_channels).pack(side="left")
        ttk.Button(row, text="撤销本地修改", command=self.revert_channels).pack(side="left", padx=6)
        ttk.Button(row, text="复制选中频道", command=self.duplicate_channel).pack(side="left")

    def _session_action(self, name, *args, done=None):
        if self.busy or not self.connected:
            return
        labels = {"read_device_info":"读取设备信息","update_device_info":"保存设备设置","headset_scan":"操作耳机扫描","headset_state":"读取耳机状态","broadcast_config":"操作批量配置"}
        label = labels[name]
        if name == "headset_state" and args and args[0]:
            label = "连接耳机" if args[0]==1 else "断开耳机"
        self.set_status(label+"中...")
        def complete(result):
            self.set_status(label+"成功","success")
            if done:
                done(result)
        self.run(getattr(self.ble.session, name)(*args), complete)

    def read_info(self):
        self._session_action("read_device_info", done=self.show_info)

    def show_info(self, info):
        self.device_snapshot = info
        if self.ble.device and info.get(7):
            name = info[7].decode("utf-8", errors="replace").rstrip("\0")
            saved = [(name if address.upper() == self.ble.device.upper() else old_name, address)
                     for old_name, address in self.devices]
            if saved != self.devices:
                index = self.devbox.current()
                self._set_devices(saved)
                if 0 <= index < len(saved):
                    self.devbox.current(index)
                self.save_devices(saved)
        for tag, var in self.info_values.items():
            value = info.get(tag, "未返回")
            if isinstance(value, bytes):
                value = value.decode("gbk", errors="replace").rstrip("\0")
            var.set(value)
        ch = fields(info.get(9, b""))
        for tag, var in self.setting_values.items():
            values = ch if isinstance(tag, str) else info
            number = int(tag[2:]) if isinstance(tag, str) else tag
            present = number in values
            value = values.get(number)
            if tag == 7:
                var.set(value.decode("utf-8", errors="replace").rstrip("\0") if present else "")
            else:
                var.set(self.setting_options[tag].get(value, f"未知({value})") if present else "未返回")
            supported = present and (tag == 7 or value in self.setting_options[tag])
            self.setting_widgets[tag].configure(state="normal" if supported else "disabled")
        self.set_status("设备信息已刷新")
        self.log("设备信息：" + repr(info))

    def write_info(self):
        if self.busy or not self.connected or self.device_snapshot is None:
            return
        try:
            changes, channel_changes = {}, {}
            old_ch = fields(self.device_snapshot.get(9, b""))
            for tag, var in self.setting_values.items():
                old = old_ch if isinstance(tag, str) else self.device_snapshot
                number = int(tag[2:]) if isinstance(tag, str) else tag
                if number not in old:
                    continue
                if tag == 7:
                    value = var.get().encode("utf-8")
                    if not value:
                        raise ValueError("蓝牙名称不能为空")
                    if value.decode("utf-8") == old[number].decode("utf-8", errors="replace").rstrip("\0"):
                        continue
                else:
                    if var.get() == f"未知({old[number]})":
                        continue
                    value = next(k for k, v in self.setting_options[tag].items() if v == var.get())
                if value != old[number]:
                    (channel_changes if isinstance(tag, str) else changes)[number] = value
            self._session_action("update_device_info", changes, channel_changes, done=self.show_info)
        except Exception as error:
            messagebox.showerror("设置错误", str(error))

    def ear_scan(self, option):
        if option == 0:
            self.headsets.clear(); self.headset_box["values"] = []; self.headset_var.set("")
        if self.ble.session:
            self.ble.session.on_headset = lambda info: self.enqueue(lambda: self._headset_found(info))
        self._session_action("headset_scan", option, done=lambda _: self.headset_status.set(["扫描中", "已暂停扫描", "已停止扫描"][option]))

    def _headset_found(self, info):
        if not self.connected:
            return
        mac = info.get(1, b"").hex(":").upper()
        name = info.get(2, b"").decode("gbk", errors="replace").rstrip("\0")
        label = f"{name} | {mac}"
        self.headsets[label] = info
        self.headset_box["values"] = list(self.headsets)
        if not self.headset_var.get():
            self.headset_var.set(label)

    def ear_state(self, option):
        ear = self.headsets.get(self.headset_var.get())
        if option == 1 and not ear:
            return messagebox.showinfo("提示", "请先扫描并选择耳机")
        def done(info):
            state = fields(info.get(4, b""))
            name = state.get(2, b"").decode("gbk", errors="replace").rstrip("\0")
            self.headset_status.set(f"设备耳机状态：{name if name else '未连接耳机' if not state.get(1) else state[1].hex(':').upper()}")
            self.log("耳机状态：" + repr(info))
        self._session_action("headset_state", option, ear, done=done)

    def broadcast(self, start):
        if start and (not self.snapshot_complete or self.known_kinds()!={1,2} or self.dirty_keys()):
            return messagebox.showinfo("提示", "请先完整读取频道，并把本地修改写入设备，再开始批量配置")
        self._session_action("broadcast_config", start, done=lambda _: self.set_status("批量配置已开始" if start else "批量配置已停止"))

    def backup_channels(self):
        if not self.snapshot_complete:
            return messagebox.showinfo("提示", "请先完整读取设备频道")
        from datetime import datetime
        path = DATA_DIR / f"N1_{datetime.now():%Y%m%d_%H%M%S}.json"
        snapshot = [c for c in self.original if c.kind in self.known_kinds()]
        path.write_text(json.dumps([c.to_apk() for c in snapshot], ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"设备频道备份：{path}")
        self.set_status(f"已备份 {len(snapshot)} 个已读取频道","success")

    def revert_channels(self):
        if not self.busy:
            self.order_mapping = {}
            self.current_channel = self.device_current_channel
            self.channels = [replace(c) for c in self.original]
            self.select()
            self.refresh(); self.set_status("已撤销本地修改")

    def duplicate_channel(self):
        old = self.selected()
        if old and not self.busy:
            if not self.connected or not self.snapshot_complete or 2 not in self.known_kinds():
                return messagebox.showinfo("提示", "请先连接并读取设备频道")
            seq = max((c.seq for c in self.channels + self.original if c.kind == 2), default=0) + 1
            if seq > 65535:
                return
            self.insert_channel(replace(old, kind=2, seq=seq, name=old.display_name + " 副本"))


    def enqueue(self, callback):
        if not self.closed:
            self.events.put(callback)

    def _drain(self):
        for _ in range(200):
            try:
                callback = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                LOGGER.exception("UI event failed")
        if not self.closed:
            self.after(40, self._drain)

    def log(self, text):
        LOGGER.info("UI %s", text)

    @property
    def connected(self):
        return bool(self.ble.client and self.ble.client.is_connected)

    def update_buttons(self):
        self.connect_btn.configure(text="已连接" if self.connected else "连接",
                                   state="disabled" if self.connected or self.busy else "normal")
        self.disconnect_btn.configure(state="normal" if self.connected and not self.busy else "disabled")
        self.scan_btn.configure(state="disabled" if self.busy else "normal")
        self.read_btn.configure(state="normal" if self.connected and not self.busy else "disabled")
        scope = set(self.filter_kinds()) & self.known_kinds()
        dirty = self.dirty_keys()
        self.write_btn.configure(state="normal" if self.connected and not self.busy and self.snapshot_complete and any(k[0] in scope for k in dirty) else "disabled")
        self.devbox.configure(state="disabled" if self.connected or self.busy else "readonly")
        selected = self.selected()
        preset = bool(selected and selected.kind == 1)
        preset_context = preset or self.channel_filter.get() == "预设"
        self.add_btn.configure(state="normal" if self.connected and self.snapshot_complete and 2 in self.known_kinds() and not self.busy and not preset_context else "disabled")
        self.delete_btn.configure(state="normal" if self.connected and self.snapshot_complete and selected and selected.kind in self.known_kinds() and not preset and not self.busy and not self.order_mapping else "disabled")
        custom = sorted((c for c in self.channels if c.kind == 2), key=lambda c: c.seq)
        position = next((i for i,c in enumerate(custom) if selected and c.key == selected.key), -1)
        self.up_btn.configure(state="normal" if self.snapshot_complete and 2 in self.known_kinds() and not self.busy and position > 0 else "disabled")
        self.down_btn.configure(state="normal" if self.snapshot_complete and 2 in self.known_kinds() and not self.busy and 0 <= position < len(custom)-1 else "disabled")
        existing = bool(selected and selected.key in {c.key for c in self.original})
        for key, widget in self.editor_widgets.items():
            locked = not selected or self.busy or (preset and key in ("seq", "name", "rx", "tx")) or (existing and key == "seq")
            widget.configure(state="disabled" if locked else "normal")
        for button in self.kind_buttons:
            # All new channels are custom: N1's preset bank cannot grow.
            button.configure(state="disabled")
        self.editor_hint.set("预设频道仅能修改接收/发射亚音；不能新增或删除。" if preset else "新增频道为自定义频道；已读频道的类型和序号固定。" if selected else "请选择频道")
        can_save = self.connected and not self.busy and self.snapshot_complete and selected and selected.kind in self.known_kinds()
        self.save_one_btn.configure(state="normal" if can_save and selected.key in dirty and not self.order_mapping else "disabled")
        is_current = selected and selected.key == self.current_channel
        self.switch_btn.configure(text="当前频道" if is_current else "切换到此频道",
                                  state="normal" if can_save and not is_current and not self.order_mapping else "disabled")

    def run(self, coro, done):
        if self.busy:
            coro.close()
            return
        self.busy = True
        self.set_status(self.status.get() if "中" in self.status.get() else "设备操作中...")
        self.update_buttons()
        try:
            future = self.ble.call(coro)
        except Exception as error:
            self.busy = False
            self.update_buttons()
            self.log(str(error))
            self.set_status("操作失败："+str(error),"error")
            return
        def finished(f):
            def dispatch():
                self.busy = False
                self.update_buttons()
                try:
                    done(f.result())
                    if not self.busy:
                        self.set_status(self.status.get() if "中..." not in self.status.get() else "操作成功","success")
                    self.update_dirty_marks()
                    self.update_buttons()
                except Exception as error:
                    LOGGER.exception("Operation failed")
                    self.set_status("操作失败："+str(error),"error")
                    self.log(str(error))
                    messagebox.showerror("操作失败", str(error))
            self.enqueue(dispatch)
        future.add_done_callback(finished)

    def scan(self):
        if self.busy:
            return
        self.set_status("扫描中...")
        def done(ds):
            merged = {a.upper(): (n, a) for n, a in self.devices}
            merged.update({a.upper(): (n, a) for n, a in ds})
            self._set_devices(list(merged.values()))
            self.save_devices(self.devices)
            self.set_status(f"本次发现 {len(ds)} 个 N1，已保存 {len(self.devices)} 个")
        self.run(self.ble.scan(), done)

    def _set_devices(self, ds):
        self.devices = ds
        self.devbox["values"] = [f"{n} | {a}" for n, a in ds]
        if ds:
            self.devbox.current(0)
        else:
            self.devvar.set("")

    def load_saved_devices(self):
        try:
            cp = self.config_data
            devices = []
            for section in cp.sections():
                name = cp.get(section, "name", fallback="")
                address = cp.get(section, "address", fallback="").strip()
                if name and address:
                    devices.append((name, address))
            self._set_devices(devices)
            self.set_status(f"已加载 {len(devices)} 个保存设备")
        except Exception:
            LOGGER.exception("load saved devices failed")

    def save_devices(self, ds):
        cp = self.config_data
        for section in list(cp.sections()):
            if section.startswith("device_"):
                cp.remove_section(section)
        for i, (name, address) in enumerate(ds, 1):
            cp[f"device_{i}"] = {"name": name, "address": address}
        self.save_config()

    def connect(self):
        if self.busy or self.connected:
            return
        index = self.devbox.current()
        if not 0 <= index < len(self.devices):
            return messagebox.showinfo("提示", "请选择保存设备或扫描 N1")
        name, address = self.devices[index]
        self.set_status("连接中...")
        self.channels, self.original = [], []
        self.current_channel = None
        self.device_current_channel = None
        self.order_mapping = {}
        self.snapshot_complete = False
        self.loaded_kinds = set()
        self.editor_key = None
        self.device_snapshot = None
        self.refresh()
        def done(rows):
            self.char_rows = rows
            self.log("GATT 特征:\n" + "\n".join(" | ".join(r) for r in rows))
            self.set_status("已连接 " + name)
            self.update_buttons()
            self.read_device()
        self.run(self.ble.connect(address), done)

    def _disconnected(self):
        for widget in self.setting_widgets.values():
            widget.configure(state="disabled")
        self.current_channel = None
        self.device_current_channel = None
        for row in self.tree.get_children():
            self.tree.item(row, tags=())
        self.set_status("未连接；保留配置，可继续编辑和导出")
        self.update_buttons()

    def disconnect(self):
        if not self.busy:
            self.set_status("断开连接中...")
            self.run(self.ble.disconnect(), lambda _: self._disconnected())

    def gatt(self):
        if self.char_rows and self.connected:
            self.log("GATT 特征:\n" + "\n".join(" | ".join(r) for r in self.char_rows))
        else:
            self.log("请先连接设备")

    def _state(self, key):
        self.device_current_channel = key
        self.current_channel = self.order_mapping.get(key, key)
        self.refresh()

    def double_click_channel(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row); self.select(); self.switch_selected()

    def move_channel(self, direction):
        if self.busy or not self.snapshot_complete or 2 not in self.known_kinds():
            return
        selected = self.selected()
        if not selected or selected.kind != 2:
            return
        custom = sorted((c for c in self.channels if c.kind == 2), key=lambda c: c.seq)
        index = next(i for i,c in enumerate(custom) if c.key == selected.key)
        other_index = index + direction
        if not 0 <= other_index < len(custom):
            return
        selected = self.apply()
        if selected is None:
            return
        other = next(c for c in self.channels if c.key == custom[other_index].key)
        first, second = selected.key, other.key
        moved = replace(selected, seq=other.seq)
        swapped = replace(other, seq=selected.seq)
        self.channels = sorted([c for c in self.channels if c.key not in (first, second)] + [moved, swapped], key=lambda c:c.key)
        mapping = {c.key:self.order_mapping.get(c.key, c.key) for c in self.original if c.kind == 2}
        for key, destination in mapping.items():
            if destination == first:
                mapping[key] = second
            elif destination == second:
                mapping[key] = first
        self.order_mapping = {key:value for key,value in mapping.items() if key != value}
        actual = self.device_current_channel
        if actual is None and not self.order_mapping:
            actual = self.current_channel
        self.current_channel = self.order_mapping.get(actual, actual)
        self.refresh(); self.focus_channel(moved); self.select()
        self.set_status("排序待写入；设备尚未切换频道" if self.order_mapping else "已恢复原排序")

    def refresh(self):
        self._refreshing = True
        view = self.tree.yview()
        old_rows = self.tree.get_children()
        top_index = min(len(old_rows)-1,int(view[0]*len(old_rows))) if old_rows and view else 0
        top_row = old_rows[top_index] if old_rows else None
        dirty = self.dirty_keys()
        keys = {f"{c.kind}:{c.seq}" for c in self.channels}
        for row in set(getattr(self,"_tree_rows",set())) - keys:
            if self.tree.exists(row):
                self.tree.delete(row)
        self._tree_rows = keys
        position = 0
        for c in self.channels:
            row = f"{c.kind}:{c.seq}"
            term = self.search.get().strip().lower()
            visible = c.kind in self.filter_kinds() and (not term or term in f"{c.seq:02d} {c.display_name} {c.rx} {c.tx} {format_frequency(c.rx)} {format_frequency(c.tx)}".lower())
            tags=("current",) if c.key == self.current_channel else ()
            values=(
                c.seq, "预设" if c.kind == 1 else "自定义", c.display_name + (" *" if c.key in dirty else ""),
                format_frequency(c.rx), format_frequency(c.tx), TONE_LABEL.get(c.rx_tone, f"未知({c.rx_tone})"),
                TONE_LABEL.get(c.tx_tone, f"未知({c.tx_tone})"))
            if self.tree.exists(row):
                old_values=self.tree.item(row,"values")
                if tuple(map(str,values))!=old_values or self.tree.item(row,"tags")!=tags:
                    self.tree.item(row,values=values,tags=tags)
            else:
                self.tree.insert("","end",iid=row,values=values,tags=tags)
            if visible:
                if position >= len(self.tree.get_children()) or self.tree.get_children()[position]!=row:
                    self.tree.move(row,"",position)
                position += 1
            else:
                self.tree.detach(row)
        new_rows = self.tree.get_children()
        for row in self.tree.selection():
            if row not in new_rows:
                self.tree.selection_remove(row)
        if top_row in new_rows:
            self.tree.yview_moveto(new_rows.index(top_row)/max(1,len(new_rows)))
        elif view:
            self.tree.yview_moveto(view[0])
        self._refreshing = False
        self.update_dirty_marks()
        self.update_buttons()

    def focus_channel(self, channel):
        row = f"{channel.kind}:{channel.seq}"
        if row not in self.tree.get_children():
            self.search.set("")
            self.channel_filter.set("全部")
            self.refresh()
        self.tree.selection_set(row)
        self.tree.see(row)

    def selected(self):
        selection = self.tree.selection()
        if selection:
            kind, seq = map(int, selection[0].split(":"))
            return next((c for c in self.channels if c.key == (kind, seq)), None)

    def switch_selected(self):
        c = self.selected()
        if c:
            self.switch_channel(c)

    def select(self, _=None):
        c = self.selected()
        if not c:
            self.update_buttons()
            return
        if _ is not None and self.editor_key == c.key:
            return
        self._loading_editor = True
        self.editor_key = c.key
        values = dict(seq=c.seq, kind=c.kind, name=c.name, rx=c.rx, tx=c.tx,
                      rxt=c.rx_tone, txt=c.tx_tone)
        for k, v in values.items():
            if k in self.tones:
                self.tones[k].set_value(v)
            else:
                self.vars[k].set(v)
        self._loading_editor = False
        self.update_dirty_marks()
        self.update_buttons()

    def validate(self, channels):
        seen = set()
        for c in channels:
            if c.key in seen:
                raise ValueError(f"频道类型及序号重复：{c.key}")
            seen.add(c.key)
            if c.kind not in (1, 2) or not 1 <= c.seq <= 65535:
                raise ValueError("频道类型须为 1/2，序号须为 1–65535")
            if not 0 < c.rx <= 0xffffffff or not 0 < c.tx <= 0xffffffff:
                raise ValueError("频率须为有效的正整数 Hz")
            if c.rx_tone not in TONE_LABEL or c.tx_tone not in TONE_LABEL:
                raise ValueError(f"频道 {c.key} 有未识别的亚音，不能写入")
            c.encode()  # Reject unsupported GBK characters and malformed imported protocol fields.

    def apply(self):
        if self.busy:
            return
        old = self.selected()
        if not old:
            return
        try:
            d = {k: self.vars[k].get() for k in self.vars}
            tones = [self.tones[k].get_value() for k in ("rxt", "txt")]
            item = replace(old, seq=int(d["seq"]), kind=int(d["kind"]), name=d["name"],
                           rx=int(d["rx"]), tx=int(d["tx"]), rx_tone=tones[0], tx_tone=tones[1])
            if old.key in {c.key for c in self.original} and old.key != item.key:
                raise ValueError("已读频道的类型/序号是设备标识，不能修改；请新增频道")
            validate_preset_change(item, old if old.kind == 1 else None)
            candidates = [item if c.key == old.key else c for c in self.channels]
            self.validate([item])
            if len({c.key for c in candidates}) != len(candidates):
                raise ValueError("频道类型及序号重复")
            self.channels = sorted(candidates, key=lambda c: c.key)
            self.refresh()
            self.focus_channel(item)
            self.select()
            self.set_status("本地修改待写入")
            return item
        except (ValueError, KeyError, UnicodeError) as error:
            self.set_status("输入错误："+str(error),"error")
            messagebox.showerror("输入错误", str(error))

    def save_one(self):
        if self.busy or not self.connected or not self.snapshot_complete or self.order_mapping or not self.selected() or self.selected().kind not in self.known_kinds() or self.selected().key not in self.dirty_keys():
            return
        item = self.apply()
        if item is None:
            return
        existing = item.key in {c.key for c in self.original}
        self.snapshot_complete = False
        self.set_status(f"保存 {item.display_name} 并读回核验...")
        def done(actual):
            self.channels = [actual if c.key == actual.key else c for c in self.channels]
            self.original = sorted([c for c in self.original if c.key != actual.key] + [replace(actual)], key=lambda c: c.key)
            self.snapshot_complete = True
            self.refresh(); self.update_buttons()
            self.set_status(f"频道 {actual.seq:02d} 保存成功，已读回核验")
        self.run(self.ble.session.save_channel(item, existing), done)

    def add(self):
        if self.busy or not self.connected or not self.snapshot_complete or 2 not in self.known_kinds():
            return
        if self.channel_filter.get() == "预设" or (self.selected() and self.selected().kind == 1):
            return messagebox.showinfo("提示", "设备不支持新增预设频道；请选择自定义频道后新增")
        used = {c.seq for c in self.channels + self.original if c.kind == 2}
        seq = max(used, default=0) + 1
        if seq > 65535:
            return messagebox.showerror("新增失败", "频道编号超出范围")
        dialog = tk.Toplevel(self); dialog.title("新增自定义频道"); dialog.transient(self); dialog.grab_set()
        form = ttk.Frame(dialog, padding=12); form.pack(fill="both", expand=True)
        variables = {}
        frequency_widgets = {}
        for i,(key, label, value) in enumerate([("name", "频道名称", f"自定义频道 C{seq:02d}"),
                                               ("rx", "接收频率 Hz", "430000000"), ("tx", "发射频率 Hz", "430000000")]):
            ttk.Label(form,text=label).grid(row=i,column=0,sticky="nw" if key in ("rx","tx") else "w",pady=(6,3) if key in ("rx","tx") else 3,padx=(0,8))
            variables[key] = tk.StringVar(value=value)
            widget = FrequencyInput(form,variables[key]) if key in ("rx","tx") else ttk.Entry(form,textvariable=variables[key],width=28)
            widget.grid(row=i,column=1,sticky="w",pady=4)
            if key in ("rx","tx"):
                frequency_widgets[key] = widget
        frequency_widgets["rx"].add_copy_button("同发射频率",variables["tx"])
        frequency_widgets["tx"].add_copy_button("同接收频率",variables["rx"])
        frequency_widgets["rx"].next_input = frequency_widgets["tx"]
        frequency_widgets["tx"].previous_input = frequency_widgets["rx"]
        tones = {}
        for row,key,label in [(3,"rxt","接收亚音"), (4,"txt","发射亚音")]:
            ttk.Label(form,text=label).grid(row=row,column=0,sticky="w")
            tones[key] = ToneSelector(form); tones[key].grid(row=row,column=1,pady=4)
        def save():
            try:
                channel = Channel(seq=seq, name=variables["name"].get(), rx=int(variables["rx"].get()), tx=int(variables["tx"].get()),
                                  rx_tone=tones["rxt"].get_value(), tx_tone=tones["txt"].get_value())
                self.validate([channel])
            except Exception as error:
                return messagebox.showerror("输入错误",str(error),parent=dialog)
            dialog.destroy(); self.insert_channel(channel)
        ttk.Button(form,text="新增并保存到设备",command=save).grid(row=5,column=1,pady=8)

    def insert_channel(self, channel):
        if self.busy or not self.connected or not self.snapshot_complete or 2 not in self.known_kinds():
            return
        self.snapshot_complete = False
        self.set_status(f"新增频道 {channel.seq:02d} 中...")
        def done(actual):
            self.channels = sorted(self.channels + [actual],key=lambda c:c.key)
            self.original = sorted(self.original + [replace(actual)],key=lambda c:c.key)
            self.snapshot_complete = True
            self.refresh(); self.focus_channel(actual); self.select()
            self.set_status("单个频道新增成功，已读回核验")
        self.run(self.ble.session.save_channel(channel, False),done)

    def delete(self):
        if self.busy or not self.connected or not self.snapshot_complete or self.order_mapping or self.selected() and self.selected().kind not in self.known_kinds():
            return
        c = self.selected()
        if c:
            if c.kind == 1:
                return messagebox.showinfo("提示", "设备不支持删除预设频道")
            if not messagebox.askyesno("确认删除", f"删除自定义频道 {c.seq:02d}「{c.display_name}」？\n确认后立即从设备删除。",parent=self):
                return
            if c.key not in {x.key for x in self.original}:
                self.channels = [x for x in self.channels if x.key != c.key]; self.refresh()
                return
            self.snapshot_complete = False
            self.set_status(f"删除频道 {c.seq:02d} 中...")
            def done(actual):
                baseline = {x.key:x for x in self.original if x.kind == 2 and x.key != c.key}
                if set(baseline) != {x.key for x in actual} or any(not channel_matches(baseline[x.key],x) for x in actual):
                    raise RuntimeError("删除后其他频道配置或编号发生变化，请重新读取设备")
                self.channels = [x for x in self.channels if x.key != c.key]
                self.original = sorted([x for x in self.original if x.kind != 2] + actual,key=lambda x:x.key)
                self.snapshot_complete = True
                self.device_current_channel = self.current_channel = self.ble.session.current
                self.refresh(); self.set_status("单个频道删除成功，已读回核验")
            self.run(self.ble.session.delete_channel(c),done)

    def export_json(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            Path(path).write_text(json.dumps([c.to_apk() for c in self.channels], ensure_ascii=False, indent=2), encoding="utf-8")

    def import_json(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise ValueError("JSON 须为频道数组")
            imported = [Channel.from_apk(x) for x in values]
            original = {c.key: c for c in self.original}
            # Imported protocol metadata cannot overwrite the live radio's hidden settings.
            imported = [replace(c, raw=original[c.key].raw) if c.key in original else replace(c, raw="") for c in imported]
            self.validate(imported)
            if self.original:
                validate_channel_changes(imported, self.original)
            self.order_mapping = {}
            self.current_channel = self.device_current_channel
            self.channels = sorted(imported, key=lambda c: c.key)
            self.refresh()
            self.set_status("导入完成，待写入设备")
        except Exception as error:
            messagebox.showerror("导入失败", str(error))

    def read_device(self):
        if self.busy or not self.connected:
            return
        kinds = self.filter_kinds()
        self.loaded_kinds = self.known_kinds() - set(kinds)
        if 2 in kinds:
            self.order_mapping = {}
        self.current_channel = self.order_mapping.get(self.device_current_channel,self.device_current_channel)
        self.snapshot_complete = False
        self.refresh()
        self.set_status("读取频道中...")
        def progress(ch):
            self.enqueue(lambda: self._channel_progress(ch))
        def done(channels):
            self.channels = sorted([c for c in self.channels if c.kind not in kinds]+channels,key=lambda c:c.key)
            self.original = sorted([c for c in self.original if c.kind not in kinds]+[replace(c) for c in channels],key=lambda c:c.key)
            self.loaded_kinds.update(kinds)
            self.snapshot_complete = True
            self.device_current_channel = self.ble.session.current
            self.current_channel = self.order_mapping.get(self.device_current_channel,self.device_current_channel)
            self.refresh()
            self.update_buttons()
            self.set_status(f"已读取 {len(channels)} 个频道")
            self.log(f"读取完整：预设 {sum(c.kind == 1 for c in channels)}，自定义 {sum(c.kind == 2 for c in channels)}")
            self.read_info()
        self.run(self.ble.read_channels(progress, kinds), done)

    def _channel_progress(self, ch):
        if not self.connected:
            return
        self.channels = [c for c in self.channels if c.key != ch.key] + [ch]
        self.channels.sort(key=lambda c: c.key)
        self.refresh()
        self.set_status(f"读取频道中... 已收到 {len(self.channels)} 个")

    def read_current(self):
        if not self.busy and self.connected:
            self.set_status("读取当前频道中...")
            self.run(self.ble.read_current(), self._state)

    def switch_channel(self, c):
        if not self.busy and self.connected and self.snapshot_complete:
            if self.order_mapping:
                return messagebox.showinfo("排序待写入", "请先点击写入设备保存排序，再切换频道")
            if c.key not in {x.key for x in self.original}:
                return messagebox.showinfo("提示", "新频道需要先写入设备才能切换")
            self.set_status(f"切换至 {c.display_name} 中...")
            def done(key):
                self._state(key)
                self.set_status(f"切换成功，已读回确认：{c.display_name}")
            self.run(self.ble.switch(c), done)

    def write_device(self):
        if self.busy or not self.connected or not self.snapshot_complete:
            return
        kinds = tuple(sorted(set(self.filter_kinds()) & self.known_kinds()))
        if not kinds:
            return
        selected = self.selected()
        if selected and selected.kind in kinds and self.editor_differs(selected) and self.apply() is None:
            return
        try:
            desired = [replace(c) for c in self.channels if c.kind in kinds]
            original = [replace(c) for c in self.original if c.kind in kinds]
            self.validate(desired)
            validate_channel_changes(desired, original)
        except Exception as error:
            self.set_status("写入失败："+str(error),"error")
            return messagebox.showerror("写入失败", str(error))
        current_mapping = dict(self.order_mapping) if 2 in kinds else {}
        self.snapshot_complete = False
        self.set_status("写入并读回核验中...")
        def done(channels):
            self.channels = sorted([c for c in self.channels if c.kind not in kinds]+channels,key=lambda c:c.key)
            self.original = sorted([c for c in self.original if c.kind not in kinds]+[replace(c) for c in channels],key=lambda c:c.key)
            self.snapshot_complete = True
            if 2 in kinds:
                self.order_mapping = {}
            self.device_current_channel = self.ble.session.current
            self.current_channel = self.order_mapping.get(self.device_current_channel,self.device_current_channel)
            self.refresh()
            self.update_buttons()
            self.set_status("写入成功，已读回核验")
        self.run(self.ble.sync(desired, original, current_mapping, kinds), done)

    def close(self):
        self.save_config(layout=True)
        for tooltip in self.tooltips:
            tooltip.hide()
        self.closed = True
        if self.ble.loop and self.ble.loop.is_running():
            future = self.ble.call(self.ble.disconnect())
            future.add_done_callback(lambda _: self.ble.loop.call_soon_threadsafe(self.ble.loop.stop))
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
