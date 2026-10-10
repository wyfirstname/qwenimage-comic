/* ============================================================
   漫画工作室 - 前端逻辑（pet 式分步流程）
   剧本 → 角色提取 → 场景 → 分镜 → 出图 → 排版导出
   依赖 app.js 提供的 api / toast / $ / el / els / escapeHtml / API
   ============================================================ */

const S = {
  presets: null,
  defaults: null,
  projects: [],
  project: null,
  characters: [],
  scenes: [],
  panels: [],
  renders: [],
  exports: [],
  stats: {},
  sceneStats: {},
  step: "script",
  llm: null,
  aiTask: null,          // 正在轮询的 AI 任务
  aiTimer: null,
  pollTimer: null,
  jobSource: null,
  timers: {},            // 自动保存防抖
  refInput: null,
  refTargetId: null,
  ready: false,
};

const STEP_ORDER = ["script", "character", "scene", "panel", "draw", "export"];

const PANEL_STATUS_TEXT = {
  draft: "未生成", queued: "排队", running: "生成中", done: "完成", failed: "失败",
};

/* ============================================================
   视图与步骤切换
   ============================================================ */
function switchView(name) {
  els("#mainNav .navitem").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("view-image").classList.toggle("hidden", name !== "image");
  $("view-comic").classList.toggle("hidden", name !== "comic");
  try {
    history.replaceState(null, "", name === "comic" ? `?view=comic&step=${S.step}` : location.pathname);
  } catch (e) { /* 忽略 */ }
  if (name === "comic") initComicOnce();
}

function setStep(name) {
  if (!STEP_ORDER.includes(name)) return;
  S.step = name;
  els("#comicSteps .step").forEach((b) => b.classList.toggle("active", b.dataset.step === name));
  STEP_ORDER.forEach((s) => $("step-" + s).classList.toggle("active", s === name));
}

function gotoStep(name) { setStep(name); }

/* ============================================================
   预设
   ============================================================ */
function fillSelect(sel, items, current) {
  if (!sel) return;
  sel.innerHTML = items
    .map((i) => `<option value="${i.key}"${i.key === current ? " selected" : ""}>${escapeHtml(i.name)}</option>`)
    .join("");
}

/* 画风库：按分组做 <optgroup>，全量 327 条（+「常用」5 个旧画风） */
function fillGroupedStyleSelect(sel, groups, styles, current) {
  if (!sel) return;
  const byGroup = {};
  styles.forEach((s) => {
    const g = s.group || "未分组";
    (byGroup[g] = byGroup[g] || []).push(s);
  });
  const order = (groups && groups.length ? groups.map((g) => g.group) : Object.keys(byGroup));
  let html = "";
  order.forEach((g) => {
    const list = byGroup[g];
    if (!list || !list.length) return;
    html += `<optgroup label="${escapeHtml(g)}">`;
    list.forEach((s) => {
      const label = s.reference ? `${s.key} · ${s.name}（${s.reference}）` : `${s.key} · ${s.name}`;
      html += `<option value="${s.key}"${s.key === current ? " selected" : ""}>${escapeHtml(label)}</option>`;
    });
    html += `</optgroup>`;
  });
  sel.innerHTML = html;
}

/* 生成档位：dev / target / 自定义（空值=用手填的尺寸与步数） */
function fillProfileSelect(sel, profiles, current) {
  if (!sel) return;
  const opts = ['<option value="">自定义（用下方尺寸 / 步数）</option>'];
  (profiles || []).forEach((p) => {
    opts.push(`<option value="${p.key}"${p.key === current ? " selected" : ""}>${escapeHtml(p.name)}</option>`);
  });
  sel.innerHTML = opts.join("");
}

/* 主题色：36 色（handraw），第一项为「不使用」 */
function fillColorSelect(sel, colors, current) {
  if (!sel) return;
  const opts = ['<option value="">（不使用主题色）</option>'];
  (colors || []).forEach((c) => {
    const label = `${c.name} ${c.name_en}${c.quote ? " · " + c.quote : ""}`;
    opts.push(`<option value="${c.key}"${c.key === current ? " selected" : ""}>${escapeHtml(label)}</option>`);
  });
  sel.innerHTML = opts.join("");
}

/* 分镜节奏模板：handraw SB-*（按节奏家族分组） */
function fillRhythmSelect(sel, rhythms, current) {
  if (!sel) return;
  const opts = ['<option value="">（默认节奏：自动交替景别）</option>'];
  const byFam = {};
  (rhythms || []).forEach((r) => {
    const fam = r.family_name || "其他";
    (byFam[fam] = byFam[fam] || []).push(r);
  });
  Object.keys(byFam).forEach((fam) => {
    opts.push(`<optgroup label="${escapeHtml(fam)}">`);
    byFam[fam].forEach((r) => {
      const dg = r.degraded ? "（逐格降级可用）" : "";
      opts.push(`<option value="${r.key}"${r.key === current ? " selected" : ""}>${escapeHtml(r.key + " " + r.name + dg)}</option>`);
    });
    opts.push("</optgroup>");
  });
  sel.innerHTML = opts.join("");
}

function presetName(group, key) {
  const list = (S.presets && S.presets[group]) || [];
  const hit = list.find((x) => x.key === key);
  return hit ? hit.name : (key || "—");
}

function shotOptions(current) {
  return ((S.presets && S.presets.shots) || [])
    .map((s) => `<option value="${s.key}"${s.key === current ? " selected" : ""}>${escapeHtml(s.name)}</option>`)
    .join("");
}

function shotLabel(key) {
  const list = (S.presets && S.presets.shots) || [];
  const hit = list.find((x) => x.key === key);
  return hit ? hit.name : (key || "");
}

async function loadPresets() {
  if (S.presets) return S.presets;
  S.presets = await api("/comic/presets");
  const { styles, style_groups, colors, profiles, layouts, shots, rhythms } = S.presets;
  const defProfile = S.presets.default_profile || "dev";
  fillGroupedStyleSelect($("newProjectStyle"), style_groups, styles, "jp_bw");
  fillGroupedStyleSelect($("setStyle"), style_groups, styles, "jp_bw");
  fillSelect($("newProjectLayout"), layouts, "grid_2x2");
  fillSelect($("setLayout"), layouts, "grid_2x2");
  fillSelect($("renderLayout"), layouts, "grid_2x2");
  fillProfileSelect($("newProjectProfile"), profiles, defProfile);
  fillProfileSelect($("setProfile"), profiles, "");
  fillColorSelect($("newProjectTheme"), colors, "");
  fillColorSelect($("setTheme"), colors, "");
  fillRhythmSelect($("setRhythm"), rhythms, "");
  renderAttr();
  const legend = $("scriptLegend");
  if (legend) {
    legend.innerHTML =
      shots.map((s) => `<code>[${escapeHtml(s.name)}]</code>`).join("") +
      `<code>第 2 页</code><code>「对白」</code><code>[音效：轰隆]</code>`;
  }
  return S.presets;
}

/* 画风库署名（MIT 要求保留原仓库地址） */
function renderAttr() {
  const a = (S.presets && S.presets.attribution) || null;
  const box = $("comicAttr");
  if (!box || !a) return;
  box.innerHTML = `画风库 / 主题色 / 分镜节奏数据来自 ` +
    `<a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(a.source)}</a>` +
    ` v${escapeHtml(a.version)}（MIT）· ${a.styles} 风格 / ${a.colors} 主题色 / ${a.layouts} 排版图型`;
  const sm = $("styleAttr");
  if (sm) sm.innerHTML = `数据来源：<a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(a.source)}</a> v${escapeHtml(a.version)}（MIT）`;
}


/* ============================================================
   画风库浏览器（分组 + 搜索 + 缩略图网格；缩略图缺失时降级为编号块）
   ============================================================ */
const SB = { target: "set", group: "", keyword: "", page: 1, pageSize: 60, total: 0, loading: false };

async function openStyleBrowser(target) {
  SB.target = target || "set";
  SB.group = "";
  SB.keyword = "";
  SB.page = 1;
  $("styleModal").classList.remove("hidden");
  $("styleSearch").value = "";
  renderStyleGroups();
  await loadStyles();
}

function closeStyleBrowser() { $("styleModal").classList.add("hidden"); }

function renderStyleGroups() {
  const box = $("styleGroups");
  if (!box) return;
  const groups = (S.presets && S.presets.style_groups) || [];
  const chips = [{ group: "", count: (S.presets && S.presets.styles || []).length, label: "全部" }]
    .concat(groups.map((g) => ({ group: g.group, count: g.count })));
  box.innerHTML = chips.map((c) => {
    const on = (c.group || "") === SB.group ? " on" : "";
    const label = c.group === "" ? "全部" : shortenGroup(c.group);
    return `<button class="style-chip${on}" data-group="${escapeHtml(c.group)}">${escapeHtml(label)} <b>${c.count}</b></button>`;
  }).join("");
  els(".style-chip", box).forEach((b) => {
    b.onclick = () => { SB.group = b.dataset.group || ""; SB.page = 1; renderStyleGroups(); loadStyles(); };
  });
}

function shortenGroup(g) {
  // "FA 国际社论幽默 / Editorial & Humor Doodle" → "FA 国际社论幽默"
  const s = String(g || "");
  return s.split("/")[0].trim() || s;
}

