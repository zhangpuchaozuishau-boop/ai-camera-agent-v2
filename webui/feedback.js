// Feedback controls, trajectory chart and draggable frame. Loaded after app.js.
let curT = 0;

function evalTraj(t) {
  const k = shot.target_trajectory.keyframes;
  for (let i = 0; i < k.length - 1; i++) {
    const a = k[i], b = k[i + 1];
    if (t >= a.time_offset && t <= b.time_offset) {
      const w = (t - a.time_offset) / (b.time_offset - a.time_offset);
      const m = (n) => a.frame_state[n] === null ? null : a.frame_state[n] * (1 - w) + b.frame_state[n] * w;
      return {center_x: m('center_x'), center_y: m('center_y'), subject_height_ratio: m('subject_height_ratio'), distance: m('distance')};
    }
  }
  return k[k.length - 1].frame_state;
}

function renderFbCtl() {
  const el = $('fbCtl');
  if (!shot) { el.innerHTML = '先生成计划'; return; }
  const dur = shot.expected_duration;
  const tabs = script.shots.length > 1 ? '<div class="shots">' + script.shots.map((s) =>
    `<button class="ghost ${s.shot_id === shot.shot_id ? 'active' : ''}" onclick="selectShot('${esc(s.shot_id)}')">${esc(s.shot_id)}</button>`).join('') + '</div>' : '';
  let status;
  if (!session) status = '启动会话中…';
  else if (session.error) status = `<span class="err">会话未启动：${esc(session.error.code)} ${esc(session.error.reason || '')}</span>（Mock 可达性为 UNREACHABLE/UNKNOWN 时会在执行前拒绝）`;
  else status = `会话 ${session.session_id} · 状态 <b id="stState">${session.state.status}</b> · Mock 执行事件 ${session.events.map((e) => e.status).join(' → ')}`;
  el.innerHTML = tabs + `<p>${status}</p>` +
    `<label class="row" style="justify-content:flex-start">轨迹时间 t <input id="tR" type="range" min="0" max="${dur}" step="0.1" value="${curT}"> <span id="tV">${curT.toFixed(1)}</span>s / ${dur}s</label>` +
    `<label class="row" style="justify-content:flex-start;gap:14px">
      修正权限 <select id="cap"><option>ALL</option><option>VISUAL_ONLY</option><option>HORIZONTAL_ONLY</option><option>NONE</option></select>
      修正上限 <input id="maxc" type="number" value="3" min="0" style="width:60px">
      <button class="ghost" onclick="selectShot(shot.shot_id)">重置会话</button></label>` +
    `<label class="row" style="justify-content:flex-start;gap:8px">
      <button class="ghost" onclick="snap(0,0,0)">贴合目标</button>
      <button class="ghost" onclick="snap(0,0.06,-0.08)">偏下+变小</button>
      <button class="ghost" onclick="snap(0.1,0,0)">偏右 0.1</button>
      <button class="ghost" onclick="snap(0.04,0,0)">偏右 0.04（容差内）</button>
      <label><input id="lost" type="checkbox" ${box.lost ? 'checked' : ''}> 主体丢失</label>
      ${shot.target_trajectory.keyframes[0].frame_state.distance !== null ? '距离 <input id="dIn" type="text" style="width:60px" placeholder="空=缺失">' : ''}
    </label>` +
    `<button id="sendBtn" ${session && !session.error ? '' : 'disabled'}>发送当前观测 → Feedback</button>
     <button class="ghost" id="autoBtn" ${session && !session.error ? '' : 'disabled'}>自动跑完整条轨迹（每0.5s，贴合目标）</button>
     <button class="ghost" onclick="pause('pause')">模拟执行器确认暂停</button>
     <button class="ghost" onclick="pause('resume')">确认恢复</button>`;
  $('tR').oninput = (e) => setTime(+e.target.value);
  $('lost').onchange = (e) => { box.lost = e.target.checked; drawFrame(); };
  if (session && !session.error) {
    $('sendBtn').onclick = () => send(curT);
    $('autoBtn').onclick = autoRun;
  }
}

function setTime(t) {
  curT = t;
  if ($('tV')) $('tV').textContent = t.toFixed(1);
  if ($('tR')) $('tR').value = t;
  drawAll();
}

