/* ============================================================
   本地图片生成器 - 前端逻辑（无框架，直接操作 DOM）
   ============================================================ */

const API = "/api/v1";
const state = {
  mode: "txt2img",
  refImage: null,          // data URL
  currentJobId: null,
  eventSource: null,
  gallery: { page: 1, pageSize: 24, total: 0 },
  config: null,
  status: null,
  drawerImage: null,
  generated: [],           // 本次会话生成结果
};

const $ = (id) => document.getElementById(id);
const el = (sel, root = document) => root.querySelector(sel);
const els = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---------------- 通用请求 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : data;
    const msg = typeof detail === "string" ? detail : (detail && detail.message) || res.statusText;
    throw new Error(msg || `请求失败 (${res.status})`);
  }
  return data;
}

function toast(message, ms = 2600) {
  const t = $("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

/* ============================================================
   参数收集
   ============================================================ */
function currentSize() {
  const preset = $("sizePreset").value;
  if (preset === "custom") {
    return { width: Number($("width").value), height: Number($("height").value) };
  }
  const [w, h] = preset.split("x").map(Number);
  return { width: w, height: h };
}

function collectParams() {
  const { width, height } = currentSize();
  const seedRaw = Number($("seed").value);
  const seed = $("seedLock").checked ? (seedRaw >= 0 ? seedRaw : -1) : -1;
  const p = {
    prompt: $("prompt").value.trim(),
    negative_prompt: $("negative").value.trim(),
    width, height,
    steps: Number($("steps").value),
    guidance_scale: Number($("guidance").value),
    seed,
    batch_size: Number($("batch").value),
    mode: state.mode,
    strength: Number($("strength").value),
    channel: "auto",
  };
  if (state.mode === "img2img" && state.refImage) {
    p.image = state.refImage;
    p.ref_images = [state.refImage];
  }
  return p;
}

function validate(p) {
  if (!p.prompt) return "请先输入提示词";
  if (p.width % 64 || p.height % 64) return "宽高必须是 64 的整数倍";
  if (p.mode === "img2img" && !state.refImage) return "图生图模式请先选择参考图";
  return null;
}

function showError(msg) {
  const box = $("errorText");
  if (!msg) { box.classList.add("hidden"); box.textContent = ""; return; }
  box.textContent = msg;
  box.classList.remove("hidden");
}

/* ============================================================
   进度
   ============================================================ */
function setProgress(percent, text) {
  $("progressFill").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  if (text) $("progressText").textContent = text;
}

function subscribeJob(jobId) {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  state.currentJobId = jobId;
  const es = new EventSource(`${API}/jobs/${jobId}/events`);
  state.eventSource = es;

  es.addEventListener("status", (e) => {
    const d = JSON.parse(e.data);
    setProgress(d.progress || 1, "已提交…");
  });
  es.addEventListener("progress", (e) => {
    const d = JSON.parse(e.data);
    setProgress(d.percent || 0, d.message || `采样 ${d.step}/${d.total}`);
  });
  es.addEventListener("image", (e) => {
    const d = JSON.parse(e.data);
    appendResult(d);
  });
  es.addEventListener("done", (e) => {
    const d = JSON.parse(e.data);
    setProgress(100, d.status === "succeeded" ? "完成" : "已结束");
    es.close(); state.eventSource = null;
    setBusy(false);
    refreshJobs(); refreshGallery(); refreshStats();
    if (d.status === "succeeded") toast(`生成完成，共 ${d.count} 张`);
  });
  es.addEventListener("error", (e) => {
    let msg = "生成失败";
    try { msg = JSON.parse(e.data).error || msg; } catch {}
    showError(msg);
    setProgress(0, "失败");
    es.close(); state.eventSource = null;
    setBusy(false);
    refreshJobs();
  });
  es.onerror = () => {
    // 网络中断（非任务错误）：让轮询兜底
    es.close(); state.eventSource = null;
  };
}

function setBusy(busy) {
  $("btnGenerate").disabled = busy;
  $("btnBatch").disabled = busy;
  $("btnGenerate").textContent = busy ? "生成中…" : "开始生成";
}

/* ============================================================
   结果展示
   ============================================================ */
function appendResult(item) {
  const canvas = $("canvas");
  const empty = $("canvasEmpty");
  if (empty) empty.remove();

  const existing = el("#mainResult", canvas);
  if (existing) existing.remove();

  const img = document.createElement("img");
  img.id = "mainResult";
  img.src = item.url;
  img.alt = "生成结果";
  img.onclick = () => openDrawer(item.id);
  canvas.appendChild(img);

  const thumb = document.createElement("img");
  thumb.src = item.url;
  thumb.title = `seed ${item.seed}`;
  thumb.onclick = () => openDrawer(item.id);
  $("thumbs").prepend(thumb);
}

function clearCanvas() {
  $("canvas").innerHTML =
    '<div class="empty" id="canvasEmpty"><div class="empty-icon">◈</div><p>还没有生成结果</p><p class="muted">在左侧输入提示词后点击「开始生成」</p></div>';
  $("thumbs").innerHTML = "";
}

/* ============================================================
   生成动作
   ============================================================ */
async function doGenerate() {
  const p = collectParams();
  const err = validate(p);
  if (err) { showError(err); return; }
  showError("");
  clearCanvas();
  setBusy(true);
  setProgress(2, "提交中…");
  try {
    const job = await api("/generate", { method: "POST", body: JSON.stringify(p) });
    (job.images || []).forEach(appendResult);
    setProgress(100, "完成");
    refreshGallery(); refreshStats(); refreshJobs();
    toast("生成完成");
  } catch (e) {
    showError(e.message);
    setProgress(0, "失败");
  } finally {
    setBusy(false);
  }
}

async function doBatch() {
  const p = collectParams();
  const err = validate(p);
  if (err) { showError(err); return; }
  showError("");
  const count = Number($("batch").value);
  try {
    const res = await api("/jobs", {
      method: "POST",
      body: JSON.stringify({ ...p, batch_size: count }),
    });
    toast(`已加入队列（前方 ${res.position - 1} 个任务）`);
    switchTab("jobs");
    refreshJobs(); refreshStats();
    subscribeJob(res.job_id);
  } catch (e) {
    showError(e.message);
  }
}

/* ============================================================
   图库
   ============================================================ */
async function refreshGallery() {
  const params = new URLSearchParams({
    page: state.gallery.page,
    page_size: state.gallery.pageSize,
    keyword: $("gallerySearch").value.trim(),
    favorite_only: $("favOnly").checked ? "true" : "false",
  });
  try {
    const data = await api(`/gallery?${params}`);
    state.gallery.total = data.total;
    const grid = $("galleryGrid");
    grid.innerHTML = "";
    if (!data.items.length) {
      grid.innerHTML = '<p class="muted" style="padding:20px">暂无图片</p>';
    }
    data.items.forEach((item) => grid.appendChild(galleryCard(item)));
    const pages = Math.max(1, Math.ceil(data.total / state.gallery.pageSize));
    $("pageInfo").textContent = `${state.gallery.page} / ${pages}`;
    $("galleryTotal").textContent = `共 ${data.total} 张`;
    $("galleryCount").textContent = data.total;
    $("pagePrev").disabled = state.gallery.page <= 1;
    $("pageNext").disabled = state.gallery.page >= pages;
  } catch (e) {
    toast("图库加载失败：" + e.message);
  }
}

function galleryCard(item) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <img src="${item.url}" alt="" loading="lazy" />
    ${item.flagged ? '<span class="card-flag">已标记</span>' : ""}
    <span class="card-fav ${item.favorite ? "on" : ""}">${item.favorite ? "★" : "☆"}</span>
    <div class="card-body">
      <div class="card-prompt">${escapeHtml(item.prompt || "")}</div>
      <div class="card-meta">${item.width}×${item.height} · seed ${item.seed}</div>
    </div>`;
  el("img", card).onclick = () => openDrawer(item.id);
  el(".card-fav", card).onclick = async (ev) => {
    ev.stopPropagation();
    try {
      await api(`/gallery/${item.id}/favorite?favorite=${item.favorite ? "false" : "true"}`, { method: "POST" });
      refreshGallery(); refreshStats();
    } catch (e) { toast(e.message); }
  };
  return card;
}

/* ============================================================
   图片详情抽屉
   ============================================================ */
async function openDrawer(imageId) {
  try {
    const list = await api(`/gallery?page=1&page_size=1&job_id=`);
    // 直接从图库数据里找，避免额外接口：改用单个查询
  } catch {}
  // 用 gallery 检索拿不到单条，改为从当前渲染过的数据缓存获取
  const item = await fetchImageById(imageId);
  if (!item) { toast("图片信息不存在"); return; }

  state.drawerImage = item;
  $("drawerImg").src = item.url;
  $("drawerDownload").href = item.url;
  $("drawerFavorite").textContent = item.favorite ? "取消收藏" : "收藏";
  const rows = [
    ["尺寸", `${item.width} × ${item.height}`],
    ["步数", item.steps],
    ["引导", item.guidance],
    ["种子", item.seed],
    ["模式", item.mode === "img2img" ? "图生图" : "文生图"],
    ["耗时", item.elapsed_ms ? (item.elapsed_ms / 1000).toFixed(1) + " 秒" : "-"],
    ["时间", item.created_at],
    ["文件", item.path],
  ];
  $("drawerMeta").innerHTML =
    rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v ?? "-"))}</dd>`).join("") +
    `<dt>提示词</dt><dd>${escapeHtml(item.prompt || "")}</dd>` +
    (item.negative ? `<dt>负面</dt><dd>${escapeHtml(item.negative)}</dd>` : "");

  $("drawerMask").classList.remove("hidden");
  $("drawer").classList.remove("hidden");
}