async function loadStyles() {
  if (SB.loading) return;
  SB.loading = true;
  try {
    const q = new URLSearchParams({
      group: SB.group, keyword: SB.keyword, page: String(SB.page), page_size: String(SB.pageSize),
    });
    const d = await api(`/comic/styles?${q.toString()}`);
    SB.total = d.total || 0;
    if (SB.page === 1) $("styleGrid").innerHTML = "";
    (d.items || []).forEach((it) => $("styleGrid").appendChild(styleCard(it)));
    $("styleModalMeta").textContent = `共 ${d.total} 个${SB.group ? "（" + shortenGroup(SB.group) + "）" : ""}`;
    $("styleMore").innerHTML = (SB.page * SB.pageSize < SB.total)
      ? `<button class="link-btn" id="styleMoreBtn">加载更多（已显示 ${$("styleGrid").children.length}/${d.total}）</button>`
      : `已全部显示`;
    const mb = $("styleMoreBtn");
    if (mb) mb.onclick = () => { SB.page += 1; loadStyles(); };
  } catch (e) {
    toast("画风库加载失败：" + e.message);
  } finally {
    SB.loading = false;
  }
}

function styleCard(it) {
  const node = document.createElement("div");
  node.className = "style-card";
  node.dataset.key = it.key;
  const thumb = it.thumb
    ? `<img src="${it.thumb}" alt="" loading="lazy" />`
    : `<div class="style-noimg">${escapeHtml((it.key || "").split("-")[0])}<br/>${escapeHtml((it.key || "").split("-")[1] || "")}</div>`;
  node.innerHTML = `
    <div class="style-thumb">${thumb}</div>
    <div class="style-meta">
      <div class="style-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</div>
      <div class="style-ref">${escapeHtml(it.key)}${it.reference ? " · " + escapeHtml(it.reference) : ""}</div>
    </div>`;
  node.onclick = () => pickStyle(it.key);
  return node;
}

function pickStyle(key) {
  const selId = SB.target === "new" ? "newProjectStyle" : "setStyle";
  const sel = $(selId);
  if (sel) {
    if (!Array.from(sel.options).some((o) => o.value === key)) {
      // 该画风不在下拉里（不该发生，兜底补一个）
      const opt = document.createElement("option");
      opt.value = key; opt.textContent = key;
      sel.appendChild(opt);
    }
    sel.value = key;
  }
  closeStyleBrowser();
  if (SB.target === "set" && S.project) {
    saveProjectSettings(true).then(() => toast(`画风已切换为 ${key}`));
  } else {
    toast(`画风已选：${key}`);
  }
}


async function loadLlmStatus() {
  try {
    const st = await api("/comic/llm/status");
    S.llm = st;
    const badge = $("llmBadge");
    badge.textContent = st.available ? `本地文本模型就绪 · ${st.model}` : "本地文本模型未就绪";
    badge.className = "badge " + (st.available ? "badge-local" : "badge-mock");
    $("llmHint").textContent = st.available
      ? `复用 Qwen-Image 自带文本编码器（${st.model_gb}GB，${st.device}），不联网。${st.speed_note || ""}`
      : (st.reason || "不可用");
  } catch (e) {
    $("llmBadge").textContent = "文本模型状态获取失败";
  }
}

/* ============================================================
   项目（卡片式）
   ============================================================ */
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(String(iso).replace(" ", "T"));
  if (isNaN(d.getTime())) return String(iso).slice(5, 16);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function projectCard(p) {
  const card = document.createElement("div");
  card.className = "project-card" + (S.project && S.project.id === p.id ? " current" : "");
  card.dataset.id = p.id;
  const total = p.panel_count || 0;
  const done = p.done_count || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const isEmpty = !total && !(p.char_count || 0) && !(p.synopsis_brief || "").trim();
  if (isEmpty) card.classList.add("placeholder-project");
  card.innerHTML = `
    <div class="pc-top">
      <span class="pc-name">${escapeHtml(p.title || "未命名")}</span>
      ${S.project && S.project.id === p.id ? '<span class="pc-current-tag">当前</span>' : ""}
    </div>
    <div class="pc-brief">${escapeHtml(p.synopsis_brief || "（还没有剧本）")}</div>
    <div class="pc-tags">
      <span>${escapeHtml(presetName("styles", p.style))}</span>
      <span>${escapeHtml(presetName("layouts", p.layout))}</span>
      <span>${p.width}×${p.height}</span>
    </div>
    <div class="pc-bar"><i style="width:${pct}%"></i></div>
    <div class="pc-foot">
      <span>分镜 ${done}/${total} · 对白 ${p.dialogue_count || 0} · 角色 ${p.char_ready || 0}/${p.char_count || 0}</span>
      <span>${escapeHtml(fmtTime(p.updated_at))}</span>
    </div>
    <button class="icon-btn pc-del" title="删除项目">✕</button>`;
  card.onclick = (e) => {
    if (e.target.closest(".pc-del")) return;
    selectProject(p.id);
  };
  el(".pc-del", card).onclick = (e) => { e.stopPropagation(); deleteProject(p.id, p.title); };
  return card;
}

function renderProjectCards() {
  const box = $("projectCards");
  $("projectCount").textContent = S.projects.length;
  $("projectBoardMeta").textContent = S.projects.length
    ? `共 ${S.projects.length} 个，点卡片切换；卡片上的进度条是分镜完成度`
    : "还没有项目，点「＋ 新建项目」开始";
  box.innerHTML = "";
  if (!S.projects.length) {
    box.innerHTML = `<p class="muted" style="margin:0">还没有任何漫画项目。</p>`;
    return;
  }
  S.projects.forEach((p) => box.appendChild(projectCard(p)));
}

async function loadProjects(keepCurrent = true) {
  const data = await api("/comic/projects");
  S.projects = data.items || [];
  if (!S.projects.length) {
    S.project = null;
    renderProjectCards();
    applyDetail({ project: null, characters: [], scenes: [], panels: [], renders: [], stats: {}, scene_stats: {} });
    return;
  }
  const cur = keepCurrent && S.project ? S.projects.find((p) => p.id === S.project.id) : null;
  const target = cur || S.projects[0];
  renderProjectCards();
  await selectProject(target.id);
}

async function selectProject(id) {
  if (!id) return;
  try {
    const d = await api(`/comic/projects/${id}`);
    applyDetail(d);
    renderProjectCards();
  } catch (e) { toast("载入失败：" + e.message); }
}

async function createProject() {
  const title = $("newProjectTitle").value.trim() || "未命名漫画";
  const body = {
    title,
    style: $("newProjectStyle").value,
    layout: $("newProjectLayout").value,
    profile: $("newProjectProfile").value || "",
    theme_color: $("newProjectTheme").value || "",
    // 不传尺寸/步数：由后端按档位（dev/target）决定；选了「自定义」则用 .env 默认值
  };
  try {
    const p = await api("/comic/projects", { method: "POST", body: JSON.stringify(body) });
    $("newProjectTitle").value = "";
    $("newProjectForm").classList.add("hidden");
    S.project = p;
    await loadProjects(false);
    toast("项目已创建");
  } catch (e) { toast("创建失败：" + e.message); }
}

async function deleteProject(id, title) {
  if (!confirm(`删除项目「${title || id}」？该项目的角色、分镜、成品页都会一并删除，不可恢复。`)) return;
  try {
    await api(`/comic/projects/${id}`, { method: "DELETE" });
    if (S.project && S.project.id === id) S.project = null;
    await loadProjects(false);
    toast("项目已删除");
  } catch (e) { toast("删除失败：" + e.message); }
}

function toggleProjectBoard(force) {
  const board = $("projectBoard");
  const open = force === undefined ? board.classList.contains("collapsed") : force;
  board.classList.toggle("collapsed", !open);
  $("btnCollapseBoard").textContent = open ? "收起" : "展开";
}

function applyDetail(d) {
  S.project = d.project || null;
  S.characters = d.characters || [];
  S.scenes = d.scenes || [];
  S.panels = d.panels || [];
  S.renders = d.renders || [];
  S.exports = d.exports || [];
  S.stats = d.stats || {};
  S.sceneStats = d.scene_stats || {};

  if (!S.project) {
    $("curMeta").textContent = "还没有项目，点「＋ 新建项目」开始";
    $("curProjectTitle").textContent = "未选择项目";
  } else {
    $("curProjectTitle").textContent = S.project.title || "未命名";
    const prof = profileInfo(S.project.profile);
    const theme = ((S.presets && S.presets.colors) || []).find((c) => c.key === S.project.theme_color);
    $("curMeta").textContent =
      `${presetName("styles", S.project.style)} · ${presetName("layouts", S.project.layout)} · ` +
      `单格 ${S.project.width}×${S.project.height} / ${S.project.steps} 步` +
      (prof ? ` · ${prof.name}` : "") +
      (theme ? ` · 主题色 ${theme.name}` : "");
  }
  fillSettingsForm();
  renderBadges();
  renderScriptStep();
  if (!isEditing("#charList")) renderCharacters();
  if (!isEditing("#sceneList")) renderScenes();
  syncPanelList();
  renderDrawGrid();
  renderRenders();
  renderExports();
  updateComicEstimate();
  renderProjectCards();
}

