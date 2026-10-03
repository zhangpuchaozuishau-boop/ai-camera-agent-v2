// Agent 1 storyboard tab. Reuses api/esc/f2 from app.js and selectShot/drawAll from feedback.js.
const MOVES = {static: '固定', push_in: '推', pull_out: '拉', pan_left: '左摇', pan_right: '右摇', tilt_up: '上仰', tilt_down: '下俯',
  pedestal_up: '升', pedestal_down: '降', truck_left: '左移', truck_right: '右移', arc_left: '左弧移', arc_right: '右弧移',
  orbit_left: '左环绕', orbit_right: '右环绕', follow: '跟', zoom_in: '变焦推', zoom_out: '变焦拉'};
const SB_COLORS = ['#4f9dff', '#f0883e', '#3fb950', '#d2a8ff', '#ff7b72', '#79c0ff', '#e3b341', '#56d4dd'];
const TRANS = {cut: '硬切', dissolve: '叠化', fade_in: '淡入', fade_out: '淡出'};

function sbInit() {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<h2>0. 分镜规划 Agent（用户需求 → 分镜脚本：镜头/时长/景别/机位/运镜）</h2>
    <label class="row" style="justify-content:flex-start;gap:12px">
      镜头上限 <input id="sbMax" type="number" value="8" min="1" max="12" style="width:56px">
      单镜头最长 <input id="sbShotMax" type="number" value="15" min="1" style="width:56px">s
      总时长上限 <input id="sbTotal" type="number" value="60" min="1" style="width:56px">s
    </label>
    <details><summary>允许的运镜（取消勾选 = 机器人不支持；要求不支持的运镜会被拒绝）</summary>
      <div id="sbMoves" style="display:flex;flex-wrap:wrap;gap:8px;margin:6px 0">${Object.entries(MOVES).map(([k, v]) =>
        `<label><input type="checkbox" value="${k}" checked> ${v}<span class="muted"> ${k}</span></label>`).join('')}</div></details>
    <button id="sbBtn" style="width:100%;margin-top:6px">生成分镜脚本</button>
    <div id="sbStatus" class="muted" style="margin-top:6px"></div>`;
  const left = document.querySelector('main > section');
  left.insertBefore(card, left.children[1]);
  const out = document.createElement('div');
  out.className = 'card';
  out.innerHTML = '<h2>分镜脚本输出</h2><div id="sbOut" class="muted">在左侧输入需求后点“生成分镜脚本”</div>';
  $('right').insertBefore(out, $('right').firstChild);
  $('sbBtn').onclick = runStoryboard;
}

async function runStoryboard() {
  const moves = [...document.querySelectorAll('#sbMoves input:checked')].map((i) => i.value);
  const body = {text: $('text').value, mode: $('mode').value, model: $('model').value,
    max_shots: +$('sbMax').value, max_shot_duration: +$('sbShotMax').value, max_total_duration: +$('sbTotal').value,
    allowed_moves: moves.length === Object.keys(MOVES).length ? null : moves};
  $('sbBtn').disabled = true;
  $('sbStatus').textContent = body.mode === 'real' ? '正在调用 DeepSeek 规划分镜…（通常 10–30 秒）' : '离线 fixture…';
  let r;
  try { r = await api('storyboard', body); } catch (e) { r = {ok: false, error: e}; }
  $('sbBtn').disabled = false;
  history.unshift({text: '[分镜] ' + body.text, r});
  renderHistory();
  renderStoryboard(r);
}

function renderStoryboard(r) {
  const head = `<p class="muted">模式 ${r.mode} · 模型 ${esc(r.model || '')} · 耗时 ${r.total_latency_s}s · 模型调用 ${(r.provider_calls || []).length} 次</p>`;
  if (!r.ok) {
    $('sbStatus').innerHTML = `<span class="err">失败：${esc(r.error?.code)}</span>`;
    $('sbOut').innerHTML = head + `<p class="err"><b>${esc(r.error?.code)}</b>：${esc(r.error?.reason)}</p>` +
      (r.error?.context && Object.keys(r.error.context).length ? `<pre>${esc(JSON.stringify(r.error.context, null, 2))}</pre>` : '') +
      callsHtml(r);
    return;
  }
  const sb = r.storyboard;
  $('sbStatus').innerHTML = `<span style="color:var(--ok)">PASS：${sb.shots.length} 个镜头，总长 ${r.total_duration}s</span>`;
  const total = r.total_duration;
  const bar = '<div style="display:flex;height:34px;border-radius:6px;overflow:hidden;margin:8px 0">' + r.timeline.map((t, i) =>
    `<div title="${esc(sb.shots[i].title)}" style="flex:${t.duration};background:${SB_COLORS[i % 8]}55;border-right:2px solid var(--bg);padding:2px 6px;font-size:11px;overflow:hidden;white-space:nowrap">` +
    `${t.index}. ${t.shot_size_zh}·${t.camera_move_zh}<br>${t.start}–${t.end}s</div>`).join('') + '</div>' +
    `<div class="muted" style="display:flex;justify-content:space-between"><span>0s</span><span>${total}s</span></div>`;
  const rows = sb.shots.map((s, i) => {
    const t = r.timeline[i], k = s.target_trajectory.keyframes, a = k[0].frame_state, b = k[k.length - 1].frame_state;
    return `<tr><td><b>${t.index}</b></td><td>${t.start}–${t.end}s<br><span class="muted">${s.duration}s</span></td>` +
      `<td><b>${esc(s.title)}</b><br><span class="muted">${esc(s.purpose)}</span></td>` +
      `<td>${t.shot_size_zh}</td><td>${t.camera_angle_zh}</td><td><b>${t.camera_move_zh}</b><br><span class="muted">${s.camera_move} · ${s.move_speed}</span></td>` +
      `<td>${TRANS[s.transition_in] || s.transition_in}</td><td>${esc(s.frame_description)}${s.subject_action ? '<br><span class="muted">主体：' + esc(s.subject_action) + '</span>' : ''}</td>` +
      `<td class="muted">(${f2(a.center_x)},${f2(a.center_y)}) h${f2(a.subject_height_ratio)}<br>→ (${f2(b.center_x)},${f2(b.center_y)}) h${f2(b.subject_height_ratio)}</td>` +
      `<td class="muted">${esc(t.rig_hint)}</td>` +
      `<td><button class="ghost" onclick="sbDebug('${esc(s.shot_id)}')">调试</button></td></tr>`;
  }).join('');
  const warn = r.warnings.length ? `<p style="color:var(--warn)">⚠ 运镜与画面轨迹不一致：${r.warnings.map((w) => esc(w.shot_id + ' ' + w.reason)).join('；')}</p>` : '';
  $('sbOut').innerHTML = head +
    `<p><b>${esc(sb.title)}</b> · 风格：${esc(sb.style)} · 主体：${esc(sb.subject.name)}（${esc(sb.subject.subject_type)}）</p>` +
    `<p class="muted">${esc(sb.overall_goal)}</p>` + bar + warn +
    `<table><thead><tr><th>#</th><th>时间</th><th>镜头</th><th>景别</th><th>机位</th><th>运镜</th><th>转场</th><th>画面描述</th><th>主体画面位置 起→止</th><th>机构提示(MOCK)</th><th></th></tr></thead><tbody>${rows}</tbody></table>` +
    (sb.director_notes ? `<p><b>导演备注：</b>${esc(sb.director_notes)}</p>` : '') +
    `<details><summary>完整分镜 JSON</summary><pre>${esc(JSON.stringify(sb, null, 2))}</pre></details>` + callsHtml(r);
  window.__sbScript = r.script;
}

function sbDebug(shotId) {
  // Hand the storyboard to the existing trajectory/feedback panels as ShotScript 0.2.
  script = window.__sbScript;
  $('planOut').innerHTML = `<p class="muted">来自分镜脚本（已转换为 ShotScript 0.2）</p><pre>${esc(JSON.stringify(script, null, 2))}</pre>`;
  selectShot(shotId);
  $('fbCtl').scrollIntoView({behavior: 'smooth'});
}

// #last re-renders the newest logged storyboard (also handy for screenshots).
sbInit();
if (location.hash.includes('last')) {
  api('last').then((r) => { renderStoryboard(r); if (r.ok && r.storyboard) sbDebug(r.storyboard.shots[0].shot_id); }).catch(() => {});
}