async function fetchImageById(imageId) {
  // 后端未提供单图 GET 之外，这里用图库分页搜索兜底：遍历当前页数据
  try {
    const data = await api(`/gallery?page=1&page_size=200`);
    const found = data.items.find((x) => x.id === imageId);
    if (found) return found;
  } catch {}
  return null;
}

function closeDrawer() {
  $("drawerMask").classList.add("hidden");
  $("drawer").classList.add("hidden");
}

/* ============================================================
   任务列表
   ============================================================ */
async function refreshJobs() {
  try {
    const data = await api("/jobs?limit=30");
    const list = $("jobList");
    list.innerHTML = "";
    if (!data.items.length) {
      list.innerHTML = '<p class="muted">暂无任务</p>';
    }
    data.items.forEach((job) => {
      const node = document.createElement("div");
      node.className = "job-item";
      node.innerHTML = `
        <div class="job-main">
          <div class="job-prompt">${escapeHtml(job.prompt)}</div>
          <div class="job-meta">
            <span>${job.id}</span>
            <span>${job.created_at.replace("T", " ")}</span>
            ${job.error ? `<span style="color:var(--danger)">${escapeHtml(job.error)}</span>` : ""}
          </div>
          <div class="job-bar"><div style="width:${job.progress || 0}%"></div></div>
        </div>
        <span class="tag tag-${job.status}">${statusText(job.status)}</span>`;
      node.onclick = () => { if (job.status === "queued" || job.status === "running") subscribeJob(job.id); };
      list.appendChild(node);
    });
    const active = data.items.filter((j) => j.status === "running" || j.status === "queued").length;
    $("jobCount").textContent = active;
  } catch (e) { /* 静默 */ }
}