function isEditing(selector) {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest(selector));
}

function renderBadges() {
  const s = S.stats || {};
  const lines = S.project && S.project.synopsis ? S.project.synopsis.split("\n").filter(Boolean).length : 0;
  $("badgeScript").textContent = lines ? lines + " 行" : "—";
  $("badgeCharacter").textContent = S.characters.length;
  $("badgeScene").textContent = `${(S.sceneStats && S.sceneStats.done) || 0}/${S.scenes.length}`;
  $("badgePanel").textContent = S.panels.length;
  $("badgeDraw").textContent = `${s.done || 0}/${s.total || 0}`;
  $("badgeExport").textContent = S.renders.length;
}

function fillSettingsForm() {
  const p = S.project;
  if (!p) return;
  if (isEditing("#projectSettings") || isEditing("#scriptText")) return;
  $("setStyle").value = p.style;
  $("setLayout").value = p.layout;
  if ($("setProfile")) $("setProfile").value = p.profile || "";
  if ($("setBatch")) $("setBatch").value = String(p.batch || 1);
  if ($("setTheme")) $("setTheme").value = p.theme_color || "";
  if ($("setRhythm")) $("setRhythm").value = p.rhythm_template || "";
  setSizeSelect(p.width, p.height);
  setStepsSelect(p.steps);
  updateProfileHint(p.profile);
  $("setGuidance").value = String(p.guidance);
  $("setGuidanceVal").textContent = Number(p.guidance).toFixed(1);
  const rs = Number(p.ref_strength || 0.55);
  $("setRefStrength").value = String(rs);
  $("setRefStrengthVal").textContent = rs.toFixed(2);
  $("setKeepStyle").checked = !!p.keep_style;
  $("setUseRef").checked = !!p.use_ref;
  $("setNegative").value = p.negative || "";
  $("setCharNegative").value = p.char_negative || "";
  $("renderLayout").value = p.layout;
}

/* 尺寸 / 步数下拉：项目值不在候选里时动态补一个选项，避免下拉显示错位 */
function setSizeSelect(w, h) {
  const sel = $("setSize");
  const val = `${w}x${h}`;
  if (sel && !Array.from(sel.options).some((o) => o.value === val)) {
    const opt = document.createElement("option");
    opt.value = val;
    opt.textContent = `${w}×${h}`;
    sel.appendChild(opt);
  }
  sel.value = val;
}

function setStepsSelect(steps) {
  const sel = $("setSteps");
  const val = String(steps);
  if (sel && !Array.from(sel.options).some((o) => o.value === val)) {
    const opt = document.createElement("option");
    opt.value = val;
    opt.textContent = `${val} 步`;
    sel.appendChild(opt);
  }
  sel.value = val;
}

function profileInfo(key) {
  const list = (S.presets && S.presets.profiles) || [];
  return list.find((x) => x.key === key) || null;
}

function updateProfileHint(key) {
  const hint = $("profileHint");
  if (!hint) return;
  const p = profileInfo(key);
  if (p) {
    hint.textContent = `${p.name}：${p.desc}（${p.width}×${p.height} / ${p.steps} 步）`;
  } else {
    hint.textContent = "自定义档：分辨率 / 步数 / 引导系数按下方手填值生效（本机 6GB 建议 768 / 8 步）。";
  }
}

/* 恢复默认反向提示词（与后端 comic_defaults 保持一致，由接口下发） */
async function resetNegative() {
  if (!S.project) return toast("请先选择或新建项目");
  if (!S.defaults) {
    try { S.defaults = await api("/comic/defaults"); }
    catch (e) { return toast("默认值获取失败：" + e.message); }
  }
  $("setNegative").value = S.defaults.project_negative || "";
  $("setCharNegative").value = S.defaults.char_negative || "";
  await saveProjectSettings(true);
  toast("已恢复默认反向提示词");
}

function renderScriptStep() {
  const p = S.project;
  if (!p) return;
  if (!isEditing("#scriptText")) $("scriptText").value = p.synopsis || "";
  updateScriptEstimate();
}

async function saveProjectSettings(silent = false) {
  if (!S.project) return toast("请先选择或新建项目");
  const [w, h] = $("setSize").value.split("x").map(Number);
  const body = {
    style: $("setStyle").value,
    layout: $("setLayout").value,
    profile: $("setProfile") ? ($("setProfile").value || "") : "",
    theme_color: $("setTheme") ? ($("setTheme").value || "") : "",
    rhythm_template: $("setRhythm") ? ($("setRhythm").value || "") : "",
    batch: $("setBatch") ? Number($("setBatch").value || 1) : 1,
    width: w, height: h,
    steps: Number($("setSteps").value),
    guidance: Number($("setGuidance").value),
    ref_strength: Number($("setRefStrength").value),
    keep_style: $("setKeepStyle").checked,
    use_ref: $("setUseRef").checked,
    negative: $("setNegative").value,
    char_negative: $("setCharNegative").value,
  };
  try {
    await api(`/comic/projects/${S.project.id}`, { method: "PATCH", body: JSON.stringify(body) });
    await refreshDetail();
    if (!silent) toast("设置已保存");
  } catch (e) { toast("保存失败：" + e.message); }
}

async function refreshDetail() {
  if (!S.project) return null;
  const d = await api(`/comic/projects/${S.project.id}`);
  applyDetail(d);
  await loadProjects(true);
  return d;
}

/* ============================================================
   剧本
   ============================================================ */
async function saveScriptText() {
  if (!S.project) return;
  const text = $("scriptText").value;
  if (text === (S.project.synopsis || "")) return;
  try {
    await api(`/comic/projects/${S.project.id}`, {
      method: "PATCH", body: JSON.stringify({ synopsis: text }),
    });
    S.project.synopsis = text;
    renderBadges();
  } catch (e) { /* 静默，避免打断输入 */ }
}

async function previewScript() {
  if (!S.project) return toast("请先选择或新建项目");
  const text = $("scriptText").value;
  if (!text.trim()) return toast("请先写或粘贴剧本");
  try {
    const data = await api(`/comic/projects/${S.project.id}/script/preview`, {
      method: "POST", body: JSON.stringify({ text }),
    });
    const preview = data.items.slice(0, 3)
      .map((i, n) => `${n + 1}.[${shotLabel(i.shot)}] ${i.scene || i.dialogue}`).join(" ／ ");
    $("scriptHint").textContent = `解析出 ${data.count} 格`;
    toast(preview ? preview.slice(0, 70) + (data.count > 3 ? " …" : "") : `解析出 ${data.count} 格`, 5200);
  } catch (e) { toast("解析失败：" + e.message); }
}

async function ruleParseToPanels() {
  if (!S.project) return toast("请先选择或新建项目");
  await saveScriptText();
  const text = $("scriptText").value.trim();
  if (!text) return toast("请先写或粘贴剧本");
  try {
    const data = await api(`/comic/projects/${S.project.id}/script`, {
      method: "POST", body: JSON.stringify({ text, replace: true }),
    });
    $("scriptHint").textContent = `规则拆格：${data.created} 格`;
    await refreshDetail();
    gotoStep("panel");
    toast(`已用规则拆出 ${data.created} 格分镜`);
  } catch (e) { toast("拆格失败：" + e.message); }
}

/* ============================================================
   本地 AI 任务（长耗时，后台跑 + 轮询）
   ============================================================ */
const AI_SUCCESS = {
  script_generate: "剧本已生成",
  script_continue: "续写完成",
  script_polish: "剧本已润色",
  characters_extract: "角色提取完成",
  scenes_extract: "场景提取完成",
  storyboard: "智能分镜完成",
};

async function runAiTask(path, body, label, onDone) {
  if (!S.project) return toast("请先选择或新建项目");
  if (S.aiTask) return toast("已有 AI 任务在跑，请等它结束或取消");
  setComicProgress(4, `${label} 提交中…`);
  $("btnCancelAi").classList.remove("hidden");
  try {
    const task = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
    S.aiTask = task;
    toast(`${label} 已开始：本地 CPU 推理较慢，短任务约 1 分钟，长任务可能 5-10 分钟`, 6000);
    pollAiTask(onDone);
  } catch (e) {
    $("btnCancelAi").classList.add("hidden");
    setComicProgress(0, "失败");
    toast(`${label} 提交失败：${e.message}`);
  }
}

function pollAiTask(onDone) {
  clearTimeout(S.aiTimer);
  S.aiTimer = setTimeout(async () => {
    if (!S.aiTask) return;
    try {
      const t = await api(`/comic/ai/tasks/${S.aiTask.id}`);
      S.aiTask = t;
      const secs = Math.round(t.elapsed || 0);
      setComicProgress(
        t.status === "running" ? 45 : 10,
        `${t.label}：${t.message || ""}${secs ? `（${secs}s）` : ""}`
      );
      if (t.status === "running" || t.status === "queued") return pollAiTask(onDone);
      S.aiTask = null;
      $("btnCancelAi").classList.add("hidden");
      if (t.status === "done") {
        setComicProgress(100, `${t.label} 完成（${Math.round(t.elapsed)}s）`);
        toast(aiSuccessMessage(t), 7000);
        await refreshDetail();
        if (onDone) onDone(t);
      } else {
        setComicProgress(0, `${t.label} 失败`);
        toast((t.error || "任务失败").slice(0, 220), 8000);
      }
    } catch (e) {
      S.aiTask = null;
      $("btnCancelAi").classList.add("hidden");
      setComicProgress(0, "任务轮询失败");
      toast("任务状态获取失败：" + e.message);
    }
  }, 2000);
}

