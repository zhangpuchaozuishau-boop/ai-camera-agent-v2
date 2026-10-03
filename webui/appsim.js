// Panel 9: App interface rehearsal. This panel talks to /api/app/* exactly like the
// phone app will (same paths, same JSON, same errors), so the browser becomes the App.
const APPSIM = {session: null, shots: [], loop: null, box: {cx: 0.62, cy: 0.62, h: 0.24},
                dragging: false, timer: null, target: null, sent: 0, accepted: 0};
const AS_W = 1920, AS_H = 1080;                       // 演示用的画幅（和真实检测同一坐标系）

function asNum(v) { return v === '' || v === null ? null : Number(v); }
function asErr(e) { return !e ? '未知' : (e.code ? `${e.code}: ${e.reason || ''}` : JSON.stringify(e)); }

async function appsimCall(name, body) {
  const base = ($('asBase').value || '').trim().replace(/\/+$/, '');
  const url = base + '/api/app/' + name;
  const headers = {'Content-Type': 'application/json'};
  const token = ($('asToken').value || '').trim();
  if (token) headers['X-Agent-Token'] = token;
  const started = performance.now();
  let status = 0, data = null;
  try {
    const res = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body || {})});
    status = res.status;
    data = await res.json();
  } catch (error) {
    data = {ok: false, error: {code: 'FETCH_FAILED', reason: String(error)}};
  }
  const ms = (performance.now() - started).toFixed(0);
  asLog(`→ POST ${name} ${JSON.stringify(body || {})}\n← ${status} (${ms}ms) ${JSON.stringify(data).slice(0, 900)}`);
  return {status, data};
}

function asLog(text) {
  const el = $('asLog');
  if (el.dataset.fresh !== '1') { el.textContent = ''; el.dataset.fresh = '1'; }
  el.textContent += (el.textContent ? '\n' : '') + text;
  el.scrollTop = el.scrollHeight;
}