function statusText(s) {
  return { queued: "排队", running: "运行", succeeded: "完成", failed: "失败", canceled: "取消" }[s] || s;
}

async function refreshStats() {
  try {
    const s = await api("/system/stats");
    $("galleryCount").textContent = s.images;
  } catch {}
}

/* ============================================================
   模型状态
   ============================================================ */
async function refreshStatus() {
  try {
    const st = await api("/models/status");
    state.status = st;
    const badge = $("engineBadge");
    badge.className = "badge";
    if (st.engine_mode === "mock") {
      badge.classList.add("badge-mock");
      badge.textContent = "占位模式";
    } else if (st.loaded) {
      badge.classList.add("badge-local");
      badge.textContent = "本地模型已加载";
    } else if (st.last_error) {
      badge.classList.add("badge-error");
      badge.textContent = "模型加载失败";
    } else {
      badge.classList.add("badge-idle");
      badge.textContent = "本地模型未加载";
    }

    const parts = [];
    if (st.gpu_name) parts.push(st.gpu_name);
    if (st.vram_total_gb) parts.push(`显存 ${st.vram_free_gb}/${st.vram_total_gb} GB`);
    if (st.queue_size) parts.push(`队列 ${st.queue_size}`);
    if (st.last_load_seconds) parts.push(`加载 ${st.last_load_seconds}s`);
    $("vramInfo").textContent = parts.join(" · ") || "—";

    const note = $("engineNote");
    if (st.note) { note.textContent = st.note; note.classList.remove("hidden"); }
    else note.classList.add("hidden");
  } catch (e) { /* 静默 */ }
}