function aiSuccessMessage(t) {
  const r = t.result || {};
  let msg = AI_SUCCESS[t.kind] || "完成";
  if (r.appended_pages) {
    msg += `：本次新增 ${r.appended_pages} 页，现共 ${r.total_pages} 页（${r.total_pages * 4} 格）`;
  }
  if (r.created !== undefined) {
    msg += `：${r.created} 格`;
    if (r.dialogue_cells !== undefined) msg += `，其中 ${r.dialogue_cells} 格带对白`;
    if (r.dialogue_recovered) msg += `（自动补回 ${r.dialogue_recovered} 条漏掉的对白）`;
  }
  return msg;
}

async function cancelAiTask() {
  if (!S.aiTask) return;
  try {
    await api(`/comic/ai/tasks/${S.aiTask.id}/cancel`, { method: "POST" });
    toast("已请求取消");
  } catch (e) { toast(e.message); }
  S.aiTask = null;
  clearTimeout(S.aiTimer);
  $("btnCancelAi").classList.add("hidden");
  setComicProgress(0, "已取消");
}

function aiScript() {
  const idea = $("scriptIdea").value.trim();
  const pages = Number($("scriptPages").value);
  const mins = estimateScriptMinutes(pages);
  if (pages > 4 && !confirm(
    `将分幕连续生成 ${pages} 页（每幕 4 页，共 ${Math.ceil(pages / 4)} 幕）。\n` +
    `本机 CPU 文本推理较慢，预计需要约 ${mins} 分钟，期间请不要关闭页面。\n\n继续吗？`)) return;
  runAiTask(
    `/comic/projects/${S.project ? S.project.id : ""}/script/generate`,
    { idea, pages },
    "AI 写剧本",
    () => gotoStep("script")
  );
}

function aiContinue() {
  if (!S.project) return toast("请先选择或新建项目");
  if (!(S.project.synopsis || "").trim()) return toast("还没有剧本，先用「AI 写剧本」生成第一幕");
  const pages = Number($("scriptPages").value);
  const mins = estimateScriptMinutes(pages);
  if (!confirm(
    `在现有剧本后面续写 ${pages} 页（${pages * 4} 格），预计约 ${mins} 分钟。\n\n继续吗？`)) return;
  runAiTask(
    `/comic/projects/${S.project.id}/script/continue`,
    { idea: $("scriptIdea").value.trim(), pages },
    "AI 续写剧本",
    () => gotoStep("script")
  );
}

/* 粗估：每格约 30 字，CPU 文本推理约 2 秒/字 */
function estimateScriptMinutes(pages) {
  const cells = Math.max(1, Number(pages) || 1) * 4;
  return Math.max(1, Math.round((cells * 30 * 2) / 60));
}

function updateScriptEstimate() {
  const pages = Number($("scriptPages").value) || 1;
  const chunks = Math.ceil(pages / 4);
  const tip = pages > 4
    ? `将分 ${chunks} 幕连续生成，预计约 ${estimateScriptMinutes(pages)} 分钟`
    : `单幕生成，预计约 ${estimateScriptMinutes(pages)} 分钟`;
  $("scriptEstimate").textContent = `${pages} 页（${pages * 4} 格）：${tip}`;
}

function aiPolish() {
  runAiTask(
    `/comic/projects/${S.project ? S.project.id : ""}/script/polish`,
    { text: $("scriptText").value },
    "AI 润色剧本"
  );
}

function aiCharacters() {
  runAiTask(
    `/comic/projects/${S.project ? S.project.id : ""}/characters/extract`,
    { script: $("scriptText").value, count: Number($("charCount").value), replace: true },
    "AI 提取角色",
    () => gotoStep("character")
  );
}

function aiScenes() {
  runAiTask(
    `/comic/projects/${S.project ? S.project.id : ""}/scenes/extract`,
    { script: $("scriptText").value, count: Number($("sceneCount").value), replace: true },
    "AI 提取场景",
    () => gotoStep("scene")
  );
}

function aiStoryboard() {
  runAiTask(
    `/comic/projects/${S.project ? S.project.id : ""}/storyboard`,
    { text: $("scriptText").value, replace: true },
    "AI 智能分镜",
    () => gotoStep("panel")
  );
}

/* ============================================================
   角色
   ============================================================ */
function renderCharacters() {
  const box = $("charList");
  const ready = S.characters.filter((c) => c.ref_url).length;
  $("charMeta").textContent = S.characters.length
    ? `共 ${S.characters.length} 个角色，${ready} 个已有形象图；形象图为半身立绘，出图时按`
      + `「形象锁定强度」自动保持一致`
    : "从剧本里自动提取人物，再用本地模型生成半身立绘（无需上传任何参考图）";
  if (!S.characters.length) {
    box.innerHTML = `<div class="empty"><div class="empty-icon">◈</div>
      <p>还没有角色</p><p class="muted">点「AI 从剧本提取角色」，或手动添加</p></div>`;
    return;
  }
  box.innerHTML = "";
  S.characters.forEach((c) => box.appendChild(characterCard(c)));
}

function charStatusText(c) {
  const st = c.status || "draft";
  if (st === "queued") return "已排队，等待出图";
  if (st === "running") return "本地模型生成中…";
  if (st === "failed") return c.error || "生成失败";
  if (c.ref_url) {
    return c.ref_source === "upload"
      ? "已使用你上传的图"
      : "形象图已就绪 · 分镜自动沿用";
  }
  return "点上方按钮，本地模型自动画半身立绘";
}

function characterCard(c) {
  const card = document.createElement("div");
  card.className = "char-card";
  card.dataset.id = c.id;
  const st = c.status || "draft";
  const thumb = c.ref_url
    ? `<img src="${c.ref_url}" alt="" loading="lazy" />`
    : `<span>形象图<br/>待生成</span>`;
  card.innerHTML = `
    <div class="cc-head">
      <input type="text" class="cc-name" value="${escapeHtml(c.name)}" placeholder="角色名" />
      <span class="tag tag-draft">${escapeHtml(c.role || "配角")}</span>
      <span class="tag tag-${sceneTagClass(st)}">${PANEL_STATUS_TEXT[st] || st}</span>
      <span class="spacer"></span>
      <button class="icon-btn cc-prompt" title="查看形象图提示词">词</button>
      <button class="icon-btn cc-del" title="删除角色">✕</button>
    </div>
    <div class="cc-body">
      <div class="cc-side">
        <div class="cc-ref${c.ref_url ? " has" : ""}" title="角色形象图，由本地模型生成">${thumb}</div>
        <button class="btn btn-secondary cc-gen">${c.ref_url ? "重绘形象图" : "✦ 生成形象图"}</button>
        <button class="link-btn cc-upload">上传替换</button>
        <p class="cc-status muted">${escapeHtml(charStatusText(c))}</p>
      </div>
      <div class="cc-fields">
        <div class="grid-2">
          <input type="text" class="cc-gender" placeholder="性别" value="${escapeHtml(c.gender || "")}" />
          <input type="text" class="cc-age" placeholder="年龄" value="${escapeHtml(c.age || "")}" />
        </div>
        <input type="text" class="cc-personality" placeholder="性格：冷静、嘴硬心软" value="${escapeHtml(c.personality || "")}" />
        <textarea class="cc-appearance" rows="2" placeholder="外貌：银发红瞳、左眉有疤">${escapeHtml(c.appearance || "")}</textarea>
        <input type="text" class="cc-outfit" placeholder="服装：黑色风衣、红围巾" value="${escapeHtml(c.outfit || "")}" />
        <textarea class="cc-detail" rows="2" placeholder="英文绘图细节（AI 提取时自动生成）">${escapeHtml(c.detail_prompt || "")}</textarea>
        <textarea class="cc-negative" rows="2" placeholder="该角色额外反提示词（可留空，默认已压制畸形/多指/怪表情）">${escapeHtml(c.negative || "")}</textarea>
      </div>
    </div>`;

  const save = (patch) => {
    clearTimeout(card._t);
    card._t = setTimeout(async () => {
      try { await api(`/comic/characters/${c.id}`, { method: "PATCH", body: JSON.stringify(patch) }); }
      catch (e) { toast("保存失败：" + e.message); }
    }, 650);
  };
  const bind = (sel, field) => {
    const node = el(sel, card);
    if (node) node.oninput = (e) => { c[field] = e.target.value; save({ [field]: e.target.value }); };
  };
  bind(".cc-name", "name");
  bind(".cc-gender", "gender");
  bind(".cc-age", "age");
  bind(".cc-personality", "personality");
  bind(".cc-appearance", "appearance");
  bind(".cc-outfit", "outfit");
  bind(".cc-detail", "detail_prompt");
  bind(".cc-negative", "negative");

  const img = el("img", card);
  if (img) img.onclick = () => window.open(img.src, "_blank");
  el(".cc-del", card).onclick = () => deleteCharacter(c.id);
  el(".cc-gen", card).onclick = (e) => generateCharacter(c.id, e.target);
  el(".cc-upload", card).onclick = () => pickCharRef(c.id);
  el(".cc-prompt", card).onclick = () => showCharacterPrompt(c.id);
  return card;
}

