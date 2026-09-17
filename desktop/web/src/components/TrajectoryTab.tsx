// 轨迹 XY:桥的 /trajectory 增量拉取(2s 轮询)画实时轨迹。
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { findXY } from '../detectXY';
import { drawXy } from '../drawXy';

export default function TrajectoryTab() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const ptsRef = useRef<[number, number][]>([]);
  const sinceRef = useRef(0);
  const [count, setCount] = useState(0);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await api.trajectory(sinceRef.current);
        if (!alive) return;
        for (const p of d.points) {
          const xy = findXY(p as Record<string, unknown>);
          if (xy) ptsRef.current.push(xy);
        }
        sinceRef.current = d.count;
        setCount(ptsRef.current.length);
      } catch { /* 桥短暂不可达,下一拍重试 */ }
    };
    tick();
    const timer = setInterval(tick, 2000);
    return () => { alive = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    // 数据 2s 才拉一轮,500ms 重绘足够流畅,不空转 rAF
    const draw = () => {
      if (canvasRef.current) {
        drawXy(canvasRef.current, [{ pts: ptsRef.current, color: '#7dd3fc' }]);
      }
    };
    draw();
    const timer = setInterval(draw, 500);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="chart-wrap">
      <div className="chart-toggles">
        <label>live trajectory</label>
        <label className="mono">{count} pts</label>
        <button className="btn" onClick={() => { ptsRef.current = []; sinceRef.current = 0; setCount(0); }}>
          clear view
        </button>
      </div>
      <div className="chart-host">
        <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
      </div>
    </div>
  );
}
