#!/usr/bin/env python3
"""旧车桥扩展(bridge extension)—— 领域功能外置的收容模块。

2026-09-14 还债:以下功能从 tuning_tool.py 通用核原样迁出(设计文档
docs/2026-09-14-bridge-extension-headless.md):
  - TELPT/TELPTDUMP 轨迹点累积 + /trajectory 六端点(GET/SAVE/LOAD/CLEAR/DELETE/LIST)
  - 相机捕获管理器 + /camera/status /camera/save + CIMG 混排流解析器
  - ASR 语音桥行处理与预热导入

加载方式:根 config.json `"bridge_extension": "bridge_ext.py"`(或根布局
缺键时的约定名默认)。EXT_API == 1。

契约要点(tuning_tool.py 通用核反向依赖这些钩子的语义):
  - 本模块禁止 `import tuning_tool`(脚本态 __main__ 双导入陷阱);
    桥内依赖(app.data_dir / app.send_command / app.queue_log /
    app.telemetry_lock)全部经钩子的 app 参数注入。
  - on_packet 在桥的 telemetry_lock 内被调 → 本模块轨迹状态天然受锁
    保护;/trajectory 读取端点同样先取 app.telemetry_lock。
  - on_line 返回 False = 行继续流经核的正常 dispatch(相机 CAMMETA/
    CAMSTAT 与 ACK 回显都必须能进遥测/命令响应路径,不能被扩展吞掉);
    返回 True 仅用于 ASR 音频帧(核内无此行协议)。
  - create_stream_parser 返回的解析器实现 feed/timed_out/close 接口,
    协议错误与断连 partial 记账内聚在解析器侧完成。
  - ROUTES 值签名 fn(app, qs, body) -> dict | (code, dict),
    端点状态码与迁移前一致(400/404/500 分支原样保留)。
"""
import json
import time

EXT_API = 1

# ---------------------------------------------------------------------------
# 模块状态(init 注入后有效)
# ---------------------------------------------------------------------------
_mgr = None          # CameraCaptureManager(相机捕获管理器)
_traj_dir = None     # 轨迹持久化目录
_traj_points = []    # 滚动窗口(on_packet 在 telemetry_lock 内写)
_TRAJ_MAX = 5000     # 窗口帽(迁移前为核内硬编码,语义不变)
_traj_total = 0      # 累计计数,trim 不复位(clear 才复位)


def init(app):
    global _mgr, _traj_dir
    from camera_capture import CameraCaptureManager
    _mgr = CameraCaptureManager(app.data_dir)
    _traj_dir = app.data_dir / 'trajectories'
    _traj_dir.mkdir(parents=True, exist_ok=True)


def on_start(app):
    # 预热导入 asr_bridge:exe 里首次从冻结归档导入要解压(~1-2s),
    # 若拖到首帧到达时在 RX 线程上做,会把音频帧交付打成突发。
    # 核已在后台线程调用本钩子。
    __import__('asr_bridge')


def on_packet(app, parsed):
    # 调用方保证:已在 telemetry_lock 内
    global _traj_total
    ptype = parsed.get('_packet', '')
    if ptype == 'TELPT':
        pt = {k: v for k, v in parsed.items() if not k.startswith('_')}
        _traj_points.append(pt)
        _traj_total += 1
        if len(_traj_points) > _TRAJ_MAX:
            del _traj_points[:-_TRAJ_MAX]
    elif ptype == 'TELPTDUMP':
        pass  # Header-only packet (count field); points follow as TELPT


def on_line(app, source, line):
    # 相机元数据/状态/订阅 ACK 都要被管理器看到;行本身继续流经核的
    # 正常 dispatch(ACK 回显必须进 cmd_result / batch 匹配路径)。
    if _mgr is not None:
        try:
            _mgr.handle_line(line)
        except Exception:
            pass
    if line.startswith('ASR '):
        # 综合科目语音桥:把带 session_id 的音频帧交给 asr_bridge(非阻塞,
        # 讯飞 WSS 在后台线程运行)。最终结果按同一 session 回发
        # "ASR:<sid>:SEQ,<count>,<ids...>",迟到结果由车端丢弃。
        try:
            from asr_bridge import handle_asr_line
            handle_asr_line(line, app.send_command)
        except Exception as e:
            app.queue_log('log', f'[{source}] ASR bridge error: {e}')
        return True
    return False


def on_frame(app, source, frame):
    if _mgr is None:
        return
    if not _mgr.accept_frame(frame):
        app.queue_log(
            'log',
            f'[{source}] rejected camera frame seq={frame.frame_seq}: '
            'missing or mismatched CAMMETA/CAMSTAT'
        )