async function appsimInit() {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <h2>9. App 接口联调（这一块就是"手机 App"，走 /api/app/*）</h2>
    <label class="row" style="justify-content:flex-start;gap:8px;flex-wrap:wrap">
      Agent 地址 <input id="asBase" style="width:290px">
      Token <input id="asToken" placeholder="启动时 --token 才需要" style="width:150px">
      <button id="asHello">1. 握手</button>
      <button id="asAll">一键跑完整流程</button>
      <span id="asState" class="muted">未连接</span>
    </label>
    <p class="muted" id="asPhoneBase"></p>
    <p class="muted" id="asAppLog">手机连接记录：等待手机请求…（每 3 秒刷新）</p>
    <div class="grid2" style="margin-top:8px">
      <div>
        <h3 style="margin:0 0 6px;font-size:13px">用户需求 → 分镜</h3>
        <textarea id="asText" rows="3" style="width:100%">拍桌面青铜器：从偏低占画幅高度 40%，用 5 秒升到中部并放大到 70%</textarea>
        <label class="row" style="justify-content:flex-start;gap:8px;flex-wrap:wrap">
          模式 <select id="asMode"><option value="fixture">离线 fixture（0 次调用）</option><option value="real">真实 DeepSeek</option></select>
          机器人 <input id="asHost" value="127.0.0.1:8899" style="width:130px">
          主体高 <input id="asHeight" value="200" style="width:56px">mm
          速度 <input id="asSpeed" value="40" style="width:46px">%
        </label>
        <div style="margin-top:6px"><button id="asSession">2. 建会话</button>
          <span id="asSum" class="muted"></span></div>
        <div class="row" style="justify-content:flex-start;gap:8px;margin-top:8px">
          <label class="row" style="gap:6px">镜头 <select id="asShot"></select></label>
          <button id="asStart">3. 启动镜头</button>
          <button id="asNext" class="ghost">下一镜</button>
          <button id="asStop" class="ghost">停止</button>
          <button id="asClose" class="ghost">关闭会话</button>
        </div>
      </div>
      <div>
        <h3 style="margin:0 0 6px;font-size:13px">主体绿框（拖动 = 移动主体，滚轮 = 改大小）</h3>
        <canvas id="asCanvas" width="360" height="270" style="cursor:move"></canvas>
        <div class="row" style="justify-content:flex-start;gap:6px">
          <button id="asFrame">4. 上报一帧</button>
          <button id="asBurst">连续上报 10 帧</button>
          <button id="asLost" class="ghost">主体丢失</button>
        </div>
        <p class="muted">灰虚线 = 计划目标框；绿框 = 本"手机"上报的框。坐标按像素（1920×1080）发送，和真实检测一致。</p>
      </div>
    </div>
    <pre id="asLog">点「一键跑完整流程」：握手 → 建会话 → 启动镜头 → 上报若干帧 → 主体丢失 → 下一镜 → 停止 → 关闭。
每一步都打印真实 HTTP 状态与响应，和 App 同学看到的一模一样。</pre>`;
  $('right').appendChild(card);
  $('asBase').placeholder = '留空 = 本页同源（' + location.origin + '）';
  $('asPhoneBase').textContent = '手机 App 填的基址：' + location.origin + '/api/app/（用手机能访问到的地址打开本页）';
  fetch('api/net', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    .then((r) => r.json())
    .then((net) => {
      if (!net.ok) return;
      $('asPhoneBase').textContent += '　本机可选地址：' +
        net.candidates.map((c) => c.url).join('  |  ') + '　' + net.how_to_pick;
    })
    .catch(() => {});
  APPSIM.box = {cx: 0.62, cy: 0.62, h: 0.24};
  $('asHello').onclick = asHello;
  $('asAll').onclick = asRunAll;
  $('asSession').onclick = asSession;
  $('asStart').onclick = () => asStart(null);
  $('asNext').onclick = asNext;
  $('asStop').onclick = asStop;
  $('asClose').onclick = asClose;
  $('asFrame').onclick = () => asFrame(0);
  $('asBurst').onclick = asBurst;
  $('asLost').onclick = () => asFrame(0, true);
  asBindCanvas();
  asDraw();
  asPollAppLog();
  setInterval(asPollAppLog, 3000);
}

async function asPollAppLog() {
  try {
    const r = await (await fetch('api/net/applog', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                    body: '{}'})).json();
    const rows = (r.entries || []).slice(-6).map((e) =>
      `${e.time} ${e.method} ${e.path.replace('/api/', '')} → ${e.status}${e.ms === null ? '' : ' ' + e.ms + 'ms'}` +
      `　来自 ${e.remote}${e.agent ? ' [' + e.agent + ']' : ''}` +
      (e.error ? `　错误：${e.error}` : ''));
    $('asAppLog').textContent = rows.length
      ? '手机连接记录（最近 ' + rows.length + ' 条）：' + rows.join('　|　') + '　  本机端口 ' + r.port
      : '手机连接记录：还没有收到任何 App 请求（手机点“连接并使用”后这里会出现）。';
  } catch (error) { /* 页面刚加载时忽略 */ }
}

// ------------------------------------------------------------------ 步骤

async function asHello() {
  const {data} = await appsimCall('hello', {host: $('asHost').value, probe: true});
  if (!data.ok) { $('asState').textContent = '握手失败 ' + asErr(data.error); return; }
  $('asState').textContent = `已连接 api ${data.api_version}｜标定 ${data.calibration.measured}/${data.calibration.total}`
    + `｜机器人 ${data.robot.reachable ? '可达 ' + ((data.robot.status || {}).battery_mv || '') + 'mV' : '不可达'}`;
}

async function asSession() {
  const {data} = await appsimCall('session', {
    text: $('asText').value, mode: $('asMode').value, host: $('asHost').value,
    subject_height_mm: asNum($('asHeight').value), speed_pct: asNum($('asSpeed').value)});
  if (!data.ok) { $('asSum').textContent = '失败 ' + asErr(data.error); return; }
  APPSIM.session = data.session_id;
  APPSIM.shots = data.shots;
  $('asShot').innerHTML = data.shots.map((s, i) =>
    `<option value="${i}">${i + 1}. ${esc(s.shot_id)} ${esc(s.shot_size_zh)}/${esc(s.camera_angle_zh)}/${esc(s.camera_move_zh)} ${s.start}-${s.end}s</option>`).join('');
  $('asSum').textContent = `会话 ${data.session_id}｜总长 ${data.total_duration}s｜${data.shots.length} 镜`;
  asSyncShot();
  return data;
}