async function loadModel() {
  toast("正在加载模型，可能需要较长时间…");
  try {
    await api("/models/load", { method: "POST" });
    toast("模型已加载");
  } catch (e) {
    showError(e.message);
    toast("加载失败");
  }
  refreshStatus();
}

async function unloadModel() {
  try {
    await api("/models/unload", { method: "POST" });
    toast("已释放显存");
  } catch (e) { toast(e.message); }
  refreshStatus();
}

/* ============================================================
   模型下载指引
   ============================================================ */
async function openGuide() {
  const modal = $("guideModal");
  modal.classList.remove("hidden");
  const body = $("guideBody");
  body.innerHTML = "加载中…";
  try {
    const data = await api("/system/model-files");
    const f = data.files;
    const row = (label, info, name) => `
      <tr>
        <td>${label}</td>
        <td><code>${name}</code></td>
        <td>${info.size_gb ? info.size_gb + " GB" : "—"}</td>
        <td class="${info.exists ? "file-ok" : "file-missing"}">${info.exists ? "已存在" : "缺失"}</td>
      </tr>`;
    body.innerHTML = `
      <p><strong>模型目录：</strong><code>${escapeHtml(data.model_dir)}</code></p>
      <table>
        <thead><tr><th>类型</th><th>文件</th><th>大小</th><th>状态</th></tr></thead>
        <tbody>
          ${row("扩散模型", f.transformer, f.transformer.path.split(/[\\/]/).pop())}
          ${row("文本编码器", f.text_encoder, f.text_encoder.path.split(/[\\/]/).pop())}
          ${row("VAE", f.vae, f.vae.path.split(/[\\/]/).pop())}
        </tbody>
      </table>
      <p><strong>下载命令</strong>（国内走镜像，把命令粘贴到项目根目录执行）：</p>
      <pre>${escapeHtml(data.download_command)}</pre>
      <p class="muted">
        说明：<code>ENGINE_MODE=mock</code> 为占位出图，不需要模型即可体验全部界面与流程；
        改为 <code>comfy</code>（推荐，本机 ComfyUI 引擎）或 <code>local</code> 并备齐上述文件后即为真实推理。
      </p>
      <p class="muted">
        注意：Q8_0 量化存在已知的张量尺寸不匹配问题，建议使用 Q4_K_M / Q5_K_M / Q6_K。
      </p>`;
  } catch (e) {
    body.innerHTML = `<p class="file-missing">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

/* ============================================================
   参数估算
   ============================================================ */
let estimateTimer = null;
function scheduleEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(runEstimate, 260);
}

async function runEstimate() {
  const { width, height } = currentSize();
  try {
    const data = await api("/preview", {
      method: "POST",
      body: JSON.stringify({
        width, height,
        steps: Number($("steps").value),
        batch_size: Number($("batch").value),
      }),
    });
    let text = `预估 ${data.estimated_seconds.toFixed(1)}s · ${data.megapixels}MP`;
    if (data.estimated_vram_gb) text += ` · 约 ${data.estimated_vram_gb}GB 显存`;
    $("estimate").textContent = text;
    if (data.warnings.length) showError(data.warnings.join("；"));
  } catch {}
}

/* ============================================================
   交互绑定
   ============================================================ */
function switchTab(name) {
  els(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  els(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${name}`));
  if (name === "gallery") refreshGallery();
  if (name === "jobs") refreshJobs();
}

