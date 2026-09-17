#!/usr/bin/env python3
"""
SmartCar Tuning Tool - Config-driven desktop tuning GUI.

All domain-specific knowledge lives in config/profile data and the
bridge extension module (bridge_ext.py, declared via config.json
"bridge_extension"). This file is the generic framework: TCP, HTTP,
minimal connection panel, bridge-extension hooks. You should NOT need
to edit this file for normal use.

--headless runs the bridge as a pure service: no GUI, logs to
runtime/bridge_headless.log (+ stdout when a console exists), exit via
POST /shutdown or Ctrl+C.
"""

import importlib.util
import json
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Load user config
# ---------------------------------------------------------------------------
from config_loader import (cfg, resource_path, app_dir, profile_schema_path,
                           PROFILE_NAME, UNSET)
import guardrails as G  # P0-2 护栏引擎(通用核,与 driver/MCP 三处共用)

# ---------------------------------------------------------------------------
# Async file writer (non-blocking disk I/O)
# ---------------------------------------------------------------------------
class _AsyncFileWriter:
    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=512)
        self._t = threading.Thread(target=self._worker, daemon=True, name='file-writer')
        self._t.start()

    def write(self, path: Path, text: str):
        try: self._q.put_nowait(('w', path, text))
        except queue.Full: pass

    def append(self, path: Path, text: str):
        try: self._q.put_nowait(('a', path, text))
        except queue.Full: pass

    def _worker(self):
        while True:
            try: item = self._q.get(timeout=2.0)
            except queue.Empty: continue
            mode, path, text = item
            try:
                with path.open(mode, encoding='utf-8') as fh:
                    fh.write(text)
            except Exception: pass

_file_writer = _AsyncFileWriter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def _offline_dir(dname: str) -> Path:
    """离线产物目录(paths/ recordings/):exe 在 dist/ 跑时 app_dir 是 dist,
    而 path_pipeline 的产物在上一级 tcp_tool/ → 本目录没有就回退父目录。"""
    d = app_dir() / dname
    if d.is_dir():
        return d
    return app_dir().parent / dname

ENCODING = 'utf-8'
BRIDGE_VERSION = '2026-09-14.bridge-ext-v1'
DATA_DIR = app_dir() / 'runtime'

# ---------------------------------------------------------------------------
# Web UI static hosting (Phase C2, 设计文档 §6.4)
# ---------------------------------------------------------------------------
_WEB_CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.map': 'application/json; charset=utf-8',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
}

def _web_dist_dir():
    """新前端产物目录:app_dir()/web_dist/(热载,拷目录即生效)优先于
    PyInstaller 内嵌副本;都没有返回 None(调用方回退旧 dashboard)。"""
    external = app_dir() / 'web_dist'
    if external.is_dir():
        return external
    embedded = resource_path('web_dist')
    if embedded.is_dir():
        return embedded
    return None
LATEST_TEXT_PATH = DATA_DIR / 'latest_telemetry.txt'
LATEST_JSON_PATH = DATA_DIR / 'latest_telemetry.json'
HISTORY_JSONL_PATH = DATA_DIR / 'telemetry_history.jsonl'
MAX_HISTORY = 500
MAX_PLOT_POINTS = getattr(cfg, 'MAX_PLOT_POINTS', 2000)

# ---------------------------------------------------------------------------
# Glassmorphism Dark Theme
# ---------------------------------------------------------------------------
THEME = {
    # Backgrounds (layered depth)
    'bg_deep':    '#0f0f23',
    'bg_mid':     '#1a1a2e',
    'bg_surface': '#16213e',
    # Glass panels (opaque approximations for tkinter compatibility)
    'glass':      '#1b1b30',
    'glass_hover':'#22223a',
    'glass_border':'#2a2a45',
    # Text
    'text':       '#e0e7ff',
    'text_muted': '#94a3b8',
    'text_dim':   '#475569',
    # Accent (purple-blue)
    'accent':     '#7c3aed',
    'accent_light':'#a78bfa',
    'accent_bg':  '#2d1a5e',
    # Status
    'ok':         '#22c55e',
    'warn':       '#f59e0b',
    'danger':     '#ef4444',
    # Chart line colors
    'ch0':        '#818cf8',
    'ch1':        '#34d399',
    'ch2':        '#f87171',
    'ch3':        '#c084fc',
    'ch4':        '#fbbf24',
    'ch5':        '#38bdf8',
    'ch6':        '#fb923c',
    # Console
    'console_bg': '#080812',
    'console_fg': '#64748b',
}
_accent = getattr(cfg, 'ACCENT_COLOR', None)
if _accent:
    THEME['accent'] = _accent

CHART_COLORS = [THEME['ch0'], THEME['ch1'], THEME['ch2'], THEME['ch3'],
                THEME['ch4'], THEME['ch5'], THEME['ch6']]