async function generateCharacter(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "提交中…"; }
  try {
    const res = await api(`/comic/characters/${id}/generate`, { method: "POST" });
    toast("形象图已加入队列");
    subscribeComicJob(res.job_id);
    startPolling();
  } catch (e) { toast("提交失败：" + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "✦ 生成形象图"; } }
}

async function drawCharacters() {
  if (!S.project) return toast("请先选择或新建项目");
  if (!S.characters.length) return toast("还没有角色");
  try {
    const res = await api(`/comic/projects/${S.project.id}/characters/generate`, {
      method: "POST", body: JSON.stringify({ only_missing: true }),
    });
    toast(res.message);
    startPolling();
    await refreshDetail();
  } catch (e) { toast("提交失败：" + e.message); }
}

async function showCharacterPrompt(id) {
  const modal = $("promptModal");
  modal.classList.remove("hidden");
  $("promptModalBody").innerHTML = "加载中…";
  try {
    const d = await api(`/comic/characters/${id}/prompt`);
    $("promptModalBody").innerHTML =
      `<p class="muted">${escapeHtml(d.name)} · 形象图提示词</p>
       <h4>正向</h4><pre>${escapeHtml(d.prompt)}</pre>
       <h4>负向</h4><pre>${escapeHtml(d.negative_prompt)}</pre>`;
  } catch (e) { $("promptModalBody").textContent = "加载失败：" + e.message; }
}

async function addCharacter() {
  if (!S.project) return toast("请先选择或新建项目");
  try {
    await api(`/comic/projects/${S.project.id}/characters`, {
      method: "POST", body: JSON.stringify({ name: "新角色", role: "配角" }),
    });
    await refreshDetail();
  } catch (e) { toast(e.message); }
}

async function deleteCharacter(id) {
  if (!confirm("删除该角色？分镜中的勾选会一并移除。")) return;
  try {
    await api(`/comic/characters/${id}`, { method: "DELETE" });
    await refreshDetail();
    toast("角色已删除");
  } catch (e) { toast(e.message); }
}

function pickCharRef(charId) {
  S.refTargetId = charId;
  if (!S.refInput) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.hidden = true;
    input.onchange = async (e) => {
      const file = e.target.files[0];
      input.value = "";
      if (!file || !S.refTargetId) return;
      const dataUrl = await new Promise((res) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.readAsDataURL(file);
      });
      try {
        await api(`/comic/characters/${S.refTargetId}/ref`, {
          method: "POST", body: JSON.stringify({ image: dataUrl }),
        });
        await refreshDetail();
        toast("已用你的图替换形象图，之后出图会用它保持形象");
      } catch (err) { toast("上传失败：" + err.message); }
    };
    document.body.appendChild(input);
    S.refInput = input;
  }
  S.refInput.click();
}

/* ============================================================
   场景
   ============================================================ */
function renderScenes() {
  const box = $("sceneList");
  const st = S.sceneStats || {};
  $("sceneMeta").textContent = S.scenes.length
    ? `共 ${S.scenes.length} 个场景，已出概念图 ${st.done || 0} 个`
    : "提取场景设定，并可用本地模型生成场景概念图当作背景锚点";
  if (!S.scenes.length) {
    box.innerHTML = `<div class="empty"><div class="empty-icon">◈</div>
      <p>还没有场景</p><p class="muted">点「AI 提取场景」，或手动添加</p></div>`;
    return;
  }
  box.innerHTML = "";
  S.scenes.forEach((s) => box.appendChild(sceneCard(s)));
}

function sceneTagClass(status) {
  if (status === "done") return "succeeded";
  if (status === "draft") return "draft";
  return status;
}

function sceneCard(s) {
  const card = document.createElement("div");
  card.className = "scene-card";
  card.dataset.id = s.id;
  const thumb = s.image_url
    ? `<img src="${s.image_url}" alt="" loading="lazy" />`
    : `<div class="pc-placeholder">${PANEL_STATUS_TEXT[s.status] || "未出图"}</div>`;
  card.innerHTML = `
    <div class="sc-head">
      <input type="text" class="sc-name" value="${escapeHtml(s.name)}" placeholder="场景名" />
      <span class="tag tag-${sceneTagClass(s.status)}">${PANEL_STATUS_TEXT[s.status] || s.status}</span>
      <span class="spacer"></span>
      <button class="icon-btn sc-up" title="上移">↑</button>
      <button class="icon-btn sc-down" title="下移">↓</button>
      <button class="icon-btn sc-prompt" title="查看提示词">词</button>
      <button class="icon-btn sc-del" title="删除">✕</button>
    </div>
    <div class="sc-body">
      <div class="sc-thumb">${thumb}</div>
      <div class="sc-fields">
        <input type="text" class="sc-location" placeholder="地点：市中心写字楼天台" value="${escapeHtml(s.location || "")}" />
        <div class="grid-2">
          <input type="text" class="sc-time" placeholder="时间：夜" value="${escapeHtml(s.time_of_day || "")}" />
          <input type="text" class="sc-atmo" placeholder="氛围：霓虹反光" value="${escapeHtml(s.atmosphere || "")}" />
        </div>
        <textarea class="sc-desc" rows="2" placeholder="画面描述：主要陈设与光源">${escapeHtml(s.desc || "")}</textarea>
        <textarea class="sc-en" rows="2" placeholder="英文概念图提示词（AI 提取时自动生成）">${escapeHtml(s.prompt || "")}</textarea>
        <div class="pc-actions">
          <button class="btn btn-secondary sc-gen">${s.status === "done" ? "重绘场景图" : "生成场景图"}</button>
          <span class="pc-status muted">${s.status === "failed" && s.error ? escapeHtml(s.error) : ""}</span>
        </div>
      </div>
    </div>`;

  const save = (patch) => {
    clearTimeout(card._t);
    card._t = setTimeout(async () => {
      try { await api(`/comic/scenes/${s.id}`, { method: "PATCH", body: JSON.stringify(patch) }); }
      catch (e) { toast("保存失败：" + e.message); }
    }, 650);
  };
  const bind = (sel, field) => {
    const node = el(sel, card);
    if (node) node.oninput = (e) => { s[field] = e.target.value; save({ [field]: e.target.value }); };
  };
  bind(".sc-name", "name");
  bind(".sc-location", "location");
  bind(".sc-time", "time_of_day");
  bind(".sc-atmo", "atmosphere");
  bind(".sc-desc", "desc");
  bind(".sc-en", "prompt");

  const img = el("img", card);
  if (img) img.onclick = () => window.open(img.src, "_blank");
  el(".sc-up", card).onclick = () => moveScene(s.id, -1);
  el(".sc-down", card).onclick = () => moveScene(s.id, 1);
  el(".sc-prompt", card).onclick = () => showScenePrompt(s.id);
  el(".sc-del", card).onclick = () => deleteScene(s.id);
  el(".sc-gen", card).onclick = (e) => generateScene(s.id, e.target);
  return card;
}

async function addScene() {
  if (!S.project) return toast("请先选择或新建项目");
  try {
    await api(`/comic/projects/${S.project.id}/scenes`, {
      method: "POST", body: JSON.stringify({ name: "新场景" }),
    });
    await refreshDetail();
  } catch (e) { toast(e.message); }
}

async function deleteScene(id) {
  if (!confirm("删除该场景？")) return;
  try {
    await api(`/comic/scenes/${id}`, { method: "DELETE" });
    await refreshDetail();
    toast("场景已删除");
  } catch (e) { toast(e.message); }
}

async function moveScene(id, direction) {
  try {
    await api(`/comic/scenes/${id}/move`, { method: "POST", body: JSON.stringify({ direction }) });
    await refreshDetail();
  } catch (e) { toast(e.message); }
}

async function showScenePrompt(id) {
  const modal = $("promptModal");
  modal.classList.remove("hidden");
  $("promptModalBody").innerHTML = "加载中…";
  try {
    const d = await api(`/comic/scenes/${id}/prompt`);
    $("promptModalBody").innerHTML = `
      <p><strong>场景：</strong>${escapeHtml(d.name)}（环境概念图，无人物）</p>
      <p><strong>正向提示词</strong></p><pre>${escapeHtml(d.prompt)}</pre>
      <p><strong>负面提示词</strong></p><pre>${escapeHtml(d.negative_prompt)}</pre>`;
  } catch (e) {
    $("promptModalBody").innerHTML = `<p class="file-missing">${escapeHtml(e.message)}</p>`;
  }
}

async function generateScene(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "提交中…"; }
  try {
    const res = await api(`/comic/scenes/${id}/generate`, { method: "POST" });
    toast("场景图已加入队列");
    subscribeComicJob(res.job_id);
    startPolling();
    // 必须立即刷新：否则本地 S.scenes 仍是旧状态，
    // 轮询首次 tick 会因 hasActiveWork() 为 false 而立刻自我关闭。
    await refreshDetail();
  } catch (e) { toast("提交失败：" + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "生成场景图"; } }
}