def create_stream_parser(app):
    from camera_capture import CameraProtocolError, MixedCameraStreamParser

    class _RecordingParser:
        """混排解析器包装:把协议错误/断连 partial 记账内聚到解析器侧,
        核只见通用 feed/timed_out/close 接口。"""

        def __init__(self):
            self._p = MixedCameraStreamParser()

        def feed(self, data):
            try:
                return self._p.feed(data)
            except CameraProtocolError:
                _mgr.record_protocol_error()
                raise

        def timed_out(self):
            return self._p.frame_timed_out()

        def close(self):
            before = self._p.partial_frames
            self._p.disconnect()
            if self._p.partial_frames != before:
                _mgr.record_partial(self._p.partial_frames - before)

    return _RecordingParser()


def shutdown(app):
    # 管理器 worker 是 daemon 线程,随进程退出收尾;无显式 stop 接口
    pass


# ---------------------------------------------------------------------------
# HTTP 路由(原核内端点逐行迁移,状态码保持)
# ---------------------------------------------------------------------------
def _parse_qs_int(qs, key, default=0):
    for part in qs.split('&'):
        if part.startswith(key + '='):
            try:
                return int(part.split('=', 1)[1])
            except ValueError:
                pass
    return default


def _get_trajectory(app, qs, body):
    # 增量拉取 ?since=N:窗口算术与迁移前一致(track_dump.py 依赖)
    since = _parse_qs_int(qs, 'since', 0)
    with app.telemetry_lock:
        total = _traj_total
        win_start = total - len(_traj_points)
        local_since = max(0, since - win_start)
        pts = list(_traj_points[local_since:])
    return {'points': pts, 'count': total, 'since': since}


def _get_trajectory_list(app, qs, body):
    files = []
    for f in sorted(_traj_dir.glob('*.json')):
        try:
            meta = json.loads(f.read_text(encoding='utf-8'))
            files.append({
                'filename': f.name,
                'name': meta.get('name', f.stem),
                'point_count': len(meta.get('points', [])),
                'created': meta.get('created', ''),
            })
        except Exception:
            pass
    return {'files': files}


def _post_trajectory_save(app, qs, body):
    name = str(body.get('name', 'untitled')).strip() or 'untitled'
    # Use live points if none provided
    points = body.get('points', None)
    if points is None:
        with app.telemetry_lock:
            points = list(_traj_points)
    if not points:
        return 400, {'error': 'no points to save'}
    safe_name = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in name)
    fname = f'{safe_name}.json'
    payload = {
        'name': name,
        'points': points,
        'point_count': len(points),
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    fpath = _traj_dir / fname
    fpath.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding='utf-8')
    return {'status': 'saved', 'filename': fname, 'count': len(points)}


def _post_trajectory_load(app, qs, body):
    fname = str(body.get('filename', ''))
    fpath = (_traj_dir / fname).resolve()
    # parent 比对而非 startswith:防 '../' 和同前缀兄弟目录(trajectoriesX)绕过
    if fpath.parent != _traj_dir.resolve():
        return 400, {'error': 'invalid path'}
    if not fpath.exists() or not fpath.suffix == '.json':
        return 404, {'error': 'file not found'}
    try:
        return json.loads(fpath.read_text(encoding='utf-8'))
    except Exception as e:
        return 500, {'error': str(e)}


def _post_trajectory_clear(app, qs, body):
    global _traj_total
    with app.telemetry_lock:
        _traj_points.clear()
        _traj_total = 0
    return {'status': 'cleared'}


def _post_trajectory_delete(app, qs, body):
    fname = str(body.get('filename', ''))
    fpath = (_traj_dir / fname).resolve()
    if fpath.parent != _traj_dir.resolve():
        return 400, {'error': 'invalid path'}
    if fpath.exists() and fpath.suffix == '.json':
        fpath.unlink()
        return {'status': 'deleted', 'filename': fname}
    return 404, {'error': 'file not found'}


def _get_camera_status(app, qs, body):
    return _mgr.status()


def _post_camera_save(app, qs, body):
    try:
        saved = _mgr.save_session(str(body.get('label', '')))
        return {'status': 'saved', 'path': str(saved.resolve())}
    except (OSError, ValueError) as exc:
        return 400, {'error': str(exc)}


ROUTES = {
    ('GET', '/camera/status'): _get_camera_status,
    ('GET', '/trajectory'): _get_trajectory,
    ('GET', '/trajectory/list'): _get_trajectory_list,
    ('POST', '/camera/save'): _post_camera_save,
    ('POST', '/trajectory/save'): _post_trajectory_save,
    ('POST', '/trajectory/load'): _post_trajectory_load,
    ('POST', '/trajectory/clear'): _post_trajectory_clear,
    ('POST', '/trajectory/delete'): _post_trajectory_delete,
}