function bindEvents() {
  // 提示词
  $("prompt").addEventListener("input", (e) => {
    $("promptLen").textContent = e.target.value.length;
    scheduleEstimate();
  });

  // 模式切换
  els("#modeSwitch .seg").forEach((btn) => {
    btn.onclick = () => {
      els("#modeSwitch .seg").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.mode = btn.dataset.mode;
      $("refField").style.display = state.mode === "img2img" ? "" : "none";
    };
  });

  // 参考图
  $("btnPickRef").onclick = () => $("refFile").click();
  $("refDrop").onclick = () => $("refFile").click();
  $("refFile").onchange = (e) => { if (e.target.files[0]) readRefFile(e.target.files[0]); };
  ["dragover", "dragleave", "drop"].forEach((evt) => {
    $("refDrop").addEventListener(evt, (e) => {
      e.preventDefault();
      $("refDrop").classList.toggle("dragover", evt === "dragover");
      if (evt === "drop" && e.dataTransfer.files[0]) readRefFile(e.dataTransfer.files[0]);
    });
  });
  // 画布拖入图片 → 自动进入图生图
  const canvas = $("canvas");
  ["dragover", "dragleave", "drop"].forEach((evt) => {
    canvas.addEventListener(evt, (e) => {
      e.preventDefault();
      canvas.classList.toggle("dragover", evt === "dragover");
      if (evt === "drop" && e.dataTransfer.files[0]) {
        readRefFile(e.dataTransfer.files[0]);
        els("#modeSwitch .seg").forEach((b) => {
          const on = b.dataset.mode === "img2img";
          b.classList.toggle("active", on);
          if (on) state.mode = "img2img";
        });
        $("refField").style.display = "";
        switchTab("canvas");
        toast("已载入参考图，切换到图生图模式");
      }
    });
  });

  // 尺寸
  $("sizePreset").onchange = (e) => {
    const custom = e.target.value === "custom";
    $("customSize").style.display = custom ? "" : "none";
    if (!custom) {
      const [w, h] = e.target.value.split("x");
      $("width").value = w; $("height").value = h;
    }
    scheduleEstimate();
  };
  $("width").oninput = scheduleEstimate;
  $("height").oninput = scheduleEstimate;

  // 滑杆
  const sliders = [
    ["steps", "stepsVal", (v) => v],
    ["guidance", "guidanceVal", (v) => Number(v).toFixed(1)],
    ["batch", "batchVal", (v) => v],
    ["strength", "strengthVal", (v) => Number(v).toFixed(2)],
  ];
  sliders.forEach(([id, out, fmt]) => {
    $(id).oninput = (e) => {
      $(out).textContent = fmt(e.target.value);
      scheduleEstimate();
    };
  });

  // 预设
  $("btnPresetFast").onclick = () => {
    $("sizePreset").value = "1024x1024"; $("sizePreset").onchange({ target: $("sizePreset") });
    $("steps").value = 12; $("stepsVal").textContent = "12";
    $("guidance").value = 3.5; $("guidanceVal").textContent = "3.5";
    scheduleEstimate();
    toast("已应用「快速预览」参数");
  };
  $("btnPresetHQ").onclick = () => {
    $("sizePreset").value = "1536x1536"; $("sizePreset").onchange({ target: $("sizePreset") });
    $("steps").value = 30; $("stepsVal").textContent = "30";
    $("guidance").value = 4.5; $("guidanceVal").textContent = "4.5";
    scheduleEstimate();
    toast("已应用「高质量」参数");
  };

  // 种子
  $("btnRandomSeed").onclick = () => {
    $("seed").value = Math.floor(Math.random() * 2147483647);
  };

  // 生成
  $("btnGenerate").onclick = doGenerate;
  $("btnBatch").onclick = doBatch;

  // Tab
  els(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

  // 图库
  $("btnRefreshGallery").onclick = () => { state.gallery.page = 1; refreshGallery(); };
  $("gallerySearch").oninput = () => { state.gallery.page = 1; refreshGallery(); };
  $("favOnly").onchange = () => { state.gallery.page = 1; refreshGallery(); };
  $("pagePrev").onclick = () => { if (state.gallery.page > 1) { state.gallery.page--; refreshGallery(); } };
  $("pageNext").onclick = () => { state.gallery.page++; refreshGallery(); };

  // 任务
  $("btnRefreshJobs").onclick = refreshJobs;

  // 抽屉
  $("drawerClose").onclick = closeDrawer;
  $("drawerMask").onclick = closeDrawer;
  $("drawerFavorite").onclick = async () => {
    const item = state.drawerImage;
    if (!item) return;
    try {
      await api(`/gallery/${item.id}/favorite?favorite=${item.favorite ? "false" : "true"}`, { method: "POST" });
      item.favorite = item.favorite ? 0 : 1;
      $("drawerFavorite").textContent = item.favorite ? "取消收藏" : "收藏";
      refreshGallery();
    } catch (e) { toast(e.message); }
  };
  $("drawerReuse").onclick = () => {
    const item = state.drawerImage;
    if (!item) return;
    $("prompt").value = item.prompt || "";
    $("promptLen").textContent = ($("prompt").value || "").length;
    $("negative").value = item.negative || "";
    if (item.steps) { $("steps").value = item.steps; $("stepsVal").textContent = item.steps; }
    if (item.guidance) { $("guidance").value = item.guidance; $("guidanceVal").textContent = Number(item.guidance).toFixed(1); }
    if (item.seed >= 0) { $("seed").value = item.seed; $("seedLock").checked = true; }
    $("sizePreset").value = "custom";
    $("sizePreset").onchange({ target: $("sizePreset") });
    $("width").value = item.width; $("height").value = item.height;
    closeDrawer();
    switchTab("canvas");
    toast("参数已复现到左侧面板");
  };
  $("drawerAsRef").onclick = async () => {
    const item = state.drawerImage;
    if (!item) return;
    try {
      const blob = await (await fetch(item.url)).blob();
      const reader = new FileReader();
      reader.onload = () => {
        setRefImage(reader.result);
        els("#modeSwitch .seg").forEach((b) => {
          const on = b.dataset.mode === "img2img";
          b.classList.toggle("active", on);
          if (on) state.mode = "img2img";
        });
        $("refField").style.display = "";
        closeDrawer();
        switchTab("canvas");
        toast("已作为参考图载入");
      };
      reader.readAsDataURL(blob);
    } catch (e) { toast("载入失败：" + e.message); }
  };
  $("drawerDelete").onclick = async () => {
    const item = state.drawerImage;
    if (!item || !confirm("确定删除这张图片？文件也会一并删除。")) return;
    try {
      await api(`/gallery/${item.id}`, { method: "DELETE" });
      closeDrawer(); refreshGallery(); refreshStats();
      toast("已删除");
    } catch (e) { toast(e.message); }
  };

  // 模型按钮
  $("btnLoad").onclick = loadModel;
  $("btnUnload").onclick = unloadModel;
  $("btnGuide").onclick = openGuide;
  $("guideClose").onclick = () => $("guideModal").classList.add("hidden");
  $("guideModal").onclick = (e) => { if (e.target.id === "guideModal") $("guideModal").classList.add("hidden"); };

  // 快捷键
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doGenerate(); }
    if (e.key === "Escape") { closeDrawer(); $("guideModal").classList.add("hidden"); }
  });
}

function readRefFile(file) {
  const reader = new FileReader();
  reader.onload = () => setRefImage(reader.result);
  reader.readAsDataURL(file);
}

function setRefImage(dataUrl) {
  state.refImage = dataUrl;
  const img = $("refPreview");
  img.src = dataUrl;
  img.style.display = "block";
  $("refHint").style.display = "none";
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

/* ============================================================
   启动
   ============================================================ */
async function init() {
  bindEvents();
  await refreshStatus();
  await refreshGallery();
  await refreshJobs();
  await runEstimate();

  // 定期刷新状态；有活跃任务时刷新任务列表
  setInterval(refreshStatus, 5000);
  setInterval(() => {
    if ($("pane-jobs").classList.contains("active")) refreshJobs();
  }, 3000);
}

init();