async function drawScenes() {
  if (!S.project) return toast("请先选择或新建项目");
  if (!S.scenes.length) return toast("还没有场景");
  try {
    const res = await api(`/comic/projects/${S.project.id}/scenes/generate`, {
      method: "POST", body: JSON.stringify({ only_missing: true }),
    });
    toast(res.message);
    await refreshDetail();
    startPolling();
  } catch (e) { toast("提交失败：" + e.message); }
}

/* ============================================================
   分镜
   ============================================================ */
function syncPanelList() {
  const box = $("panelList");
  $("panelMeta").textContent = S.panels.length
    ? `共 ${S.panels.length} 格 · 已完成 ${(S.stats && S.stats.done) || 0} 格`
    : "把剧本拆成漫画格子；AI 拆镜会用上角色与场景设定";
  const cards = els(".panel-card", box);
  if (!S.panels.length || cards.length !== S.panels.length) {
    renderPanels();
    return;
  }
  S.panels.forEach((p, i) => {
    const card = cards[i];
    if (!card || card.dataset.id !== p.id) { renderPanels(); return; }
    card.className = `panel-card status-${p.status}`;
    const tag = el(".tag", card);
    if (tag) {
      tag.className = `tag tag-${p.status === "done" ? "succeeded" : p.status}`;
      tag.textContent = PANEL_STATUS_TEXT[p.status] || p.status;
    }
    const thumb = el(".pc-thumb", card);
    if (thumb) {
      const hasImg = !!el("img", thumb);
      if (p.image_url && !hasImg) {
        thumb.innerHTML = `<img src="${p.image_url}" alt="" loading="lazy" />`;
        el("img", thumb).onclick = () => window.open(p.image_url, "_blank");
      } else if (!p.image_url && hasImg) {
        thumb.innerHTML = `<div class="pc-placeholder">${PANEL_STATUS_TEXT[p.status] || "未生成"}</div>`;
      }
    }
    const status = el(".pc-status", card);
    if (status) {
      status.textContent = p.status === "failed" && p.error
        ? p.error : (p.seed >= 0 ? "seed " + p.seed : "");
    }
    const gen = el(".pc-gen", card);
    if (gen && !gen.disabled) gen.textContent = p.status === "done" ? "重绘本格" : "生成本格";
  });
}

function renderPanels() {
  const box = $("panelList");
  if (!S.panels.length) {
    box.innerHTML = `<div class="empty"><div class="empty-icon">◈</div>
      <p>还没有分镜</p>
      <p class="muted">在「剧本」步骤用「规则拆格」或「AI 智能分镜」，也可以点「＋ 加一格」</p></div>`;
    return;
  }
  box.innerHTML = "";
  S.panels.forEach((p) => box.appendChild(panelCard(p)));
}

function panelCard(p) {
  const card = document.createElement("div");
  card.className = `panel-card status-${p.status}`;
  card.dataset.id = p.id;
  const thumb = p.image_url
    ? `<img src="${p.image_url}" alt="" loading="lazy" title="点击查看大图" />`
    : `<div class="pc-placeholder">${PANEL_STATUS_TEXT[p.status] || "未生成"}</div>`;
  const charChips = S.characters.map((c) => {
    const on = (p.character_ids || []).includes(c.id) ? " on" : "";
    return `<button class="chip${on}" data-char="${c.id}">${escapeHtml(c.name)}</button>`;
  }).join("") || '<span class="muted">未建角色</span>';
  const sceneOpts = ['<option value="">（未指定场景）</option>']
    .concat(S.scenes.map((s) =>
      `<option value="${s.id}"${s.id === p.scene_id ? " selected" : ""}>${escapeHtml(s.name)}</option>`))
    .join("");

  card.innerHTML = `
    <div class="pc-head">
      <span class="pc-seq">#${p.seq}</span>
      <select class="pc-shot">${shotOptions(p.shot)}</select>
      <select class="pc-scene-ref">${sceneOpts}</select>
      <span class="tag tag-${p.status === "done" ? "succeeded" : p.status}">${PANEL_STATUS_TEXT[p.status] || p.status}</span>
      <span class="spacer"></span>
      <button class="icon-btn pc-up" title="上移">↑</button>
      <button class="icon-btn pc-down" title="下移">↓</button>
      <button class="icon-btn pc-prompt" title="查看提示词与一致性参数">词</button>
      <button class="icon-btn pc-del" title="删除">✕</button>
    </div>
    <div class="pc-body">
      <div class="pc-thumb">${thumb}</div>
      <div class="pc-fields">
        <textarea class="pc-scene" rows="2" placeholder="画面描述：谁、在哪、在做什么">${escapeHtml(p.scene || "")}</textarea>
        <div class="grid-2">
          <input type="text" class="pc-dialogue" placeholder="对白 / 旁白（排版时会画成气泡）" value="${escapeHtml(p.dialogue || "")}" />
          <input type="text" class="pc-sfx" placeholder="拟声词，如 轰隆" value="${escapeHtml(p.sfx || "")}" />
        </div>
        <div class="pc-chars">${charChips}</div>
        <div class="pc-actions">
          <button class="btn btn-secondary pc-gen">${p.status === "done" ? "重绘本格" : "生成本格"}</button>
          <label class="pc-lock" title="勾选后重绘复用同一个种子，用于复现或微调提示词">
            <input type="checkbox" class="pc-lock-cb" /> 锁定种子
          </label>
          <span class="pc-status muted">${p.status === "failed" && p.error ? escapeHtml(p.error) : (p.seed >= 0 ? "seed " + p.seed : "")}</span>
        </div>
      </div>
    </div>`;

  const save = (patch) => schedulePanelSave(p.id, patch);
  el(".pc-shot", card).onchange = (e) => { p.shot = e.target.value; save({ shot: e.target.value }); };
  el(".pc-scene-ref", card).onchange = (e) => { p.scene_id = e.target.value || null; save({ scene_id: e.target.value || null }); };
  el(".pc-scene", card).oninput = (e) => { p.scene = e.target.value; save({ scene: e.target.value }); };
  el(".pc-dialogue", card).oninput = (e) => { p.dialogue = e.target.value; save({ dialogue: e.target.value }); };
  el(".pc-sfx", card).oninput = (e) => { p.sfx = e.target.value; save({ sfx: e.target.value }); };
  els(".chip", card).forEach((btn) => {
    btn.onclick = () => {
      const cid = btn.dataset.char;
      const set = new Set(p.character_ids || []);
      set.has(cid) ? set.delete(cid) : set.add(cid);
      p.character_ids = [...set];
      btn.classList.toggle("on");
      save({ character_ids: p.character_ids });
    };
  });

  const img = el("img", card);
  if (img) img.onclick = () => window.open(img.src, "_blank");
  el(".pc-up", card).onclick = () => movePanel(p.id, -1);
  el(".pc-down", card).onclick = () => movePanel(p.id, 1);
  el(".pc-prompt", card).onclick = () => showPanelPrompt(p.id);
  el(".pc-del", card).onclick = () => deletePanel(p.id);
  el(".pc-gen", card).onclick = (e) => generatePanelFromCard(card, p.id, e.target);
  return card;
}

function schedulePanelSave(panelId, patch) {
  const key = panelId;
  S.timers[key] = Object.assign(S.timers[key] || {}, patch);
  clearTimeout(S.timers[key + "_t"]);
  S.timers[key + "_t"] = setTimeout(async () => {
    const body = S.timers[key];
    S.timers[key] = {};
    try {
      await api(`/comic/panels/${panelId}`, { method: "PATCH", body: JSON.stringify(body) });
    } catch (e) { toast("保存失败：" + e.message); }
  }, 650);
}

async function movePanel(panelId, direction) {
  try {
    await api(`/comic/panels/${panelId}/move`, { method: "POST", body: JSON.stringify({ direction }) });
    await refreshDetail();
  } catch (e) { toast(e.message); }
}

async function deletePanel(panelId) {
  if (!confirm("删除这一格？")) return;
  try {
    await api(`/comic/panels/${panelId}`, { method: "DELETE" });
    await refreshDetail();
    toast("已删除");
  } catch (e) { toast(e.message); }
}

async function addPanel() {
  if (!S.project) return toast("请先选择或新建项目");
  try {
    await api(`/comic/projects/${S.project.id}/panels`, {
      method: "POST",
      body: JSON.stringify({ shot: "medium", scene: "", dialogue: "", sfx: "", character_ids: [] }),
    });
    await refreshDetail();
  } catch (e) { toast(e.message); }
}

