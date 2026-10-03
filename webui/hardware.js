// Hardware panel: compile the selected shot into DustCar commands, dry-run or drive it.
const HW = {plan: null, planId: null, poll: null};

function hwInit() {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<h2>6. 真机控制（DustCar ESP32-S3 · HTTP）</h2>
    <label class="row" style="justify-content:flex-start;gap:10px">
      机器人地址 <input id="hwHost" type="text" value="192.168.4.1" style="width:150px">
      <button class="ghost" id="hwPing">连通性检查</button>
      <span id="hwPingOut" class="muted"></span></label>
    <label class="row" style="justify-content:flex-start;gap:10px">
      主体真实高度 <input id="hwHeight" type="number" value="200" min="1" style="width:70px">mm
      底盘速度 <input id="hwSpeed" type="number" value="40" min="10" max="60" style="width:60px">%
      <label><input id="hwAutoStop" type="checkbox" checked> Feedback 判 PAUSE 时自动停车</label></label>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:6px 0">
      <button class="ghost" id="hwPlan">生成硬件动作（只算不发）</button>
      <button id="hwRun" disabled>下发执行（真机！）</button>
      <button class="ghost" id="hwStop" style="border-color:var(--bad);color:var(--bad)">急停 /api/stop + dir=stop</button>
    </div>
    <div id="hwOut" class="muted">先在下面选一个镜头，再“生成硬件动作”。</div>`;
  $('right').appendChild(card);
  $('hwPing').onclick = hwPing;
  $('hwPlan').onclick = hwCompile;
  $('hwRun').onclick = hwRun;
  $('hwStop').onclick = hwStop;
}

async function hwPing() {
  $('hwPingOut').textContent = '查询中…';
  let r;
  try { r = await api('hw/poll', {host: $('hwHost').value}); } catch (e) { r = {ok: false, error: e}; }
  $('hwPingOut').innerHTML = r.ok
    ? `<span style="color:var(--ok)">在线</span> 电量 ${r.status.battery_mv}mV · 模式 ${r.status.mode} · 舵机 ${r.actuator.servos.map((s) => s.id + ':' + s.angle).join(' ')}`
    : `<span class="err">连不上：${esc(r.error?.code)} ${esc(r.error?.reason || '')}</span>`;
}

async function hwCompile() {
  if (!shot || !script) { $('hwOut').innerHTML = '<span class="err">先在第 0 步生成分镜，或点镜头行的“调试”。</span>'; return; }
  $('hwPlan').disabled = true;
  let r;
  try {
    r = await api('hw/plan', {script, shot_id: shot.shot_id, host: $('hwHost').value,
      subject_height_mm: +$('hwHeight').value, speed_pct: +$('hwSpeed').value,
      reachability: $('reach').value});
  } catch (e) { r = {error: e}; }
  $('hwPlan').disabled = false;
  if (!r.ok) {
    $('hwOut').innerHTML = `<p class="err"><b>${esc(r.error.code)}</b>：${esc(r.error.reason)}</p>` +
      (r.error.context ? `<pre>${esc(JSON.stringify(r.error.context, null, 2))}</pre>` : '');
    $('hwRun').disabled = true;
    return;
  }
  HW.plan = r;
  const rows = r.actions.map((a, i) => `<tr><td>${i + 1}</td><td><b>${esc(a.action_name)}</b></td>` +
    `<td><code>${esc(JSON.stringify(a.parameters))}</code></td><td class="muted">${hwHttp(a)}</td></tr>`).join('');
  const notes = r.notes.map((n) => `<tr><td>${n.t0}–${n.t1}s</td><td>h ${n.height_ratio.join(' → ')}</td>` +
    `<td>${n.distance_mm.join(' → ')} mm</td><td>${n.dolly_mm} mm</td><td>${n.pan_deg.join(' → ')}°</td>` +
    `<td>${n.tilt_deg.join(' → ')}°</td><td>${n.dolly_seconds !== undefined ? n.dolly_seconds + 's / ' + n.segment_seconds + 's' +
      (n.timing_ok ? ' ✔' : ' <span class="err">来不及</span>') : '—'}</td></tr>`).join('');
  $('hwOut').innerHTML =
    `<p class="muted">目标 ${esc(r.target)} · registry ${esc(r.registry_revision)} · 机构动作累计 ${r.commanded_seconds}s` +
    `（上限 ${r.max_run_seconds}s ${r.within_run_limit ? '✔' : '✘'}）</p>` +
    `<table><thead><tr><th>#</th><th>动作</th><th>下发参数</th><th>对应固件调用</th></tr></thead><tbody>${rows}</tbody></table>` +
    `<details open><summary>映射计算（画面目标 → 毫米/角度）</summary><table><thead><tr><th>段</th><th>高度占比</th>` +
    `<th>距离</th><th>进退</th><th>水平角</th><th>俯仰角</th><th>耗时</th></tr></thead><tbody>${notes}</tbody></table></details>` +
    `<p class="muted">共 ${r.unverified_constants.length} 个常量仍未标定（verified=false）——</p>` +
    `<details><summary>展开未标定常量</summary><pre>${esc(r.unverified_constants.join('\\n'))}</pre></details>`;
  $('hwRun').disabled = false;
}

