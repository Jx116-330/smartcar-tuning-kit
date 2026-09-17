// 趟对比分析:/paths/list 选文件(paths 优化线 + recordings 实跑捕获),
// /paths/load 读点后多趟叠加。点提取走形状嗅探(points[] / rows[] 都行)。
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { findXY } from '../detectXY';
import { drawXy, XySeries } from '../drawXy';

const PALETTE = ['#5eead4', '#38bdf8', '#fbbf24', '#c084fc', '#ff5577', '#4ade80'];

interface FileEntry { dir: string; filename: string; size_kb: number; mtime: string }

function extractPoints(data: unknown): [number, number][] {
  const out: [number, number][] = [];
  const d = data as Record<string, unknown>;
  const arr = (d?.points ?? d?.rows) as Record<string, unknown>[] | undefined;
  if (!Array.isArray(arr)) return out;
  for (const item of arr) {
    const xy = findXY(item);
    if (xy) out.push(xy);
  }
  return out;
}

export default function CompareTab() {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [series, setSeries] = useState<XySeries[]>([]);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    api.pathsList()
      .then((d) => {
        const all: FileEntry[] = [];
        for (const [dir, list] of Object.entries(d)) {
          for (const f of list) all.push({ dir, ...f });
        }
        setFiles(all);
      })
      .catch(() => setFiles([]));
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const out: XySeries[] = [];
      let i = 0;
      for (const key of selected) {
        const f = files.find((x) => `${x.dir}/${x.filename}` === key);
        if (!f) continue;
        try {
          const data = await api.pathsLoad(f.dir, f.filename);
          out.push({
            pts: extractPoints(data),
            color: PALETTE[i % PALETTE.length],
            label: f.filename,
          });
        } catch { /* skip */ }
        i++;
      }
      if (alive) setSeries(out);
    };
    load();
    return () => { alive = false; };
  }, [selected, files]);

  // 只在数据变化 / 容器尺寸变化时重绘,不空转 rAF
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => drawXy(canvas, series);
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas.parentElement ?? canvas);
    return () => ro.disconnect();
  }, [series]);

  const toggle = (key: string) => {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key); else next.add(key);
    setSelected(next);
  };

  const dirs = [...new Set(files.map((f) => f.dir))];
  return (
    <div className="compare-layout">
      <div className="compare-files">
        {dirs.map((dir) => (
          <div key={dir}>
            <div className="dir-head">{dir}/</div>
            {files.filter((f) => f.dir === dir).map((f) => {
              const key = `${f.dir}/${f.filename}`;
              return (
                <label key={key}>
                  <input type="checkbox" checked={selected.has(key)} onChange={() => toggle(key)} />
                  <span>{f.filename} <span style={{ color: 'var(--hud-mute)' }}>{f.size_kb}k</span></span>
                </label>
              );
            })}
          </div>
        ))}
        {!files.length && <div className="dir-head">no files</div>}
      </div>
      <div className="compare-stage">
        <canvas ref={canvasRef} />
        <div className="compare-legend">
          {series.map((s) => (
            <div key={s.label} style={{ color: s.color }}>
              ─ {s.label} ({s.pts.length} pts)
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
