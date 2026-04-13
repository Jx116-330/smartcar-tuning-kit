"""
SmartCar Tuning Tool - Config Loader.

Loads UI configuration from config.json (portable/exe mode) or falls back
to the legacy tuning_config.py module (development mode).
Provides resource_path() and app_dir() for PyInstaller compatibility.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Path helpers (PyInstaller compatible)
# ---------------------------------------------------------------------------
def app_dir() -> Path:
    """Directory containing the .exe (frozen) or this script (dev mode).
    Used for: config.json, runtime/ data directory."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    """Absolute path to a *bundled* resource (e.g. dashboard.html).
    In --onefile mode PyInstaller extracts to sys._MEIPASS."""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parent / relative


# ---------------------------------------------------------------------------
# Config class - wraps JSON data with attribute access
# ---------------------------------------------------------------------------
class Config:
    """Configuration namespace loaded from a JSON file.

    Exposes UPPER_CASE attributes so existing ``getattr(cfg, 'PLOT_KEYS', [])``
    patterns in tuning_tool.py continue to work unchanged.
    """

    # Built-in defaults (used when a key is missing from both JSON and legacy)
    _DEFAULTS: dict = {
        'app_title': 'SmartCar Tuning Tool',
        'network': {
            'http_host': '127.0.0.1',
            'http_port': 9898,
        },
        'protocol': {
            'telemetry_prefixes': ['TEL', 'TELG', 'TELA'],
            'response_prefixes': ['ACK', 'ERR'],
            'key_map': {},
        },
        'ui': {
            'plot_channels': [
                {'key': 'gx',   'color': '#4fc3f7', 'visible': True},
                {'key': 'gy',   'color': '#81c784', 'visible': True},
                {'key': 'gz',   'color': '#e57373', 'visible': True},
                {'key': 'gxyz', 'color': '#ba68c8', 'visible': True},
            ],
            'primary_metrics': [
                {'key': 'gx', 'label': 'GX'}, {'key': 'gy', 'label': 'GY'},
                {'key': 'gz', 'label': 'GZ'}, {'key': 'gxyz', 'label': 'GXYZ'},
            ],
            'detail_metrics': [
                {'key': 'roll', 'label': 'ROLL'}, {'key': 'pitch', 'label': 'PITCH'},
                {'key': 'yaw', 'label': 'YAW'}, {'key': 'anorm', 'label': 'ANORM'},
                {'key': 'bias_ok', 'label': 'BIAS_OK'}, {'key': 'bias_cal', 'label': 'BIAS_CAL'},
                {'key': 'bias_n', 'label': 'BIAS_N'}, {'key': 'bias_t', 'label': 'BIAS_T'},
            ],
            'extended_metrics': [
                {'key': 'gcx', 'label': 'GCX'}, {'key': 'gcy', 'label': 'GCY'},
                {'key': 'gcz', 'label': 'GCZ'},
                {'key': 'gbx', 'label': 'GBX'}, {'key': 'gby', 'label': 'GBY'},
                {'key': 'gbz', 'label': 'GBZ'},
                {'key': 'bias_flash', 'label': 'FLASH'},
                {'key': 'q0', 'label': 'Q0'}, {'key': 'q1', 'label': 'Q1'},
                {'key': 'q2', 'label': 'Q2'}, {'key': 'q3', 'label': 'Q3'},
                {'key': 'ax', 'label': 'AX'}, {'key': 'ay', 'label': 'AY'},
                {'key': 'az', 'label': 'AZ'}, {'key': 'anorm', 'label': 'ANORM'},
                {'key': 'att_upd', 'label': 'ATT_UPD'},
            ],
            'accent_color': None,
            'max_plot_points': 2000,
        },
        'commands': {
            'quick_commands': [
                {'label': 'GET PID', 'command': 'GET PID'},
                {'label': 'SAVE PID', 'command': 'SAVE PID'},
            ],
            'custom_tabs': [],
            'command_tabs': [],
        },
        'simulation': {
            'enabled': True,
            'prefix': 'TELG',
        },
        'custom_state_file': None,
        'auto_open_browser': False,
    }

    def __init__(self, data: dict | None = None):
        self._raw = self._deep_merge(self._DEFAULTS, data or {})

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Recursively merge *override* into a copy of *base*."""
        result = {}
        for key in set(base) | set(override):
            if key in override and key in base:
                if isinstance(base[key], dict) and isinstance(override[key], dict):
                    result[key] = Config._deep_merge(base[key], override[key])
                else:
                    result[key] = override[key]
            elif key in override:
                result[key] = override[key]
            else:
                result[key] = base[key]
        return result

    @staticmethod
    def _kl_list(items: list) -> list[tuple]:
        """Convert [{"key": k, "label": l}, ...] → [(k, l), ...]."""
        return [(d['key'], d['label']) for d in items]

    @staticmethod
    def _kcv_list(items: list) -> list[tuple]:
        """Convert [{"key": k, "color": c, "visible": v}, ...] → [(k, c, v), ...]."""
        return [(d['key'], d['color'], d.get('visible', True)) for d in items]

    @staticmethod
    def _custom_tabs_convert(tabs: list) -> list[dict]:
        """Convert custom_tabs fields/result_keys from JSON objects to tuples."""
        out = []
        for tab in tabs:
            t = dict(tab)
            if 'fields' in t:
                t['fields'] = [(f['key'], f['label']) for f in t['fields']]
            if 'result_keys' in t:
                t['result_keys'] = [(r['prefix'], r['var_key']) for r in t['result_keys']]
            out.append(t)
        return out

    # -- attribute access (UPPER_CASE for backward compat) ---------------
    @property
    def APP_TITLE(self) -> str:
        return self._raw['app_title']

    @property
    def HTTP_HOST(self) -> str:
        return self._raw['network']['http_host']

    @property
    def HTTP_PORT(self) -> int:
        return self._raw['network']['http_port']

    @property
    def TELEMETRY_PREFIXES(self) -> tuple:
        return tuple(self._raw['protocol']['telemetry_prefixes'])

    @property
    def RESPONSE_PREFIXES(self) -> tuple:
        return tuple(self._raw['protocol']['response_prefixes'])

    @property
    def KEY_MAP(self) -> dict:
        return self._raw['protocol']['key_map']

    @property
    def PLOT_KEYS(self) -> list[tuple]:
        return self._kcv_list(self._raw['ui']['plot_channels'])

    @property
    def PRIMARY_METRICS(self) -> list[tuple]:
        return self._kl_list(self._raw['ui']['primary_metrics'])

    @property
    def DETAIL_METRICS(self) -> list[tuple]:
        return self._kl_list(self._raw['ui']['detail_metrics'])

    @property
    def EXTENDED_METRICS(self) -> list[tuple]:
        return self._kl_list(self._raw['ui']['extended_metrics'])

    @property
    def QUICK_COMMANDS(self) -> list[dict]:
        return self._raw['commands']['quick_commands']

    @property
    def CUSTOM_TABS(self) -> list[dict]:
        return self._custom_tabs_convert(self._raw['commands']['custom_tabs'])

    @property
    def COMMAND_TABS(self) -> list[dict]:
        return self._raw['commands']['command_tabs']

    @property
    def CUSTOM_STATE_FILE(self):
        return self._raw.get('custom_state_file')

    @property
    def MAX_PLOT_POINTS(self) -> int:
        return self._raw['ui'].get('max_plot_points', 2000)

    @property
    def ACCENT_COLOR(self):
        return self._raw['ui'].get('accent_color')

    @property
    def SIMULATION_ENABLED(self) -> bool:
        return self._raw['simulation'].get('enabled', True)

    @property
    def AUTO_OPEN_BROWSER(self) -> bool:
        return self._raw.get('auto_open_browser', False)

    # -- simulation (generic, config-driven) ----------------------------
    def build_simulated_packet(self, tick: int) -> str | None:
        sim = self._raw['simulation']
        if not sim.get('enabled', True):
            return None
        prefix = sim.get('prefix', 'TELG')
        t = tick * 0.1

        # Collect all metric keys and assign deterministic waveforms
        all_keys = set()
        for k, _ in self.PRIMARY_METRICS:
            all_keys.add(k)
        for k, _ in self.DETAIL_METRICS:
            all_keys.add(k)
        for k, _ in self.EXTENDED_METRICS:
            all_keys.add(k)
        for k, _, _ in self.PLOT_KEYS:
            all_keys.add(k)

        # Custom channel overrides
        channels = sim.get('channels', {})

        parts = [prefix, f'ms={tick * 100}']
        for i, key in enumerate(sorted(all_keys)):
            if key in channels:
                ch = channels[key]
                amp = ch.get('amplitude', 1.0)
                period = ch.get('period', 8.0 + i * 2)
            else:
                # Deterministic defaults based on key index
                amp = 0.5 + (hash(key) % 20) * 0.1
                period = 6.0 + (hash(key) % 15)
            use_sin = (i % 2 == 0)
            val = amp * (math.sin(t / period) if use_sin else math.cos(t / period))
            parts.append(f'{key}={val:.3f}')

        return ','.join(parts)

    @staticmethod
    def status_banner_logic(parsed):
        """Always return None — use default logic."""
        return None

    # -- JSON export for /config HTTP endpoint --------------------------
    def to_http_config(self) -> dict:
        """Return config subset for the dashboard's /config endpoint."""
        return {
            'app_title': self.APP_TITLE,
            'plot_channels': [{'key': k, 'color': c, 'visible': v}
                              for k, c, v in self.PLOT_KEYS],
            'primary_metrics': [{'key': k, 'label': l}
                                for k, l in self.PRIMARY_METRICS],
            'detail_metrics': [{'key': k, 'label': l}
                               for k, l in self.DETAIL_METRICS],
            'extended_metrics': [{'key': k, 'label': l}
                                 for k, l in self.EXTENDED_METRICS],
            'quick_commands': self.QUICK_COMMANDS,
        }


# ---------------------------------------------------------------------------
# Load config with priority chain
# ---------------------------------------------------------------------------
def _load_config():
    """Priority: config.json > tuning_config.py (dev fallback) > defaults."""
    json_path = app_dir() / 'config.json'
    if json_path.exists():
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return Config(data)
        except json.JSONDecodeError as e:
            # Show error before the tkinter window opens, then use defaults
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                _r = _tk.Tk(); _r.withdraw()
                _mb.showerror(
                    'config.json Error',
                    f'Failed to parse config.json:\n\n{e}\n\n'
                    f'Check line {e.lineno}, column {e.colno}.\n'
                    f'Falling back to built-in defaults.'
                )
                _r.destroy()
            except Exception:
                pass  # headless / very early error

    # Dev-mode fallback: try legacy Python config
    try:
        import tuning_config as _legacy
        return _legacy
    except ImportError:
        pass

    return Config()


cfg = _load_config()