async function showPanelPrompt(panelId) {
  const modal = $("promptModal");
  modal.classList.remove("hidden");
  $("promptModalBody").innerHTML = "加载中…";
  try {
    const d = await api(`/comic/panels/${panelId}/prompt`);
    const refs = d.ref_characters || [];
    const missing = d.missing_ref || [];
    const slots = d.ref_slots || [];
    const charSlots = slots.filter((s) => s.kind === "portrait");
    const hasScene = slots.some((s) => s.kind === "scene");
    const kindText = !slots.length
      ? "文生图（无参考图）"
      : hasScene
        ? "融合编辑 · 场景 + 角色参考图（Qwen-Image 2.1 官方通道，CFG=1）"
        : "融合编辑 · 角色参考图（Qwen-Image 2.1 官方通道，CFG=1）";
    const bits = [
      `<p><strong>画风：</strong>${escapeHtml(presetName("styles", d.style))}` +
      (d.style_info && d.style_info.reference ? `（参考 ${escapeHtml(d.style_info.reference)}）` : "") +
      ` · <strong>景别：</strong>${escapeHtml(shotLabel(d.shot))} · ` +
      `<strong>出图方式：</strong>${kindText}</p>`,
    ];
    if (d.theme_line) bits.push(`<p><strong>主题色：</strong>${escapeHtml(d.theme_line)}</p>`);
    if (d.profile) {
      const pf = profileInfo(d.profile);
      bits.push(`<p class="muted">生成档位：${escapeHtml(pf ? pf.name : d.profile)}` +
        (d.batch > 1 ? ` · 候选 ${d.batch} 张` : "") + `</p>`);
    }
    if (charSlots.length) {
      bits.push(`<p class="muted">参考图顺序：${escapeHtml(
        slots.map((s) => `${s.slot}=${s.kind === "scene" ? "场景" : ""}${s.name}`).join("、"))}` +
        `（image_1 决定输出尺寸与画面基调）</p>`);
    }
    if (missing.length) {
      bits.push(`<p class="file-missing">这些角色还没有形象图，本格不会被约束：` +
        `${escapeHtml(missing.join("、"))}（先去「角色提取」生成形象图）</p>`);
    }
    if (d.char_negative_is_default && !d.negative_is_default) {
      bits.push(`<p class="muted">人物反向提示词正在使用系统默认值。</p>`);
    } else if (d.negative_is_default) {
      bits.push(`<p class="muted">反向提示词正在使用系统默认值（已压制畸形 / 多指 / 怪表情）。</p>`);
    }
    bits.push(`<p><strong>正向提示词</strong></p><pre>${escapeHtml(d.prompt)}</pre>`);
    bits.push(`<p><strong>负向提示词</strong></p><pre>${escapeHtml(d.negative_prompt)}</pre>`);
    $("promptModalBody").innerHTML = bits.join("");
  } catch (e) {
    $("promptModalBody").innerHTML = `<p class="file-missing">${escapeHtml(e.message)}</p>`;
  }
}

/* ============================================================
   出图
   ============================================================ */
function renderDrawGrid() {
  const box = $("drawGrid");
  const s = S.stats || {};
  $("drawMeta").textContent = S.panels.length
    ? `共 ${S.panels.length} 格，已完成 ${s.done || 0}，进行中 ${s.active || 0}${s.failed ? `，失败 ${s.failed}` : ""}`
    : "还没有分镜，先去「分镜」步骤拆格";
  if (!S.panels.length) {
    box.innerHTML = `<div class="empty"><div class="empty-icon">◈</div><p>还没有分镜</p>
      <p class="muted">先去「分镜」步骤生成格子</p></div>`;
    return;
  }
  box.innerHTML = "";
  S.panels.forEach((p) => {
    const node = document.createElement("div");
    node.className = `draw-card status-${p.status}`;
    node.dataset.id = p.id;
    node.innerHTML = `
      <div class="dc-thumb">${p.image_url
        ? `<img src="${p.image_url}" alt="" loading="lazy" />`
        : `<div class="pc-placeholder">${PANEL_STATUS_TEXT[p.status] || "未生成"}</div>`}</div>
      <div class="dc-body">
        <div class="dc-title">#${p.seq} · ${escapeHtml(shotLabel(p.shot))}</div>
        <div class="dc-scene">${escapeHtml((p.scene || "").slice(0, 40) || "—")}</div>
        <div class="dc-dialogue${p.dialogue ? "" : " none"}"${p.dialogue
          ? ` title="${escapeHtml(p.dialogue)}"` : ""}>${p.dialogue
          ? "「" + escapeHtml(p.dialogue) + "」"
          : "无对白"}</div>
        <div class="dc-actions">
          <button class="btn btn-secondary dc-gen">${p.status === "done" ? "重绘" : "生成本格"}</button>
          <span class="tag tag-${p.status === "done" ? "succeeded" : p.status}">${PANEL_STATUS_TEXT[p.status] || p.status}</span>
        </div>
      </div>`;
    const img = el("img", node);
    if (img) img.onclick = () => window.open(img.src, "_blank");
    el(".dc-gen", node).onclick = (e) => generatePanel(p.id, e.target, true);
    box.appendChild(node);
  });
}

async function generatePanel(panelId, btn, reseed = true) {
  if (btn) { btn.disabled = true; btn.textContent = "提交中…"; }
  try {
    const res = await api(
      `/comic/panels/${panelId}/generate?reseed=${reseed ? "true" : "false"}`,
      { method: "POST" });
    toast(reseed
      ? `已加入队列（新种子 ${res.seed}）`
      : `已加入队列（沿用种子 ${res.seed}）`);
    subscribeComicJob(res.job_id);
    startPolling();
    await refreshDetail();
  } catch (e) {
    toast("提交失败：" + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "生成本格"; }
  }
}

/* 卡片上的按钮：按「锁定种子」勾选状态决定是否换种子 */
function generatePanelFromCard(card, panelId, btn) {
  const lock = el(".pc-lock-cb", card);
  return generatePanel(panelId, btn, !(lock && lock.checked));
}

async function generateAll(onlyMissing) {
  if (!S.project) return toast("请先选择或新建项目");
  if (!S.panels.length) return toast("还没有分镜");
  setComicProgress(2, "提交中…");
  try {
    const res = await api(`/comic/projects/${S.project.id}/generate`, {
      method: "POST", body: JSON.stringify({ only_missing: !!onlyMissing }),
    });
    setComicProgress(4, res.message);
    toast(res.message);
    startPolling();
    await refreshDetail();
  } catch (e) {
    toast("提交失败：" + e.message);
    setComicProgress(0, "失败");
  }
}

function setComicProgress(percent, text) {
  $("comicProgressFill").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  if (text) $("comicProgressText").textContent = text;
}

function subscribeComicJob(jobId) {
  if (!jobId) return;
  if (S.jobSource) { S.jobSource.close(); S.jobSource = null; }
  const es = new EventSource(`${API}/jobs/${jobId}/events`);
  S.jobSource = es;
  es.addEventListener("progress", (e) => {
    const d = JSON.parse(e.data);
    setComicProgress(d.percent || 0, d.message || `采样 ${d.step}/${d.total}`);
  });
  es.addEventListener("image", () => refreshDetail().catch(() => {}));
  es.addEventListener("done", () => {
    setComicProgress(100, "本格完成，继续下一格…");
    es.close(); S.jobSource = null;
    refreshDetail().catch(() => {});
  });
  es.addEventListener("error", () => {
    es.close(); S.jobSource = null;
    refreshDetail().catch(() => {});
  });
  es.onerror = () => { es.close(); S.jobSource = null; };
}

function hasActiveWork() {
  const pActive = S.panels.some((p) => p.status === "queued" || p.status === "running");
  const sActive = S.scenes.some((x) => x.status === "queued" || x.status === "running");
  const cActive = S.characters.some((x) => x.status === "queued" || x.status === "running");
  return pActive || sActive || cActive;
}

function startPolling() {
  if (S.pollTimer) return;
  S.pollTimer = setInterval(async () => {
    if (!S.project) return;
    try { await refreshDetail(); } catch { /* 忽略瞬时错误 */ }
    const s = S.stats || {};
    const total = s.total || 0;
    const done = s.done || 0;
    if (total) setComicProgress(Math.round((done / total) * 100), `已完成 ${done}/${total} 格`);
    if (!hasActiveWork()) {
      clearInterval(S.pollTimer);
      S.pollTimer = null;
      setComicProgress(100, "就绪");
    }
  }, 5000);
}

/* ============================================================
   排版导出
   ============================================================ */
async function renderPages() {
  if (!S.project) return toast("请先选择或新建项目");
  setComicProgress(30, "排版渲染中…");
  try {
    const res = await api(`/comic/projects/${S.project.id}/render`, {
      method: "POST",
      body: JSON.stringify({
        layout: $("renderLayout").value,
        page_width: Number($("renderWidth").value),
        bubble_position: $("renderBubble").value,
      }),
    });
    setComicProgress(100, `已生成 ${res.pages} 页`);
    toast(`已生成 ${res.pages} 页（${res.layout_name}）`);
    await refreshDetail();
  } catch (e) {
    setComicProgress(0, "渲染失败");
    toast("渲染失败：" + e.message);
  }
}