async function asStart(shotId) {
  if (!APPSIM.session) { await asSession(); if (!APPSIM.session) return; }
  const shot = APPSIM.shots[Number($('asShot').value)] || {};
  const {data} = await appsimCall('session/shot/start', {session_id: APPSIM.session, shot_id: shotId || shot.shot_id,
                                                 host: $('asHost').value, confirm: true, speed_pct: asNum($('asSpeed').value)});
  if (data.ok) {
    $('asState').textContent = `镜头 ${data.shot.shot_id} 已启动 plan=${data.plan_id}`;
    asLog('   实际下发：' + data.plan.map((a) => a.action_name + JSON.stringify(a.parameters)).join('；'));
  }
  APPSIM.sent = 0; APPSIM.accepted = 0;
  return data;
}

function asSyncShot() {
  const shot = APPSIM.shots[Number($('asShot').value)];
  if (!shot) return;
  const curve = shot.target_curve[0];
  APPSIM.target = curve;
  APPSIM.box = {cx: curve.center_x + 0.12, cy: curve.center_y, h: curve.subject_height_ratio};
  asDraw();
}

async function asFrame(drift, lost) {
  if (!APPSIM.session) { $('asState').textContent = '先建会话'; return null; }
  const b = APPSIM.box;
  const pixels = {x1: Math.round((b.cx - 0.1) * AS_W), y1: Math.round((b.cy - b.h / 2) * AS_H),
                  x2: Math.round((b.cx + 0.1) * AS_W), y2: Math.round((b.cy + b.h / 2) * AS_H)};
  const frame = lost ? {timestamp: Date.now() / 1000, lost: true}
                     : {timestamp: Date.now() / 1000, ...pixels};
  const {data} = await appsimCall('session/observe', {session_id: APPSIM.session, frames: [frame],
                                              frame_width: AS_W, frame_height: AS_H});
  if (!data.ok) { $('asState').textContent = '上报失败 ' + asErr(data.error); return null; }
  const row = data.results[0];
  APPSIM.sent += 1;
  APPSIM.accepted += row.gate.accepted ? 1 : 0;
  $('asState').textContent = `第 ${APPSIM.sent} 帧：${row.gate.accepted ? 'gate accept' : row.gate.code}｜`
    + `${row.decision || '—'}${row.reason ? ' (' + row.reason + ')' : ''}｜已采纳 ${APPSIM.accepted}/${APPSIM.sent}`;
  if (row.target) APPSIM.target = row.target;
  if (row.decision === 'ADJUST' && !row.deferred && row.corrections) {
    asLog('   修正：' + row.corrections.map((c) => c.dimension === 'PAN' || c.dimension === 'TILT'
      ? `${c.dimension} ${visFixed(c.delta_deg, 2)}° → 舵机 ${c.commanded_angle}°${c.clamped ? '（限幅）' : ''}`
      : `${c.dimension} ${visFixed(c.travel_mm, 1)}mm → ${c.dir} ${c.speed_pct}% ${c.duration_ms}ms`).join('；'));
  }
  if (row.deferred) asLog('   修正被推迟：' + row.deferred);
  if (row.executed) asLog('   实际下发：' + JSON.stringify(row.executed).slice(0, 400));
  for (const hint of data.hints || []) asLog('   提示：' + hint);
  if (drift) {                                   // 本地模拟机器人朝目标收敛，便于看曲线
    APPSIM.box.cx += (APPSIM.target.center_x - APPSIM.box.cx) * drift;
    APPSIM.box.cy += (APPSIM.target.center_y - APPSIM.box.cy) * drift;
    APPSIM.box.h += (APPSIM.target.subject_height_ratio - APPSIM.box.h) * drift;
  }
  asDraw();
  return row;
}

