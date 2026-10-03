// Panel 7: vision closed loop (the app's green box drives the rig).
// Panel 8: calibration form (writes agent_system/hardware/calibration.json).
const VIS = {loop: null, fields: [], busy: false};

const visNum = (v) => (v === null || v === undefined || v === '' ? null : Number(v));
const visFixed = (v, n = 3) => (v === null || v === undefined || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(n));

async function visInit() {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <h2>7. 视觉闭环（App 上报绿框 → Gate → Feedback → 真机修正）</h2>
    <div class="row" style="justify-content:flex-start;flex-wrap:wrap;gap:10px">
      <label class="row" style="gap:6px">镜头 <select id="vShot"></select></label>
      <label class="row" style="gap:6px">主体真实高度 <input id="vHeight" type="number" value="200" style="width:70px">mm</label>
      <label class="row" style="gap:6px">底盘速度 <input id="vSpeed" type="number" value="40" style="width:60px">%</label>
      <label class="row" style="gap:4px"><input id="vDry" type="checkbox" checked>dry-run（只编译，不发真机）</label>
      <label class="row" style="gap:4px"><input id="vConfirm" type="checkbox">确认驱动真机</label>
      <button id="vStart">启动循环</button>
      <button id="vStop" class="ghost">停止 / 急停</button>
      <span id="vState" class="muted">未启动</span>
    </div>
    <div class="grid2" style="margin-top:10px">
      <div>
        <h3 style="margin:0 0 6px;font-size:13px">上报一帧观测（绿色框 左上 / 右下）</h3>
        <div style="display:flex;gap:6px;align-items:center;margin:4px 0">左上 x1 <input id="vx1" style="width:74px"> y1 <input id="vy1" style="width:74px"></div>
        <div style="display:flex;gap:6px;align-items:center;margin:4px 0">右下 x2 <input id="vx2" style="width:74px"> y2 <input id="vy2" style="width:74px"></div>
        <div style="display:flex;gap:6px;align-items:center;margin:4px 0">
          制式 <select id="vSpace"><option value="normalized">归一化 0..1</option><option value="pixel">像素坐标</option></select>
          画幅 <input id="vfw" value="1920" style="width:60px"> × <input id="vfh" value="1080" style="width:60px">
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px">
          <button id="vSend">发送一帧</button>
          <button id="vLost" class="ghost">主体丢失</button>
          <button id="vAuto" class="ghost">连续跟随模拟</button>
        </div>
        <div class="row" style="justify-content:flex-start;gap:6px">
          <label class="row" style="gap:4px">起始偏移 <input id="vOff" value="0.08" style="width:56px"></label>
          <label class="row" style="gap:4px">每帧步长 <input id="vStep" value="0.02" style="width:56px"></label>
          <label class="row" style="gap:4px">帧数 <input id="vFrames" value="4" style="width:44px"></label>
          <label class="row" style="gap:4px">间隔ms <input id="vGap" value="600" style="width:56px"></label>
        </div>
        <p class="muted">框的像素/归一化换算在 Agent 侧做；越界或翻转的框会被拒（不会硬改成合法值）。</p>
      </div>
      <div>
        <canvas id="vCanvas" width="360" height="270"></canvas>
        <p class="muted">灰虚线 = 该时刻计划里的目标框（位置/高度占比）；绿实线 = 上报的框。</p>
      </div>
    </div>
    <pre id="vLog">先在上面生成分镜（第 1 块），再回这里启动循环。</pre>`;
  $('right').appendChild(card);
  $('vStart').onclick = visStart;
  $('vStop').onclick = visStop;
  $('vSend').onclick = () => visFrame(0);
  $('vLost').onclick = visLost;
  $('vAuto').onclick = visAuto;
  $('vShot').onchange = visPrefill;
  visInputs([0.40, 0.45, 0.60, 0.75]);      // 可直接发送的默认框
  visDraw(null, null);
  await visCalLoad();
}

// ------------------------------------------------------------------ 闭环

function visShots() {
  const script = window.__sbScript;
  const sel = $('vShot');
  if (!script || !script.shots || !script.shots.length) return null;
  sel.innerHTML = script.shots.map((s, i) =>
    `<option value="${esc(s.shot_id)}">${i + 1}. ${esc(s.shot_id)} ${esc(s.shot_goal || s.shot_size || '')}</option>`).join('');
  return script;
}

function visTargetKeyframe(shotId) {           // frame_state = center_x / center_y / subject_height_ratio
  const script = window.__sbScript;
  const shot = script && script.shots.find((s) => s.shot_id === shotId);
  const keys = shot && shot.target_trajectory && shot.target_trajectory.keyframes;
  return keys && keys.length ? keys[0].frame_state : null;
}

function visPrefill() {
  const key = visTargetKeyframe($('vShot').value);
  if (!key) return;
  visInputs([key.center_x - 0.1, key.center_y - key.subject_height_ratio / 2,
             key.center_x + 0.1, key.center_y + key.subject_height_ratio / 2]);
}

async function visStart() {
  const script = window.__sbScript;
  if (!script) { $('vState').textContent = '请先生成分镜'; return; }
  const dry = $('vDry').checked;
  if (!dry && !$('vConfirm').checked) { $('vState').textContent = '真机会动：请勾选“确认驱动真机”'; return; }
  $('vState').textContent = '启动中…';
  try {
    const r = await api('hw/loop/start', {
      script, shot_id: $('vShot').value, subject_height_mm: visNum($('vHeight').value),
      speed_pct: visNum($('vSpeed').value), dry_run: dry, confirm: $('vConfirm').checked,
      host: (document.getElementById('hwHost') || {}).value});
    VIS.loop = r.loop_id;
    $('vState').textContent = (r.dry_run ? 'dry-run 循环 ' : '真机循环 ') + r.loop_id.slice(-8);
    visLog(`[启动] ${r.loop_id}${r.dry_run ? '（dry-run）' : ''}\n初始计划：\n` +
      r.plan.map((a) => '  ' + a.action_name + ' ' + JSON.stringify(a.parameters)).join('\n'));
    visPrefill();
  } catch (e) { $('vState').textContent = '启动失败：' + visErr(e); }
}

async function visStop() {
  if (!VIS.loop) return;
  try {
    const r = await api('hw/loop/stop', {loop_id: VIS.loop});
    visLog('\n[停止] ' + JSON.stringify(r.result));
    $('vState').textContent = '已停止';
  } catch (e) { visLog('\n[停止失败] ' + visErr(e)); }
}

function visInputs(set) {
  if (set) {
    $('vx1').value = set[0].toFixed(3); $('vy1').value = set[1].toFixed(3);
    $('vx2').value = set[2].toFixed(3); $('vy2').value = set[3].toFixed(3);
    return null;
  }
  return {x1: Number($('vx1').value), y1: Number($('vy1').value),
          x2: Number($('vx2').value), y2: Number($('vy2').value)};
}

function visBox(offset) {
  const key = visTargetKeyframe($('vShot').value);
  if (!key) return null;
  const cx = key.center_x + offset, cy = key.center_y, h = key.subject_height_ratio;
  return {x1: cx - 0.1, y1: cy - h / 2, x2: cx + 0.1, y2: cy + h / 2};
}

async function visFrame(offset) {
  if (!VIS.loop) { $('vState').textContent = '先启动循环'; return; }
  const b = offset ? visBox(offset) : visInputs();
  if (!b || [b.x1, b.y1, b.x2, b.y2].some((v) => !Number.isFinite(v))) {
    $('vState').textContent = '绿框坐标必须是四个数字'; return;
  }
  const space = $('vSpace').value;
  const payload = {loop_id: VIS.loop, ...b, space, frame_width: visNum($('vfw').value),
                   frame_height: visNum($('vfh').value)};
  if (space === 'pixel') {
    const w = Number($('vfw').value), h = Number($('vfh').value);
    payload.x1 = b.x1 * w; payload.x2 = b.x2 * w; payload.y1 = b.y1 * h; payload.y2 = b.y2 * h;
  }
  try {
    const r = await api('hw/loop/observe', payload);
    visLog(visRow(r));
    visDraw(r.target, r.box);
  } catch (e) { visLog('\n[这一帧被拒] ' + visErr(e)); }
}

async function visLost() {
  if (!VIS.loop) { $('vState').textContent = '先启动循环'; return; }
  try {
    const r = await api('hw/loop/observe', {loop_id: VIS.loop, lost: true});
    visLog(visRow(r));
  } catch (e) { visLog('\n[失败] ' + visErr(e)); }
}

async function visAuto() {
  const off0 = Number($('vOff').value), step = Number($('vStep').value);
  const frames = Number($('vFrames').value), gap = Number($('vGap').value);
  for (let i = 0; i < frames; i++) {
    await visFrame(off0 + step * i);
    await new Promise((res) => setTimeout(res, gap));
  }
}

function visRow(r) {
  const g = r.gate, d = r.decision;
  const tgt = r.target ? `目标 x=${visFixed(r.target.center_x)} y=${visFixed(r.target.center_y)} h=${visFixed(r.target.subject_height_ratio)}` : '';
  const obs = r.box ? `实测 x=${visFixed((r.box.x1 + r.box.x2) / 2)} y=${visFixed((r.box.y1 + r.box.y2) / 2)} h=${visFixed(r.box.y2 - r.box.y1)}` : '实测 —（未收到框）';
  const comp = d && d.correction_intent && d.correction_intent.components
    ? d.correction_intent.components.map((c) => `${c.dimension} err=${visFixed(c.error, 4)}`).join(', ') : '—';
  let out = `\n[t=${visFixed(r.trajectory_time, 2)}s 帧龄=${visFixed(r.age, 3)}s] Gate ${g.accepted ? 'accepted' : g.code + '（丢弃）'}`;
  out += `\n  ${tgt}\n  ${obs}`;
  out += `\n  决策 ${d ? d.decision + (d.reason ? ' / ' + d.reason : '') : '无（本帧未采纳）'}`;
  if (comp !== '—') out += `\n  偏差分量 ${comp}`;
  if (r.corrections && r.corrections.length) {
    out += '\n  下发修正：' + r.corrections.map((c) => {
      if (c.dimension === 'PAN' || c.dimension === 'TILT') {
        return `${c.dimension} ${visFixed(c.delta_deg, 2)}° → 舵机 ${c.commanded_angle}°${c.clamped ? '（已限幅）' : ''}`;
      }
      return `${c.dimension} ${visFixed(c.travel_mm, 1)}mm → ${c.dir} ${c.speed_pct}% ${c.duration_ms}ms${c.clamped ? '（已限幅）' : ''}`;
    }).join('；');
  }
  if (r.deferred) out += `\n  修正被推迟：${r.deferred}（最小间隔未到）`;
  if (r.executed && r.executed.length) {
    out += '\n  实际下发：' + r.executed.map((e) =>
      `\n    ${e.action_name} ${JSON.stringify(e.parameters)} → ${e.ok ? 'ok' : '失败'} `
      + `${JSON.stringify(e.response || {})} (${e.seconds}s)`).join('');
  }
  return out;
}

function visErr(e) {
  if (!e) return '未知错误';
  if (e.code) return `${e.code}: ${e.reason || ''}`;
  if (e.error) return visErr(e.error);
  return String(e.message || e);
}

function visLog(text) {
  const el = $('vLog');
  el.textContent = el.textContent === '先在上面生成分镜（第 1 块），再回这里启动循环。' ? text.trim() : el.textContent + text;
  el.scrollTop = el.scrollHeight;
}

function visDraw(target, box) {
  const c = $('vCanvas'), ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.fillStyle = '#0b0e13';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.strokeStyle = '#2c3340';
  ctx.strokeRect(0.5, 0.5, c.width - 1, c.height - 1);
  const rect = (b, color, dash) => {
    if (!b) return;
    ctx.save();
    ctx.setLineDash(dash ? [5, 4] : []);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(b.x1 * c.width, b.y1 * c.height, (b.x2 - b.x1) * c.width, (b.y2 - b.y1) * c.height);
    ctx.restore();
  };
  if (target) rect({x1: target.center_x - 0.1, x2: target.center_x + 0.1,
                    y1: target.center_y - target.subject_height_ratio / 2,
                    y2: target.center_y + target.subject_height_ratio / 2},
                   '#9aa4b2', true);
  rect(box, '#3fb950', false);
}

// ------------------------------------------------------------- 标定表

async function visCalLoad() {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<h2>8. 标定表（填完点保存 → 写入 calibration.json，自动备份 .bak）</h2>
    <div id="calNote" class="muted">加载中…</div>
    <table id="calTable"><thead><tr><th>常量</th><th>值</th><th>单位</th><th>影响</th></tr></thead><tbody></tbody></table>
    <div class="row" style="justify-content:flex-start;gap:8px;margin-top:8px">
      <button id="calSave">保存已填项</button>
      <button id="calSheet" class="ghost">导出待填清单（Markdown）</button>
      <span id="calOut" class="muted"></span>
    </div>`;
  $('right').appendChild(card);
  $('calSave').onclick = visCalSave;
  $('calSheet').onclick = visCalSheet;
  await visCalReload();
}