function renderRenders() {
  const box = $("renderList");
  // 每个页码可能有多个历史批次（重排会新增一批）：只展示每页最新一份，按页码升序
  const latest = new Map();
  S.renders.forEach((r) => {
    const cur = latest.get(r.page);
    if (!cur || String(r.created_at || "") > String(cur.created_at || "")) latest.set(r.page, r);
  });
  const list = [...latest.values()].sort((a, b) => a.page - b.page);
  $("exportMeta").textContent = list.length
    ? `已生成 ${list.length} 页成品，可下载`
    : "把分镜图拼成漫画页，自动绘制气泡、拟声词与页码";
  if (!list.length) {
    box.innerHTML = '<p class="muted">还没有成品页，出图后点「生成漫画页」</p>';
    return;
  }
  box.innerHTML = "";
  list.forEach((r) => {
    const node = document.createElement("div");
    node.className = "render-item";
    node.innerHTML = `
      <img src="${r.url}" alt="" loading="lazy" />
      <div class="ri-meta">
        <span>第 ${r.page} 页 · ${r.width}×${r.height}</span>
        <div class="ri-actions">
          <a class="link-btn" href="${r.url}" download>下载</a>
          <button class="link-btn ri-del">删除</button>
        </div>
      </div>`;
    el("img", node).onclick = () => window.open(r.url, "_blank");
    el(".ri-del", node).onclick = async () => {
      try { await api(`/comic/renders/${r.id}`, { method: "DELETE" }); await refreshDetail(); }
      catch (e) { toast(e.message); }
    };
    box.appendChild(node);
  });
}

async function exportPdf() {
  if (!S.project) return toast("请先选择或新建项目");
  if (!S.renders.length) {
    toast("还没有成品页，先按当前设置排版一次…");
    await renderPages();
    if (!S.renders.length) return;
  }
  setComicProgress(50, "合并 PDF 中…");
  try {
    const res = await api(`/comic/projects/${S.project.id}/export`, {
      method: "POST",
      body: JSON.stringify({
        layout: $("renderLayout").value,
        page_width: Number($("renderWidth").value),
        bubble_position: $("renderBubble").value,
        color_mode: ($("exportColor") || {}).value || "color",
      }),
    });
    setComicProgress(100, `PDF 已生成（${res.pages} 页）`);
    toast(`已导出 PDF：${res.filename}`);
    downloadUrl(res.url, res.filename);
    await refreshDetail();
  } catch (e) {
    setComicProgress(0, "导出失败");
    toast("导出失败：" + e.message);
  }
}

function downloadUrl(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function renderExports() {
  const box = $("exportList");
  if (!box) return;
  const items = S.exports || [];
  if (!items.length) {
    box.innerHTML = '<p class="muted">还没有导出，点「导出 PDF（整部）」生成</p>';
    return;
  }
  box.innerHTML = "";
  items.forEach((x) => {
    const node = document.createElement("div");
    node.className = "export-item";
    const kb = x.size ? `${Math.max(1, Math.round(x.size / 1024))} KB` : "";
    node.innerHTML = `
      <span class="export-name" title="${escapeHtml(x.name)}">${escapeHtml(x.name)}</span>
      <span class="export-actions">
        <span class="muted">${kb}</span>
        <a class="link-btn" href="${x.url}" download>下载</a>
        <button class="link-btn ex-del">删除</button>
      </span>`;
    el(".ex-del", node).onclick = async () => {
      if (!confirm(`删除导出的 PDF「${x.name}」？`)) return;
      try {
        await api(`/comic/projects/${S.project.id}/exports/${encodeURIComponent(x.name)}`,
          { method: "DELETE" });
        await refreshDetail();
      } catch (e) { toast(e.message); }
    };
    box.appendChild(node);
  });
}

/* ============================================================
   工作量估算
   ============================================================ */
async function updateComicEstimate() {
  const p = S.project;
  if (!p) { $("estimateComic").textContent = ""; return; }
  try {
    const d = await api("/preview", {
      method: "POST",
      body: JSON.stringify({ width: p.width, height: p.height, steps: p.steps, batch_size: 1 }),
    });
    const perMin = d.estimated_seconds / 60;
    const todo = Math.max(0, ((S.stats && S.stats.total) || 0) - ((S.stats && S.stats.done) || 0));
    let text = `单格约 ${perMin.toFixed(1)} 分钟`;
    if (todo) text += ` · 剩余 ${todo} 格约 ${(perMin * todo / 60).toFixed(1)} 小时`;
    $("estimateComic").textContent = text;
  } catch { $("estimateComic").textContent = ""; }
}

/* ============================================================
   事件绑定 / 初始化
   ============================================================ */
function bindComicEvents() {
  els("#mainNav .navitem").forEach((b) => (b.onclick = () => switchView(b.dataset.view)));
  els("#comicSteps .step").forEach((b) => (b.onclick = () => setStep(b.dataset.step)));

  $("btnToggleNewProject").onclick = () => {
    toggleProjectBoard(true);
    $("newProjectForm").classList.toggle("hidden");
  };
  $("btnCreateProject").onclick = createProject;
  $("btnToggleProjects").onclick = () => toggleProjectBoard();
  $("btnCollapseBoard").onclick = () => toggleProjectBoard();
  $("btnRefreshProject").onclick = () => {
    loadLlmStatus();
    refreshDetail().then(() => toast("已刷新")).catch((e) => toast(e.message));
  };
  $("btnSaveProject").onclick = () => saveProjectSettings();
  $("btnResetNegative").onclick = resetNegative;

  $("btnScriptHelp").onclick = () => {
    toast("一行一格；[特写] 指定景别；第 2 页 分页；「台词」加对白；[音效：轰隆] 加拟声词", 6000);
  };
  $("btnPreviewScript").onclick = previewScript;
  $("btnParseScript").onclick = ruleParseToPanels;
  $("btnRuleScript").onclick = ruleParseToPanels;
  $("btnAiScript").onclick = aiScript;
  $("btnAiContinue").onclick = aiContinue;
  $("btnAiPolish").onclick = aiPolish;
  $("btnAiCharacters").onclick = aiCharacters;
  $("btnAiScenes").onclick = aiScenes;
  $("btnAiStoryboard").onclick = aiStoryboard;
  $("btnCancelAi").onclick = cancelAiTask;

  $("btnAddCharacter").onclick = addCharacter;
  $("btnDrawCharacters").onclick = drawCharacters;
  $("btnAddScene").onclick = addScene;
  $("btnDrawScenes").onclick = drawScenes;
  $("btnAddPanel").onclick = addPanel;
  $("btnGenerateAll").onclick = () => generateAll(false);
  $("btnGenerateMissing").onclick = () => generateAll(true);
  $("btnRender").onclick = renderPages;
  $("btnExportPdf").onclick = exportPdf;

  let scriptTimer = null;
  $("scriptText").oninput = () => {
    clearTimeout(scriptTimer);
    scriptTimer = setTimeout(saveScriptText, 900);
  };
  $("scriptPages").onchange = updateScriptEstimate;
  $("setGuidance").oninput = (e) => { $("setGuidanceVal").textContent = Number(e.target.value).toFixed(1); };
  $("setRefStrength").oninput = (e) => { $("setRefStrengthVal").textContent = Number(e.target.value).toFixed(2); };
  $("renderWidth").oninput = (e) => { $("renderWidthVal").textContent = e.target.value; };
  $("setSize").onchange = updateComicEstimate;
  $("setSteps").onchange = updateComicEstimate;

  // 生成档位切换：自动带出该档位的尺寸 / 步数，并给出提示
  if ($("setProfile")) {
    $("setProfile").onchange = () => {
      const p = profileInfo($("setProfile").value);
      if (p) {
        setSizeSelect(p.width, p.height);
        setStepsSelect(p.steps);
        $("setGuidance").value = String(p.guidance);
        $("setGuidanceVal").textContent = Number(p.guidance).toFixed(1);
      }
      updateProfileHint($("setProfile").value);
      updateComicEstimate();
    };
  }

  // 画风库浏览器
  if ($("btnBrowseStyle")) $("btnBrowseStyle").onclick = () => openStyleBrowser("set");
  if ($("btnBrowseStyleNew")) $("btnBrowseStyleNew").onclick = () => openStyleBrowser("new");
  if ($("styleModalClose")) $("styleModalClose").onclick = closeStyleBrowser;
  if ($("styleModal")) $("styleModal").onclick = (e) => { if (e.target.id === "styleModal") closeStyleBrowser(); };
  if ($("styleSearch")) {
    let st = null;
    $("styleSearch").oninput = (e) => {
      clearTimeout(st);
      const v = e.target.value;
      st = setTimeout(() => { SB.keyword = v.trim(); SB.page = 1; loadStyles(); }, 300);
    };
  }

  $("promptModalClose").onclick = () => $("promptModal").classList.add("hidden");
  $("promptModal").onclick = (e) => { if (e.target.id === "promptModal") $("promptModal").classList.add("hidden"); };
}

let comicReady = false;
async function initComicOnce() {
  if (comicReady) return;
  comicReady = true;
  try {
    await loadPresets();
    await loadProjects(false);
    setStep(S.step);
    loadLlmStatus();
  } catch (e) {
    toast("漫画模块初始化失败：" + e.message);
  }
}

bindComicEvents();

// 支持带参数直达漫画工作室：/?view=comic&step=character
// 注意：switchView 会改写 URL，所以 step 必须在调用它之前先解析出来。
try {
  const q = new URLSearchParams(location.search);
  const want = q.get("step");
  if (want && STEP_ORDER.includes(want)) S.step = want;
  if (q.get("view") === "comic") switchView("comic");
} catch (e) { /* 忽略 */ }