function hwHttp(a) {
  const p = a.parameters;
  if (a.action_name === 'servo_set') return `POST /api/servo {"id":${p.id},"angle":${p.angle}}`;
  if (a.action_name === 'chassis_drive') return `GET /api/cmd?dir=${p.dir}&speed=${p.speed} 持续${p.duration_ms}ms（每180ms重发）→ stop`;
  if (a.action_name === 'stepper_run') return `POST /api/motor {"id":${p.id},"dir":${p.dir},"rpm":${p.rpm},"duration_ms":${p.duration_ms}}`;
  if (a.action_name === 'actuator_stop') return `POST /api/stop {"target":"${p.target}","emergency":${p.emergency}}`;
  if (a.action_name === 'chassis_stop') return 'GET /api/cmd?dir=stop';
  return '';
}

async function hwRun() {
  if (!confirm('即将驱动真机：底盘会移动、舵机会转动。确认执行？')) return;
  $('hwRun').disabled = true;
  $('hwOut').innerHTML = '<p>已下发，执行中…</p>';
  let r;
  try {
    r = await api('hw/run', {script, shot_id: shot.shot_id, host: $('hwHost').value, confirm: true,
      subject_height_mm: +$('hwHeight').value, speed_pct: +$('hwSpeed').value, reachability: $('reach').value});
  } catch (e) { r = {error: e}; }
  if (!r.ok) {
    $('hwOut').innerHTML = `<p class="err"><b>${esc(r.error.code)}</b>：${esc(r.error.reason)}</p>`;
    $('hwRun').disabled = false;
    return;
  }
  HW.planId = r.plan_id;
  $('hwOut').innerHTML = `<p>plan ${r.plan_id} → ${esc(r.host)}</p>` + hwActionsTable(r.actions) + '<div id="hwLive">执行中…</div>';
  hwWatch();
}

function hwActionsTable(actions) {
  return '<table><thead><tr><th>#</th><th>动作</th><th>参数</th></tr></thead><tbody>' +
    actions.map((a, i) => `<tr><td>${i + 1}</td><td>${esc(a.action_name)}</td><td><code>${esc(JSON.stringify(a.parameters))}</code></td></tr>`).join('') +
    '</tbody></table>';
}

async function hwWatch() {
  if (!HW.planId) return;
  let r;
  try { r = await api('hw/state', {plan_id: HW.planId}); } catch (e) { return; }
  const el = $('hwLive');
  if (!el) return;
  const events = r.events.map((e) => `<span class="tag ${e.status === 'failed' ? 'bad' : e.status === 'completed' ? 'ok' : ''}">${e.status}</span>`).join(' ');
  const log = r.action_log.map((a) => `<tr><td>${esc(a.action_name)}</td><td>${a.ok ? '✔' : '<span class="err">✘</span> ' + esc(a.error?.code || '')}</td>` +
    `<td><code>${esc(JSON.stringify(a.parameters))}</code></td><td class="muted">${a.response?.keepalives !== undefined ? '保活 ' + a.response.keepalives + ' 次' : ''}</td><td>${a.seconds}s</td></tr>`).join('');
  const reqs = r.requests.map((q) => `<tr><td>${esc(q.method)}</td><td>${esc(q.path)}</td><td><code>${esc(JSON.stringify(q.payload ?? ''))}</code></td><td class="muted">${esc(JSON.stringify(q.response).slice(0, 60))}</td></tr>`).join('');
  el.innerHTML = `<p>${events} ${r.is_running ? '运行中…' : '已结束'} ${r.error ? '<span class="err">' + esc(r.error.code) + '：' + esc(r.error.reason) + '</span>' : ''}</p>` +
    (log ? `<table><thead><tr><th>动作</th><th>结果</th><th>参数</th><th>备注</th><th>耗时</th></tr></thead><tbody>${log}</tbody></table>` : '') +
    (reqs ? `<details><summary>HTTP 请求日志（${r.requests.length} 条，含底盘保活）</summary><table><thead><tr><th>方法</th><th>路径</th><th>载荷</th><th>响应</th></tr></thead><tbody>${reqs}</tbody></table></details>` : '');
  if (r.is_running) setTimeout(hwWatch, 500);
  else $('hwRun').disabled = false;
}

async function hwStop() {
  let r;
  try { r = await api('hw/stop', {host: $('hwHost').value, emergency: false}); } catch (e) { r = {error: e}; }
  $('hwPingOut').innerHTML = r.ok
    ? `<span class="err">已发送急停</span> ${esc(JSON.stringify(r.result))}`
    : `<span class="err">急停失败 ${esc(r.error?.code || r.error?.reason || '')}</span>`;
}

// Feedback PAUSE -> physical stop, if the checkbox is armed.
window.hwAutoStop = async () => {
  if (!$('hwAutoStop') || !$('hwAutoStop').checked) return null;
  try { return await api('hw/stop', {host: $('hwHost').value}); } catch (e) { return {error: e}; }
};

// #hw deep-link: wait for a storyboard/shot, then compile automatically (used for checks).
hwInit();
if (location.hash.includes('hw')) {
  let tries = 0;
  const timer = setInterval(() => {
    if (window.__sbScript && typeof shot !== 'undefined' && shot) { clearInterval(timer); hwCompile(); }
    else if (++tries > 20) clearInterval(timer);
  }, 400);
}
