#!/usr/bin/env python3
"""
SmartCar Tuning Tool - Config-driven desktop tuning GUI.

All domain-specific knowledge lives in tuning_config.py.
This file is the generic framework: TCP, HTTP, chart, cards, commands.
You should NOT need to edit this file for normal use.
"""

import json
import math
import queue
import socket
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ttkbootstrap as ttkb
from ttkbootstrap.constants import *

# ---------------------------------------------------------------------------
# Load user config
# ---------------------------------------------------------------------------
import tuning_config as cfg

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
ENCODING = 'utf-8'
DATA_DIR = Path(__file__).resolve().parent / 'runtime'
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

    def __init__(self, root: ttkb.Window):
        self.root = root
        root.configure(bg=THEME['bg_deep'])

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # --- state ---
        self.mode_var = tk.StringVar(value='server')
        self.host_var = tk.StringVar(value='0.0.0.0')
        self.port_var = tk.StringVar(value='8080')
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
        self.latest_telemetry = None
        self.latest_parsed = {}
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

        # Metric StringVars (auto-generated from config)
        self.metric_vars = {}
        for k in ALL_METRIC_KEYS:
            self.metric_vars[k] = tk.StringVar(value='--')
        self.metric_vars['fix_state'] = tk.StringVar(value='--')
        self.metric_vars['health_state'] = tk.StringVar(value='--')

        # Plot state
        self.plot_series = {k: [] for k, _, _ in getattr(cfg, 'PLOT_KEYS', [])}
        self.plot_enabled_vars = {}
        for k, _, default_on in getattr(cfg, 'PLOT_KEYS', []):
            self.plot_enabled_vars[k] = tk.BooleanVar(value=default_on)
        self.plot_smooth_var = tk.BooleanVar(value=False)
        self.plot_fill_var = tk.BooleanVar(value=False)
        self.chart_summary_var = tk.StringVar(value='Waiting for data...')
        self.plot_scale_mode = tk.StringVar(value='auto')
        self.plot_fixed_min = tk.StringVar(value='-50')
        self.plot_fixed_max = tk.StringVar(value='50')

        # Zoom state
        self.zoom_view_start = 0
        self.zoom_view_count = 0
        self.zoom_auto_follow = True
        self.zoom_drag_start = None
        self.chart_crosshair_x = None
        self._minimap_drag_x = None

        # Sparkline
        self.sparkline_data = {k: [] for k, _ in getattr(cfg, 'PRIMARY_METRICS', [])}
        self.sparkline_max = 20
        self._sparkline_canvases = {}
        self._hover_pending = False

        # Custom tab vars
        self._custom_tab_vars = {}
        for tab in getattr(cfg, 'CUSTOM_TABS', []):
            for fk, _ in tab.get('fields', []):
                if fk not in self.metric_vars:
                    self.metric_vars[fk] = tk.StringVar(value='--')
            for _, vk in tab.get('result_keys', []):
                if vk not in self.metric_vars:
                    self.metric_vars[vk] = tk.StringVar(value='--')

        # Command tab param vars
        self._cmd_param_vars = {}

        # Connection panel state
        self._conn_panel_expanded = False

        self._build_ui()
        self.start_http_server()
        self.root.after(100, self.process_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ======================================================================
    # UI Construction - Two Column Layout
    # ======================================================================
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=THEME['bg_deep'])
        outer.pack(fill='both', expand=True)

        # Top metrics bar spanning full width
        self._build_top_bar(outer)

        # Main area: vertical paned - workarea + console
        main_pw = tk.PanedWindow(outer, orient='vertical', bg=THEME['bg_deep'],
                                  sashwidth=4, sashrelief='flat')
        main_pw.pack(fill='both', expand=True)

        workarea = tk.PanedWindow(main_pw, orient='horizontal', bg=THEME['bg_deep'],
                                   sashwidth=4, sashrelief='flat')
        main_pw.add(workarea, stretch='always')

        # Two columns: chart (left) + right panel
        chart_area = self._build_chart_area(workarea)
        right = self._build_right_panel(workarea)

        workarea.add(chart_area, width=700, minsize=400, stretch='always')
        workarea.add(right, width=380, minsize=280, stretch='always')

        console = self._build_console(main_pw)
        main_pw.add(console, height=100, minsize=60, stretch='never')

    # --- Top Metrics Bar ---
    def _build_top_bar(self, parent):
        bar = tk.Frame(parent, bg=THEME['bg_mid'])
        bar.pack(fill='x', padx=0, pady=0)

        inner = tk.Frame(bar, bg=THEME['bg_mid'])
        inner.pack(fill='both', expand=True, padx=8, pady=6)

        primary = getattr(cfg, 'PRIMARY_METRICS', [])
        for i, (key, label) in enumerate(primary):
            self._build_metric_card(inner, key, label, i)
            inner.columnconfigure(i, weight=1)

    # --- Metric Card with Sparkline ---
    def _build_metric_card(self, parent, key, label, index):
        card_bg = '#1c2640'
        card = tk.Frame(parent, bg=card_bg, highlightbackground='#2a3555',
                         highlightthickness=1, padx=10, pady=6)
        card.grid(row=0, column=index, padx=4, pady=2, sticky='nsew')

        # Accent line at top — use channel-specific color
        plot_keys = getattr(cfg, 'PLOT_KEYS', [])
        accent_color = plot_keys[index][1] if index < len(plot_keys) else THEME['accent_light']
        accent = tk.Frame(card, bg=accent_color, height=3)
        accent.pack(fill='x')

        # Top row: label + trend arrow
        top_row = tk.Frame(card, bg=card_bg)
        top_row.pack(fill='x')
        tk.Label(top_row, text=label, bg=card_bg, fg=THEME['text_muted'],
                 font=('Segoe UI', 9)).pack(side='left')

        trend_label = tk.Label(top_row, text='', bg=card_bg,
                               fg=THEME['text_muted'], font=('Segoe UI', 9))
        trend_label.pack(side='right')
        if not hasattr(self, '_trend_labels'):
            self._trend_labels = {}
        self._trend_labels[key] = trend_label

        # Value
        tk.Label(card, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                 bg=card_bg, fg=THEME['text'],
                 font=('Consolas', 16, 'bold')).pack(anchor='w')

        # Sparkline canvas — store channel color alongside canvas
        line_color = plot_keys[index][1] if index < len(plot_keys) else THEME['accent_light']
        spark_canvas = tk.Canvas(card, bg=card_bg, highlightthickness=0,
                                  height=28)
        spark_canvas.pack(fill='x', pady=(2, 0))
        self._sparkline_canvases[key] = (spark_canvas, line_color)

    # --- Chart Area (left column) ---
    def _build_chart_area(self, parent):
        center = tk.Frame(parent, bg=THEME['bg_deep'])

        # Chart frame
        chart_frame = tk.Frame(center, bg=THEME['bg_mid'])
        chart_frame.pack(fill='both', expand=True, padx=4, pady=4)

        # Chart canvas
        self.chart_canvas = tk.Canvas(chart_frame, bg='#0f172a', highlightthickness=0)
        self.chart_canvas.pack(fill='both', expand=True)

        # Bind mouse events for zoom/pan/hover
        self.chart_canvas.bind('<MouseWheel>', self._on_chart_scroll)
        self.chart_canvas.bind('<ButtonPress-1>', self._on_chart_press)
        self.chart_canvas.bind('<B1-Motion>', self._on_chart_drag)
        self.chart_canvas.bind('<ButtonRelease-1>', self._on_chart_release)
        self.chart_canvas.bind('<Double-Button-1>', lambda e: self._chart_zoom_reset())
        self.chart_canvas.bind('<Motion>', self._on_chart_hover)
        self.chart_canvas.bind('<Leave>', self._on_chart_leave)

        # Minimap canvas (below chart)
        self.minimap_canvas = tk.Canvas(chart_frame, bg='#131325', highlightthickness=0,
                                         height=45)
        self.minimap_canvas.pack(fill='x', padx=0, pady=(0, 0))
        self.minimap_canvas.bind('<ButtonPress-1>', self._on_minimap_press)
        self.minimap_canvas.bind('<B1-Motion>', self._on_minimap_drag)

        # Chart controls row
        ctrl = tk.Frame(chart_frame, bg=THEME['bg_mid'])
        ctrl.pack(fill='x', padx=4, pady=2)

        for key, color, _ in getattr(cfg, 'PLOT_KEYS', []):
            cb = tk.Checkbutton(ctrl, text=key, variable=self.plot_enabled_vars[key],
                                 bg=THEME['bg_mid'], fg=color, selectcolor=THEME['bg_deep'],
                                 font=('Consolas', 9), command=self.draw_chart)
            cb.pack(side='left', padx=2)

        tk.Checkbutton(ctrl, text='Smooth', variable=self.plot_smooth_var,
                        bg=THEME['bg_mid'], fg=THEME['text_muted'], selectcolor=THEME['bg_deep'],
                        command=self.draw_chart).pack(side='right', padx=2)
        tk.Checkbutton(ctrl, text='Fill', variable=self.plot_fill_var,
                        bg=THEME['bg_mid'], fg=THEME['text_muted'], selectcolor=THEME['bg_deep'],
                        command=self.draw_chart).pack(side='right', padx=2)

        # Zoom buttons
        zoom_frame = tk.Frame(ctrl, bg=THEME['bg_mid'])
        zoom_frame.pack(side='right', padx=6)
        ttkb.Button(zoom_frame, text='+', width=2, bootstyle='secondary-outline',
                     command=self._chart_zoom_in).pack(side='left', padx=1)
        ttkb.Button(zoom_frame, text='-', width=2, bootstyle='secondary-outline',
                     command=self._chart_zoom_out).pack(side='left', padx=1)
        ttkb.Button(zoom_frame, text='Reset', width=5, bootstyle='secondary-outline',
                     command=self._chart_zoom_reset).pack(side='left', padx=1)

        # Scale controls
        scale_frame = tk.Frame(chart_frame, bg=THEME['bg_mid'])
        scale_frame.pack(fill='x', padx=4, pady=1)
        tk.Label(scale_frame, text='Scale:', bg=THEME['bg_mid'], fg=THEME['text_muted']).pack(side='left')
        ttkb.Combobox(scale_frame, textvariable=self.plot_scale_mode, values=['auto', 'fixed'],
                     width=6, state='readonly').pack(side='left', padx=2)
        tk.Label(scale_frame, text='Min:', bg=THEME['bg_mid'], fg=THEME['text_muted']).pack(side='left')
        ttkb.Entry(scale_frame, textvariable=self.plot_fixed_min, width=6).pack(side='left', padx=2)
        tk.Label(scale_frame, text='Max:', bg=THEME['bg_mid'], fg=THEME['text_muted']).pack(side='left')
        ttkb.Entry(scale_frame, textvariable=self.plot_fixed_max, width=6).pack(side='left', padx=2)

        # Summary
        tk.Label(chart_frame, textvariable=self.chart_summary_var, bg=THEME['bg_mid'],
                 fg=THEME['text_muted'], font=('Consolas', 9), anchor='w').pack(fill='x', padx=4)

        return center

    # --- Right Panel: connection + status + notebook + commands + send ---
    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=THEME['bg_mid'])

        # Connection header (collapsible)
        self._build_connection_header(right)

        # Status banner
        banner = tk.Frame(right, bg=THEME['bg_mid'])
        banner.pack(fill='x', padx=4, pady=2)
        tk.Label(banner, textvariable=self.metric_vars['fix_state'], bg=THEME['bg_mid'],
                 fg=THEME['accent_light'], font=('Segoe UI', 10, 'bold')).pack(side='left', padx=4)
        tk.Label(banner, textvariable=self.metric_vars['health_state'], bg=THEME['bg_mid'],
                 fg=THEME['ok'], font=('Segoe UI', 10)).pack(side='right', padx=4)

        # Notebook with tabs
        nb = ttk.Notebook(right)
        nb.pack(fill='both', expand=True, padx=4, pady=4)

        # Tab 1: Telemetry
        self._build_tab_telemetry(nb)

        # Custom status tabs
        for tab_def in getattr(cfg, 'CUSTOM_TABS', []):
            self._build_custom_status_tab(nb, tab_def)

        # Command tabs
        for tab_def in getattr(cfg, 'COMMAND_TABS', []):
            self._build_command_tab(nb, tab_def)

        # Pinned quick commands + send at the bottom
        bottom_frame = tk.Frame(right, bg=THEME['bg_deep'])
        bottom_frame.pack(fill='x', side='bottom', padx=4, pady=4)

        # Quick commands
        qc_list = getattr(cfg, 'QUICK_COMMANDS', [])
        if qc_list:
            qc_frame = tk.Frame(bottom_frame, bg=THEME['bg_mid'])
            qc_frame.pack(fill='x', pady=(0, 4))
            qc_inner = tk.Frame(qc_frame, bg=THEME['bg_mid'])
            qc_inner.pack(fill='x', padx=4, pady=4)
            for qc in qc_list:
                cmd = qc['command']
                ttkb.Button(qc_inner, text=qc['label'],
                            command=lambda c=cmd: self.send_quick(c),
                            bootstyle='secondary-outline').pack(side='left', padx=2)

        # Simulation buttons
        if hasattr(cfg, 'build_simulated_packet') and cfg.build_simulated_packet is not None:
            sim_frame = tk.Frame(bottom_frame, bg=THEME['bg_mid'])
            sim_frame.pack(fill='x', pady=(0, 4))
            sim_inner = tk.Frame(sim_frame, bg=THEME['bg_mid'])
            sim_inner.pack(fill='x', padx=4, pady=4)
            ttkb.Button(sim_inner, text='Start Sim', command=self.start_simulation,
                        bootstyle='info-outline').pack(side='left', padx=2)
            ttkb.Button(sim_inner, text='Stop Sim', command=self.stop_simulation,
                        bootstyle='secondary-outline').pack(side='left', padx=2)

        # Send box
        send_frame = tk.Frame(bottom_frame, bg=THEME['bg_mid'])
        send_frame.pack(fill='x')
        send_inner = tk.Frame(send_frame, bg=THEME['bg_mid'])
        send_inner.pack(fill='x', padx=4, pady=4)

        self.send_entry = ttkb.Entry(send_inner)
        self.send_entry.pack(fill='x', side='left', expand=True, padx=(0, 4))
        self.send_entry.bind('<Return>', lambda e: self.send_data())

        ttkb.Button(send_inner, text='Send', command=self.send_data,
                    bootstyle='info', width=6).pack(side='right')

        crlf_row = tk.Frame(send_frame, bg=THEME['bg_mid'])
        crlf_row.pack(fill='x', padx=4, pady=(0, 2))
        tk.Checkbutton(crlf_row, text='Append CRLF', variable=self.crlf_var,
                        bg=THEME['bg_mid'], fg=THEME['text_muted'],
                        selectcolor=THEME['bg_deep']).pack(anchor='w')

        return right

    # --- Connection Header (collapsible) ---
    def _build_connection_header(self, parent):
        header = tk.Frame(parent, bg=THEME['bg_mid'])
        header.pack(fill='x', padx=4, pady=(4, 2))

        # Top bar: always visible - click to toggle
        top_bar = tk.Frame(header, bg=THEME['bg_mid'], cursor='hand2')
        top_bar.pack(fill='x')
        top_bar.bind('<Button-1>', lambda e: self._toggle_connection_panel())

        # Connection status dot
        self.conn_dot = tk.Canvas(top_bar, width=12, height=12, bg=THEME['bg_mid'],
                                   highlightthickness=0)
        self.conn_dot.pack(side='left', padx=(6, 4), pady=4)
        self.conn_dot.create_oval(2, 2, 10, 10, fill=THEME['text_dim'], outline='', tags='dot')
        self.conn_dot.bind('<Button-1>', lambda e: self._toggle_connection_panel())

        self.conn_badge = tk.Label(top_bar, text='Stopped', bg=THEME['bg_mid'],
                                    fg=THEME['text_muted'], font=('Segoe UI', 10, 'bold'))
        self.conn_badge.pack(side='left', padx=2)
        self.conn_badge.bind('<Button-1>', lambda e: self._toggle_connection_panel())

        self._conn_toggle_arrow = tk.Label(top_bar, text='\u25B6', bg=THEME['bg_mid'],
                                            fg=THEME['text_dim'], font=('Segoe UI', 8))
        self._conn_toggle_arrow.pack(side='right', padx=6)
        self._conn_toggle_arrow.bind('<Button-1>', lambda e: self._toggle_connection_panel())

        # Collapsible detail panel
        self._conn_detail = tk.Frame(header, bg=THEME['bg_mid'])
        # Initially collapsed - don't pack

        mode_frame = tk.Frame(self._conn_detail, bg=THEME['bg_mid'])
        mode_frame.pack(fill='x', padx=4, pady=2)
        ttkb.Radiobutton(mode_frame, text='Server', variable=self.mode_var, value='server',
                          command=self.on_mode_change, bootstyle='info-toolbutton').pack(side='left', padx=2)
        ttkb.Radiobutton(mode_frame, text='Client', variable=self.mode_var, value='client',
                          command=self.on_mode_change, bootstyle='info-toolbutton').pack(side='left', padx=2)

        for label, var in [('Host:', self.host_var), ('Port:', self.port_var)]:
            row = tk.Frame(self._conn_detail, bg=THEME['bg_mid'])
            row.pack(fill='x', padx=4, pady=1)
            tk.Label(row, text=label, bg=THEME['bg_mid'], fg=THEME['text_muted'],
                     width=5, anchor='e', font=('Segoe UI', 9)).pack(side='left')
            ttkb.Entry(row, textvariable=var, width=14).pack(side='left', padx=2)

        btn_frame = tk.Frame(self._conn_detail, bg=THEME['bg_mid'])
        btn_frame.pack(fill='x', padx=4, pady=4)
        self.start_btn = ttkb.Button(btn_frame, text='Start', command=self.start,
                                      bootstyle='success', width=8)
        self.start_btn.pack(side='left', padx=2)
        self.stop_btn = ttkb.Button(btn_frame, text='Stop', command=self.stop,
                                     bootstyle='danger', width=8, state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        # Connection list
        self.conn_listbox = tk.Listbox(self._conn_detail, bg=THEME['bg_deep'], fg=THEME['text'],
                                        height=3, font=('Consolas', 9),
                                        selectbackground=THEME['accent'],
                                        highlightthickness=0, borderwidth=0)
        self.conn_listbox.pack(fill='x', padx=4, pady=4)

        # Also keep a legacy conn_summary label for refresh_connection_summary
        self.conn_summary = tk.Label(self._conn_detail, text='Stopped', bg=THEME['bg_mid'],
                                      fg=THEME['text_muted'], font=('Segoe UI', 9))
        self.conn_summary.pack(fill='x', padx=4)

    def _toggle_connection_panel(self):
        if self._conn_panel_expanded:
            self._conn_detail.pack_forget()
            self._conn_toggle_arrow.config(text='\u25B6')
            self._conn_panel_expanded = False
        else:
            self._conn_detail.pack(fill='x')
            self._conn_toggle_arrow.config(text='\u25BC')
            self._conn_panel_expanded = True

    # --- Telemetry Tab ---
    def _build_tab_telemetry(self, notebook):
        tab = tk.Frame(notebook, bg=THEME['bg_mid'])
        notebook.add(tab, text='Telemetry')

        # Detail metrics
        det_frame = tk.Frame(tab, bg=THEME['bg_mid'])
        det_frame.pack(fill='x', padx=4, pady=4)
        tk.Label(det_frame, text='Details', bg=THEME['bg_mid'], fg=THEME['text_muted'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=2, pady=(0, 2))

        det_grid = tk.Frame(det_frame, bg=THEME['bg_surface'])
        det_grid.pack(fill='x')
        for i, (key, label) in enumerate(getattr(cfg, 'DETAIL_METRICS', [])):
            r, c = divmod(i, 2)
            tk.Label(det_grid, text=label, bg=THEME['bg_surface'], fg=THEME['text_muted'],
                     font=('Consolas', 9), width=10, anchor='e').grid(row=r, column=c*2, padx=4, pady=2)
            tk.Label(det_grid, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=THEME['bg_surface'], fg=THEME['text'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=4, pady=2, sticky='w')

        # Extended metrics (scrollable)
        ext_outer = tk.Frame(tab, bg=THEME['bg_mid'])
        ext_outer.pack(fill='both', expand=True, padx=4, pady=4)
        tk.Label(ext_outer, text='Extended', bg=THEME['bg_mid'], fg=THEME['text_muted'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=2, pady=(0, 2))

        ext_frame = tk.Frame(ext_outer, bg=THEME['bg_surface'])
        ext_frame.pack(fill='both', expand=True)

        canvas = tk.Canvas(ext_frame, bg=THEME['bg_surface'], highlightthickness=0)
        scrollbar = ttkb.Scrollbar(ext_frame, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=THEME['bg_surface'])
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>',
                    lambda ev: canvas.yview_scroll(-1 * (ev.delta // 120), 'units')))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        for i, (key, label) in enumerate(getattr(cfg, 'EXTENDED_METRICS', [])):
            r, c = divmod(i, 2)
            tk.Label(inner, text=label, bg=THEME['bg_surface'], fg=THEME['text_muted'],
                     font=('Consolas', 9), width=10, anchor='e').grid(row=r, column=c*2, padx=4, pady=2)
            tk.Label(inner, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=THEME['bg_surface'], fg=THEME['text'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=4, pady=2, sticky='w')

    # --- Custom Status Tab ---
    def _build_custom_status_tab(self, notebook, tab_def):
        name = tab_def.get('name', 'Custom')
        fields = tab_def.get('fields', [])
        result_keys = tab_def.get('result_keys', [])

        tab = tk.Frame(notebook, bg=THEME['bg_mid'])
        notebook.add(tab, text=name)

        for i, (key, label) in enumerate(fields):
            r, c = divmod(i, 2)
            tk.Label(tab, text=label, bg=THEME['bg_mid'], fg=THEME['text_muted'],
                     font=('Consolas', 9), width=12, anchor='e').grid(row=r, column=c*2, padx=4, pady=2)
            tk.Label(tab, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=THEME['bg_mid'], fg=THEME['text'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=4, pady=2, sticky='w')

        if result_keys:
            res_frame = tk.Frame(tab, bg=THEME['bg_surface'])
            base_row = (len(fields) + 1) // 2 + 1
            res_frame.grid(row=base_row, column=0, columnspan=4, sticky='ew', padx=4, pady=4)
            tk.Label(res_frame, text='Last Results', bg=THEME['bg_surface'],
                     fg=THEME['text_muted'], font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=4, pady=2)
            for i, (tag, var_key) in enumerate(result_keys):
                row = tk.Frame(res_frame, bg=THEME['bg_surface'])
                row.pack(fill='x', padx=4, pady=1)
                tk.Label(row, text=f'{tag}:', bg=THEME['bg_surface'], fg=THEME['text_muted'],
                         font=('Consolas', 9)).pack(side='left')
                tk.Label(row, textvariable=self.metric_vars.get(var_key, tk.StringVar(value='--')),
                         bg=THEME['bg_surface'], fg=THEME['text'],
                         font=('Consolas', 9), wraplength=250, anchor='w').pack(side='left', padx=4)

    # --- Command Tab ---
    def _build_command_tab(self, notebook, tab_def):
        name = tab_def.get('name', 'Commands')
        buttons = tab_def.get('buttons', [])
        params = tab_def.get('params', [])

        tab = tk.Frame(notebook, bg=THEME['bg_mid'])
        notebook.add(tab, text=name)

        for btn in buttons:
            cmd = btn['command']
            ttkb.Button(tab, text=btn['label'],
                        command=lambda c=cmd: self._send_custom_cmd(c),
                        bootstyle='secondary-outline').pack(fill='x', padx=8, pady=2)

        for p in params:
            pf = tk.Frame(tab, bg=THEME['bg_mid'])
            pf.pack(fill='x', padx=8, pady=2)
            tk.Label(pf, text=p['label'], bg=THEME['bg_mid'], fg=THEME['text_muted'],
                     font=('Consolas', 9), width=16, anchor='e').pack(side='left')
            var = tk.StringVar(value=p.get('default', ''))
            self._cmd_param_vars[p['prefix']] = var
            entry = ttkb.Entry(pf, textvariable=var, width=12)
            entry.pack(side='left', padx=4)
            prefix = p['prefix']
            ttkb.Button(pf, text='Set',
                        command=lambda pr=prefix, v=var: self._send_custom_cmd(f'{pr} {v.get()}'),
                        bootstyle='info', width=4).pack(side='left')

    # --- Console ---
    def _build_console(self, parent):
        frame = tk.Frame(parent, bg=THEME['bg_deep'])
        self.log_text = tk.Text(frame, bg=THEME['console_bg'], fg=THEME['console_fg'], height=5,
                                 font=('Consolas', 9), state='disabled', wrap='none',
                                 highlightthickness=0, borderwidth=0)
        scrollbar = ttkb.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.log_text.pack(fill='both', expand=True)
        return frame

    # ======================================================================
    # Sparkline Updates
    # ======================================================================
    def _update_sparklines(self, parsed):
        for key, _ in getattr(cfg, 'PRIMARY_METRICS', []):
            val = parsed.get(key)
            if val is not None and isinstance(val, (int, float)):
                hist = self.sparkline_data.setdefault(key, [])
                hist.append(float(val))
                if len(hist) > self.sparkline_max:
                    del hist[:-self.sparkline_max]

                # Update trend arrow with delta value
                if key in getattr(self, '_trend_labels', {}):
                    if len(hist) >= 2:
                        delta = hist[-1] - hist[-2]
                        if delta > 0.01:
                            color = THEME['ok']
                            arrow = '\u25B2'
                        elif delta < -0.01:
                            color = THEME['danger']
                            arrow = '\u25BC'
                        else:
                            color = THEME['text_dim']
                            arrow = '\u25C6'
                        self._trend_labels[key].config(
                            fg=color, text=f'{arrow} {abs(delta):.2f}')

                # Draw sparkline
                if key in self._sparkline_canvases:
                    self._draw_sparkline(key)

    def _draw_sparkline(self, key):
        canvas, line_color = self._sparkline_canvases[key]
        canvas.delete('all')
        data = self.sparkline_data.get(key, [])
        if len(data) < 2:
            return

        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 60)
        h = max(canvas.winfo_height(), 14)
        padding = 2

        mn = min(data)
        mx = max(data)
        span = mx - mn
        if span == 0:
            span = 1

        points = []
        for i, v in enumerate(data):
            x = padding + (w - 2 * padding) * i / max(len(data) - 1, 1)
            y = padding + (h - 2 * padding) * (1 - (v - mn) / span)
            points.extend((x, y))

        if len(points) >= 4:
            canvas.create_line(*points, fill=line_color, width=1, smooth=True)

    # ======================================================================
    # Chart Zoom / Pan
    # ======================================================================
    def _chart_zoom_in(self):
        self._apply_zoom(0.5, 0.5)

    def _chart_zoom_out(self):
        self._apply_zoom(2.0, 0.5)

    def _chart_zoom_reset(self):
        self.zoom_view_start = 0
        self.zoom_view_count = 0
        self.zoom_auto_follow = True
        self.draw_chart()

    def _apply_zoom(self, factor, center_frac):
        # Find longest visible series
        max_len = 0
        for key, _, _ in getattr(cfg, 'PLOT_KEYS', []):
            if self.plot_enabled_vars.get(key, tk.BooleanVar(value=False)).get():
                max_len = max(max_len, len(self.plot_series.get(key, [])))
        if max_len < 2:
            return

        old_count = self.zoom_view_count if self.zoom_view_count > 0 else max_len
        new_count = int(old_count * factor)
        new_count = max(10, min(new_count, max_len))

        if new_count >= max_len:
            self.zoom_view_start = 0
            self.zoom_view_count = 0
            self.zoom_auto_follow = True
        else:
            center_idx = self.zoom_view_start + int(old_count * center_frac)
            new_start = center_idx - int(new_count * center_frac)
            new_start = max(0, min(new_start, max_len - new_count))
            self.zoom_view_start = new_start
            self.zoom_view_count = new_count
            # Disable auto-follow when manually zooming
            self.zoom_auto_follow = (new_start + new_count >= max_len)

        self.draw_chart()

    def _on_chart_scroll(self, event):
        canvas = self.chart_canvas
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 1)
        frac = event.x / w
        if event.delta > 0:
            self._apply_zoom(0.7, frac)
        else:
            self._apply_zoom(1.4, frac)

    def _on_chart_press(self, event):
        self.zoom_drag_start = event.x

    def _on_chart_drag(self, event):
        if self.zoom_drag_start is None:
            return
        if self.zoom_view_count <= 0:
            return

        canvas = self.chart_canvas
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 1)

        dx = self.zoom_drag_start - event.x
        self.zoom_drag_start = event.x

        # Find max_len
        max_len = 0
        for key, _, _ in getattr(cfg, 'PLOT_KEYS', []):
            if self.plot_enabled_vars.get(key, tk.BooleanVar(value=False)).get():
                max_len = max(max_len, len(self.plot_series.get(key, [])))
        if max_len < 2:
            return

        points_per_pixel = self.zoom_view_count / w
        shift = int(dx * points_per_pixel)
        if shift == 0:
            return

        new_start = self.zoom_view_start + shift
        new_start = max(0, min(new_start, max_len - self.zoom_view_count))
        self.zoom_view_start = new_start
        self.zoom_auto_follow = (new_start + self.zoom_view_count >= max_len)
        self.draw_chart()

    def _on_chart_release(self, event):
        self.zoom_drag_start = None

    def _on_chart_hover(self, event):
        self.chart_crosshair_x = event.x
        if not self._hover_pending:
            self._hover_pending = True
            self.root.after(30, self._flush_hover)

    def _on_chart_leave(self, event):
        self.chart_crosshair_x = None
        self._hover_pending = False
        self.draw_chart()

    def _flush_hover(self):
        self._hover_pending = False
        if self.chart_crosshair_x is not None:
            self.draw_chart()

    # ======================================================================
    # Minimap
    # ======================================================================
    def _draw_minimap(self):
        canvas = self.minimap_canvas
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 60)
        h = max(canvas.winfo_height(), 20)
        canvas.delete('all')
        canvas.create_rectangle(0, 0, w, h, fill='#0b0f1a', outline='')

        plot_keys = getattr(cfg, 'PLOT_KEYS', [])
        visible_keys = [k for k, _, _ in plot_keys if self.plot_enabled_vars.get(k, tk.BooleanVar(value=False)).get()]

        # Find max_len
        max_len = 0
        all_vals = []
        for key in visible_keys:
            series = self.plot_series.get(key, [])
            max_len = max(max_len, len(series))
            all_vals.extend(series)

        if max_len < 2 or not all_vals:
            return

        mn = min(all_vals)
        mx = max(all_vals)
        span = mx - mn
        if span == 0:
            span = 1

        color_map = {k: c for k, c, _ in plot_keys}
        pad = 2
        for key in visible_keys:
            color = color_map.get(key, '#ffffff')
            series = self.plot_series.get(key, [])
            if len(series) < 2:
                continue
            points = []
            for i, v in enumerate(series):
                x = pad + (w - 2 * pad) * i / max(len(series) - 1, 1)
                y = pad + (h - 2 * pad) * (1 - (v - mn) / span)
                points.extend((x, y))
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=1, smooth=True)

        # Viewport rectangle when zoomed
        if self.zoom_view_count > 0 and max_len > 0:
            vp_x1 = pad + (w - 2 * pad) * self.zoom_view_start / max_len
            vp_x2 = pad + (w - 2 * pad) * min(self.zoom_view_start + self.zoom_view_count, max_len) / max_len
            canvas.create_rectangle(vp_x1, 1, vp_x2, h - 1,
                                     outline=THEME['accent_light'], width=1, fill='', dash=(2, 2))

    def _on_minimap_press(self, event):
        self._minimap_drag_x = event.x
        self._on_minimap_drag(event)

    def _on_minimap_drag(self, event):
        canvas = self.minimap_canvas
        canvas.update_idletasks()
        w = max(canvas.winfo_width(), 1)

        max_len = 0
        for key, _, _ in getattr(cfg, 'PLOT_KEYS', []):
            if self.plot_enabled_vars.get(key, tk.BooleanVar(value=False)).get():
                max_len = max(max_len, len(self.plot_series.get(key, [])))
        if max_len < 2 or self.zoom_view_count <= 0:
            return

        frac = event.x / w
        center_idx = int(frac * max_len)
        new_start = center_idx - self.zoom_view_count // 2
        new_start = max(0, min(new_start, max_len - self.zoom_view_count))
        self.zoom_view_start = new_start
        self.zoom_auto_follow = (new_start + self.zoom_view_count >= max_len)
        self.draw_chart()

    # ======================================================================
    # Protocol Parsing
    # ======================================================================
    def parse_telemetry_text(self, text: str):
        parsed = {}
        if not any(text.startswith(p) for p in self._ALL_PREFIXES):
            return parsed
        packet_type = text.split(',', 1)[0].strip()
        parsed['_packet'] = packet_type
        for part in text.split(',')[1:]:
            if '=' not in part:
                continue
            key, value = part.split('=', 1)
            parsed[key.strip()] = try_parse_number(value.strip())
        return parsed

    def merge_telemetry_packet(self, parsed: dict):
        with self.telemetry_lock:
            ptype = parsed.get('_packet', '')
            if self._KEY_MAP and ptype in getattr(cfg, 'TELEMETRY_PREFIXES', ()):
                expanded = {}
                for k, v in parsed.items():
                    expanded[self._KEY_MAP.get(k, k)] = v
                self.latest_parsed.update(expanded)
            else:
                self.latest_parsed.update(parsed)

    def _handle_special_packet(self, ptype: str, parsed: dict):
        with self.telemetry_lock:
            if ptype in self.custom_state:
                self.custom_state[ptype] = dict(parsed)
                self.custom_state[ptype]['_ts'] = time.time()
            if ptype in ('ACK', 'ERR'):
                self.latest_command_result = {
                    'cmd': parsed.get('cmd', ''),
                    'ack': ptype,
                    'data': dict(parsed),
                    'ts': time.time(),
                }
        self.queue_log('custom_ui', None)

    # ======================================================================
    # Queue & Event Loop
    # ======================================================================
    def queue_log(self, kind, data):
        try: self.log_queue.put_nowait((kind, data))
        except queue.Full: pass

    def process_queue(self):
        MAX_PER_CYCLE = 30
        interval = 100
        try:
            log_lines = []
            need_chart = False
            need_custom_ui = False
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
                        need_chart = True
                elif kind == 'status':
                    self.refresh_connection_summary()
                elif kind == 'connections':
                    self.refresh_connection_list()
                elif kind == 'custom_ui':
                    need_custom_ui = True

            if log_lines:
                self._flush_log(log_lines)
            if need_chart:
                try: self._update_ui_from_latest()
                except Exception: pass
                try: self.draw_chart()
                except Exception: pass
            if need_custom_ui:
                try: self._refresh_custom_panels()
                except Exception: pass

            if count >= MAX_PER_CYCLE:
                interval = 50
        except Exception:
            pass
        finally:
            self.root.after(interval, self.process_queue)

    def _flush_log(self, lines):
        self.log_text.config(state='normal')
        for line in lines:
            self.log_text.insert('end', line + '\n')
        total = int(self.log_text.index('end-1c').split('.')[0])
        if total > 3000:
            self.log_text.delete('1.0', f'{total - 3000}.0')
        self.log_text.see('end')
        self.log_text.config(state='disabled')

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

        # Auto-follow logic for zoom
        if self.zoom_auto_follow and self.zoom_view_count > 0:
            max_len = max((len(self.plot_series.get(k, []))
                           for k, _, _ in getattr(cfg, 'PLOT_KEYS', [])), default=0)
            if max_len > self.zoom_view_count:
                self.zoom_view_start = max_len - self.zoom_view_count

        # Async file writes
        _file_writer.write(LATEST_TEXT_PATH, text)
        _file_writer.write(LATEST_JSON_PATH, json.dumps(entry, ensure_ascii=False, default=str))
        _file_writer.append(HISTORY_JSONL_PATH, json.dumps(entry, ensure_ascii=False, default=str) + '\n')

    def _update_ui_from_latest(self):
        with self.telemetry_lock:
            parsed = dict(self.latest_parsed)
        self.update_metric_cards(parsed)
        self.update_status_banner(parsed)
        self._update_sparklines(parsed)

    def _refresh_custom_panels(self):
        with self.telemetry_lock:
            state_copy = {k: dict(v) for k, v in self.custom_state.items()}
            cmd_result = dict(self.latest_command_result)

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

        # Write custom state file
        self._write_custom_state_file(state_copy, cmd_result)

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
    # Chart
    # ======================================================================
    def get_plot_scale_bounds(self, all_values):
        mode = self.plot_scale_mode.get()
        if mode == 'fixed':
            try:
                mn = float(self.plot_fixed_min.get())
                mx = float(self.plot_fixed_max.get())
                if mn >= mx: return None, None, mode
                return mn, mx, mode
            except ValueError:
                return None, None, mode

        if not all_values:
            return -1, 1, mode
        mn, mx = min(all_values), max(all_values)
        margin = max((mx - mn) * 0.1, 0.5)
        return mn - margin, mx + margin, mode

    def smooth_values(self, values):
        if len(values) < 5:
            return values
        result = list(values)
        for i in range(2, len(values) - 2):
            result[i] = sum(values[i-2:i+3]) / 5.0
        return result

    def draw_chart(self):
        canvas = self.chart_canvas
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 240)
        height = max(canvas.winfo_height(), 260)
        left_pad, right_pad, top_pad, bottom_pad = 56, 18, 16, 30
        plot_w = max(width - left_pad - right_pad, 10)
        plot_h = max(height - top_pad - bottom_pad, 10)

        canvas.delete('all')
        canvas.create_rectangle(0, 0, width, height, fill='#0f172a', outline='#0f172a')
        canvas.create_rectangle(left_pad, top_pad, left_pad + plot_w, top_pad + plot_h, outline='#334155', width=1)

        plot_keys = getattr(cfg, 'PLOT_KEYS', [])
        visible_keys = [k for k, _, _ in plot_keys if self.plot_enabled_vars.get(k, tk.BooleanVar(value=False)).get()]

        # Determine zoom viewport
        max_series_len = 0
        for key in visible_keys:
            max_series_len = max(max_series_len, len(self.plot_series.get(key, [])))

        view_start = 0
        view_count = max_series_len
        if self.zoom_view_count > 0 and self.zoom_view_count < max_series_len:
            view_start = self.zoom_view_start
            view_count = self.zoom_view_count

        all_values = []
        for key in visible_keys:
            series = self.plot_series.get(key, [])
            sliced = series[view_start:view_start + view_count]
            all_values.extend(sliced)

        if not visible_keys:
            canvas.create_text(width / 2, height / 2, text='No channels enabled', fill='#94a3b8', font=('Segoe UI', 12))
            self._draw_minimap()
            return
        if not all_values:
            canvas.create_text(width / 2, height / 2, text='No telemetry yet', fill='#94a3b8', font=('Segoe UI', 12))
            self._draw_minimap()
            return

        min_v, max_v, scale_mode = self.get_plot_scale_bounds(all_values)
        if min_v is None or max_v is None:
            canvas.create_text(width / 2, height / 2, text='Invalid fixed scale', fill='#fca5a5', font=('Segoe UI', 12))
            self._draw_minimap()
            return
        span = max_v - min_v
        if span == 0: span = 1

        # Grid lines
        for i in range(6):
            y = top_pad + plot_h * i / 5
            value = max_v - span * i / 5
            canvas.create_line(left_pad, y, left_pad + plot_w, y, fill='#1e293b')
            canvas.create_text(left_pad - 8, y, text=f'{value:.1f}', fill='#94a3b8', anchor='e', font=('Consolas', 9))

        # Dashed center gridline
        center_y = top_pad + plot_h / 2
        canvas.create_line(left_pad, center_y, left_pad + plot_w, center_y,
                           fill='#334155', dash=(4, 4))

        color_map = {k: c for k, c, _ in plot_keys}
        for key in visible_keys:
            color = color_map.get(key, '#ffffff')
            raw_series = self.plot_series.get(key, [])
            raw_values = raw_series[view_start:view_start + view_count]
            if len(raw_values) < 2:
                continue
            values = self.smooth_values(raw_values) if self.plot_smooth_var.get() else raw_values

            points = []
            for idx, v in enumerate(values):
                x = left_pad + plot_w * idx / max(len(values) - 1, 1)
                y = top_pad + (max_v - v) / span * plot_h
                points.extend((x, y))

            # Gradient fill with stipple
            if self.plot_fill_var.get() and len(points) >= 4:
                polygon = [points[0], top_pad + plot_h] + points + [points[-2], top_pad + plot_h]
                canvas.create_polygon(*polygon, fill=color, stipple='gray25', outline='')

            # Glow effect: wider faint line behind
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=6, smooth=True,
                                   splinesteps=32, stipple='gray50')

            # Main Bezier curve
            canvas.create_line(*points, fill=color, width=3, smooth=True, splinesteps=32)

            # Endpoint dot with glow
            if len(points) >= 2:
                ex, ey = points[-2], points[-1]
                # Glow
                canvas.create_oval(ex - 7, ey - 7, ex + 7, ey + 7,
                                   fill=color, outline='', stipple='gray50')
                # Solid dot
                canvas.create_oval(ex - 4, ey - 4, ex + 4, ey + 4,
                                   fill=color, outline='')

        # Hover crosshair
        if self.chart_crosshair_x is not None:
            cx = self.chart_crosshair_x
            if left_pad <= cx <= left_pad + plot_w:
                canvas.create_line(cx, top_pad, cx, top_pad + plot_h,
                                   fill='#475569', dash=(3, 3), width=1)

        summary = [f'scale={scale_mode}[{min_v:.1f},{max_v:.1f}]']
        if self.zoom_view_count > 0:
            summary.append(f'zoom=[{view_start}:{view_start + view_count}]')
        for k, c, _ in plot_keys:
            vals = self.plot_series.get(k, [])
            if vals:
                marker = '\u25cf' if k in visible_keys else '\u25cb'
                summary.append(f'{marker} {k}={vals[-1]:.2f}')
        self.chart_summary_var.set(' | '.join(summary[:12]))

        # Draw minimap
        self._draw_minimap()

    # ======================================================================
    # HTTP API Server
    # ======================================================================
    def start_http_server(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
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
                except FileNotFoundError:
                    self._json_response({'error': 'dashboard.html not found'}, 404)

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def do_GET(self):
                path = self.path.split('?')[0].rstrip('/')
                if path == '' or path == '/dashboard':
                    dashboard = Path(__file__).resolve().parent / 'dashboard.html'
                    self._serve_file(str(dashboard), 'text/html; charset=utf-8')
                elif path == '/latest':
                    with app.telemetry_lock:
                        payload = dict(app.latest_parsed)
                        payload['custom_state'] = {k: dict(v) for k, v in app.custom_state.items()}
                        payload['cmd_result'] = dict(app.latest_command_result)
                    self._json_response(payload)
                elif path == '/snapshot':
                    with app.telemetry_lock:
                        payload = {
                            'latest': dict(app.latest_parsed),
                            'custom_state': {k: dict(v) for k, v in app.custom_state.items()},
                            'cmd_result': dict(app.latest_command_result),
                            'connection': {'running': app.running, 'mode': app.mode_var.get()},
                            '_ts': time.time(),
                        }
                    self._json_response(payload)
                elif path == '/history':
                    with app.telemetry_lock:
                        history = list(app.telemetry_history[-100:])
                    self._json_response(history)
                elif path == '/status':
                    self._json_response({
                        'running': app.running,
                        'mode': app.mode_var.get(),
                        'host': app.host_var.get(),
                        'port': app.port_var.get(),
                        'connections': len(app.server_connections),
                    })
                else:
                    self._json_response({'error': 'not found'}, 404)

            def do_POST(self):
                path = self.path.split('?')[0].rstrip('/')
                if path == '/command':
                    length = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    command = str(body.get('command', '')).strip()
                    if command:
                        app.pending_command = command
                        app.pending_command_time = time.time()
                        app.send_command(command)
                        self._json_response({'status': 'sent', 'command': command})
                    else:
                        self._json_response({'error': 'empty command'}, 400)
                else:
                    self._json_response({'error': 'not found'}, 404)

        host = getattr(cfg, 'HTTP_HOST', '127.0.0.1')
        port = getattr(cfg, 'HTTP_PORT', 9898)
        try:
            self.http_server = ThreadingHTTPServer((host, port), Handler)
            t = threading.Thread(target=self.http_server.serve_forever, daemon=True, name='http')
            t.start()
        except Exception:
            self.http_server = None

    # ======================================================================
    # TCP Server / Client
    # ======================================================================
    def start(self):
        if self.running: return
        self.running = True
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')

        if self.mode_var.get() == 'server':
            self.server_thread = threading.Thread(target=self.run_server, daemon=True)
            self.server_thread.start()
        else:
            self.client_thread = threading.Thread(target=self.run_client, daemon=True)
            self.client_thread.start()
        self.queue_log('status', None)

    def stop(self):
        self.running = False
        self.close_all_sockets()
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        self.queue_log('status', None)

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
                    t = threading.Thread(target=self.handle_client, args=(cid, client_sock, addr), daemon=True)
                    t.start()
                except socket.timeout:
                    continue
        except Exception as e:
            self.queue_log('log', f'[Server] Error: {e}')
        finally:
            self.queue_log('status', None)

    def handle_client(self, cid, sock, addr):
        recv_buf = ''
        try:
            sock.settimeout(1.0)
            while self.running:
                try:
                    data = sock.recv(4096)
                    if not data:
                        break
                    recv_buf += data.decode(ENCODING, errors='replace')
                    recv_buf = self._consume_stream_buffer(f'client#{cid}', recv_buf)
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            sock.close()
            self.server_connections.pop(cid, None)
            self.queue_log('log', f'[Server] Client #{cid} disconnected')
            self.queue_log('connections', None)

    def run_client(self):
        host = self.host_var.get()
        port = int(self.port_var.get())
        recv_buf = ''
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5.0)
            self.client_socket.connect((host, port))
            self.client_socket.settimeout(1.0)
            self.queue_log('log', f'[Client] Connected to {host}:{port}')
            self.queue_log('status', None)

            while self.running:
                try:
                    data = self.client_socket.recv(4096)
                    if not data:
                        break
                    recv_buf += data.decode(ENCODING, errors='replace')
                    recv_buf = self._consume_stream_buffer('server', recv_buf)
                except socket.timeout:
                    continue
                except Exception:
                    break
        except Exception as e:
            self.queue_log('log', f'[Client] Error: {e}')
        finally:
            if self.client_socket:
                self.client_socket.close()
                self.client_socket = None
            self.queue_log('status', None)

    def _consume_stream_buffer(self, source, buf):
        while '\n' in buf:
            line, buf = buf.split('\n', 1)
            line = line.rstrip('\r').strip()
            if not line:
                continue
            self._dispatch_line(source, line)
        # Handle very long buffer without newline
        if len(buf) > 4096:
            buf = ''
        return buf

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

        if self.mode_var.get() == 'server':
            for cid, (sock, _) in list(self.server_connections.items()):
                try: sock.sendall(data.encode(ENCODING))
                except: pass
        elif self.client_socket:
            try: self.client_socket.sendall(data.encode(ENCODING))
            except: pass

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
        if not self.running:
            self.conn_badge.config(text='Stopped', fg=THEME['text_muted'])
            self.conn_summary.config(text='Stopped', fg=THEME['text_muted'])
            self.conn_dot.itemconfig('dot', fill=THEME['text_dim'])
        elif self.mode_var.get() == 'server':
            n = len(self.server_connections)
            self.conn_badge.config(text=f'Server ({n} clients)', fg=THEME['ok'])
            self.conn_summary.config(text=f'Server running ({n} clients)', fg=THEME['ok'])
            self.conn_dot.itemconfig('dot', fill=THEME['ok'])
        else:
            connected = self.client_socket is not None
            self.conn_badge.config(
                text='Connected' if connected else 'Connecting...',
                fg=THEME['ok'] if connected else THEME['warn'])
            self.conn_summary.config(
                text='Connected' if connected else 'Connecting...',
                fg=THEME['ok'] if connected else THEME['warn'])
            self.conn_dot.itemconfig('dot', fill=THEME['ok'] if connected else THEME['warn'])

    def refresh_connection_list(self):
        self.conn_listbox.delete(0, 'end')
        for cid, (_, addr) in self.server_connections.items():
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
            time.sleep(0.1)

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
    def on_close(self):
        self.running = False
        self.sim_running = False
        self.close_all_sockets()
        if self.http_server:
            try: self.http_server.shutdown()
            except: pass
        self.root.destroy()


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == '__main__':
    root = ttkb.Window(
        title=getattr(cfg, 'APP_TITLE', 'SmartCar Tuning Tool'),
        themename='darkly',
        size=(1440, 850),
    )
    app = TuningToolApp(root)
    root.mainloop()
