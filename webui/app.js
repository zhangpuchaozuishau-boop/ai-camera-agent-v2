// Agent V2 web test console. Relative URLs only (page is served from the root).
const $ = (id) => document.getElementById(id);
const api = async (name, body) => {
  const res = await fetch('api/' + name, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) throw data.error || data;
  return data;
};
const f2 = (v) => v === null || v === undefined ? '—' : Number(v).toFixed(3);
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));

let script = null, shot = null, session = null, history = [];
let box = {cx: 0.5, cy: 0.5, h: 0.4, lost: false};

async function loadConfig() {
  try {
    const c = await api('config');
    $('model').value = c.model || '';
    $('cfgTag').textContent = `${c.base_url} · ${c.model} · key ${c.api_key_present ? '已配置' : '缺失'}`;
    $('cfgTag').className = 'tag ' + (c.api_key_present ? 'ok' : 'bad');
    $('cfgDump').textContent = '== instructions ==\n' + c.instructions + '\n\n== planner_config ==\n' +
      JSON.stringify(c.planner_config, null, 2) + '\n\n== capability ==\n' + JSON.stringify(c.capability, null, 2) +
      '\n\n== schema ==\n' + JSON.stringify(c.schema, null, 2);
  } catch (e) { $('cfgTag').textContent = '配置加载失败'; }
}

document.querySelectorAll('[data-ex]').forEach((b) => b.onclick = () => { $('text').value = b.dataset.ex; });

$('planBtn').onclick = async () => {
  const body = {text: $('text').value, mode: $('mode').value, model: $('model').value,
                reachability: $('reach').value, allow_distance: $('dist').checked};
  $('planBtn').disabled = true;
  $('planStatus').textContent = body.mode === 'real' ? '正在调用 DeepSeek…' : '离线 fixture…';
  let r;
  try { r = await api('plan', body); } catch (e) { r = {ok: false, error: e}; }
  $('planBtn').disabled = false;
  history.unshift({text: body.text, r});
  renderHistory();
  renderPlan(r);
};

function renderHistory() {
  $('hist').querySelector('tbody').innerHTML = history.map((h, i) =>
    `<tr><td>${history.length - i}</td><td class="${h.r.ok ? '' : 'err'}">${h.r.ok ? 'PASS' : esc(h.r.error?.code || 'ERR')}</td>` +
    `<td>${h.r.total_latency_s ?? '—'}s</td><td>${esc(h.text.slice(0, 26))}</td></tr>`).join('');
}

function callsHtml(r) {
  if (!r.provider_calls || !r.provider_calls.length) return '<p class="muted">无模型调用（离线 fixture）</p>';
  return r.provider_calls.map((c, i) => {
    const u = c.usage ? `tokens in ${c.usage.input_tokens} / out ${c.usage.output_tokens}` : '';
    let raw = c.output_text || c.error || '';
    try { raw = JSON.stringify(JSON.parse(raw), null, 2); } catch (_) {}
    const repair = (() => { try { return JSON.parse(c.input[0].content).repair_error; } catch (_) { return null; } })();
    return `<details><summary>第 ${i + 1} 次调用 · ${c.latency_s}s · ${c.status || 'error'} · ${u}` +
      `${repair ? ' · <b>修复重试</b>' : ''}</summary>` +
      (repair ? `<pre>repair_error: ${esc(JSON.stringify(repair, null, 2))}</pre>` : '') +
      `<pre>${esc(raw)}</pre></details>`;
  }).join('');
}

function renderPlan(r) {
  const head = `<p class="muted">模式 ${r.mode} · 模型 ${esc(r.model || '')} · 总耗时 ${r.total_latency_s}s · 模型调用 ${(r.provider_calls || []).length} 次</p>`;
  if (!r.ok) {
    $('planStatus').innerHTML = `<span class="err">失败：${esc(r.error?.code)}</span>`;
    $('planOut').innerHTML = head + `<p class="err"><b>${esc(r.error?.code)}</b>：${esc(r.error?.reason)}</p>` +
      (r.error?.context && Object.keys(r.error.context).length ? `<pre>${esc(JSON.stringify(r.error.context, null, 2))}</pre>` : '') +
      (r.error?.trace ? `<pre>${esc(r.error.trace)}</pre>` : '') + callsHtml(r);
    script = null; shot = null; session = null; drawAll(); renderFbCtl();
    return;
  }
  $('planStatus').innerHTML = '<span style="color:var(--ok)">PASS：结构校验 + 业务校验通过</span>';
  script = r.script;
  const shots = script.shots.map((s) => {
    const kf = s.target_trajectory.keyframes.map((k) => `<tr><td>${k.time_offset}s</td><td>${f2(k.frame_state.center_x)}</td>` +
      `<td>${f2(k.frame_state.center_y)}</td><td>${f2(k.frame_state.subject_height_ratio)}</td><td>${f2(k.frame_state.distance)}</td></tr>`).join('');
    const t = s.target_trajectory.tolerance;
    return `<p><b>${esc(s.shot_id)}</b> · ${esc(s.shot_goal)}<br><span class="muted">主体动作：${esc(s.subject_action ?? 'null（固定物体）')} · ` +
      `时长 ${s.expected_duration}s · 转场 ${esc(s.transition ?? 'null')} · 容差 x±${t.center_x_tolerance} y±${t.center_y_tolerance} h±${t.height_ratio_tolerance}` +
      `${t.distance_tolerance !== null ? ' d±' + t.distance_tolerance : ''}</span></p>` +
      `<table><thead><tr><th>时间</th><th>center_x</th><th>center_y</th><th>高度占比</th><th>距离</th></tr></thead><tbody>${kf}</tbody></table>`;
  }).join('');
  $('planOut').innerHTML = head + `<p><b>总目标：</b>${esc(script.overall_goal)}</p>` + shots +
    `<details><summary>完整 ShotScript JSON</summary><pre>${esc(JSON.stringify(script, null, 2))}</pre></details>` + callsHtml(r);
  selectShot(script.shots[0].shot_id);
}

async function selectShot(id) {
  shot = script.shots.find((s) => s.shot_id === id);
  session = null;
  $('fbLog').querySelector('tbody').innerHTML = '';
  try {
    session = await api('session', {script, shot_id: id, reachability: $('reach').value,
                                    correction_capability: $('cap')?.value || 'ALL', max_corrections: +($('maxc')?.value || 3)});
  } catch (e) { session = {error: e}; }
  renderFbCtl();
  setTime(0);
}