async function visCalReload() {
  try {
    const r = await api('hw/config');
    VIS.fields = r.calibration_form || [];
    const body = $('calTable').querySelector('tbody');
    body.innerHTML = VIS.fields.map((f) => `
      <tr>
        <td><code>${esc(f.key)}</code> <span class="tag ${f.measured ? 'ok' : ''}">${f.measured ? '已标定' : '未标定'}</span></td>
        <td><input data-cal="${esc(f.key)}" title="${esc(f.how)}" placeholder="${esc(f.value)}" style="width:96px"></td>
        <td>${esc(f.unit)}</td>
        <td class="muted">${esc(f.effect)}</td>
      </tr>`).join('');
    const done = VIS.fields.filter((f) => f.measured).length;
    $('calNote').innerHTML = `已标定 <b>${done}/${VIS.fields.length}</b> 项；悬停“值”输入框可以看到这一项怎么测。` +
      (done < VIS.fields.length ? ' <span class="tag bad">其余仍是 placeholder，算出来的距离/角度不可信</span>' : ' <span class="tag ok">全部已标定</span>');
  } catch (e) { $('calNote').textContent = '加载失败：' + visErr(e); }
}

async function visCalSave() {
  const values = {};
  document.querySelectorAll('input[data-cal]').forEach((el) => {
    const raw = el.value.trim();
    if (raw === '') return;
    values[el.dataset.cal] = /^[+-]?\d+(\.\d+)?$/.test(raw) ? Number(raw) : raw;
  });
  if (!Object.keys(values).length) { $('calOut').textContent = '没有填任何值'; return; }
  $('calOut').textContent = '保存中…';
  try {
    const r = await api('hw/calibrate', {values});
    $('calOut').innerHTML = r.ok
      ? `<span class="tag ok">已写入 ${Object.keys(r.applied).length} 项</span> 剩余未标定 ${r.still_unverified.length} 项；备份 ${esc(r.backup)}`
      : `<span class="tag bad">部分失败</span> ${esc(JSON.stringify(r.problems))}`;
    await visCalReload();
  } catch (e) { $('calOut').textContent = '保存失败：' + visErr(e); }
}

async function visCalSheet() {
  try {
    const r = await api('hw/calibrate', {action: 'worksheet'});
    const blob = new Blob([r.markdown], {type: 'text/markdown'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'CALIBRATION_WORKSHEET.md';
    a.click();
    $('calOut').textContent = '已导出 ' + r.path;
  } catch (e) { $('calOut').textContent = '导出失败：' + visErr(e); }
}

visInit();
