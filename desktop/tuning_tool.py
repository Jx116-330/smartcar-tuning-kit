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
MAX_PLOT_POINTS = 120

CARD_THEME = {
    'bg': '#111827', 'panel': '#0f172a', 'border': '#1f2937',
    'title': '#94a3b8', 'value': '#f8fafc', 'muted': '#cbd5e1',
    'accent': '#38bdf8', 'ok': '#22c55e', 'warn': '#f59e0b', 'danger': '#ef4444',
}

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

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(getattr(cfg, 'APP_TITLE', 'SmartCar Tuning Tool'))
        root.geometry('1440x850')
        root.configure(bg=CARD_THEME['bg'])

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

        self._build_ui()
        self.start_http_server()
        self.root.after(100, self.process_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ======================================================================
    # UI Construction
    # ======================================================================
    def _build_ui(self):
        f = tk.Frame(self.root, bg=CARD_THEME['bg'])
        f.pack(fill='both', expand=True)

        main_pw = tk.PanedWindow(f, orient='vertical', bg=CARD_THEME['bg'],
                                  sashwidth=4, sashrelief='flat')
        main_pw.pack(fill='both', expand=True)

        workarea = tk.PanedWindow(main_pw, orient='horizontal', bg=CARD_THEME['bg'],
                                   sashwidth=4, sashrelief='flat')
        main_pw.add(workarea, stretch='always')

        sidebar = self._build_sidebar(workarea)
        center = self._build_center(workarea)
        right = self._build_right_panel(workarea)

        workarea.add(sidebar, width=220, minsize=180, stretch='never')
        workarea.add(center, width=580, minsize=300, stretch='always')
        workarea.add(right, width=350, minsize=250, stretch='always')

        console = self._build_console(main_pw)
        main_pw.add(console, height=100, minsize=60, stretch='never')

    # --- Sidebar ---
    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=CARD_THEME['panel'])

        # Connection
        conn_frame = tk.LabelFrame(sb, text='Connection', bg=CARD_THEME['panel'],
                                    fg=CARD_THEME['title'], font=('Segoe UI', 10, 'bold'))
        conn_frame.pack(fill='x', padx=6, pady=4)

        mode_frame = tk.Frame(conn_frame, bg=CARD_THEME['panel'])
        mode_frame.pack(fill='x', padx=4, pady=2)
        tk.Radiobutton(mode_frame, text='Server', variable=self.mode_var, value='server',
                        bg=CARD_THEME['panel'], fg=CARD_THEME['value'], selectcolor=CARD_THEME['bg'],
                        command=self.on_mode_change).pack(side='left')
        tk.Radiobutton(mode_frame, text='Client', variable=self.mode_var, value='client',
                        bg=CARD_THEME['panel'], fg=CARD_THEME['value'], selectcolor=CARD_THEME['bg'],
                        command=self.on_mode_change).pack(side='left')

        for label, var in [('Host:', self.host_var), ('Port:', self.port_var)]:
            row = tk.Frame(conn_frame, bg=CARD_THEME['panel'])
            row.pack(fill='x', padx=4, pady=1)
            tk.Label(row, text=label, bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                     width=5, anchor='e').pack(side='left')
            tk.Entry(row, textvariable=var, bg=CARD_THEME['bg'], fg=CARD_THEME['value'],
                     insertbackground=CARD_THEME['value'], width=14).pack(side='left', padx=2)

        btn_frame = tk.Frame(conn_frame, bg=CARD_THEME['panel'])
        btn_frame.pack(fill='x', padx=4, pady=4)
        self.start_btn = tk.Button(btn_frame, text='Start', command=self.start,
                                    bg=CARD_THEME['ok'], fg='white', width=8)
        self.start_btn.pack(side='left', padx=2)
        self.stop_btn = tk.Button(btn_frame, text='Stop', command=self.stop,
                                   bg=CARD_THEME['danger'], fg='white', width=8, state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        self.conn_summary = tk.Label(conn_frame, text='Stopped', bg=CARD_THEME['panel'],
                                      fg=CARD_THEME['muted'], font=('Segoe UI', 9))
        self.conn_summary.pack(fill='x', padx=4)

        # Connection list
        self.conn_listbox = tk.Listbox(sb, bg=CARD_THEME['bg'], fg=CARD_THEME['value'],
                                        height=3, font=('Consolas', 9))
        self.conn_listbox.pack(fill='x', padx=6, pady=4)

        # Quick commands
        qc_list = getattr(cfg, 'QUICK_COMMANDS', [])
        if qc_list:
            qc_frame = tk.LabelFrame(sb, text='Quick Commands', bg=CARD_THEME['panel'],
                                      fg=CARD_THEME['title'], font=('Segoe UI', 10, 'bold'))
            qc_frame.pack(fill='x', padx=6, pady=4)
            for qc in qc_list:
                cmd = qc['command']
                tk.Button(qc_frame, text=qc['label'],
                          command=lambda c=cmd: self.send_quick(c),
                          bg=CARD_THEME['border'], fg=CARD_THEME['value']).pack(fill='x', padx=4, pady=1)

        # Simulation
        if hasattr(cfg, 'build_simulated_packet') and cfg.build_simulated_packet is not None:
            sim_frame = tk.LabelFrame(sb, text='Simulation', bg=CARD_THEME['panel'],
                                       fg=CARD_THEME['title'], font=('Segoe UI', 10, 'bold'))
            sim_frame.pack(fill='x', padx=6, pady=4)
            tk.Button(sim_frame, text='Start Sim', command=self.start_simulation,
                      bg=CARD_THEME['accent'], fg='white').pack(fill='x', padx=4, pady=1)
            tk.Button(sim_frame, text='Stop Sim', command=self.stop_simulation,
                      bg=CARD_THEME['border'], fg=CARD_THEME['value']).pack(fill='x', padx=4, pady=1)

        # Send box
        send_frame = tk.LabelFrame(sb, text='Send', bg=CARD_THEME['panel'],
                                    fg=CARD_THEME['title'], font=('Segoe UI', 10, 'bold'))
        send_frame.pack(fill='x', padx=6, pady=4, side='bottom')
        self.send_entry = tk.Entry(send_frame, bg=CARD_THEME['bg'], fg=CARD_THEME['value'],
                                    insertbackground=CARD_THEME['value'])
        self.send_entry.pack(fill='x', padx=4, pady=2)
        self.send_entry.bind('<Return>', lambda e: self.send_data())
        tk.Checkbutton(send_frame, text='Append CRLF', variable=self.crlf_var,
                        bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                        selectcolor=CARD_THEME['bg']).pack(padx=4, anchor='w')
        tk.Button(send_frame, text='Send', command=self.send_data,
                  bg=CARD_THEME['accent'], fg='white').pack(fill='x', padx=4, pady=2)

        return sb

    # --- Center: Plot ---
    def _build_center(self, parent):
        center = tk.Frame(parent, bg=CARD_THEME['bg'])

        # Chart
        chart_frame = tk.Frame(center, bg=CARD_THEME['panel'])
        chart_frame.pack(fill='both', expand=True, padx=4, pady=4)

        self.chart_canvas = tk.Canvas(chart_frame, bg='#0f172a', highlightthickness=0)
        self.chart_canvas.pack(fill='both', expand=True)

        # Chart controls
        ctrl = tk.Frame(chart_frame, bg=CARD_THEME['panel'])
        ctrl.pack(fill='x', padx=4, pady=2)

        for key, color, _ in getattr(cfg, 'PLOT_KEYS', []):
            cb = tk.Checkbutton(ctrl, text=key, variable=self.plot_enabled_vars[key],
                                 bg=CARD_THEME['panel'], fg=color, selectcolor=CARD_THEME['bg'],
                                 font=('Consolas', 9), command=self.draw_chart)
            cb.pack(side='left', padx=2)

        tk.Checkbutton(ctrl, text='Smooth', variable=self.plot_smooth_var,
                        bg=CARD_THEME['panel'], fg=CARD_THEME['title'], selectcolor=CARD_THEME['bg'],
                        command=self.draw_chart).pack(side='right', padx=2)
        tk.Checkbutton(ctrl, text='Fill', variable=self.plot_fill_var,
                        bg=CARD_THEME['panel'], fg=CARD_THEME['title'], selectcolor=CARD_THEME['bg'],
                        command=self.draw_chart).pack(side='right', padx=2)

        # Scale controls
        scale_frame = tk.Frame(chart_frame, bg=CARD_THEME['panel'])
        scale_frame.pack(fill='x', padx=4, pady=1)
        tk.Label(scale_frame, text='Scale:', bg=CARD_THEME['panel'], fg=CARD_THEME['title']).pack(side='left')
        ttk.Combobox(scale_frame, textvariable=self.plot_scale_mode, values=['auto', 'fixed'],
                     width=6, state='readonly').pack(side='left', padx=2)
        tk.Label(scale_frame, text='Min:', bg=CARD_THEME['panel'], fg=CARD_THEME['title']).pack(side='left')
        tk.Entry(scale_frame, textvariable=self.plot_fixed_min, width=6, bg=CARD_THEME['bg'],
                 fg=CARD_THEME['value'], insertbackground=CARD_THEME['value']).pack(side='left', padx=2)
        tk.Label(scale_frame, text='Max:', bg=CARD_THEME['panel'], fg=CARD_THEME['title']).pack(side='left')
        tk.Entry(scale_frame, textvariable=self.plot_fixed_max, width=6, bg=CARD_THEME['bg'],
                 fg=CARD_THEME['value'], insertbackground=CARD_THEME['value']).pack(side='left', padx=2)

        # Summary
        tk.Label(chart_frame, textvariable=self.chart_summary_var, bg=CARD_THEME['panel'],
                 fg=CARD_THEME['muted'], font=('Consolas', 9), anchor='w').pack(fill='x', padx=4)

        return center

    # --- Right Panel: Metric tabs + custom tabs ---
    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=CARD_THEME['bg'])

        # Status banner
        banner = tk.Frame(right, bg=CARD_THEME['panel'])
        banner.pack(fill='x', padx=4, pady=2)
        tk.Label(banner, textvariable=self.metric_vars['fix_state'], bg=CARD_THEME['panel'],
                 fg=CARD_THEME['accent'], font=('Segoe UI', 10, 'bold')).pack(side='left', padx=4)
        tk.Label(banner, textvariable=self.metric_vars['health_state'], bg=CARD_THEME['panel'],
                 fg=CARD_THEME['ok'], font=('Segoe UI', 10)).pack(side='right', padx=4)

        # Notebook
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

        return right

    def _build_tab_telemetry(self, notebook):
        tab = tk.Frame(notebook, bg=CARD_THEME['panel'])
        notebook.add(tab, text='Telemetry')

        # Primary metric cards
        cards_frame = tk.Frame(tab, bg=CARD_THEME['panel'])
        cards_frame.pack(fill='x', padx=4, pady=4)
        for i, (key, label) in enumerate(getattr(cfg, 'PRIMARY_METRICS', [])):
            self._build_metric_card(cards_frame, key, label, i)

        # Detail metrics
        det_frame = tk.LabelFrame(tab, text='Details', bg=CARD_THEME['panel'],
                                   fg=CARD_THEME['title'], font=('Segoe UI', 9, 'bold'))
        det_frame.pack(fill='x', padx=4, pady=2)
        for i, (key, label) in enumerate(getattr(cfg, 'DETAIL_METRICS', [])):
            r, c = divmod(i, 2)
            tk.Label(det_frame, text=label, bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                     font=('Consolas', 9), width=10, anchor='e').grid(row=r, column=c*2, padx=2, pady=1)
            tk.Label(det_frame, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=CARD_THEME['panel'], fg=CARD_THEME['value'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=2, pady=1, sticky='w')

        # Extended metrics (scrollable)
        ext_frame = tk.LabelFrame(tab, text='Extended', bg=CARD_THEME['panel'],
                                   fg=CARD_THEME['title'], font=('Segoe UI', 9, 'bold'))
        ext_frame.pack(fill='both', expand=True, padx=4, pady=2)

        canvas = tk.Canvas(ext_frame, bg=CARD_THEME['panel'], highlightthickness=0)
        scrollbar = tk.Scrollbar(ext_frame, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD_THEME['panel'])
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-1 * (e.delta // 120), 'units'))

        for i, (key, label) in enumerate(getattr(cfg, 'EXTENDED_METRICS', [])):
            r, c = divmod(i, 2)
            tk.Label(inner, text=label, bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                     font=('Consolas', 9), width=10, anchor='e').grid(row=r, column=c*2, padx=2, pady=1)
            tk.Label(inner, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=CARD_THEME['panel'], fg=CARD_THEME['value'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=2, pady=1, sticky='w')

    def _build_custom_status_tab(self, notebook, tab_def):
        name = tab_def.get('name', 'Custom')
        fields = tab_def.get('fields', [])
        result_keys = tab_def.get('result_keys', [])

        tab = tk.Frame(notebook, bg=CARD_THEME['panel'])
        notebook.add(tab, text=name)

        for i, (key, label) in enumerate(fields):
            r, c = divmod(i, 2)
            tk.Label(tab, text=label, bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                     font=('Consolas', 9), width=12, anchor='e').grid(row=r, column=c*2, padx=2, pady=1)
            tk.Label(tab, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                     bg=CARD_THEME['panel'], fg=CARD_THEME['value'],
                     font=('Consolas', 10), anchor='w').grid(row=r, column=c*2+1, padx=2, pady=1, sticky='w')

        if result_keys:
            res_frame = tk.LabelFrame(tab, text='Last Results', bg=CARD_THEME['panel'],
                                       fg=CARD_THEME['title'], font=('Segoe UI', 9, 'bold'))
            base_row = (len(fields) + 1) // 2 + 1
            res_frame.grid(row=base_row, column=0, columnspan=4, sticky='ew', padx=4, pady=4)
            for i, (tag, var_key) in enumerate(result_keys):
                tk.Label(res_frame, text=f'{tag}:', bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                         font=('Consolas', 9)).grid(row=i, column=0, padx=2, sticky='e')
                tk.Label(res_frame, textvariable=self.metric_vars.get(var_key, tk.StringVar(value='--')),
                         bg=CARD_THEME['panel'], fg=CARD_THEME['value'],
                         font=('Consolas', 9), wraplength=250, anchor='w').grid(row=i, column=1, padx=2, sticky='w')

    def _build_command_tab(self, notebook, tab_def):
        name = tab_def.get('name', 'Commands')
        buttons = tab_def.get('buttons', [])
        params = tab_def.get('params', [])

        tab = tk.Frame(notebook, bg=CARD_THEME['panel'])
        notebook.add(tab, text=name)

        for btn in buttons:
            cmd = btn['command']
            tk.Button(tab, text=btn['label'],
                      command=lambda c=cmd: self._send_custom_cmd(c),
                      bg=CARD_THEME['border'], fg=CARD_THEME['value'],
                      font=('Segoe UI', 10)).pack(fill='x', padx=8, pady=2)

        for p in params:
            pf = tk.Frame(tab, bg=CARD_THEME['panel'])
            pf.pack(fill='x', padx=8, pady=2)
            tk.Label(pf, text=p['label'], bg=CARD_THEME['panel'], fg=CARD_THEME['title'],
                     font=('Consolas', 9), width=16, anchor='e').pack(side='left')
            var = tk.StringVar(value=p.get('default', ''))
            self._cmd_param_vars[p['prefix']] = var
            entry = tk.Entry(pf, textvariable=var, bg=CARD_THEME['bg'], fg=CARD_THEME['value'],
                             insertbackground=CARD_THEME['value'], width=12)
            entry.pack(side='left', padx=4)
            prefix = p['prefix']
            tk.Button(pf, text='Set',
                      command=lambda pr=prefix, v=var: self._send_custom_cmd(f'{pr} {v.get()}'),
                      bg=CARD_THEME['accent'], fg='white', width=4).pack(side='left')

    def _build_metric_card(self, parent, key, label, index):
        card = tk.Frame(parent, bg=CARD_THEME['bg'], highlightbackground=CARD_THEME['border'],
                         highlightthickness=1, padx=6, pady=4)
        card.grid(row=0, column=index, padx=3, pady=3, sticky='nsew')
        parent.columnconfigure(index, weight=1)

        accent = tk.Frame(card, bg=CARD_THEME['accent'], height=3)
        accent.pack(fill='x')
        tk.Label(card, text=label, bg=CARD_THEME['bg'], fg=CARD_THEME['title'],
                 font=('Segoe UI', 9)).pack(anchor='w')
        tk.Label(card, textvariable=self.metric_vars.get(key, tk.StringVar(value='--')),
                 bg=CARD_THEME['bg'], fg=CARD_THEME['value'],
                 font=('Consolas', 16, 'bold')).pack(anchor='w')

    # --- Console ---
    def _build_console(self, parent):
        frame = tk.Frame(parent, bg=CARD_THEME['bg'])
        self.log_text = tk.Text(frame, bg='#0a0a0a', fg='#d1d5db', height=5,
                                 font=('Consolas', 9), state='disabled', wrap='none')
        scrollbar = tk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.log_text.pack(fill='both', expand=True)
        return frame

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

        # Async file writes
        _file_writer.write(LATEST_TEXT_PATH, text)
        _file_writer.write(LATEST_JSON_PATH, json.dumps(entry, ensure_ascii=False, default=str))
        _file_writer.append(HISTORY_JSONL_PATH, json.dumps(entry, ensure_ascii=False, default=str) + '\n')

    def _update_ui_from_latest(self):
        with self.telemetry_lock:
            parsed = dict(self.latest_parsed)
        self.update_metric_cards(parsed)
        self.update_status_banner(parsed)

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
        all_values = []
        for key in visible_keys:
            all_values.extend(self.plot_series.get(key, []))

        if not visible_keys:
            canvas.create_text(width / 2, height / 2, text='No channels enabled', fill='#94a3b8', font=('Segoe UI', 12))
            return
        if not all_values:
            canvas.create_text(width / 2, height / 2, text='No telemetry yet', fill='#94a3b8', font=('Segoe UI', 12))
            return

        min_v, max_v, scale_mode = self.get_plot_scale_bounds(all_values)
        if min_v is None or max_v is None:
            canvas.create_text(width / 2, height / 2, text='Invalid fixed scale', fill='#fca5a5', font=('Segoe UI', 12))
            return
        span = max_v - min_v
        if span == 0: span = 1

        for i in range(6):
            y = top_pad + plot_h * i / 5
            value = max_v - span * i / 5
            canvas.create_line(left_pad, y, left_pad + plot_w, y, fill='#1e293b')
            canvas.create_text(left_pad - 8, y, text=f'{value:.1f}', fill='#94a3b8', anchor='e', font=('Consolas', 9))

        color_map = {k: c for k, c, _ in plot_keys}
        for key in visible_keys:
            color = color_map.get(key, '#ffffff')
            raw_values = self.plot_series.get(key, [])
            if len(raw_values) < 2:
                continue
            values = self.smooth_values(raw_values) if self.plot_smooth_var.get() else raw_values
            points = []
            for idx, v in enumerate(values):
                x = left_pad + plot_w * idx / max(len(values) - 1, 1)
                y = top_pad + (max_v - v) / span * plot_h
                points.extend((x, y))
            if self.plot_fill_var.get() and len(points) >= 4:
                polygon = [points[0], top_pad + plot_h] + points + [points[-2], top_pad + plot_h]
                canvas.create_polygon(*polygon, fill=color, stipple='gray25', outline='')
            canvas.create_line(*points, fill=color, width=3, smooth=self.plot_smooth_var.get(), splinesteps=20)
            canvas.create_oval(points[-2] - 4, points[-1] - 4, points[-2] + 4, points[-1] + 4, fill=color, outline='')

        summary = [f'scale={scale_mode}[{min_v:.1f},{max_v:.1f}]']
        for k, c, _ in plot_keys:
            vals = self.plot_series.get(k, [])
            if vals:
                marker = '\u25cf' if k in visible_keys else '\u25cb'
                summary.append(f'{marker} {k}={vals[-1]:.2f}')
        self.chart_summary_var.set(' | '.join(summary[:12]))

    # ======================================================================
    # HTTP API Server
    # ======================================================================
    def start_http_server(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass

            def _json_response(self, data, code=200):
                body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split('?')[0].rstrip('/')
                if path == '/latest':
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
            self.conn_summary.config(text='Stopped', fg=CARD_THEME['muted'])
        elif self.mode_var.get() == 'server':
            n = len(self.server_connections)
            self.conn_summary.config(text=f'Server running ({n} clients)', fg=CARD_THEME['ok'])
        else:
            connected = self.client_socket is not None
            self.conn_summary.config(
                text='Connected' if connected else 'Connecting...',
                fg=CARD_THEME['ok'] if connected else CARD_THEME['warn'])

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
    root = tk.Tk()
    app = TuningToolApp(root)
    root.mainloop()
