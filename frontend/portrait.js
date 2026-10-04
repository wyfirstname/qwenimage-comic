/* ============================================================
   人像编辑 —— Qwen-Image 2.1 官方图像编辑玩法
   ------------------------------------------------------------
   设计约束（重要）：
   * 本文件是**独立视图**，不修改 comic.js / app.js 的任何函数体，
     只复用它们的公共函数（api / toast / openDrawer / refreshGallery）。
   * 导航接管采用「包装原有 onclick」的方式，漫画工作室的切换逻辑不受影响。
   * Tab 使用独立的 pt- 类名，避免被 app.js 的全局 .tab 绑定误抓。
   ============================================================ */
(function () {
  "use strict";

  const PORTRAIT_API = `${API}/portrait`;
  const MAX_IMAGES = 3;

  const pt = {
    loaded: false,
    items: [],
    map: {},
    groups: [],
    preset: null,
    images: [],          // [{ id, name, data, w, h }]
    mp: 0.8,
    jobId: null,
    es: null,
    rewriteId: null,
    rewriteTimer: null,
    results: [],
  };

  const P = (id) => document.getElementById(id);
  const PALL = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  /* ---------------- 提示 ---------------- */
  function ptError(msg) {
    const e = P("ptError");
    e.textContent = msg;
    e.classList.remove("hidden");
  }
  function ptClearError() { P("ptError").classList.add("hidden"); }

  function ptProgress(percent, text) {
    P("ptProgressFill").style.width = `${Math.max(0, Math.min(100, percent))}%`;
    P("ptProgressText").textContent = text || "就绪";
  }

  /* ---------------- 视图切换 ---------------- */
  function hidePortrait() { P("view-portrait").classList.add("hidden"); }

  function showPortrait() {
    P("view-image").classList.add("hidden");
    P("view-comic").classList.add("hidden");
    P("view-portrait").classList.remove("hidden");
    PALL("#mainNav .navitem").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === "portrait"));
    try { history.replaceState(null, "", "?view=portrait"); } catch (e) { /* 忽略 */ }
    if (!pt.loaded) initPortrait();
  }

  function hookNav() {
    PALL("#mainNav .navitem").forEach((b) => {
      const original = b.onclick;               // comic.js 注册的切换逻辑
      if (b.dataset.view === "portrait") {
        b.onclick = () => showPortrait();
      } else {
        b.onclick = (ev) => {
          hidePortrait();
          if (typeof original === "function") original.call(b, ev);
        };
      }
    });
  }

  /* ---------------- 玩法切换 ---------------- */
  function current() { return pt.map[pt.preset] || null; }

  function onPresetChange() {
    const p = current();
    if (!p) return;
    P("ptDesc").textContent = p.desc || "";
    P("ptOfficial").textContent = p.official ? `官方依据：${p.official}` : "";
    P("ptLevelTag").textContent = p.kind === "txt2img" ? "文生图" : (p.level_label || p.level);
    P("ptSizeField").style.display = p.kind === "txt2img" ? "" : "none";
    P("ptMpField").style.display = p.kind === "txt2img" ? "none" : "";
    P("ptImgHint").textContent = p.kind === "txt2img"
      ? "本玩法不需要参考图"
      : `需要 ${p.images.min}${p.images.max !== p.images.min ? "~" + p.images.max : ""} 张`;
    renderVars();
    renderImages();
    compose();
  }

  function renderVars() {
    const p = current();
    const box = P("ptVars");
    box.innerHTML = "";
    if (!p) return;
    (p.fields || []).forEach((f) => {
      const wrap = document.createElement("div");
      wrap.className = "mini-form";
      const label = document.createElement("label");
      label.textContent = f.label + (f.required ? " *" : "");
      const input = f.multiline
        ? document.createElement("textarea")
        : document.createElement("input");
      if (f.multiline) input.rows = 3;
      input.placeholder = f.placeholder || "";
      input.value = f.default || "";
      input.dataset.key = f.key;
      input.addEventListener("input", compose);
      wrap.appendChild(label);
      wrap.appendChild(input);
      if (f.hint) {
        const h = document.createElement("p");
        h.className = "hint muted";
        h.textContent = f.hint;
        wrap.appendChild(h);
      }
      box.appendChild(wrap);
    });
  }

  function collectValues() {
    const out = {};
    PALL("#ptVars [data-key]").forEach((el) => { out[el.dataset.key] = el.value || ""; });
    return out;
  }

  /* ---------------- 参考图 ---------------- */
  function renderImages() {
    const box = P("ptThumbs");
    box.innerHTML = "";
    pt.images.forEach((it, idx) => {
      const d = document.createElement("div");
      d.className = "pt-thumb";
      d.title = idx === 0 ? "image_1（决定输出尺寸）" : `点击设为 image_1`;
      d.innerHTML = `
        <span class="pt-thumb-no">image_${idx + 1}</span>
        <button class="pt-thumb-del" title="移除">×</button>
        <img src="${it.data}" alt="" />
        ${idx === 0 ? '<span class="pt-thumb-first">决定输出尺寸</span>' : ""}`;
      d.querySelector(".pt-thumb-del").onclick = (ev) => {
        ev.stopPropagation();
        pt.images.splice(idx, 1);
        renderImages(); compose();
      };
      d.onclick = () => {
        if (idx === 0) return;
        const [item] = pt.images.splice(idx, 1);
        pt.images.unshift(item);
        renderImages(); compose();
      };
      box.appendChild(d);
    });

    const p = current();
    const dropHint = P("ptDropHint");
    if (p && p.kind === "txt2img") {
      dropHint.textContent = "本玩法为文生图，无需参考图";
    } else if (pt.images.length) {
      dropHint.textContent = `已上传 ${pt.images.length} 张，继续添加或点击缩略图调整顺序`;
    } else {
      dropHint.textContent = "点击选择，或把图片拖到这里（可多选）";
    }
    P("ptImgTip").style.display = p && p.kind === "txt2img" ? "none" : "";
  }

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const img = new Image();
        img.onload = () => resolve({
          id: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
          name: file.name,
          data: reader.result,
          w: img.naturalWidth,
          h: img.naturalHeight,
        });
        img.onerror = () => reject(new Error("图片解码失败"));
        img.src = reader.result;
      };
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsDataURL(file);
    });
  }

  async function addFiles(files) {
    const list = Array.from(files || []).filter((f) => f.type.startsWith("image/"));
    if (!list.length) return;
    const p = current();
    const limit = p ? Math.min(p.images.max || MAX_IMAGES, MAX_IMAGES) : MAX_IMAGES;
    for (const f of list) {
      if (pt.images.length >= limit) {
        toast(`最多 ${limit} 张参考图（官方建议 1~3 张）`);
        break;
      }
      try {
        pt.images.push(await readFile(f));
      } catch (e) {
        ptError(String(e.message || e));
      }
    }
    renderImages();
    compose();
  }

  /* ---------------- 尺寸预算（与后端 normalize_reference 同一套口径） ---------------- */
  function budgetSize(w, h, mp) {
    const maxSide = 1280;
    let scale = 1;
    if (w * h > mp * 1e6) scale = Math.min(scale, Math.sqrt((mp * 1e6) / (w * h)));
    if (Math.max(w, h) > maxSide) scale = Math.min(scale, maxSide / Math.max(w, h));
    const r = (v) => Math.max(256, Math.round(v / 64) * 64);
    return [r(w * scale), r(h * scale)];
  }

  function effectiveSize() {
    const p = current();
    if (p && p.kind === "edit" && pt.images.length) {
      return budgetSize(pt.images[0].w, pt.images[0].h, pt.mp);
    }
    return [Number(P("ptWidth").value) || 768, Number(P("ptHeight").value) || 1024];
  }

  /* ---------------- 组装提示词 ---------------- */
  function payloadBase() {
    return {
      preset: pt.preset,
      values: collectValues(),
      image_count: pt.images.length,
      prompt_override: P("ptPrompt").value.trim(),
    };
  }

  let composeTimer = null;
  function compose() {
    clearTimeout(composeTimer);
    composeTimer = setTimeout(doCompose, 180);
    updateEstimate();
  }

  async function doCompose() {
    if (!pt.preset) return;
    try {
      const info = await api("/portrait/compose", {
        method: "POST",
        body: JSON.stringify({ ...payloadBase(), prompt_override: "" }),
      });
      P("ptPrompt").value = info.prompt;
      P("ptPromptState").textContent = "模板自动组装";
      renderSections(info.sections, info);
      renderWarnings(info.warnings);
      ptClearError();
    } catch (e) {
      ptError(String(e.message || e));
    }
  }

  function renderSections(sections, info) {
    const box = P("ptSections");
    box.innerHTML = (sections || []).map((s) => `
      <div class="pt-sec">
        <span class="pt-sec-label">${esc(s.label)}</span>
        <div class="pt-sec-text">${esc(s.text)}</div>
      </div>`).join("") || `<p class="muted">${esc(info && info.name ? info.name : "")}</p>`;
  }

  function renderWarnings(list) {
    const box = P("ptWarnings");
    if (!list || !list.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    box.innerHTML = `<ul>${list.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
    box.classList.remove("hidden");
  }

  /* ---------------- 耗时估算 ---------------- */
  let estTimer = null;
  function updateEstimate() {
    clearTimeout(estTimer);
    estTimer = setTimeout(async () => {
      const [w, h] = effectiveSize();
      const steps = Number(P("ptSteps").value) || 8;
      try {
        const r = await api("/preview", {
          method: "POST",
          body: JSON.stringify({ width: w, height: h, steps, batch_size: 1 }),
        });
        const mins = r.estimated_seconds / 60;
        const p = current();
        const sizeNote = p && p.kind === "edit"
          ? (pt.images.length
            ? `输出尺寸按 image_1 计：${w}×${h}`
            : "输出尺寸将在上传参考图后按 image_1 计算")
          : `输出尺寸：${w}×${h}`;
        P("ptEstimate").textContent =
          `${sizeNote} · 约 ${mins < 1 ? "<1" : mins.toFixed(0)} 分钟`
          + (r.warnings && r.warnings.length ? `（${r.warnings[0]}）` : "");
      } catch (e) { /* 估算失败不影响出图 */ }
    }, 300);
  }

  /* ---------------- 出图 ---------------- */
  async function doGenerate() {
    const p = current();
    if (!p) return;
    ptClearError();
    if (p.kind === "edit" && pt.images.length < p.images.min) {
      ptError(`「${p.name}」需要${p.images.min === p.images.max ? "" : "至少 "}`
        + `${p.images.min} 张参考图，当前 ${pt.images.length} 张`);
      return;
    }
    const [w, h] = effectiveSize();
    const seedLocked = P("ptSeedLock").checked;
    const seed = seedLocked ? (Number(P("ptSeed").value) || 0) : -1;
    const btn = P("ptGenerate");
    btn.disabled = true;
    btn.textContent = "提交中…";
    ptProgress(1, "提交任务…");
    try {
      const res = await api("/portrait/submit", {
        method: "POST",
        body: JSON.stringify({
          ...payloadBase(),
          images: pt.images.map((i) => i.data),
          steps: Number(P("ptSteps").value) || 8,
          seed,
          count: 1,
          width: w,
          height: h,
          max_mp: pt.mp,
        }),
      });
      pt.jobId = res.job_id;
      P("ptCanvasEmpty").classList.add("hidden");
      P("ptResults").innerHTML = "";
      ptProgress(2, "已排队…");
      renderSections(res.sections, res);
      renderWarnings(res.warnings);
      toast(`任务已提交（${res.width}×${res.height}）`);
      subscribe(res.job_id);
    } catch (e) {
      ptError(String(e.message || e));
      ptProgress(0, "提交失败");
      btn.disabled = false;
      btn.textContent = "开始生成";
    }
  }

  function subscribe(jobId) {
    if (pt.es) { pt.es.close(); pt.es = null; }
    const es = new EventSource(`${API}/jobs/${jobId}/events`);
    pt.es = es;
    let got = 0;

    es.addEventListener("status", () => ptProgress(3, "已提交…"));
    es.addEventListener("progress", (e) => {
      let d = {};
      try { d = JSON.parse(e.data); } catch { /* 忽略 */ }
      ptProgress(d.percent || 5, d.message || `采样 ${d.step || 0}/${d.total || "?"}`);
    });
    es.addEventListener("image", (e) => {
      let d = {};
      try { d = JSON.parse(e.data); } catch { /* 忽略 */ }
      got += 1;
      P("ptCanvasEmpty").classList.add("hidden");
      addResultThumb(d);
    });
    es.addEventListener("done", (e) => {
      let d = {};
      try { d = JSON.parse(e.data); } catch { /* 忽略 */ }
      es.close(); pt.es = null;
      finishBusy();
      if (d.status === "succeeded") {
        ptProgress(100, `完成，共 ${d.count || got} 张`);
        toast(`生成完成，共 ${d.count || got} 张`);
      } else {
        ptProgress(0, "已结束");
      }
      refreshGallerySafe();
      refreshStatsSafe();
    });
    es.addEventListener("error", (e) => {
      let msg = "生成失败";
      try { msg = JSON.parse(e.data).error || msg; } catch { /* 忽略 */ }
      ptProgress(0, "失败");
      ptError(msg);
      es.close(); pt.es = null;
      finishBusy();
    });
    es.onerror = () => { es.close(); pt.es = null; };   // 传输中断：任务状态以数据库为准
  }

  function finishBusy() {
    const btn = P("ptGenerate");
    btn.disabled = false;
    btn.textContent = "开始生成";
  }

  function addResultThumb(d) {
    const img = document.createElement("img");
    img.src = d.url;
    img.alt = `seed ${d.seed}`;
    img.title = `seed ${d.seed} · ${d.width}×${d.height} · 点击查看大图`;
    img.onclick = () => { if (typeof openDrawer === "function") openDrawer(d.id); };
    P("ptResults").prepend(img);
  }

  function refreshGallerySafe() {
    try { if (typeof refreshGallery === "function") refreshGallery(); } catch (e) { /* 忽略 */ }
  }
  function refreshStatsSafe() {
    try { if (typeof refreshStats === "function") refreshStats(); } catch (e) { /* 忽略 */ }
  }

  /* ---------------- AI 改写（本地文本模型，官方建议） ---------------- */
  async function doRewrite() {
    const p = current();
    if (!p) return;
    ptClearError();
    const btn = P("ptRewrite");
    btn.disabled = true;
    btn.textContent = "改写中…";
    ptProgress(5, "AI 改写提示词…");
    try {
      const task = await api("/portrait/rewrite", {
        method: "POST",
        body: JSON.stringify(payloadBase()),
      });
      pt.rewriteId = task.id;
      pollRewrite(task.id);
    } catch (e) {
      ptError(String(e.message || e));
      btn.disabled = false;
      btn.textContent = "AI 改写提示词";
    }
  }

  function pollRewrite(taskId) {
    clearTimeout(pt.rewriteTimer);
    const tick = async () => {
      try {
        const t = await api(`/portrait/rewrite/tasks/${taskId}`);
        if (t.status === "done") {
          const prompt = (t.result && t.result.prompt) || "";
          if (prompt) {
            P("ptPrompt").value = prompt;
            P("ptPromptState").textContent = `AI 改写（${t.elapsed || 0} 秒）`;
            switchPtTab("prompt");
            toast("已改写，可在「提示词」页查看或微调");
          }
          ptProgress(100, "改写完成");
          resetRewriteBtn();
          return;
        }
        if (t.status === "failed" || t.status === "canceled") {
          ptError(t.error || "改写失败");
          ptProgress(0, t.status === "canceled" ? "已取消" : "改写失败");
          resetRewriteBtn();
          return;
        }
        ptProgress(Math.min(95, 10 + (t.elapsed || 0) / 2), t.message || "改写中…");
        pt.rewriteTimer = setTimeout(tick, 2500);
      } catch (e) {
        ptError(String(e.message || e));
        resetRewriteBtn();
      }
    };
    tick();
  }

  function resetRewriteBtn() {
    const btn = P("ptRewrite");
    btn.disabled = false;
    btn.textContent = "AI 改写提示词";
  }

  /* ---------------- Tab ---------------- */
  function switchPtTab(name) {
    PALL(".pt-tab").forEach((t) => t.classList.toggle("active", t.dataset.pttab === name));
    PALL(".pt-pane").forEach((p) => p.classList.toggle("active", p.id === `pt-pane-${name}`));
  }

  /* ---------------- 说明 ---------------- */
  function renderHelp() {
    P("ptHelp").innerHTML = `
      <h3>玩法总览（按官方 Qwen-Image 2.1 图像编辑文档实现）</h3>
      <ul>
        <li><b>场景重绘人像</b>：保留人物身份，重建整个场景（可换装、换动作）。对应官方
          Traditional clothing / Mangrove / Indoor candid portrait 三例。</li>
        <li><b>发型编辑 / 表情编辑</b>：只改一处，其余全部不动，提示词尾部固定声明「三不变」。</li>
        <li><b>老照片修复</b>：去噪去划痕、上色、提清晰度，强调<b>不虚构、不美化</b>。</li>
        <li><b>照片风格化</b>：整张转成指定画风，结构不得变形、不留照片斑块。</li>
        <li><b>多图合成 / 换装</b>：官方 2509 多图输入能力，「人+人」「人+商品」「人+场景」，
          1~3 张效果最佳。</li>
        <li><b>编辑风人像</b>：不需要参考图，纯文生图，对应官方 Editorial portrait 示例。</li>
      </ul>

      <h3>官方提示词结构（本页模板已内置）</h3>
      <ul>
        <li><b>① 先锁身份再改场景</b>：开头必须写「以 image_1 中人物为身份基准，面部五官…沿用原图不变」。</li>
        <li><b>② 局部编辑写「三不变」</b>：保持原图取景、构图、缩放级别与人物大小位置不变，不作重新构图或整体重绘。</li>
        <li><b>③ 新增元素顺应原图光线</b>：新加的手、表情要写明受光与阴影遵循原图统一光照逻辑。</li>
        <li><b>④ 修复类强调「不虚构」</b>：宁可保留自然柔化，也不让模型编造细节或美化面部。</li>
      </ul>

      <h3>本机实现说明</h3>
      <ul>
        <li>参考图按上传顺序编号为 <code>image_1…image_N</code>，提示词里直接用编号指代；
          <b>image_1 决定输出尺寸</b>（官方约定：latent 取自参考图）。</li>
        <li>本机走官方多参考图编辑通道，固定 <code>CFG=1</code>、<code>denoise=1</code>，
          因此<b>负面提示词不生效</b>——「不要什么」要写进正向提示词，模板已内置常见禁止项。</li>
        <li>参考图会被压到「像素预算」内（默认 0.8MP，最长边 1280，边长对齐 64 的倍数）。
          本机 RTX 2060 6GB 实测约 260 秒/步/百万像素，2K 原图直出会跑到数小时。</li>
        <li><b>AI 改写</b>复用 Qwen-Image 自带的 Qwen3-VL-8B 文本编码器（本地、不联网、不额外下载模型），
          官方文档指出编辑类任务不改写提示词时结果容易不稳定。</li>
        <li>出图走既有出图队列，结果同时进入「图片生成 → 图库」，按
          <code>outputs/portrait/日期/</code> 归档。</li>
      </ul>`;
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    P("ptPreset").onchange = () => { pt.preset = P("ptPreset").value; onPresetChange(); };
    P("ptPickBtn").onclick = () => P("ptFile").click();
    P("ptDrop").onclick = () => P("ptFile").click();
    P("ptFile").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };
    ["dragover", "dragleave", "drop"].forEach((evt) => {
      P("ptDrop").addEventListener(evt, (e) => {
        e.preventDefault();
        P("ptDrop").classList.toggle("dragover", evt === "dragover");
        if (evt === "drop") addFiles(e.dataTransfer.files);
      });
    });

    PALL("#ptMp .seg").forEach((btn) => {
      btn.onclick = () => {
        PALL("#ptMp .seg").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        pt.mp = Number(btn.dataset.mp) || 0.8;
        compose();
      };
    });

    P("ptSteps").oninput = (e) => {
      P("ptStepsVal").textContent = e.target.value;
      updateEstimate();
    };
    P("ptWidth").oninput = updateEstimate;
    P("ptHeight").oninput = updateEstimate;
    P("ptSeedRandom").onclick = () => { P("ptSeed").value = -1; };
    P("ptSeedLock").onchange = (e) => {
      if (e.target.checked && Number(P("ptSeed").value) < 0) {
        P("ptSeed").value = Math.floor(Math.random() * 2147483647);
      }
    };

    P("ptComposeBtn").onclick = () => doCompose();
    P("ptRewrite").onclick = doRewrite;
    P("ptGenerate").onclick = doGenerate;
    P("ptPrompt").addEventListener("input", () => {
      P("ptPromptState").textContent = "已手动编辑";
    });

    PALL(".pt-tab").forEach((t) => {
      t.onclick = () => switchPtTab(t.dataset.pttab);
    });
  }

  /* ---------------- 初始化 ---------------- */
  async function initPortrait() {
    pt.loaded = true;
    bind();
    renderHelp();
    try {
      const data = await api("/portrait/presets");
      pt.items = data.items || [];
      pt.groups = data.groups || [];
      pt.map = {};
      pt.items.forEach((i) => { pt.map[i.id] = i; });

      const sel = P("ptPreset");
      sel.innerHTML = "";
      pt.groups.forEach((g) => {
        const og = document.createElement("optgroup");
        og.label = g;
        pt.items.filter((i) => i.group === g).forEach((i) => {
          const o = document.createElement("option");
          o.value = i.id;
          o.textContent = i.name;
          og.appendChild(o);
        });
        sel.appendChild(og);
      });
      pt.preset = pt.items.length ? pt.items[0].id : null;
      sel.value = pt.preset || "";
      onPresetChange();
      ptProgress(0, "就绪");
    } catch (e) {
      ptError(`玩法加载失败：${e.message || e}`);
    }
  }

  /* 启动 */
  hookNav();
  try {
    const q = new URLSearchParams(location.search);
    if (q.get("view") === "portrait") showPortrait();
  } catch (e) { /* 忽略 */ }
})();
