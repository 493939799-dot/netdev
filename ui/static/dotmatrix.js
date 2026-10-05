/* ============================================================
   NETDEV 工作台 · 点阵眼形背景动画模块 v1.0
   来源：reference_基准页面.html（已验收），零依赖，Canvas 2D
   用法：
     <canvas id="dots"></canvas>（position:fixed;inset:0;z-index:0）
     initDotMatrix(document.getElementById('dots'));
   移植到 Vue/React：把 initDotMatrix 挂到 mounted/useEffect，
   卸载时调用返回的 destroy()。
   ============================================================ */

function initDotMatrix(cv){
  const ctx = cv.getContext('2d');
  let W, H, DPR, dots = [], rafId = 0, destroyed = false;

  /* ---- 三档配色（同设计稿） ---- */
  const GRAY1 = [74,76,74], GRAY2 = [92,94,92], MGREEN = [64,118,82], ACC = [61,220,132];

  /* ---- 眼形几何：比例随窗口，旋转 -12° ---- */
  const TH = -12 * Math.PI / 180, cosT = Math.cos(TH), sinT = Math.sin(TH);
  const CX0 = () => W * 0.515, CY0 = () => H * 0.47;
  const RX1 = () => Math.min(W * 0.41, 680), RY1 = () => Math.min(H * 0.34, 350);
  const RX2 = () => RX1() * 0.60, RY2 = () => RY1() * 0.50;

  function build(){
    dots = [];
    const STEP = 14;                                  /* 网格步长 */
    const rx1 = RX1(), ry1 = RY1(), rx2 = RX2(), ry2 = RY2();
    for(let y = 40; y < H - 10; y += STEP){
      for(let x = 20; x < W - 10; x += STEP){
        const dx = x - CX0(), dy = y - CY0();
        const u = cosT * dx + sinT * dy, v = -sinT * dx + cosT * dy;
        const d1 = Math.hypot(u / rx1, v / ry1);      /* 外眼眶 */
        const d2 = Math.hypot(u / rx2, v / ry2);      /* 内眼眶 */
        const g = Math.exp(-Math.pow((d1 - 1) / 0.13, 2))
                + 0.9 * Math.exp(-Math.pow((d2 - 1) / 0.11, 2));
        if(Math.random() > g * 0.95 + 0.06) continue; /* 密度阈值 + 消散噪声 */
        let c, a;
        if(d1 > 1.02 && Math.random() < 0.20){ c = ACC;    a = 200 + 55 * Math.min(d1, 1.4); }
        else if(d1 > 0.55 || d2 > 0.75){       c = MGREEN; a = 170 + 70 * Math.min(g, 1.3); }
        else { c = Math.random() < 0.5 ? GRAY1 : GRAY2;     a = 120 + 100 * Math.min(g + 0.2, 1); }
        dots.push({
          x, y,
          s: 7 + ((d1 > 0.95 && Math.random() < 0.14) ? 3 : 0),
          r: c[0], g: c[1], b: c[2],
          a0: Math.min(a, 255) / 255,
          ph: Math.random() * Math.PI * 2,              /* 呼吸相位 */
          sp: 0.3 + Math.random() * 0.9,                /* 呼吸速度 */
          tw: Math.random() < 0.04,                     /* 4% 闪烁星点 */
          dx: (Math.random() - 0.5) * 5, dy: (Math.random() - 0.5) * 5
        });
      }
    }
  }

  function resize(){
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = W * DPR; cv.height = H * DPR;
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }

  /* 品牌位绿色径向光晕（呼吸） */
  let glowGrad = null, glowKey = '';
  function drawGlow(t){
    const key = W + 'x' + H;
    if(glowKey !== key){
      glowGrad = ctx.createRadialGradient(130, 26, 0, 130, 26, 430);
      glowGrad.addColorStop(0, 'rgba(61,220,132,0.12)');
      glowGrad.addColorStop(1, 'rgba(61,220,132,0)');
      glowKey = key;
    }
    ctx.globalAlpha = 0.85 + 0.15 * Math.sin(t * 0.4);
    ctx.fillStyle = glowGrad;
    ctx.fillRect(0, 0, 500, 500);
    ctx.globalAlpha = 1;
  }

  let last = 0;
  function frame(ts){
    if(destroyed) return;
    const t = ts / 1000;
    if(ts - last >= 33){                              /* ~30fps 节流 */
      last = ts;
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = '#070707'; ctx.fillRect(0, 0, W, H);
      drawGlow(t);
      const ox = Math.sin(t * 0.11) * 14;             /* 眼心游移 ±14px */
      const oy = Math.cos(t * 0.09) * 10;
      for(const d of dots){
        let a = d.a0 * (0.72 + 0.28 * Math.sin(t * d.sp + d.ph));   /* 呼吸 */
        if(d.tw){ a *= (Math.sin(t * 3 + d.ph * 7) > 0.55 ? 1.6 : 0.4); a = Math.min(a, 1); }
        const x = d.x + ox + Math.sin(t * 0.23 + d.ph) * d.dx;      /* 个体微漂移 */
        const y = d.y + oy + Math.cos(t * 0.19 + d.ph) * d.dy;
        ctx.fillStyle = `rgba(${d.r},${d.g},${d.b},${a.toFixed(3)})`;
        ctx.fillRect(x, y, d.s, d.s);
      }
    }
    rafId = requestAnimationFrame(frame);
  }

  const onResize = () => resize();
  addEventListener('resize', onResize);

  if(matchMedia('(prefers-reduced-motion: reduce)').matches){
    /* 无障碍：减动效环境渲染静态一帧 */
    resize();
    ctx.fillStyle = '#070707'; ctx.fillRect(0, 0, W, H); drawGlow(0);
    for(const d of dots){ ctx.fillStyle = `rgba(${d.r},${d.g},${d.b},${d.a0})`; ctx.fillRect(d.x, d.y, d.s, d.s); }
  }else{
    resize();
    rafId = requestAnimationFrame(frame);
  }

  return { destroy(){ destroyed = true; cancelAnimationFrame(rafId); removeEventListener('resize', onResize); } };
}

/* 全局暴露 + 自动挂载（auto-init on #dots） */
window.initDotMatrix = initDotMatrix;
document.addEventListener('DOMContentLoaded', () => {
  const cv = document.getElementById('dots');
  if(cv && !cv.__dotMatrix) cv.__dotMatrix = initDotMatrix(cv);
});
