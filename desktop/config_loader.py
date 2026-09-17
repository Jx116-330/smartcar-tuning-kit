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
# Bridge extension sentinel (2026-09-14 设计文档 §1)
# ---------------------------------------------------------------------------
class _Unset:
    """键未声明(区别于显式 null)。三态 str | None | UNSET 必须在
    _DEFAULTS 深合并后仍可区分:__init__ 的 deep_merge 会把 default 值
    灌进缺键,所以 _DEFAULTS 刻意不含 bridge_extension,缺省由此保留。"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'UNSET'


UNSET = _Unset()


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
# Profile resolution (契约 v1, 设计文档 §2a / docs/protocol_contract_v1.md §5)
# ---------------------------------------------------------------------------
def _profile_from_argv() -> str | None:
    """从命令行解析 --profile <name> / --profile=<name>(exe 与源码同参)。
    在 import 时执行,保证 cfg 在模块级消费前已定稿。"""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == '--profile' and i + 1 < len(argv):
            return argv[i + 1].strip() or None
        if a.startswith('--profile='):
            return a.split('=', 1)[1].strip() or None
    return None


PROFILE_NAME = _profile_from_argv()


def _profile_dir() -> Path | None:
    """profiles/<name>/ 目录;不存在返回 None(回退根目录配置)。"""
    if PROFILE_NAME:
        d = app_dir() / 'profiles' / PROFILE_NAME
        if d.is_dir():
            return d
    return None


def _config_path() -> Path | None:
    if PROFILE_NAME:
        p = app_dir() / 'profiles' / PROFILE_NAME / 'config.json'
        if p.is_file():
            return p
    p = app_dir() / 'config.json'
    return p if p.is_file() else None


def profile_schema_path() -> Path | None:
    """当前生效的 control_schema.json 路径(profile 优先,回退根目录)。
    /schema 端点与 schema_hash 版本锚共用此决议,保证两处永远同源。"""
    if PROFILE_NAME:
        p = app_dir() / 'profiles' / PROFILE_NAME / 'control_schema.json'
        if p.is_file():
            return p
    p = app_dir() / 'control_schema.json'
    return p if p.is_file() else None


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
            # 契约 v1 字段(默认 kv = 原车行为,老 config 无感):
            # frame_mode: kv=key=value 帧 / csv=位置式 D 帧(docs/protocol_contract_v1.md)
            # channels:  csv 模式下 {通道名: [列序字段名]},列序与固件逐列一致
            # err_prefixes: 回执里属"错误"的前缀,用于 /batch 状态判定(ack 集合=response-err)
            'frame_mode': 'kv',
            'channels': {},
            'err_prefixes': ['ERR'],
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
        # 启动时自动开 TCP 服务（按 network.tcp_host / tcp_port 监听）。
        # 默认 False 保持原行为；置 True 让工具一启动就监听，自动化驱动场景必备。
        'auto_start_tcp': False,
        'tcp_host': '0.0.0.0',
        'tcp_port': '8080',
        # 自动心跳(2026-06-10):TCP 有客户端连着就由桥周期性发心跳命令刷新
        # 固件 Gate8(心跳超时 abort)计时,不再需要外部脚本保活。默认 'HB'——
        # 固件专用心跳:刷新 last_cmd_rx_ms 后【不回任何响应】(见 tuning_dispatch.c),
        # 所以零污染 cmd_result、不会注入 ACK 干扰 dump/upload 的 ACK 步进协议。
        # (老固件无 HB handler 会回 ERR,桥已对心跳命令的响应做 cmd_result 抑制。)
        'heartbeat': {
            'enabled': True,
            'command': 'HB',
            # 500ms:< 固件默认 Gate8 超时 800ms,所以即使没设 HEARTBEAT_TIMEOUT 也不假
            # 触发;配 5000ms 超时则可容 ~9 次丢包(抗 wifi 瞬断)。HB 无响应,2Hz 极廉价。
            'interval_ms': 500,
        },
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
    def FRAME_MODE(self) -> str:
        """kv=key=value 帧(原车) / csv=位置式 D 帧(契约 v1)。"""
        return str(self._raw['protocol'].get('frame_mode', 'kv')).lower()

    @property
    def CHANNELS(self) -> dict:
        """csv 模式列序表:{通道名: [字段名...]}。兼容 {'fields': [...]} 与裸数组两种写法。"""
        ch = self._raw['protocol'].get('channels', {}) or {}
        out = {}
        for name, defn in ch.items():
            if isinstance(defn, dict):
                out[str(name)] = [str(f) for f in defn.get('fields', [])]
            elif isinstance(defn, list):
                out[str(name)] = [str(f) for f in defn]
        return out

    @property
    def ERR_PREFIXES(self) -> tuple:
        """回执里属"错误"的前缀(/batch 判 err 用;ack 集合 = response - err)。"""
        return tuple(self._raw['protocol'].get('err_prefixes', ['ERR']))

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
    def AUTO_START_TCP(self) -> bool:
        return bool(self._raw.get('auto_start_tcp', False))

    @property
    def TCP_HOST(self) -> str:
        return str(self._raw.get('tcp_host', '0.0.0.0'))

    @property
    def TCP_PORT(self) -> str:
        return str(self._raw.get('tcp_port', '8080'))

    @property
    def HEARTBEAT_ENABLED(self) -> bool:
        return bool(self._raw.get('heartbeat', {}).get('enabled', True))

    @property
    def HEARTBEAT_COMMAND(self) -> str:
        return str(self._raw.get('heartbeat', {}).get('command', 'HB'))

    @property
    def HEARTBEAT_INTERVAL_MS(self) -> int:
        return int(self._raw.get('heartbeat', {}).get('interval_ms', 2000))

    @property
    def AUTO_OPEN_BROWSER(self) -> bool:
        return self._raw.get('auto_open_browser', False)

    @property
    def BRIDGE_EXTENSION(self):
        """桥扩展声明三态:str(app_dir() 相对路径)| None(显式关闭)| UNSET(未声明)。
        UNSET 的语义由调用方决议:仅根布局(无 --profile)时尝试约定名
        bridge_ext.py(旧 dist 兼容);命名 profile 缺键 = 无扩展。"""
        v = self._raw.get('bridge_extension', UNSET)
        if v is UNSET or v is None:
            return v
        return str(v)

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
    """Priority: profiles/<name>/config.json(--profile)> config.json > tuning_config.py(dev fallback) > defaults."""
    json_path = _config_path()
    if json_path is not None:
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