async function asBurst() {
  for (let i = 0; i < 10; i++) {
    await asFrame(0.45);
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function asNext() {
  const {data} = await appsimCall('session/advance', {session_id: APPSIM.session, host: $('asHost').value, confirm: true});
  if (data.finished) { $('asState').textContent = '分镜已拍完'; return; }
  const index = APPSIM.shots.findIndex((s) => s.shot_id === data.shot.shot_id);
  if (index >= 0) $('asShot').value = index;
  asSyncShot();
  $('asState').textContent = `切到 ${data.shot.shot_id}`;
}

async function asStop() {
  const {data} = await appsimCall('session/stop', {session_id: APPSIM.session, reason: '网页端手动停止'});
  if (data.ok) $('asState').textContent = '已停车';
}

async function asClose() {
  const {data} = await appsimCall('session/close', {session_id: APPSIM.session});
  APPSIM.session = null;
  $('asState').textContent = data.ok ? '会话已关闭' : '关闭失败 ' + asErr(data.error);
}

async function asRunAll() {
  asLog('=== 一键完整流程（模拟手机 App 的全过程）===');
  await asHello();
  if (!(await asSession())) return;
  await asStart(null);
  for (const offset of [0.12, 0.10, 0.06, 0.02, 0.0]) {
    APPSIM.box.cx = (APPSIM.target || APPSIM.box).center_x + offset;
    APPSIM.box.h = (APPSIM.target || APPSIM.box).subject_height_ratio * 0.8;
    await asFrame(0.5);
    await new Promise((r) => setTimeout(r, 550));      // 让 0.4s 最小修正间隔过去
  }
  await asFrame(0, true);
  await asNext();
  await asStop();
  await asClose();
  asLog('=== 结束 ===');
}

// ---------------------------------------------------------------- canvas

function asBindCanvas() {
  const canvas = $('asCanvas');
  const toNorm = (event) => {
    const rect = canvas.getBoundingClientRect();
    return {x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height};
  };
  canvas.addEventListener('mousedown', (event) => {
    const p = toNorm(event);
    const b = APPSIM.box;
    if (p.x >= b.cx - 0.1 && p.x <= b.cx + 0.1 && p.y >= b.cy - b.h / 2 && p.y <= b.cy + b.h / 2) {
      APPSIM.dragging = true;
      APPSIM.grab = {dx: b.cx - p.x, dy: b.cy - p.y};
    }
  });
  window.addEventListener('mouseup', () => { APPSIM.dragging = false; });
  canvas.addEventListener('mousemove', (event) => {
    if (!APPSIM.dragging) return;
    const p = toNorm(event);
    APPSIM.box.cx = Math.max(0.12, Math.min(0.88, p.x + APPSIM.grab.dx));
    APPSIM.box.cy = Math.max(0.12, Math.min(0.88, p.y + APPSIM.grab.dy));
    asDraw();
  });
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    APPSIM.box.h = Math.max(0.06, Math.min(0.8, APPSIM.box.h * (event.deltaY > 0 ? 0.92 : 1.08)));
    asDraw();
  }, {passive: false});
}

function asDraw() {
  const canvas = $('asCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#0b0e13';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = '#2c3340';
  ctx.strokeRect(0.5, 0.5, canvas.width - 1, canvas.height - 1);
  const rect = (x, y, w, h, color, dash) => {
    ctx.save();
    ctx.setLineDash(dash ? [5, 4] : []);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
  };
  const t = APPSIM.target;
  if (t) {
    rect((t.center_x - 0.1) * canvas.width, (t.center_y - t.subject_height_ratio / 2) * canvas.height,
         0.2 * canvas.width, t.subject_height_ratio * canvas.height, '#9aa4b2', true);
    ctx.fillStyle = '#9aa4b2';
    ctx.font = '11px sans-serif';
    ctx.fillText('目标', 6, 14);
  }
  const b = APPSIM.box;
  rect((b.cx - 0.1) * canvas.width, (b.cy - b.h / 2) * canvas.height,
       0.2 * canvas.width, b.h * canvas.height, '#3fb950', false);
  ctx.fillStyle = '#3fb950';
  ctx.fillText('主动上报（可拖动）', 6, canvas.height - 8);
}

appsimInit();

// 自检钩子：地址后面加 #appsim-auto 就自动跑一遍完整流程（方便无头验证/演示）
if (location.hash.indexOf('appsim-auto') >= 0) {
  setTimeout(() => { asLog('=== 自动自检（#appsim-auto）==='); asRunAll(); }, 500);
}