function snap(dx, dy, dh) {
  const e = evalTraj(curT);
  box = {cx: e.center_x + dx, cy: e.center_y + dy, h: Math.max(0.02, e.subject_height_ratio + dh), lost: false};
  if ($('lost')) $('lost').checked = false;
  drawFrame();
}

function bboxOf(b) {
  const cl = (v) => Math.min(1, Math.max(0, v));
  return {x1: cl(b.cx - 0.1), x2: cl(b.cx + 0.1), y1: cl(b.cy - b.h / 2), y2: cl(b.cy + b.h / 2)};
}

async function send(t) {
  const dIn = $('dIn');
  const auto = $('hwAutoStop') && $('hwAutoStop').checked;
  const body = {session_id: session.session_id, t, bbox: box.lost ? null : bboxOf(box),
                distance: dIn && dIn.value.trim() !== '' ? +dIn.value : null,
                auto_stop: !!auto, host: $('hwHost') ? $('hwHost').value : undefined};
  let row;
  try { row = await api('feedback', body); } catch (e) { row = {t, error: e}; }
  appendRow(row);
  // The server performs the physical stop for a PAUSE; this only reports it.
  if (row.physical_stop) {
    appendNote(`PAUSE 已下发停车：${JSON.stringify(row.physical_stop.result)}`);
  } else if (row.decision && row.decision.decision === 'PAUSE') {
    appendNote('PAUSE 未下发停车（未勾选“PAUSE 时自动停车”）');
  }
  return row;
}

function appendNote(text) {
  const box = $('fbLog'); if (!box) return;
  const tr = document.createElement('tr');
  tr.innerHTML = `<td colspan="7" class="muted">${esc(text)}</td>`;
  box.querySelector('tbody').prepend(tr);
}

function appendRow(r) {
  const tr = document.createElement('tr');
  if (r.error) {
    tr.innerHTML = `<td>${r.t}</td><td colspan="6" class="err">${esc(r.error.code)}：${esc(r.error.reason)}</td>`;
  } else {
    const xyz = (s) => s ? `${f2(s.center_x)}, ${f2(s.center_y)}, ${f2(s.subject_height_ratio)}${s.distance !== null ? ', d=' + f2(s.distance) : ''}` : '丢失';
    const d = r.decision;
    const comps = d && d.correction_intent ? d.correction_intent.components.map((c) => `${c.dimension} ${c.error > 0 ? '+' : ''}${c.error.toFixed(3)}`).join('<br>') : '';
    tr.innerHTML = `<td>${r.t}</td><td>${xyz(r.expected)}</td><td>${xyz(r.measured)}</td>` +
      `<td>${r.gate.accepted ? 'ACCEPTED' : '<span class="err">' + esc(r.gate.code) + '</span>'}</td>` +
      `<td>${d ? `<span class="dec ${d.decision}">${d.decision}</span><br><span class="muted">${esc(d.reason)}</span>` : '—'}</td>` +
      `<td>${comps}</td><td>${r.state.status} / ${r.state.correction_count}</td>`;
    if ($('stState')) $('stState').textContent = r.state.status;
  }
  $('fbLog').querySelector('tbody').prepend(tr);
}

async function autoRun() {
  for (let t = 0; t <= shot.expected_duration + 1e-9; t += 0.5) {
    const tt = Math.round(t * 10) / 10;
    setTime(tt); snap(0, 0, 0);
    await send(tt);
  }
}

async function pause(action) {
  try {
    const r = await api('pause', {session_id: session.session_id, action});
    if ($('stState')) $('stState').textContent = r.state.status;
  } catch (e) { alert(e.code + ': ' + e.reason); }
}

function drawAll() { drawChart(); drawFrame(); }

