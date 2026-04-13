#!/usr/bin/env python3
"""
SmartCar Tuning Tool - Config-driven desktop tuning GUI.

All domain-specific knowledge lives in config.json (or tuning_config.py for dev).
This file is the generic framework: TCP, HTTP, minimal connection panel.
Data visualization is handled by the web dashboard (dashboard.html).
You should NOT need to edit this file for normal use.
"""

import json
import queue
import socket
import threading
import time
import tkinter as tk
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as ttkb
from ttkbootstrap.constants import *

# ---------------------------------------------------------------------------
# Load user config
# ---------------------------------------------------------------------------
from config_loader import cfg, resource_path, app_dir

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
DATA_DIR = app_dir() / 'runtime'
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

        # Plot state (still needed for HTTP API)
        self.plot_series = {k: [] for k, _, _ in getattr(cfg, 'PLOT_KEYS', [])}
        self.plot_enabled_vars = {}
        for k, _, default_on in getattr(cfg, 'PLOT_KEYS', []):
            self.plot_enabled_vars[k] = tk.BooleanVar(value=default_on)

        self._build_ui()
        self.start_http_server()
        self.root.after(100, self.process_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ======================================================================
    # UI Construction - Minimal Connection Panel
    # ======================================================================
    def _build_ui(self):
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
                    try: self._refresh_custom_panels()
                    except Exception: pass

            if log_lines:
                self._flush_log(log_lines)

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
                    dashboard = resource_path('dashboard.html')
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
            if getattr(cfg, 'AUTO_OPEN_BROWSER', False):
                self.root.after(800, lambda: webbrowser.open(f'http://{host}:{port}'))
        except OSError as e:
            self.http_server = None
            self.queue_log('log', f'[HTTP] Port {port} unavailable: {e}. Dashboard disabled.')
            messagebox.showwarning(
                'Dashboard Unavailable',
                f'Could not start HTTP server on port {port}.\n'
                f'Another program may be using it.\n\n'
                f'Change "http_port" in config.json and restart.'
            )

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
        size=(400, 680),
    )
    app = TuningToolApp(root)
    root.mainloop()