def try_parse_number(value: str):
    try:
        if any(ch in value for ch in ('.', 'e', 'E')):
            return float(value)
        return int(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Bridge extension mechanism (2026-09-14 设计文档 §1)
# 通用核零被调对象语义;领域功能经 config 声明的扩展模块外置。
# ---------------------------------------------------------------------------
_EXT_CONVENTION_NAME = 'bridge_ext.py'


class StreamProtocolError(Exception):
    """通用流解析协议错误(行超限等);RX 循环捕获后关闭该连接。"""


class _LineParser:
    """默认流解析器(无扩展时):仅切分文本行。行语义与扩展可提供的
    混排解析器逐条对齐——\\n 定界、去尾部 \\r、utf-8(replace) 解码、
    首尾 strip、空行丢弃、超长行按协议错误上抛后关连接。"""

    def __init__(self, max_line_bytes: int = 16384):
        self.buffer = bytearray()
        self.max_line_bytes = max_line_bytes

    def feed(self, data: bytes):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError('stream input must be bytes')
        self.buffer.extend(data)
        events = []
        while True:
            if not self.buffer:
                break
            newline = self.buffer.find(b'\n')
            if newline < 0:
                if len(self.buffer) > self.max_line_bytes:
                    self.buffer.clear()
                    raise StreamProtocolError('text line exceeds receive limit')
                break
            raw = bytes(self.buffer[:newline])
            del self.buffer[:newline + 1]
            text = raw.rstrip(b'\r').decode('utf-8', errors='replace').strip()
            if text:
                events.append(('line', text))
        return events

    def timed_out(self) -> bool:
        return False

    def close(self):
        self.buffer.clear()


class _PlainVar:
    """headless 变量替身:与 tk 变量同名 get/set 接口,无 GUI 依赖。"""

    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


def _load_bridge_ext():
    """按 config 三态决议并加载桥扩展(设计文档 §1)。

    - str  → app_dir() 相对路径加载(绝对路径原样)
    - None → 不加载(显式关闭)
    - UNSET→ 仅根布局(无 --profile)时尝试约定名 bridge_ext.py
              (旧 dist 兼容:升级前配置无此键);命名 profile 缺键 = 无扩展

    校验 EXT_API == 1(等值,非存在性)。任何失败只降级为无扩展,不炸桥。
    返回 (module|None, status_dict);status 进 /snapshot bridge 块。
    """
    declared = getattr(cfg, 'BRIDGE_EXTENSION', UNSET)
    if declared is None:
        return None, {'state': 'off',
                      'reason': 'explicitly disabled (bridge_extension: null)'}
    if declared is UNSET:
        if PROFILE_NAME:
            return None, {'state': 'off',
                          'reason': 'not declared (named profile)'}
        path = app_dir() / _EXT_CONVENTION_NAME
        if not path.is_file():
            return None, {'state': 'off', 'reason': 'not declared'}
        source = 'convention'
    else:
        p = Path(declared)
        path = p if p.is_absolute() else app_dir() / p
        if not path.is_file():
            return None, {'state': 'failed',
                          'reason': f'extension file not found: {declared}'}
        source = 'declared'
    try:
        spec = importlib.util.spec_from_file_location('bridge_ext', path)
        if spec is None or spec.loader is None:
            raise ImportError(f'cannot create import spec for {path}')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if getattr(mod, 'EXT_API', None) != 1:
            raise ImportError(f'EXT_API != 1 in {path.name}')
        return mod, {'state': 'loaded', 'source': source,
                     'path': str(path)}
    except Exception as e:
        return None, {'state': 'failed',
                      'reason': f'{type(e).__name__}: {e}'}

# ---------------------------------------------------------------------------
# Collect all metric keys from config
# ---------------------------------------------------------------------------
def _collect_all_metric_keys():
    keys = set()
    for k, _ in getattr(cfg, 'PRIMARY_METRICS', []):
        keys.add(k)
    for k, _ in getattr(cfg, 'DETAIL_METRICS', []):
        keys.add(k)
    for k, _ in getattr(cfg, 'EXTENDED_METRICS', []):
        keys.add(k)
    for tab in getattr(cfg, 'CUSTOM_TABS', []):
        for k, _ in tab.get('fields', []):
            keys.add(k)
        for _, vk in tab.get('result_keys', []):
            keys.add(vk)
    for pk, _, _ in getattr(cfg, 'PLOT_KEYS', []):
        keys.add(pk)
    # Add expanded KEY_MAP values
    for v in getattr(cfg, 'KEY_MAP', {}).values():
        keys.add(v)
    return keys

ALL_METRIC_KEYS = _collect_all_metric_keys()

# ===========================================================================
# Main Application
# ===========================================================================
class TuningToolApp:

    _TELEMETRY_PREFIXES = tuple(p + ',' for p in getattr(cfg, 'TELEMETRY_PREFIXES', ('TEL',)))
    _RESPONSE_PREFIXES = tuple(p + ',' for p in getattr(cfg, 'RESPONSE_PREFIXES', ('ACK', 'ERR')))
    _ALL_PREFIXES = _TELEMETRY_PREFIXES + _RESPONSE_PREFIXES
    _KEY_MAP = getattr(cfg, 'KEY_MAP', {})

    def __init__(self, root=None):
        self.root = root
        self.headless = root is None
        if not self.headless:
            root.configure(bg=THEME['bg_deep'])

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 扩展注入用:领域代码经 init(app) 拿数据目录,不 import 本模块
        self.data_dir = DATA_DIR

        # --- state ---
        # 默认值优先用 config 里 tcp_host / tcp_port，方便不同部署改 config 不动代码
        # headless 用 _PlainVar 替身:桥核读 .get() 的调用点零改动
        if self.headless:
            self.mode_var = _PlainVar('server')
            self.host_var = _PlainVar(getattr(cfg, 'TCP_HOST', '0.0.0.0'))
            self.port_var = _PlainVar(getattr(cfg, 'TCP_PORT', '8080'))
            self.crlf_var = _PlainVar(True)
        else:
            import tkinter as tk
            self.mode_var = tk.StringVar(value='server')
            self.host_var = tk.StringVar(value=getattr(cfg, 'TCP_HOST', '0.0.0.0'))
            self.port_var = tk.StringVar(value=getattr(cfg, 'TCP_PORT', '8080'))
            self.crlf_var = tk.BooleanVar(value=True)
        self.running = False
        self.server_socket = None
        self.client_socket = None
        self.server_connections = {}
        self.connection_id_counter = 0
        self.server_thread = None
        self.client_thread = None
        self.log_queue = queue.Queue(maxsize=2000)
        self.telemetry_lock = threading.Lock()
        self.send_lock = threading.Lock()   # sendall 非原子,多 HTTP 线程并发发命令会交错字节
        self.latest_telemetry = None
        self.latest_parsed = {}
        # 每包类型的最新一帧(2026-06-10):/latest 顶层是所有流平铺合并,
        # _packet 只剩"最后到的那个"——dashboard 按 _packet 路由时其它流的新帧
        # 会被遮蔽丢帧。这里按类型各存一份,/latest 以 packets={类型:帧} 暴露。
        self.latest_by_type = {}
        self.telemetry_history = []
        self.sim_running = False
        self.sim_thread = None
        self.sim_tick = 0

        # Custom state: per response-prefix storage
        self.custom_state = {}
        for pfx in getattr(cfg, 'RESPONSE_PREFIXES', ('ACK', 'ERR')):
            self.custom_state[pfx] = {}
        self.latest_command_result = {}
        self.pending_command = ''
        self.pending_command_time = 0.0
        self._custom_state_last_write = 0.0

        # Phase A1(2026-08-13,设计文档 §4.1):桥内命令队列 + ACK 等待。
        # /command 入队后立即返回(行为兼容);/batch 是唯一等 ACK 的调用方,
        # batch_lock 保证同一时刻只有一个 /batch 在执行。pending_ack 由 RX
        # 线程在 _handle_special_packet 里按 cmd 回显匹配后置结果并 notify。
        self.cmd_queue = queue.Queue()
        self.cmd_cond = threading.Condition()
        self.pending_ack = None   # {'cmd': <normalized>, 'result': None|dict}
        self.batch_lock = threading.Lock()
        # 响应前缀集合来自 cfg(通用性纪律:核里不写死协议字面量)
        self._response_prefix_set = frozenset(
            getattr(cfg, 'RESPONSE_PREFIXES', ('ACK', 'ERR')))
        # 契约 v1(docs/protocol_contract_v1.md):frame_mode=csv 时用位置式
        # D 帧 + 可选 !<seq> 回执;err 前缀集合判定 /batch 的 ack/err 状态。
        self._frame_mode = getattr(cfg, 'FRAME_MODE', 'kv')
        self._csv_channels = getattr(cfg, 'CHANNELS', {})
        self._err_prefix_set = frozenset(getattr(cfg, 'ERR_PREFIXES', ('ERR',)))
        self._csv_seq = 0
        threading.Thread(target=self._cmd_dispatch_loop, daemon=True,
                         name='cmd-dispatch').start()

        # Phase A3:/snapshot 聚合用桥自状态
        self._start_ts = time.time()
        self._last_hb_ts = 0.0
        self.bridge_schema_hash = self._load_schema_hash()

        # P0-2(2026-08-14):桥内护栏强制。schema/guard 启动加载一次(--profile
        # 启动参数固定);黑名单/值域/步长校验 + append-only 审计。桥是三层里
        # 唯一持有参数当前值(latest_parsed)的强制点,max_step 以桥为准。
        self._schema_dict = None
        self._guardrails = None
        try:
            sp = profile_schema_path()
            if sp:
                self._schema_dict = json.loads(
                    Path(sp).read_text(encoding='utf-8'))
                self._guardrails = G.load_schema_guard(self._schema_dict)
        except Exception:
            self._schema_dict = None
            self._guardrails = None

        # Phase B1(设计文档 §5):SSE /events 订阅者。每客户端一个有界队列,
        # 满则丢帧并计 dropped;推送一律非阻塞 put_nowait,慢客户端绝不拖累
        # 遥测/命令路径;客户端断开由 /events 写失败时清理。
        self._sse_clients = {}
        self._sse_clients_lock = threading.Lock()

        # P2(设计文档 §3.3):agent 参数提案队列。内存态、bounded 20、重启即清
        # (提案是短寿命对象,不落盘)。桥只把命令当字符串,diff 解析在前端。
        self._proposals = []
        self._proposal_seq = 0
        self._proposal_lock = threading.Lock()

        # 自动心跳(2026-06-10):TCP 连着就由桥周期发心跳命令,不靠外部脚本保活。
        self._heartbeat_enabled = bool(getattr(cfg, 'HEARTBEAT_ENABLED', True))
        self._heartbeat_cmd = str(getattr(cfg, 'HEARTBEAT_COMMAND', 'HB')).strip()
        self._heartbeat_interval_s = max(
            0.2, getattr(cfg, 'HEARTBEAT_INTERVAL_MS', 2000) / 1000.0)
        self._heartbeat_thread = None
        # 心跳命令的响应(老固件 HB→ERR)不该污染 cmd_result;匹配 ACK/ERR 的
        # cmd 回显形式:原样大写 + 空格转下划线大写(固件 ACK 用后者)。不取首
        # token——否则多词心跳(如 "GET STREAMS")会把真命令 "GET PID" 的 ACK 误伤。
        # 默认单词 'HB' 两式同为 {'HB'},精确无副作用。
        hb = self._heartbeat_cmd
        self._hb_cmd_tokens = ({hb.upper(), hb.replace(' ', '_').upper()}
                               if hb else set())

        # 桥扩展(2026-09-14):领域功能外置,config 三态决议加载;
        # 失败降级为无扩展,桥照常启动(设计文档 §1)。
        self.bridge_ext, self.bridge_ext_status = _load_bridge_ext()
        self.queue_log('log', f'[ext] bridge extension: {self.bridge_ext_status}')
        if self.bridge_ext is not None and hasattr(self.bridge_ext, 'init'):
            try:
                self.bridge_ext.init(self)
            except Exception as e:
                self.bridge_ext = None
                self.bridge_ext_status = {'state': 'failed',
                                          'reason': f'init failed: {type(e).__name__}: {e}'}
                self.queue_log('log', f'[ext] init failed, degraded: {e}')

        # /shutdown 事件(设计文档 §3):HTTP 线程只置事件,清理只发生在
        # owners 线程(GUI=Tk 泵线程 / headless=主线程),Tk 对象绝不被
        # 非 Tk 线程触碰。_pump_stop 独立于 TCP running:停 TCP 不停遥测泵。
        self.shutdown_event = threading.Event()
        self._pump_stop = threading.Event()
        self._cleaned = False
        self._headless_log_path = DATA_DIR / 'bridge_headless.log'

        # Metric StringVars (auto-generated from config)
        if self.headless:
            self.metric_vars = {k: _PlainVar('--') for k in ALL_METRIC_KEYS}
            self.metric_vars['fix_state'] = _PlainVar('--')
            self.metric_vars['health_state'] = _PlainVar('--')
        else:
            import tkinter as tk
            self.metric_vars = {}
            for k in ALL_METRIC_KEYS:
                self.metric_vars[k] = tk.StringVar(value='--')
            self.metric_vars['fix_state'] = tk.StringVar(value='--')
            self.metric_vars['health_state'] = tk.StringVar(value='--')

        # Plot state (still needed for HTTP API)
        self.plot_series = {k: [] for k, _, _ in getattr(cfg, 'PLOT_KEYS', [])}
        if self.headless:
            self.plot_enabled_vars = {
                k: _PlainVar(default_on)
                for k, _, default_on in getattr(cfg, 'PLOT_KEYS', [])}
        else:
            import tkinter as tk
            self.plot_enabled_vars = {}
            for k, _, default_on in getattr(cfg, 'PLOT_KEYS', []):
                self.plot_enabled_vars[k] = tk.BooleanVar(value=default_on)

        if self.headless:
            # 遥测/日志泵在常驻线程上跑(与 GUI 的 root.after 泵等价;
            # 间隔沿用 process_queue 的自适应节奏)
            threading.Thread(target=self._pump_loop, daemon=True, name='pump').start()
        else:
            self._build_ui()
        self.start_http_server()
        if not self.headless:
            self.root.after(100, self.process_queue)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ======================================================================
    # UI Construction - Minimal Connection Panel
    # ======================================================================
    def _build_ui(self):
        import tkinter as tk
        import ttkbootstrap as ttkb
        self.root.geometry('400x680')
        self.root.resizable(False, False)

        main = tk.Frame(self.root, bg=THEME['bg_mid'], padx=16, pady=12)
        main.pack(fill='both', expand=True)

        # Title
        tk.Label(main, text=getattr(cfg, 'APP_TITLE', 'SmartCar Tuning Tool'),
                 bg=THEME['bg_mid'], fg=THEME['accent_light'],
                 font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        _dashboard_url = f"http://{getattr(cfg, 'HTTP_HOST', '127.0.0.1')}:{getattr(cfg, 'HTTP_PORT', 9898)}"
        _url_lbl = tk.Label(main, text=f'Dashboard: {_dashboard_url}',
                            bg=THEME['bg_mid'], fg=THEME['accent_light'], font=('Segoe UI', 9),
                            cursor='hand2')
        _url_lbl.pack(anchor='w', pady=(0, 10))
        _url_lbl.bind('<Button-1>', lambda e: webbrowser.open(_dashboard_url))

        # Connection settings
        conn = tk.LabelFrame(main, text='Connection', bg=THEME['bg_mid'],
                              fg=THEME['text_muted'], font=('Segoe UI', 10, 'bold'))
        conn.pack(fill='x', pady=(0, 8))

        # Server/Client radio
        mode_frame = tk.Frame(conn, bg=THEME['bg_mid'])
        mode_frame.pack(fill='x', padx=8, pady=4)
        ttkb.Radiobutton(mode_frame, text='Server', variable=self.mode_var,
                          value='server', bootstyle='info-toolbutton',
                          command=self.on_mode_change).pack(side='left', padx=2)
        ttkb.Radiobutton(mode_frame, text='Client', variable=self.mode_var,
                          value='client', bootstyle='info-toolbutton',
                          command=self.on_mode_change).pack(side='left', padx=2)

        # Host + Port
        for label_text, var in [('Host:', self.host_var), ('Port:', self.port_var)]:
            row = tk.Frame(conn, bg=THEME['bg_mid'])
            row.pack(fill='x', padx=8, pady=2)
            tk.Label(row, text=label_text, bg=THEME['bg_mid'], fg=THEME['text_muted'],
                     width=5, anchor='e', font=('Segoe UI', 9)).pack(side='left')
            ttkb.Entry(row, textvariable=var, width=20, bootstyle='dark').pack(side='left', padx=4, fill='x', expand=True)

        # Start/Stop
        btn_frame = tk.Frame(conn, bg=THEME['bg_mid'])
        btn_frame.pack(fill='x', padx=8, pady=(4, 8))
        self.start_btn = ttkb.Button(btn_frame, text='Start', command=self.start,
                                      bootstyle='success', width=10)
        self.start_btn.pack(side='left', padx=2)
        self.stop_btn = ttkb.Button(btn_frame, text='Stop', command=self.stop,
                                     bootstyle='danger-outline', width=10, state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        # Status
        self.conn_summary = tk.Label(conn, text='Stopped', bg=THEME['bg_mid'],
                                      fg=THEME['text_dim'], font=('Segoe UI', 9))
        self.conn_summary.pack(padx=8, anchor='w')

        # Connection list
        self.conn_listbox = tk.Listbox(conn, bg=THEME['bg_deep'], fg=THEME['text'],
                                        height=3, font=('Consolas', 9),
                                        borderwidth=0, highlightthickness=0)
        self.conn_listbox.pack(fill='x', padx=8, pady=(4, 8))

        # Simulation
        if getattr(cfg, 'SIMULATION_ENABLED', hasattr(cfg, 'build_simulated_packet')):
            sim = tk.LabelFrame(main, text='Simulation', bg=THEME['bg_mid'],
                                 fg=THEME['text_muted'], font=('Segoe UI', 10, 'bold'))
            sim.pack(fill='x', pady=(0, 8))
            sim_inner = tk.Frame(sim, bg=THEME['bg_mid'])
            sim_inner.pack(fill='x', padx=8, pady=6)
            ttkb.Button(sim_inner, text='Start Sim', command=self.start_simulation,
                        bootstyle='info', width=10).pack(side='left', padx=2)
            ttkb.Button(sim_inner, text='Stop Sim', command=self.stop_simulation,
                        bootstyle='secondary-outline', width=10).pack(side='left', padx=2)

        # Send command
        send = tk.LabelFrame(main, text='Send Command', bg=THEME['bg_mid'],
                              fg=THEME['text_muted'], font=('Segoe UI', 10, 'bold'))
        send.pack(fill='x', pady=(0, 8))
        send_inner = tk.Frame(send, bg=THEME['bg_mid'])
        send_inner.pack(fill='x', padx=8, pady=6)
        self.send_entry = ttkb.Entry(send_inner, bootstyle='dark')
        self.send_entry.pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.send_entry.bind('<Return>', lambda e: self.send_data())
        ttkb.Button(send_inner, text='Send', command=self.send_data,
                    bootstyle='info', width=6).pack(side='right')

        # Console (compact)
        self.log_text = tk.Text(main, bg=THEME['bg_deep'], fg=THEME['console_fg'],
                                 height=6, font=('Consolas', 9), state='disabled',
                                 wrap='none', borderwidth=0, highlightthickness=0)
        self.log_text.pack(fill='both', expand=True)

    # ======================================================================
    # Protocol Parsing
    # ======================================================================
    def parse_telemetry_text(self, text: str):
        if not any(text.startswith(p) for p in self._ALL_PREFIXES):
            return {}
        if self._frame_mode == 'csv':
            return self._parse_csv_frame(text)
        parsed = {}
        packet_type = text.split(',', 1)[0].strip()
        parsed['_packet'] = packet_type
        for part in text.split(',')[1:]:
            if '=' not in part:
                continue
            key, value = part.split('=', 1)
            parsed[key.strip()] = try_parse_number(value.strip())
        return parsed

    def _parse_csv_frame(self, text: str):
        """契约 v1 位置式帧(docs/protocol_contract_v1.md §2.2)。列序与字段名
        全部来自 cfg protocol.channels,核内零被调对象语义。
        遥测 D,<chan>,<t_ms>,<v...> → {'_packet': <chan>, <字段>: <值>...}
        回执 OK,<seq> / ERR,<seq>[,<reason>] → {'_packet': OK, 'seq': <n>}"""
        parts = [p.strip() for p in text.rstrip('\r\n').split(',')]
        ptype = parts[0] if parts else ''
        if ptype in self._response_prefix_set:
            parsed = {'_packet': ptype}
            if len(parts) > 1 and parts[1] != '':
                parsed['seq'] = try_parse_number(parts[1])
            if len(parts) > 2 and parts[2] != '':
                parsed['reason'] = parts[2]
            return parsed
        chan = parts[1] if len(parts) > 1 else ''
        parsed = {'_packet': chan or ptype}
        if len(parts) > 2 and parts[2] != '':
            parsed['t_ms'] = try_parse_number(parts[2])
        fields = self._csv_channels.get(chan, [])
        for i, fname in enumerate(fields):
            idx = i + 3
            if idx < len(parts) and parts[idx] != '':
                parsed[fname] = try_parse_number(parts[idx])
        return parsed

    def merge_telemetry_packet(self, parsed: dict):
        ptype = parsed.get('_packet', '')
        with self.telemetry_lock:
            # 扩展钩子:on_packet 在锁内被调,扩展自有状态天然受锁保护
            # (契约见设计文档 §1);扩展异常不得破坏遥测合并路径。
            if self.bridge_ext is not None:
                try:
                    self.bridge_ext.on_packet(self, parsed)
                except Exception:
                    pass

            if self._KEY_MAP and ptype in getattr(cfg, 'TELEMETRY_PREFIXES', ()):
                expanded = {}
                for k, v in parsed.items():
                    expanded[self._KEY_MAP.get(k, k)] = v
                self.latest_parsed.update(expanded)
                self.latest_by_type[ptype] = expanded
            else:
                self.latest_parsed.update(parsed)
                self.latest_by_type[ptype] = dict(parsed)
            frame = dict(self.latest_by_type.get(ptype, parsed))
        # Phase B1:每个遥测包解析后推一帧 SSE(该类型合并帧,前端直接用)
        self._sse_offer('message', {'type': ptype, 'fields': frame, 'ts': time.time()})

    def _handle_special_packet(self, ptype: str, parsed: dict):
        with self.telemetry_lock:
            if ptype in self.custom_state:
                self.custom_state[ptype] = dict(parsed)
                self.custom_state[ptype]['_ts'] = time.time()
            if ptype in ('ACK', 'ERR'):
                # 心跳命令(默认 HB)的响应不写 cmd_result:新固件 HB 根本不回响应,
                # 老固件回 ERR,无论哪种都不该把真命令的 ACK 冲掉(否则自动心跳每
                # 2s 覆盖一次 cmd_result,读不到真 ACK——正是要根治的老问题)。
                if str(parsed.get('cmd', '')).strip().upper() not in self._hb_cmd_tokens:
                    self.latest_command_result = {
                        'cmd': parsed.get('cmd', ''),
                        'ack': ptype,
                        'data': dict(parsed),
                        'ts': time.time(),
                    }
            # Phase A1:/batch 等待方的响应匹配(前缀集合来自 cfg;cmd 回显
            # 大小写不敏感、去空白、下划线视同空格——纯字符串归一化,不含
            # 被调对象语义)。与上面 latest_command_result 写入并存,旧客户端
            # 行为不变。
            pa = self.pending_ack
            if pa is not None and pa.get('result') is None \
                    and ptype in self._response_prefix_set:
                # csv 模式按 seq 匹配(!<seq> 回执);kv 模式按 cmd 回显匹配。
                if pa.get('seq') is not None:
                    matched = (parsed.get('seq') == pa['seq'])
                else:
                    matched = self._normalize_cmd_echo(parsed.get('cmd', '')) == pa.get('cmd')
                if matched:
                    pa['result'] = {
                        'status': 'err' if ptype in self._err_prefix_set else 'ack',
                        'data': dict(parsed),
                    }
                    with self.cmd_cond:
                        self.cmd_cond.notify_all()
        self.queue_log('custom_ui', None)
        # Phase B1:命令响应也推 SSE(前端 CONSOLE 用),与遥测帧分离的事件类型
        self._sse_offer('response', {'ptype': ptype, 'fields': parsed, 'ts': time.time()})

    # ======================================================================
    # SSE push (Phase B1, 设计文档 §5)
    # ======================================================================
    def _sse_register(self):
        client = {'q': queue.Queue(maxsize=200), 'dropped': 0}
        with self._sse_clients_lock:
            self._sse_clients[id(client['q'])] = client
        return client

    def _sse_unregister(self, client):
        with self._sse_clients_lock:
            self._sse_clients.pop(id(client['q']), None)

    def _sse_offer(self, event, data):
        """非阻塞广播;无订阅者时近零开销。event='message' 时不写 event 行
        (EventSource 默认 message 事件)。"""
        with self._sse_clients_lock:
            clients = list(self._sse_clients.values())
        if not clients:
            return
        if event == 'message':
            head = ''
        else:
            head = f'event: {event}\n'
        frame = (head + 'data: ' + json.dumps(data, ensure_ascii=False, default=str)
                 + '\n\n').encode('utf-8')
        for c in clients:
            try:
                c['q'].put_nowait(frame)
            except queue.Full:
                c['dropped'] += 1

    def _sse_broadcast_link(self):
        self._sse_offer('link', self._link_payload())

    # ======================================================================
    # Command queue / batch (Phase A1, 设计文档 §4.1/§4.2)
    # ======================================================================
    @staticmethod
    def _normalize_cmd_echo(text) -> str:
        """cmd 回显归一化:大小写不敏感、去空白、下划线视同空格(固件 ACK
        回显是下划线连写形式)。纯字符串处理,不含任何被调对象语义。"""
        return ' '.join(str(text).replace('_', ' ').split()).upper()

    def _cmd_dispatch_loop(self):
        """单写者出口:入队命令在此串行 send_command,send_lock 已保证
        多 HTTP 线程并发时字节不交错;断连清理由 send_command 内部做。"""
        while True:
            cmd = self.cmd_queue.get()
            try:
                self.send_command(cmd)
                self.queue_log('log', f'[Sent] {cmd}')
                self._sse_offer('cmd', {'cmd': cmd, 'ts': time.time()})
            except Exception:
                pass

    def _await_ack(self, cmd: str, timeout_s: float):
        """发一条命令并等它自己的匹配响应。返回 (status, data, elapsed_ms),
        status ∈ ack | err | timeout。调用方必须持有 batch_lock。
        csv 模式(契约 v1)自动附加 !<seq> 并按 seq 匹配;kv 模式按 cmd 回显。"""
        if self._frame_mode == 'csv':
            with self.cmd_cond:
                self._csv_seq += 1
                seq = self._csv_seq
            pending = {'seq': seq, 'result': None}
            wire = f'{cmd} !{seq}'
        else:
            pending = {'cmd': self._normalize_cmd_echo(cmd), 'result': None}
            wire = cmd
        with self.cmd_cond:
            self.pending_ack = pending
        self.cmd_queue.put(wire)
        t0 = time.time()
        with self.cmd_cond:
            self.cmd_cond.wait_for(
                lambda: self.pending_ack is None
                        or self.pending_ack['result'] is not None,
                timeout=timeout_s)
            res = self.pending_ack['result'] if self.pending_ack else None
            self.pending_ack = None
        elapsed_ms = int((time.time() - t0) * 1000)
        if res is None:
            return 'timeout', None, elapsed_ms
        return res['status'], res['data'], elapsed_ms

    def check_guard(self, commands):
        """P0-2 护栏强制:黑名单/值域/步长(当前值=latest_parsed)。违规写审计。
        返回 (ok, check_result)。"""
        res = G.check_commands([str(c) for c in commands], self._schema_dict,
                               current_values=self.latest_parsed or {})
        if not res["ok"]:
            G.audit({"actor": "bridge", "action": "guard",
                     "commands": [str(c) for c in commands],
                     "verdict": "rejected", "role": "agent",
                     "violations": res["violations"],
                     "guard_active": res.get("guard_active")},
                    app_dir=app_dir())
        return res["ok"], res

    def run_batch(self, commands, stop_on_error=False):
        """执行一批命令并逐项返回状态(§4.2)。调用方必须已持有 batch_lock。
        status ∈ sent | ack | err | timeout | skipped。"""
        results = []
        ok = True
        skipping = False
        for item in commands:
            if not isinstance(item, dict):
                item = {}
            cmd = str(item.get('cmd', '')).strip()
            if skipping:
                results.append({'cmd': cmd, 'status': 'skipped'})
                continue
            if not cmd:
                results.append({'cmd': cmd, 'status': 'err',
                                'data': {'error': 'empty cmd'}})
                ok = False
                skipping = stop_on_error
                continue
            if str(item.get('expect', 'none')).upper() != 'ACK':
                self.cmd_queue.put(cmd)
                results.append({'cmd': cmd, 'status': 'sent'})
                continue
            try:
                timeout_ms = min(max(int(item.get('timeout_ms', 2500)), 50), 10000)
            except (TypeError, ValueError):
                timeout_ms = 2500
            status, data, elapsed_ms = self._await_ack(cmd, timeout_ms / 1000.0)
            entry = {'cmd': cmd, 'status': status, 'elapsed_ms': elapsed_ms}
            if data is not None:
                entry['data'] = data
            results.append(entry)
            if status != 'ack':
                ok = False
                skipping = stop_on_error
        return {'results': results, 'ok': ok}

    # ======================================================================
    # Proposals (P2, 设计文档 §3.3):agent 提议 -> 人在 Web 确认 -> 应用/拒绝
    # ======================================================================
    def add_proposal(self, commands, rationale='', expected=''):
        with self._proposal_lock:
            self._proposal_seq += 1
            p = {'id': self._proposal_seq, 'ts': time.time(),
                 'commands': [str(c) for c in commands],
                 'rationale': str(rationale or ''),
                 'expected': str(expected or ''),
                 'status': 'pending'}
            self._proposals.append(p)
            del self._proposals[:-20]
        self._sse_offer('proposal', dict(p))
        return p

    def list_proposals(self):
        with self._proposal_lock:
            return [dict(p) for p in self._proposals]

    def decide_proposal(self, pid, decision):
        with self._proposal_lock:
            p = next((x for x in self._proposals if x['id'] == pid), None)
            if p is None:
                return None, 'not found'
            if p['status'] != 'pending':
                return None, f"already {p['status']}"
            if decision == 'reject':
                p['status'] = 'rejected'
                p['decided_ts'] = time.time()
                out = dict(p)
            elif decision == 'apply':
                # P0-2:apply 时再强制一次(决定时刻参数当前值可能已变)
                ok, res = self.check_guard(p['commands'])
                if not ok:
                    p['status'] = 'rejected'
                    p['decided_ts'] = time.time()
                    p['violations'] = res['violations']
                    out = dict(p)
                    self._sse_offer('proposal', out)
                    return out, 'guardrail violation at apply'
                # 与 /batch 完全相同的执行路径(ACK 等待、并发独占)
                if not self.batch_lock.acquire(blocking=False):
                    return None, 'another /batch in progress'
                try:
                    res = self.run_batch(
                        [{'cmd': c, 'expect': 'ACK'} for c in p['commands']],
                        stop_on_error=True)
                finally:
                    self.batch_lock.release()
                p['status'] = 'applied'
                p['decided_ts'] = time.time()
                p['results'] = res['results']
                p['ok'] = res['ok']
                out = dict(p)
            else:
                return None, 'decision must be apply|reject'
        self._sse_offer('proposal', out)
        return out, None

    # ======================================================================
    # Queue & Event Loop
    # ======================================================================
    def queue_log(self, kind, data):
        try: self.log_queue.put_nowait((kind, data))
        except queue.Full: pass

    def process_queue(self) -> int:
        MAX_PER_CYCLE = 30
        interval = 100
        try:
            # /shutdown:GUI 模式下本函数跑在 Tk 线程,是唯一有权清理并
            # destroy root 的地方(headless 由主线程清理,泵只看 _pump_stop)
            if self.root is not None and self.shutdown_event.is_set():
                self._cleanup_steps()
                try:
                    self.root.destroy()
                except Exception:
                    pass
                return 0

            log_lines = []
            count = 0

            while count < MAX_PER_CYCLE:
                try:
                    kind, data = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                count += 1

                if kind == 'log':
                    log_lines.append(data)
                elif kind == 'telemetry':
                    source, text = data
                    parsed = self.parse_telemetry_text(text)
                    if parsed:
                        self.merge_telemetry_packet(parsed)
                        self._record_telemetry_data_only(source, text, parsed)
                        try: self._update_ui_from_latest()
                        except Exception: pass
                elif kind == 'status':
                    self.refresh_connection_summary()
                elif kind == 'connections':
                    self.refresh_connection_list()
                elif kind == 'custom_ui':
                    # 副作用保序:custom_state 文件写出提出 GUI 刷新路径,
                    # headless 下仍然落盘(设计文档 §3)
                    try:
                        state_copy, cmd_result = self._custom_state_snapshot()
                        self._write_custom_state_file(state_copy, cmd_result)
                        self._refresh_custom_panels(state_copy)
                    except Exception: pass

            if log_lines:
                self._flush_log(log_lines)

            if count >= MAX_PER_CYCLE:
                interval = 50
        except Exception:
            pass
        finally:
            if self.root is not None and not self.shutdown_event.is_set():
                self.root.after(interval, self.process_queue)
        return interval

    def _pump_loop(self):
        """headless 遥测/日志泵:节奏沿用 process_queue 的自适应间隔。"""
        while not self._pump_stop.is_set():
            interval = self.process_queue()
            self._pump_stop.wait(max(0.02, interval / 1000.0))

    def _flush_log(self, lines):
        if self.headless:
            self._headless_log(lines)
            return
        self.log_text.config(state='normal')
        for line in lines:
            self.log_text.insert('end', line + '\n')
        total = int(self.log_text.index('end-1c').split('.')[0])
        if total > 3000:
            self.log_text.delete('1.0', f'{total - 3000}.0')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _headless_log(self, lines):
        """headless 日志契约(设计文档 §3):恒写文件(5MB 轮转,windowed
        exe 无控制台也成立),stdout 尽力而为(写失败静默,不炸桥)。"""
        text = ''.join(str(l) + '\n' for l in lines)
        try:
            p = self._headless_log_path
            if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
                backup = p.with_name(p.name + '.1')
                try: backup.unlink()
                except OSError: pass
                try: p.replace(backup)
                except OSError: pass
            with p.open('a', encoding='utf-8', errors='replace') as fh:
                fh.write(text)
        except OSError:
            pass
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except Exception:
            pass  # windowed exe 无控制台

    # ======================================================================
    # Telemetry Recording
    # ======================================================================
    def _record_telemetry_data_only(self, source, text, parsed):
        entry = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'clock': round(time.time(), 3),
            'source': source,
            'text': text,
            'parsed': parsed,
        }
        with self.telemetry_lock:
            self.latest_telemetry = entry
            self.telemetry_history.append(entry)
            if len(self.telemetry_history) > MAX_HISTORY:
                self.telemetry_history = self.telemetry_history[-MAX_HISTORY:]

        # Update plot data
        for key, _, _ in getattr(cfg, 'PLOT_KEYS', []):
            if key in parsed or key in self.latest_parsed:
                val = parsed.get(key, self.latest_parsed.get(key))
                if isinstance(val, (int, float)):
                    series = self.plot_series.setdefault(key, [])
                    series.append(float(val))
                    if len(series) > MAX_PLOT_POINTS:
                        del series[:-MAX_PLOT_POINTS]

        # Async file writes
        _file_writer.write(LATEST_TEXT_PATH, text)
        _file_writer.write(LATEST_JSON_PATH, json.dumps(entry, ensure_ascii=False, default=str))
        _file_writer.append(HISTORY_JSONL_PATH, json.dumps(entry, ensure_ascii=False, default=str) + '\n')

    def _update_ui_from_latest(self):
        with self.telemetry_lock:
            parsed = dict(self.latest_parsed)
        self.update_metric_cards(parsed)
        self.update_status_banner(parsed)

    def _custom_state_snapshot(self):
        with self.telemetry_lock:
            state_copy = {k: dict(v) for k, v in self.custom_state.items()}
            cmd_result = dict(self.latest_command_result)
        return state_copy, cmd_result

    def _refresh_custom_panels(self, state_copy):
        for tab_def in getattr(cfg, 'CUSTOM_TABS', []):
            key_map = tab_def.get('key_map', self._KEY_MAP)
            # Merge all relevant state
            merged = {}
            for pfx_data in state_copy.values():
                for k, v in pfx_data.items():
                    mapped = key_map.get(k, k)
                    merged[mapped] = v
            # Also merge latest_parsed
            with self.telemetry_lock:
                for k, v in self.latest_parsed.items():
                    if k not in merged:
                        merged[k] = v

            for fk, _ in tab_def.get('fields', []):
                if fk in merged and fk in self.metric_vars:
                    val = merged[fk]
                    if isinstance(val, float):
                        self.metric_vars[fk].set(f'{val:.4f}' if abs(val) < 1 else f'{val:.2f}')
                    else:
                        self.metric_vars[fk].set(str(val))

            for tag, vk in tab_def.get('result_keys', []):
                if tag in state_copy and state_copy[tag] and vk in self.metric_vars:
                    d = state_copy[tag]
                    summary = ', '.join(f'{k}={v}' for k, v in d.items() if not k.startswith('_'))
                    self.metric_vars[vk].set(summary[:80])

    def _write_custom_state_file(self, state_copy, cmd_result):
        fname = getattr(cfg, 'CUSTOM_STATE_FILE', None)
        if not fname:
            return
        now = time.time()
        if now - self._custom_state_last_write < 0.5:
            return
        self._custom_state_last_write = now
        payload = dict(state_copy)
        payload['latest_command_result'] = cmd_result
        payload['connection'] = {'running': self.running, 'mode': self.mode_var.get()}
        payload['_updated'] = now
        _file_writer.write(DATA_DIR / fname, json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    # ======================================================================
    # Metric Cards
    # ======================================================================
    def format_metric_value(self, key, value):
        if isinstance(value, float):
            if key in ('lat', 'lon', 'latitude', 'longitude'):
                return f'{value:.6f}'
            if abs(value) > 100:
                return f'{value:.1f}'
            return f'{value:.2f}'
        return str(value)

    def update_metric_cards(self, parsed):
        for key, val in parsed.items():
            if key in self.metric_vars and not key.startswith('_'):
                self.metric_vars[key].set(self.format_metric_value(key, val))

    def update_status_banner(self, parsed):
        # Try custom logic first
        if hasattr(cfg, 'status_banner_logic') and cfg.status_banner_logic is not None:
            result = cfg.status_banner_logic(parsed)
            if result is not None:
                state_text, health_text = result
                self.metric_vars['fix_state'].set(state_text)
                self.metric_vars['health_state'].set(health_text)
                return

        # Default IMU status logic
        bias_ok = parsed.get('bias_ok', 0)
        bias_cal = parsed.get('bias_cal', 0)
        state = 'READY' if bias_ok else ('CALIBRATING' if bias_cal else 'INIT')
        self.metric_vars['fix_state'].set(state)

        gxyz = parsed.get('gxyz', 0)
        if isinstance(gxyz, (int, float)):
            health = 'STABLE' if gxyz < 5 else ('MOVING' if gxyz < 50 else 'FAST')
        else:
            health = '--'
        self.metric_vars['health_state'].set(health)

    # ======================================================================
    # HTTP API Server
    # ======================================================================
    def start_http_server(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1:SSE 长连接需要;所有响应都带 Content-Length(SSE 除外,
            # 它是 chunked-less 流,靠连接关闭界定),keep-alive 安全。
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args): pass

            def _cors_headers(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')

            def _json_response(self, data, code=200):
                body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self._cors_headers()
                self.end_headers()
                self.wfile.write(body)

            def _serve_file(self, filepath, content_type):
                try:
                    with open(filepath, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(body)))
                    self._cors_headers()
                    self.end_headers()
                    self.wfile.write(body)
                except OSError as e:
                    self._json_response({'error': f'cannot read {filepath}: {e}'}, 404)

            def _serve_dashboard(self):
                # 优先 exe/脚本同目录的 dashboard.html(改 UI 拷文件即生效,
                # 免重打包);没有才回退 PyInstaller 内嵌副本。
                external = app_dir() / 'dashboard.html'
                dashboard = external if external.exists() else resource_path('dashboard.html')
                self._serve_file(str(dashboard), 'text/html; charset=utf-8')

            def _serve_web(self, path):
                """Phase C2:新前端静态托管(/ 与 /ui,assets 走相对路径)。
                每次请求现解目录,web_dist 热载替换即时生效。返回 False = 无
                产物或路径越狱,调用方回退旧 dashboard。"""
                base = _web_dist_dir()
                if base is None:
                    return False
                rel = path
                if rel == '/ui' or rel.startswith('/ui/'):
                    rel = rel[3:] or '/'
                if rel in ('', '/'):
                    rel = '/index.html'
                base_r = base.resolve()
                fpath = (base_r / rel.lstrip('/')).resolve()
                # 路径牢笼:必须落在 base 子树内(防 ../ 越狱)
                if fpath != base_r and base_r not in fpath.parents:
                    return False
                if not fpath.is_file():
                    return False
                self._serve_file(str(fpath), _WEB_CONTENT_TYPES.get(
                    fpath.suffix.lower(), 'application/octet-stream'))
                return True

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self._cors_headers()
                self.end_headers()

            def _serve_sse(self):
                client = app._sse_register()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self._cors_headers()
                    self.end_headers()
                    # 先补一帧链路状态,让客户端立刻有上下文
                    self.wfile.write(('event: link\ndata: '
                                      + json.dumps(app._link_payload(), default=str)
                                      + '\n\n').encode('utf-8'))
                    self.wfile.flush()
                    while True:
                        try:
                            frame = client['q'].get(timeout=15.0)
                        except queue.Empty:
                            frame = b': keep-alive\n\n'
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    app._sse_unregister(client)

            def do_GET(self):
                path = self.path.split('?')[0].rstrip('/')
                if path == '/dashboard':
                    # 旧单文件 dashboard 保留为回退入口(设计文档 §6.4)
                    self._serve_dashboard()
                elif path == '' or path == '/ui' or path.startswith('/ui/') \
                        or path.startswith('/assets/'):
                    # 新前端(Phase C2);无 web 产物时回退旧 dashboard,
                    # 保证任何部署形态 UI 可用。
                    if not self._serve_web(path):
                        self._serve_dashboard()
                elif path == '/latest':
                    with app.telemetry_lock:
                        payload = dict(app.latest_parsed)
                        payload['custom_state'] = {k: dict(v) for k, v in app.custom_state.items()}
                        payload['cmd_result'] = dict(app.latest_command_result)
                        payload['packets'] = {k: dict(v) for k, v in app.latest_by_type.items()}
                    self._json_response(payload)
                elif path == '/snapshot':
                    # Phase A3(设计文档 §4.4):被动聚合视图——只读桥内已有
                    # 状态,不为此发任何车端命令。
                    with app.telemetry_lock:
                        payload = {
                            'link': app._link_payload(),
                            'latest': dict(app.latest_parsed),
                            'custom_state': {k: dict(v) for k, v in app.custom_state.items()},
                            'packets': {k: dict(v) for k, v in app.latest_by_type.items()},
                            'cmd_result': dict(app.latest_command_result),
                            'bridge': {
                                'version': BRIDGE_VERSION,
                                'schema_hash': app.bridge_schema_hash,
                                'uptime_s': int(time.time() - app._start_ts),
                                'sse_clients': len(app._sse_clients),
                                'ext': app.bridge_ext_status,
                            },
                            '_ts': time.time(),
                        }
                    self._json_response(payload)
                elif path == '/history':
                    with app.telemetry_lock:
                        history = list(app.telemetry_history[-100:])
                    self._json_response(history)
                elif path == '/events':
                    # Phase B1(设计文档 §5):SSE 推送。遥测=默认 message 事件
                    # ({"type","fields","ts"}),链路变化='link' 事件,命令/响应
                    # ='cmd'/'response' 事件;15s 无帧发 keep-alive 注释行。
                    self._serve_sse()
                elif path == '/schema':
                    # Phase C1(设计文档 §6.2):直接 serve control_schema.json,
                    # 每次请求读文件(改 schema 免重启,与热加载约定同构)。
                    # profile 化(--profile 时 serve profiles/<name>/ 下的副本)。
                    schema_path = profile_schema_path()
                    if schema_path is not None:
                        self._serve_file(str(schema_path),
                                         'application/json; charset=utf-8')
                    else:
                        self._json_response(
                            {'error': 'control_schema.json not found'}, 404)
                elif path == '/config':
                    if hasattr(cfg, 'to_http_config'):
                        self._json_response(cfg.to_http_config())
                    else:
                        self._json_response({
                            'app_title': getattr(cfg, 'APP_TITLE', 'SmartCar Tuning Tool'),
                            'plot_channels': [{'key': k, 'color': c, 'visible': v}
                                              for k, c, v in getattr(cfg, 'PLOT_KEYS', [])],
                            'primary_metrics': [{'key': k, 'label': l}
                                                for k, l in getattr(cfg, 'PRIMARY_METRICS', [])],
                            'detail_metrics': [{'key': k, 'label': l}
                                               for k, l in getattr(cfg, 'DETAIL_METRICS', [])],
                            'extended_metrics': [{'key': k, 'label': l}
                                                 for k, l in getattr(cfg, 'EXTENDED_METRICS', [])],
                            'quick_commands': getattr(cfg, 'QUICK_COMMANDS', []),
                        })
                elif path == '/status':
                    self._json_response({
                        'running': app.running,
                        'mode': app.mode_var.get(),
                        'host': app.host_var.get(),
                        'port': app.port_var.get(),
                        'connections': len(app.server_connections),
                    })
                elif path == '/proposal/list':
                    # P2(设计文档 §3.3):提案及状态列表
                    self._json_response({'proposals': app.list_proposals()})
                elif path == '/paths/list':
                    # 2026-06-10:离线优化产物目录索引(原始录制 recordings/ +
                    # 优化输出 paths/),给 dashboard 轨迹对比页选文件用。只列不读点。
                    out = {}
                    for dname in ('paths', 'recordings'):
                        d = _offline_dir(dname)
                        entries = []
                        if d.is_dir():
                            for f in sorted(d.glob('*.json'),
                                            key=lambda p: p.stat().st_mtime, reverse=True):
                                try:
                                    entries.append({
                                        'filename': f.name,
                                        'size_kb': round(f.stat().st_size / 1024.0, 1),
                                        'mtime': time.strftime('%m-%d %H:%M',
                                                               time.localtime(f.stat().st_mtime)),
                                    })
                                except OSError: pass
                        out[dname] = entries
                    self._json_response(out)
                elif path == '/paths/load':
                    qs = self.path.split('?', 1)[1] if '?' in self.path else ''
                    params = {}
                    for part in qs.split('&'):
                        if '=' in part:
                            k, v = part.split('=', 1)
                            from urllib.parse import unquote
                            params[k] = unquote(v)
                    dname = params.get('dir', 'paths')
                    fname = params.get('file', '')
                    if dname not in ('paths', 'recordings'):
                        self._json_response({'error': 'bad dir'}, 400); return
                    base = _offline_dir(dname).resolve()
                    fpath = (base / fname).resolve()
                    # 路径牢笼:必须正好落在 base 目录下(parent 比对,防 ../ 和
                    # 同前缀兄弟目录绕过 startswith 检查)且为 .json
                    if fpath.parent != base or fpath.suffix != '.json' or not fpath.is_file():
                        self._json_response({'error': 'file not found'}, 404); return
                    try:
                        self._json_response(json.loads(fpath.read_text(encoding='utf-8')))
                    except Exception as e:
                        self._json_response({'error': str(e)}, 500)
                elif self._ext_route('GET'):
                    pass  # 扩展路由已响应
                else:
                    self._json_response({'error': 'not found'}, 404)

            def _ext_route(self, method) -> bool:
                """桥扩展路由派发(设计文档 §1):内建路由未命中时问扩展的
                ROUTES 表。fn(app, qs, body) -> dict | (code, dict)。
                返回 True = 已响应(含错误响应)。"""
                if app.bridge_ext is None:
                    return False
                routes = getattr(app.bridge_ext, 'ROUTES', None)
                if not isinstance(routes, dict):
                    return False
                fn = routes.get((method, self.path.split('?')[0].rstrip('/')))
                if fn is None:
                    return False
                qs = self.path.split('?', 1)[1] if '?' in self.path else ''
                body = {}
                if method == 'POST':
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                    except (TypeError, ValueError):
                        length = 0
                    try:
                        body = json.loads(self.rfile.read(length)) if length else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._json_response({'error': 'bad json body'}, 400)
                        return True
                try:
                    out = fn(app, qs, body)
                except Exception as e:
                    self._json_response({'error': str(e)}, 500)
                    return True
                if isinstance(out, tuple):
                    code, obj = out
                    self._json_response(obj, code)
                else:
                    self._json_response(out)
                return True

            def do_POST(self):
                path = self.path.split('?')[0].rstrip('/')
                try:
                    length = int(self.headers.get('Content-Length', 0))
                except (TypeError, ValueError):
                    length = 0
                if path == '/command':
                    try:
                        body = json.loads(self.rfile.read(length)) if length else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._json_response({'error': 'bad json body'}, 400)
                        return
                    command = str(body.get('command', '')).strip()
                    if command:
                        ok, res = app.check_guard([command])
                        if not ok:
                            self._json_response(
                                {'error': 'guardrail violation',
                                 'violations': res['violations']}, 400)
                            return
                        app.pending_command = command
                        app.pending_command_time = time.time()
                        # Phase A1:入队后由 cmd-dispatch 线程串行发出(行为兼容:
                        # 仍是 fire-and-forget 立即返回,ACK 仍落 latest_command_result)
                        app.cmd_queue.put(command)
                        self._json_response({'status': 'sent', 'command': command})
                    else:
                        self._json_response({'error': 'empty command'}, 400)
                elif path == '/batch':
                    # Phase A1(设计文档 §4.2):批量命令,逐项 ACK 等待。
                    # "role" 字段本期预留不校验(未来安全分级扩展位)。
                    try:
                        body = json.loads(self.rfile.read(length)) if length else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._json_response({'error': 'bad json body'}, 400)
                        return
                    commands = body.get('commands')
                    if not isinstance(commands, list) or not commands:
                        self._json_response(
                            {'error': 'commands must be a non-empty list'}, 400)
                        return
                    cmd_strs = [str(c.get('cmd', '')) if isinstance(c, dict)
                                else str(c) for c in commands]
                    ok, res = app.check_guard(cmd_strs)
                    if not ok:
                        self._json_response(
                            {'error': 'guardrail violation',
                             'violations': res['violations']}, 400)
                        return
                    if not app.batch_lock.acquire(blocking=False):
                        self._json_response(
                            {'error': 'another /batch in progress'}, 409)
                        return
                    try:
                        self._json_response(app.run_batch(
                            commands, bool(body.get('stop_on_error', False))))
                    finally:
                        app.batch_lock.release()
                elif path == '/proposal':
                    # P2(设计文档 §3.3):agent 提交参数提案,等人确认
                    try:
                        body = json.loads(self.rfile.read(length)) if length else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._json_response({'error': 'bad json body'}, 400)
                        return
                    commands = body.get('commands')
                    if (not isinstance(commands, list) or not commands
                            or not all(isinstance(c, str) and c.strip()
                                       for c in commands)):
                        self._json_response(
                            {'error': 'commands must be a non-empty string list'},
                            400)
                        return
                    ok, res = app.check_guard(commands)
                    if not ok:
                        self._json_response(
                            {'error': 'guardrail violation',
                             'violations': res['violations']}, 400)
                        return
                    p = app.add_proposal(
                        commands, body.get('rationale'), body.get('expected'))
                    self._json_response({'id': p['id'], 'status': p['status']})
                elif path == '/proposal/decide':
                    # P2:人确认(apply 走与 /batch 相同执行路径)/拒绝
                    try:
                        body = json.loads(self.rfile.read(length)) if length else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._json_response({'error': 'bad json body'}, 400)
                        return
                    try:
                        pid = int(body.get('id'))
                    except (TypeError, ValueError):
                        self._json_response({'error': 'bad id'}, 400)
                        return
                    p, err = app.decide_proposal(pid, str(body.get('decision', '')))
                    if err:
                        self._json_response(
                            {'error': err}, 409 if 'progress' in err else 404
                            if err == 'not found' else 409)
                    else:
                        self._json_response(p)
                elif path == '/shutdown':
                    # headless 主退出通道(设计文档 §3):先发完响应再置事件,
                    # 清理只发生在 owners 线程——本 HTTP 工作线程绝不清理。
                    self._json_response({'status': 'shutting down'})
                    try:
                        self.wfile.flush()
                    except Exception:
                        pass
                    app.shutdown_event.set()
                elif self._ext_route('POST'):
                    pass  # 扩展路由已响应
                else:
                    self._json_response({'error': 'not found'}, 404)

        host = getattr(cfg, 'HTTP_HOST', '127.0.0.1')
        port = getattr(cfg, 'HTTP_PORT', 9898)
        try:
            self.http_server = ThreadingHTTPServer((host, port), Handler)
            t = threading.Thread(target=self.http_server.serve_forever, daemon=True, name='http')
            t.start()
            if getattr(cfg, 'AUTO_OPEN_BROWSER', False):
                if self.root is not None:
                    self.root.after(800, lambda: webbrowser.open(f'http://{host}:{port}'))
                # headless 不开浏览器(设计文档 §3;无显示环境)
        except OSError as e:
            self.http_server = None
            self.queue_log('log', f'[HTTP] Port {port} unavailable: {e}. Dashboard disabled.')
            if not self.headless:
                from tkinter import messagebox
                messagebox.showwarning(
                    'Dashboard Unavailable',
                    f'Could not start HTTP server on port {port}.\n'
                    f'Another program may be using it.\n\n'
                    f'Change "http_port" in config.json and restart.'
                )
            else:
                try:
                    print(f'[HTTP] Port {port} unavailable: {e}. '
                          f'Dashboard disabled.', file=sys.stderr)
                except Exception:
                    pass

    # ======================================================================
    # TCP Server / Client
    # ======================================================================
    def start(self):
        if self.running: return
        self.running = True
        if self.root is not None:
            self.start_btn.config(state='disabled')
            self.stop_btn.config(state='normal')

        # 扩展启动钩子:后台线程执行(扩展可能做重导入,exe 冻结归档首次
        # 解压要 1-2s,不能占 RX/HTTP 线程)
        if self.bridge_ext is not None and hasattr(self.bridge_ext, 'on_start'):
            fn, app = self.bridge_ext.on_start, self
            threading.Thread(target=lambda: fn(app), daemon=True,
                             name='ext-on-start').start()

        if self.mode_var.get() == 'server':
            self.server_thread = threading.Thread(target=self.run_server, daemon=True)
            self.server_thread.start()
        else:
            self.client_thread = threading.Thread(target=self.run_client, daemon=True)
            self.client_thread.start()

        # 自动心跳线程:running 且有客户端连着就周期发心跳命令。
        if self._heartbeat_enabled and self._heartbeat_cmd:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name='heartbeat')
            self._heartbeat_thread.start()
        self.queue_log('status', None)

    def stop(self):
        self.running = False
        self.close_all_sockets()
        if self.root is not None:
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
        self.queue_log('status', None)
        self._sse_broadcast_link()

    def _has_active_connection(self) -> bool:
        """是否有车端 TCP 连着(server 模式看连接表,client 模式看 socket)。"""
        if self.mode_var.get() == 'server':
            return len(self.server_connections) > 0
        return self.client_socket is not None

    # ======================================================================
    # Snapshot helpers (Phase A3)
    # ======================================================================
    def _connection_count(self) -> int:
        if self.mode_var.get() == 'server':
            return len(self.server_connections)
        return 1 if self.client_socket is not None else 0

    def _link_payload(self) -> dict:
        """链路状态载荷(/snapshot 与 SSE 'link' 事件共用,保证两边一致)。"""
        return {
            'running': self.running,
            'mode': self.mode_var.get(),
            'connections': self._connection_count(),
            'hb_age_ms': self._heartbeat_age_ms(),
        }

    def _heartbeat_age_ms(self):
        """距上一次自动心跳发出的毫秒数;还没发过心跳为 None。"""
        if not self._last_hb_ts:
            return None
        return int((time.time() - self._last_hb_ts) * 1000)

    @staticmethod
    def _load_schema_hash():
        """control_schema.json 的 schema_hash(profile 版本锚,设计文档 §2a);
        文件缺失或无该键时为 None。与 /schema 共用 profile_schema_path() 决议。"""
        try:
            p = profile_schema_path()
            if p is not None:
                return json.loads(p.read_text(encoding='utf-8')).get('schema_hash')
        except Exception:
            pass
        return None

    def _heartbeat_loop(self):
        """自动心跳:running 且有连接时,每 interval 发一次心跳命令刷新固件 Gate8。
        分片 sleep 让 stop() 能在 ~0.2s 内退出,不必等满一个周期。"""
        while self.running:
            waited = 0.0
            while waited < self._heartbeat_interval_s and self.running:
                time.sleep(0.2)
                waited += 0.2
            if not self.running:
                break
            if self._has_active_connection():
                try:
                    self.send_command(self._heartbeat_cmd)
                    self._last_hb_ts = time.time()
                except Exception:
                    pass   # 断连由 send_command 内部清理,下一拍 _has_active 自然为假

    def run_server(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host_var.get(), int(self.port_var.get())))
            self.server_socket.listen(8)
            self.server_socket.settimeout(1.0)
            self.queue_log('log', f'[Server] Listening on {self.host_var.get()}:{self.port_var.get()}')
            self.queue_log('status', None)

            while self.running:
                try:
                    client_sock, addr = self.server_socket.accept()
                    self.connection_id_counter += 1
                    cid = self.connection_id_counter
                    self.server_connections[cid] = (client_sock, addr)
                    self.queue_log('log', f'[Server] Client #{cid} connected from {addr}')
                    self.queue_log('connections', None)
                    self._sse_broadcast_link()
                    t = threading.Thread(target=self.handle_client, args=(cid, client_sock, addr), daemon=True)
                    t.start()
                except socket.timeout:
                    continue
        except Exception as e:
            self.queue_log('log', f'[Server] Error: {e}')
        finally:
            self.queue_log('status', None)

    def _new_stream_parser(self):
        """流解析器工厂:扩展优先(create_stream_parser 钩子,可提供混排
        协议解析);无扩展或工厂失败回退核默认 _LineParser(纯行切分)。
        解析器接口:feed(data)->[(type,value)], timed_out()->bool, close()。"""
        if self.bridge_ext is not None and hasattr(self.bridge_ext, 'create_stream_parser'):
            try:
                p = self.bridge_ext.create_stream_parser(self)
                if p is not None:
                    return p
            except Exception as e:
                self.queue_log('log', f'[ext] create_stream_parser failed ({e}); '
                                      'falling back to line parser')
        return _LineParser()

    def handle_client(self, cid, sock, addr):
        parser = self._new_stream_parser()
        try:
            sock.settimeout(1.0)
            while self.running:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    self._consume_stream_buffer(f'client#{cid}', parser, data)
                except socket.timeout:
                    if parser.timed_out():
                        self.queue_log('log', f'[client#{cid}] stream frame timeout; closing connection')
                        break
                    continue
                except Exception as exc:
                    # 协议错误与接收错误都关连接(扩展解析器自行记账后 re-raise)
                    self.queue_log('log', f'[client#{cid}] stream error: {exc}')
                    break
        finally:
            try:
                parser.close()
            except Exception as exc:
                self.queue_log('log', f'[client#{cid}] stream close error: {exc}')
            try:
                sock.close()
            except OSError:
                pass
            self.server_connections.pop(cid, None)
            self.queue_log('log', f'[Server] Client #{cid} disconnected')
            self.queue_log('connections', None)
            self._sse_broadcast_link()

    def run_client(self):
        host = self.host_var.get()
        port = int(self.port_var.get())
        parser = self._new_stream_parser()
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5.0)
            self.client_socket.connect((host, port))
            self.client_socket.settimeout(1.0)
            self.queue_log('log', f'[Client] Connected to {host}:{port}')
            self.queue_log('status', None)
            self._sse_broadcast_link()

            while self.running:
                try:
                    data = self.client_socket.recv(4096)
                    if not data:
                        break
                    self._consume_stream_buffer('server', parser, data)
                except socket.timeout:
                    if parser.timed_out():
                        self.queue_log('log', '[server] stream frame timeout; closing')
                        break
                    continue
                except Exception as exc:
                    self.queue_log('log', f'[server] stream error: {exc}')
                    break
        except Exception as e:
            self.queue_log('log', f'[Client] Error: {e}')
        finally:
            try:
                parser.close()
            except Exception as exc:
                self.queue_log('log', f'[server] stream close error: {exc}')
            if self.client_socket:
                try:
                    self.client_socket.close()
                except OSError:
                    pass
                finally:
                    self.client_socket = None
            self.queue_log('status', None)
            self._sse_broadcast_link()

    def _consume_stream_buffer(self, source, parser, data):
        for event_type, value in parser.feed(data):
            if event_type == 'line':
                if self.bridge_ext is not None and self._ext_on_line(source, value):
                    continue  # 扩展已消费该行(如语音桥),不再走核 dispatch
                self._dispatch_line(source, value)
            elif self.bridge_ext is not None:
                try:
                    self.bridge_ext.on_frame(self, source, value)
                except Exception:
                    pass

    def _ext_on_line(self, source, line) -> bool:
        """on_line 钩子的异常护栏:扩展炸了当'未消费'处理,行继续正常 dispatch。"""
        try:
            return bool(self.bridge_ext.on_line(self, source, line))
        except Exception:
            return False

    def _dispatch_line(self, source, text):
        if any(text.startswith(p) for p in self._TELEMETRY_PREFIXES):
            self.queue_log('telemetry', (source, text))
        elif any(text.startswith(p) for p in self._RESPONSE_PREFIXES):
            parsed = self.parse_telemetry_text(text)
            ptype = parsed.get('_packet', '')
            self._handle_special_packet(ptype, parsed)
            self.queue_log('log', f'[{source}] {text}')
        else:
            self.queue_log('log', f'[{source}] {text}')

    def close_all_sockets(self):
        if self.server_socket:
            try: self.server_socket.close()
            except: pass
            self.server_socket = None
        if self.client_socket:
            try: self.client_socket.close()
            except: pass
            self.client_socket = None
        for cid, (sock, _) in list(self.server_connections.items()):
            try: sock.close()
            except: pass
        self.server_connections.clear()

    # ======================================================================
    # Sending
    # ======================================================================
    def send_command(self, text):
        if not text: return
        data = text
        if self.crlf_var.get() and not data.endswith('\r\n'):
            data = data.rstrip('\r\n') + '\r\n'
        encoded = data.encode(ENCODING)

        # 带 1s timeout 的 send：旧实现 sock.sendall 无 timeout，当车端 wifi_spi
        # 模块 RX buffer 卡住时 sendall 会 indefinitely block，单线程 HTTP server
        # 就被卡死（curl /command 永远等不到 response，调参完全不能用）。
        # 1s 远大于正常 send 延迟（<10ms），仅在 half-open 时触发 → 立即 close +
        # 从连接表移除，让车端 cpu1_main 检测断开 + 自动重连干净 socket。
        # send_lock:多 HTTP 线程并发 POST /command 时 sendall 字节可能交错成坏行。
        # 发完把超时还原成 1.0(不是 None!):RX 线程靠 recv 的 1s 超时轮询
        # self.running;设回 None 会让 recv 永久阻塞,Stop 后线程退不出。
        with self.send_lock:
            if self.mode_var.get() == 'server':
                broken = []
                for cid, (sock, _) in list(self.server_connections.items()):
                    try:
                        sock.settimeout(1.0)
                        sock.sendall(encoded)
                    except Exception:
                        broken.append(cid)
                        try: sock.close()
                        except: pass
                for cid in broken:
                    self.server_connections.pop(cid, None)
            elif self.client_socket:
                try:
                    self.client_socket.settimeout(1.0)
                    self.client_socket.sendall(encoded)
                except Exception:
                    try: self.client_socket.close()
                    except: pass
                    self.client_socket = None

    def send_quick(self, cmd):
        self.send_command(cmd)
        self.queue_log('log', f'[Sent] {cmd}')

    def _send_custom_cmd(self, cmd):
        self.pending_command = cmd
        self.pending_command_time = time.time()
        self.send_command(cmd)
        self.queue_log('log', f'[Sent] {cmd}')

    def send_data(self):
        text = self.send_entry.get().strip()
        if text:
            self.send_command(text)
            self.queue_log('log', f'[Sent] {text}')
            self.send_entry.delete(0, 'end')

    # ======================================================================
    # Connection state
    # ======================================================================
    def on_mode_change(self):
        is_server = self.mode_var.get() == 'server'
        self.host_var.set('0.0.0.0' if is_server else '127.0.0.1')

    def refresh_connection_summary(self):
        if self.root is None:
            return
        if not self.running:
            self.conn_summary.config(text='Stopped', fg=THEME['text_dim'])
        elif self.mode_var.get() == 'server':
            n = len(self.server_connections)
            self.conn_summary.config(text=f'Server :{self.port_var.get()} ({n} clients)', fg=THEME['ok'])
        else:
            connected = self.client_socket is not None
            self.conn_summary.config(
                text=f'Connected to {self.host_var.get()}:{self.port_var.get()}' if connected else 'Connecting...',
                fg=THEME['ok'] if connected else THEME['warn'])

    def refresh_connection_list(self):
        if self.root is None:
            return
        self.conn_listbox.delete(0, 'end')
        # list() 快照:RX 线程断开时并发改 dict,直接迭代会 RuntimeError
        for cid, (_, addr) in list(self.server_connections.items()):
            self.conn_listbox.insert('end', f'#{cid} {addr[0]}:{addr[1]}')

    # ======================================================================
    # Simulation
    # ======================================================================
    def start_simulation(self):
        if self.sim_running: return
        self.sim_running = True
        self.sim_tick = 0
        self.sim_thread = threading.Thread(target=self._sim_loop, daemon=True)
        self.sim_thread.start()
        self.queue_log('log', '[Sim] Started')

    def stop_simulation(self):
        self.sim_running = False
        self.queue_log('log', '[Sim] Stopped')

    def _sim_loop(self):
        while self.sim_running:
            try:
                text = cfg.build_simulated_packet(self.sim_tick)
                if text:
                    self.queue_log('telemetry', ('sim', text))
                self.sim_tick += 1
            except Exception:
                pass
            time.sleep(0.016)  # ~60fps simulation

    # ======================================================================
    # Snapshot API (for HTTP)
    # ======================================================================
    def get_telemetry_snapshot(self):
        with self.telemetry_lock:
            return {
                'latest': dict(self.latest_parsed) if self.latest_parsed else None,
                'history': list(self.telemetry_history[-100:]),
            }

    # ======================================================================
    # Cleanup
    # ======================================================================
    def _cleanup_steps(self):
        """幂等清理链条(设计文档 §3):单步失败只记日志,绝不阻断后续
        步骤;调用方限于 owners 线程(headless 主线程 / GUI Tk 泵线程)。"""
        if self._cleaned:
            return
        self._cleaned = True

        def _tcp_off():
            self.running = False
            self.close_all_sockets()

        def _http_off():
            if self.http_server:
                self.http_server.shutdown()

        def _ext_off():
            if self.bridge_ext is not None:
                fn = getattr(self.bridge_ext, 'shutdown', None)
                if fn is not None:
                    fn(self)

        steps = (
            ('pump', lambda: self._pump_stop.set()),
            ('tcp', _tcp_off),
            ('sim', lambda: setattr(self, 'sim_running', False)),
            ('http', _http_off),
            ('ext', _ext_off),
        )
        for name, step in steps:
            try:
                step()
            except Exception as e:
                message = f'[shutdown] step {name} failed: {e}'
                if self.headless:
                    # pump 已在第一步停止；直接写文件，确保 windowed exe
                    # 没有 stderr 时仍保留清理失败诊断。
                    self._headless_log([message])
                else:
                    try:
                        print(message, file=sys.stderr)
                    except Exception:
                        pass

    def on_close(self):
        self._cleanup_steps()
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass


# ===========================================================================
# Entry point
# ===========================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--headless' in argv:
        # 纯服务模式:无 GUI(也不要求 tkinter/ttkbootstrap 可导入);
        # Ctrl+C 与 POST /shutdown 走同一幂等清理。
        app = TuningToolApp(root=None)
        if getattr(cfg, 'AUTO_START_TCP', False):
            app.start()
        try:
            app.shutdown_event.wait()
        except KeyboardInterrupt:
            pass
        app._cleanup_steps()
        sys.exit(0)

    import ttkbootstrap as ttkb
    root = ttkb.Window(
        title=getattr(cfg, 'APP_TITLE', 'SmartCar Tuning Tool'),
        themename='darkly',
        size=(400, 680),
    )
    app = TuningToolApp(root)
    # config.auto_start_tcp=True 时，工具启动后自动调用 start() 拉起 TCP 监听。
    # 用 after(0) 让 mainloop 先转一拍把 UI 初始化干净再启 TCP，避免线程
    # 提前访问还没建好的 widget。
    if getattr(cfg, 'AUTO_START_TCP', False):
        root.after(0, app.start)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    # mainloop 返回(关窗或 /shutdown)后兜底清理;/shutdown 路径已在
    # Tk 泵线程清理并 destroy,这里幂等无操作
    app._cleanup_steps()


if __name__ == '__main__':
    main()