function drawChart() {
  const c = $('chart'), g = c.getContext('2d');
  g.clearRect(0, 0, c.width, c.height);
  if (!shot) return;
  const P = 30, W = c.width - P - 10, H = c.height - P - 10, dur = shot.expected_duration;
  const X = (t) => P + t / dur * W, Y = (v) => 10 + (1 - v) * H;
  g.strokeStyle = '#2a303b'; g.fillStyle = '#8b93a3'; g.font = '11px sans-serif';
  for (let v = 0; v <= 1.001; v += 0.25) { g.beginPath(); g.moveTo(P, Y(v)); g.lineTo(P + W, Y(v)); g.stroke(); g.fillText(v.toFixed(2), 2, Y(v) + 4); }
  g.fillText('0s', P, c.height - 6); g.fillText(dur + 's', P + W - 20, c.height - 6);
  const tol = shot.target_trajectory.tolerance;
  const series = [['center_x', '#4f9dff', tol.center_x_tolerance], ['center_y', '#f0883e', tol.center_y_tolerance],
                  ['subject_height_ratio', '#3fb950', tol.height_ratio_tolerance]];
  const N = 60;
  for (const [key, col, tl] of series) {
    g.fillStyle = col + '33'; g.beginPath();
    for (let i = 0; i <= N; i++) { const t = dur * i / N; g.lineTo(X(t), Y(evalTraj(t)[key] + tl)); }
    for (let i = N; i >= 0; i--) { const t = dur * i / N; g.lineTo(X(t), Y(evalTraj(t)[key] - tl)); }
    g.fill();
    g.strokeStyle = col; g.lineWidth = 2; g.beginPath();
    for (let i = 0; i <= N; i++) { const t = dur * i / N; g.lineTo(X(t), Y(evalTraj(t)[key])); }
    g.stroke();
  }
  g.strokeStyle = '#fff'; g.lineWidth = 1; g.beginPath(); g.moveTo(X(curT), 10); g.lineTo(X(curT), 10 + H); g.stroke();
}

function drawFrame() {
  const c = $('frame'), g = c.getContext('2d'), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  g.strokeStyle = '#2a303b';
  for (let i = 1; i < 3; i++) { g.beginPath(); g.moveTo(W * i / 3, 0); g.lineTo(W * i / 3, H); g.moveTo(0, H * i / 3); g.lineTo(W, H * i / 3); g.stroke(); }
  if (!shot) return;
  const e = evalTraj(curT), tol = shot.target_trajectory.tolerance;
  const rect = (b, col, dash) => { const bb = bboxOf(b); g.setLineDash(dash); g.strokeStyle = col; g.lineWidth = 2;
    g.strokeRect(bb.x1 * W, bb.y1 * H, (bb.x2 - bb.x1) * W, (bb.y2 - bb.y1) * H); g.setLineDash([]); };
  g.fillStyle = '#4f9dff22';
  g.fillRect((e.center_x - tol.center_x_tolerance) * W, (e.center_y - tol.center_y_tolerance) * H, 2 * tol.center_x_tolerance * W, 2 * tol.center_y_tolerance * H);
  rect({cx: e.center_x, cy: e.center_y, h: e.subject_height_ratio}, '#8b93a3', [6, 4]);
  if (!box.lost) {
    rect(box, '#f0883e', []);
    g.fillStyle = '#f0883e'; g.beginPath(); g.arc(box.cx * W, box.cy * H, 3, 0, 7); g.fill();
    g.font = '11px sans-serif'; g.fillText(`x=${box.cx.toFixed(2)} y=${box.cy.toFixed(2)} h=${box.h.toFixed(2)}`, 6, H - 6);
  } else { g.fillStyle = '#f85149'; g.fillText('主体丢失 (bbox=null)', 6, H - 6); }
}

(() => {
  const c = $('frame'); let drag = null;
  const pos = (ev) => { const r = c.getBoundingClientRect(); return [(ev.clientX - r.left) / r.width, (ev.clientY - r.top) / r.height]; };
  c.onmousedown = (ev) => { const [x, y] = pos(ev); drag = [x - box.cx, y - box.cy]; };
  window.addEventListener('mouseup', () => { drag = null; });
  c.onmousemove = (ev) => { if (!drag) return; const [x, y] = pos(ev);
    box.cx = Math.min(1, Math.max(0, x - drag[0])); box.cy = Math.min(1, Math.max(0, y - drag[1])); drawFrame(); };
  c.onwheel = (ev) => { ev.preventDefault(); box.h = Math.min(1, Math.max(0.02, box.h - Math.sign(ev.deltaY) * 0.02)); drawFrame(); };
})();

loadConfig();
drawAll();
