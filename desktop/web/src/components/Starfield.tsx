// 星空背景(性能版):静态层(星云+全部恒星)离屏预渲染,只在 resize 时重算;
// 每帧只合成静态层 + 少量亮星闪烁 + 流星,并限帧 ~30fps。
// 纯视觉组件,零数据依赖。
import { useEffect, useRef } from 'react';

interface Star { x: number; y: number; r: number; phase: number; speed: number; hue: number }
interface Meteor { x: number; y: number; vx: number; vy: number; life: number }

const FRAME_MS = 33;           // ~30fps,视觉效果不变,开销减半
const TWINKLE_FRACTION = 0.2;  // 只有 20% 的星参与闪烁(亮的那些)

export default function Starfield() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let twinkles: Star[] = [];
    let meteors: Meteor[] = [];
    let w = 0; let h = 0;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    // 静态层:离屏 canvas,resize 时整体重绘一次
    const staticLayer = document.createElement('canvas');
    const sctx = staticLayer.getContext('2d')!;

    const resize = () => {
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      staticLayer.width = w * dpr; staticLayer.height = h * dpr;
      sctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // -- 静态层:星云 --
      const neb1 = sctx.createRadialGradient(w * 0.8, h * 0.15, 0, w * 0.8, h * 0.15, w * 0.5);
      neb1.addColorStop(0, 'rgba(99,102,241,0.06)');
      neb1.addColorStop(1, 'transparent');
      sctx.fillStyle = neb1;
      sctx.fillRect(0, 0, w, h);
      const neb2 = sctx.createRadialGradient(w * 0.15, h * 0.85, 0, w * 0.15, h * 0.85, w * 0.45);
      neb2.addColorStop(0, 'rgba(56,189,248,0.05)');
      neb2.addColorStop(1, 'transparent');
      sctx.fillStyle = neb2;
      sctx.fillRect(0, 0, w, h);

      // -- 静态层:全部恒星(暗的固化,亮的进闪烁列表每帧画) --
      const n = Math.floor((w * h) / 3800);
      twinkles = [];
      for (let i = 0; i < n; i++) {
        const s: Star = {
          x: Math.random() * w,
          y: Math.random() * h,
          r: 0.4 + Math.random() * 1.3,
          phase: Math.random() * Math.PI * 2,
          speed: 0.3 + Math.random() * 1.2,
          hue: Math.random() < 0.85 ? 220 : (Math.random() < 0.5 ? 45 : 280),
        };
        if (s.r > 1.1 && Math.random() < TWINKLE_FRACTION) {
          twinkles.push(s);   // 亮星:每帧单独画(带闪烁)
        } else {
          sctx.globalAlpha = 0.5 + Math.random() * 0.4;
          sctx.fillStyle = `hsl(${s.hue} 80% 88%)`;
          sctx.beginPath();
          sctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
          sctx.fill();
        }
      }
      sctx.globalAlpha = 1;
    };
    resize();
    window.addEventListener('resize', resize);

    let raf = 0;
    let lastFrame = 0;
    let lastMeteor = 0;
    const draw = (tms: number) => {
      raf = requestAnimationFrame(draw);
      if (tms - lastFrame < FRAME_MS) return;
      lastFrame = tms;
      const t = tms / 1000;

      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(staticLayer, 0, 0, w, h);

      // 亮星闪烁(数量只有总量的 ~4%)
      for (const s of twinkles) {
        const tw = 0.45 + 0.55 * Math.abs(Math.sin(s.phase + t * s.speed));
        ctx.globalAlpha = tw;
        ctx.fillStyle = `hsl(${s.hue} 80% 88%)`;
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
        if (tw > 0.9) {
          ctx.globalAlpha = tw * 0.35;
          ctx.fillRect(s.x - s.r * 3, s.y - 0.4, s.r * 6, 0.8);
          ctx.fillRect(s.x - 0.4, s.y - s.r * 3, 0.8, s.r * 6);
        }
      }
      ctx.globalAlpha = 1;

      // 流星:平均每 6-14s 一颗
      if (tms - lastMeteor > 6000 + Math.random() * 8000) {
        lastMeteor = tms;
        meteors.push({
          x: Math.random() * w * 0.7 + w * 0.15, y: -10,
          vx: 3 + Math.random() * 3, vy: 4 + Math.random() * 3, life: 1,
        });
      }
      meteors = meteors.filter((m) => m.life > 0);
      for (const m of meteors) {
        m.x += m.vx; m.y += m.vy; m.life -= 0.016;
        const grad = ctx.createLinearGradient(m.x - m.vx * 12, m.y - m.vy * 12, m.x, m.y);
        grad.addColorStop(0, 'transparent');
        grad.addColorStop(1, `rgba(200,225,255,${Math.max(m.life, 0) * 0.8})`);
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(m.x - m.vx * 12, m.y - m.vy * 12);
        ctx.lineTo(m.x, m.y);
        ctx.stroke();
      }
    };
    raf = requestAnimationFrame(draw);
    return () => { cancelAnimationFrame(raf); window.removeEventListener('resize', resize); };
  }, []);

  return <canvas ref={ref} style={{ position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none' }} />;
}
