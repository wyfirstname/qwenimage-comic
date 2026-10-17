/* ============================================================
   团子小剧场 —— 前端逻辑（无框架，直接操作 DOM）

   与后端的约定：
   * 一切慢活（AI 重写 / 出图 / 渲染）都是「提交 + 轮询项目详情」，
     状态只有一个来源（store），前端不维护第二份任务表。
   * 出图完成后由引擎回调自动回填到格子，前端轮询看到 ready 即可，
     不需要手动「采纳」。
   * 轮询期间绝不重建分镜列表——只精准更新状态徽标和图片，
     否则用户正在编辑的文字会被打断。
   ============================================================ */
(function () {
  "use strict";

  const API_BASE = (typeof API !== "undefined" ? API : "/api/v1") + "/storyboard";
  const POLL_MS = 2200;

  const S = {
    opt: null,
    projects: [],
    cur: null,          // 当前项目完整数据
    timer: null,
    beatsDirty: false,  // 分镜有未保存的修改
  };

  const $ = (id) => document.getElementById(id);
  const all = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  /* ---------------- 基础 ---------------- */

  let toastTimer = null;
  function toast(msg, ms = 2600) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
  }

  async function api(path, opts = {}) {
    const res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = {}; }
    if (!res.ok) {
      const msg = data?.detail?.message || data?.detail || text || res.status;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  function busyOf(p) {
    if (!p) return false;
    if ((p.cast || {}).status === "imaging") return true;
    if ((p.render || {}).status === "rendering") return true;
    if ((p.voice || {}).status === "running") return true;
    if (p.beats_state === "generating") return true;
    return (p.beats || []).some((b) => b.status === "imaging"
      || b.audio_status === "recording");
  }

  /* ---------------- 选项与表单 ---------------- */

  function fillSelect(sel, items, value, labeler) {
    sel.innerHTML = items.map((it) => {
      const v = it.id != null ? it.id : it.value;
      const lab = labeler ? labeler(it) : (it.label || it.id);
      return `<option value="${esc(v)}">${esc(lab)}</option>`;
    }).join("");
    if (value != null) sel.value = value;
  }

  function fillOptionForms() {
    const o = S.opt;
    if (!o) return;
    const styles = o.styles || [];
    fillSelect($("npStyle"), styles, o.defaults.style);
    fillSelect($("pjStyle"), styles, o.defaults.style);
    fillSelect($("npPreset"), o.presets, o.defaults.preset,
      (x) => `${x.label}（${x.beats} 格）`);
    fillSelect($("pjPreset"), o.presets, o.defaults.preset,
      (x) => `${x.label}（${x.beats} 格）`);
    fillSelect($("npAspect"), o.aspects, o.defaults.aspect);
    fillSelect($("npSeconds"), (o.durations || []).map((d) => ({ id: d, label: `${d} 秒` })),
      o.defaults.seconds);
    fillSelect($("pjSeconds"), (o.durations || []).map((d) => ({ id: d, label: `${d} 秒` })),
      o.defaults.seconds);
    const st = styles.find((x) => x.id === o.defaults.style);
    if (st) $("npStyleHint").textContent = `${st.ratio} · ${st.render}`;
    const llm = o.llm || {};
    $("npLlmState").textContent = llm.available
      ? `本地模型就绪：${llm.model || ""}`
      : `本地模型不可用（${llm.reason || ""}），将用规则生成分镜`;
    $("btnRewrite").disabled = !llm.available;

    // 旁白音色候选：edge 给音色名，IndexTTS 给示例参考音频
    const tts = o.tts || {};
    const list = tts.voices || [];
    const defLabel = tts.backend === "edge" ? "默认音色（晓晓）" : "内置示例音色";
    const voices = [{ id: "", label: defLabel },
      ...list.map((v) => ({ id: v.id || v.path, label: v.label }))];
    fillSelect($("voiceSpeaker"), voices, tts.speaker || "");
    const backendLabel = {
      edge: "edge-tts · 零模型",
      indextts: `IndexTTS-${tts.version || "2"}`,
      stub: "占位音",
    }[tts.backend] || (tts.version ? `IndexTTS-${tts.version}` : "");
    $("voiceBackend").textContent = tts.available ? backendLabel : "未安装";
    $("btnVoiceAll").disabled = !tts.available;
    $("btnVoiceMissing").disabled = !tts.available;
    if (!tts.available) {
      $("voiceHint").textContent = `旁白不可用：${tts.reason || ""}`;
    } else if (tts.stub) {
      $("voiceHint").textContent =
        "当前是占位音（只用来验证链路）。装 edge-tts 就有真声音：pip install edge-tts";
    } else if (tts.backend === "edge") {
      $("voiceHint").textContent =
        "在线音色，秒级出声、不占显存（需联网）。离线可装 IndexTTS。";
    }
  }

  /* ---------------- 项目列表 ---------------- */

  async function refreshProjects(keepPane = true) {
    const data = await api("/projects");
    S.projects = data.projects || [];
    const box = $("projList");
    if (!S.projects.length) {
      box.innerHTML = `<div class="muted sb-tip">还没有小片，点上面新建。</div>`;
      return;
    }
    box.innerHTML = S.projects.map((p) => {
      const rstat = p.render_status === "done" ? "ready"
        : p.render_status === "failed" ? "failed"
        : p.render_status === "rendering" ? "busy" : "";
      return `<div class="sb-proj ${S.cur && S.cur.id === p.id ? "active" : ""}" data-id="${esc(p.id)}">
        <div class="sb-proj-title">${esc(p.title || p.topic || "未命名")}</div>
        <div class="sb-proj-meta">
          <span class="sb-dot ${rstat}"></span>
          <span>${p.beats_ready}/${p.beats_count} 图</span>
          <span>${esc(p.aspect || "")}</span>
        </div>
      </div>`;
    }).join("");
    all(".sb-proj", box).forEach((el) => {
      el.onclick = () => selectProject(el.dataset.id);
    });
  }

  /* ---------------- 项目详情渲染 ---------------- */

  function showPane(which) {
    $("paneNew").classList.toggle("hidden", which !== "new");
    $("paneProj").classList.toggle("hidden", which !== "proj");
  }

  async function selectProject(pid) {
    try {
      S.cur = await api(`/projects/${pid}`);
      S.beatsDirty = false;
      renderProject();
      showPane("proj");
      refreshProjects();
      pollLoop();
      // 记住位置：刷新/分享链接都能回到这个项目
      try {
        const u = new URL(location.href);
        u.searchParams.set("p", pid);
        history.replaceState(null, "", u);
      } catch { /* file:// 等环境忽略 */ }
    } catch (e) { toast(e.message); }
  }

  function settingsInto(selPrefix, p) {
    $(selPrefix + "Style").value = p.style;
    $(selPrefix + "Preset").value = p.preset;
    const secs = $(selPrefix + "Seconds");
    if (![...secs.options].some((o) => Number(o.value) === Math.round(p.total_seconds))) {
      const op = document.createElement("option");
      op.value = Math.round(p.total_seconds);
      op.textContent = `${Math.round(p.total_seconds)} 秒`;
      secs.appendChild(op);
    }
    secs.value = Math.round(p.total_seconds);
  }

  function renderProject() {
    const p = S.cur;
    if (!p) return;
    $("pjTitle").textContent = p.title || "未命名";
    $("pjMeta").textContent =
      `${(S.opt.styles.find((s) => s.id === p.style) || {}).label || p.style} · ` +
      `${Math.round(p.total_seconds)} 秒 · ${p.width}×${p.height}`;
    $("pjTopic").textContent = p.topic || "";
    $("pjSubject").value = p.subject || "";
    $("pjLock").value = p.lock || "";
    settingsInto("pj", p);

    renderCast();
    renderVoice(p);
    renderBeats();
    renderRender();
    showPane("proj");
  }

  /* ----- 定妆照 ----- */

  function renderCast() {
    const p = S.cur;
    const c = p.cast || {};
    const img = $("castImg"), empty = $("castEmpty");
    if (c.image_url) {
      img.src = c.image_url;
      img.classList.remove("hidden");
      empty.classList.add("hidden");
    } else {
      img.classList.add("hidden");
      empty.classList.remove("hidden");
      empty.textContent = c.status === "imaging" ? "出图中…" : "还没出";
    }
    const st = { none: "还没出", imaging: "出图中…", ready: "已就绪 ✓", failed: `失败：${c.error || ""}` };
    $("castState").textContent = st[c.status] || "";
    $("btnCast").disabled = c.status === "imaging";
  }

  /* ----- 分镜 ----- */

  function beatRow(b) {
    const state = {
      empty: ["空", ""], imaging: ["出图中…", "imaging"], ready: ["已出图 ✓", "ready"],
      failed: [`失败：${b.error || ""}`, "failed"],
    }[b.status] || ["", ""];
    const img = b.image_url
      ? `<img src="${esc(b.image_url)}" alt="" loading="lazy" data-full="${esc(b.image_url)}" />`
      : `<span class="sb-emptyimg">${b.status === "imaging" ? "出图中…" : "暂无画面"}</span>`;
    return `<div class="sb-beat" data-index="${b.index}">
      <div class="sb-beat no">
        <div class="sb-beat-no">${b.index}</div>
        <div class="sb-beat-tag">${esc(b.beat || "")}</div>
      </div>
      <div class="sb-beat-main">
        <div class="sb-beat-row">
          <div style="flex:1">
            <label class="sb-lab">旁白（给人听）</label>
            <textarea data-f="narration" maxlength="60" rows="1">${esc(b.narration)}</textarea>
          </div>
          <div style="flex:1">
            <label class="sb-lab">动作（给模型画）</label>
            <textarea data-f="action" maxlength="60" rows="1">${esc(b.action)}</textarea>
          </div>
        </div>
        <div class="sb-beat-row">
          <div style="flex:2">
            <label class="sb-lab">场景（可留空）</label>
            <input data-f="scene" maxlength="120" value="${esc(b.scene || "")}" placeholder="雪后的街道、傍晚的厨房…" />
          </div>
          <div class="sb-beat-ctrls" style="flex:3">
            <div><label class="sb-lab">景别</label>
              <select data-f="shot">${(S.opt.shot_sizes || []).map((s) =>
                `<option value="${esc(s.id)}" ${s.id === b.shot ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select></div>
            <div><label class="sb-lab">镜头</label>
              <select data-f="camera">${(S.opt.cameras || []).map((c) =>
                `<option value="${esc(c.id)}" ${c.id === b.camera ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select></div>
            <div><label class="sb-lab">时长</label>
              <input data-f="duration" type="number" min="1" max="20" step="0.5" value="${b.duration}" /></div>
          </div>
        </div>
        <div class="sb-beat-foot">
          <span class="sb-beat-state ${state[1]}">${esc(state[0])}</span>
          ${b.audio_status === "ready"
            ? `<span class="sb-audio"><audio controls preload="none" src="${esc(b.audio_url)}"></audio>
                 <span class="muted sb-audio-dur">${(b.audio_duration || 0).toFixed(1)}s</span></span>
               <button class="btn btn-ghost sb-voice-one" data-index="${b.index}">重念</button>`
            : `<button class="btn btn-ghost sb-voice-one" data-index="${b.index}"
                 ${b.audio_status === "recording" ? "disabled" : ""}>${b.audio_status === "recording" ? "念中…" : "念这句"}</button>`}
          <button class="btn btn-ghost sb-shoot" data-index="${b.index}">出这一格</button>
        </div>
      </div>
      <div class="sb-beat-img" data-index="${b.index}">${img}</div>
    </div>`;
  }

  function renderBeats() {
    const p = S.cur;
    const beats = p.beats || [];
    $("beatsCount").textContent = `${beats.length} 格 · 约 ${(
      beats.reduce((s, b) => s + (b.duration || 0), 0) - Math.max(0, beats.length - 1) * (p.xfade || 0.5)
    ).toFixed(0)} 秒`;
    $("beatsList").innerHTML = beats.map(beatRow).join("");
    bindBeatEvents();
    updateShootAllState();
  }

  function updateShootAllState() {
    const anyReady = (S.cur.beats || []).some((b) => b.status === "ready");
    const anyTodo = (S.cur.beats || []).some((b) => b.status !== "ready");
    $("btnShootAll").disabled = !anyTodo;
    $("btnShootAll").textContent = anyReady && anyTodo ? "继续出图" : "全部出图";
  }

  function readBeatsFromDom() {
    return all(".sb-beat").map((el) => {
      const get = (f) => {
        const node = el.querySelector(`[data-f="${f}"]`);
        return node ? node.value.trim() : "";
      };
      return {
        narration: get("narration"), action: get("action"), scene: get("scene"),
        shot: get("shot"), camera: get("camera"),
        duration: Number(get("duration")) || undefined,
      };
    });
  }

  function bindBeatEvents() {
    all(".sb-beat").forEach((el) => {
      el.querySelectorAll("[data-f]").forEach((n) => {
        n.addEventListener("change", () => { S.beatsDirty = true; });
      });
      const shoot = el.querySelector(".sb-shoot");
      if (shoot) shoot.onclick = () => shootOne(Number(el.dataset.index));
      const say = el.querySelector(".sb-voice-one");
      if (say) say.onclick = () => voiceOne(Number(el.dataset.index), say);
    });
    all(".sb-beat-img img").forEach((im) => {
      im.onclick = () => lightbox(im.dataset.full);
    });
  }

  async function saveBeats() {
    const beats = readBeatsFromDom();
    if (!beats.length) return;
    try {
      S.cur = await api(`/projects/${S.cur.id}/beats`, {
        method: "PUT", body: JSON.stringify({ beats }),
      });
      S.beatsDirty = false;
      renderProject();
      refreshProjects();
      toast("分镜已保存");
    } catch (e) { toast(e.message); }
  }

  /* ----- 出图 ----- */

  async function shootCast() {
    try {
      $("btnCast").disabled = true;
      $("castState").textContent = "提交中…";
      await api(`/projects/${S.cur.id}/cast`, {
        method: "POST", body: JSON.stringify({ steps: 8 }),
      });
      renderCast();
      pollLoop();
    } catch (e) { toast(e.message); renderCast(); }
  }

  async function shootOne(index) {
    try {
      // 就地编辑没保存就出图，会画的是旧文案——先替用户存一次
      if (S.beatsDirty) await saveBeats();
      await api(`/projects/${S.cur.id}/shots/${index}`, {
        method: "POST", body: JSON.stringify({ steps: 8 }),
      });
      toast(`第 ${index} 格已提交出图`);
      pollLoop();
    } catch (e) { toast(e.message); }
  }

  async function shootAll() {
    if (S.beatsDirty) await saveBeats();
    const todo = (S.cur.beats || []).filter((b) => b.status !== "ready");
    if (!todo.length) return;
    for (const b of todo) {
      try {
        await api(`/projects/${S.cur.id}/shots/${b.index}`, {
          method: "POST", body: JSON.stringify({ steps: 8 }),
        });
      } catch (e) { toast(`第 ${b.index} 格：${e.message}`); break; }
    }
    toast(`已提交 ${todo.length} 格，出图完成会自动回填`);
    pollLoop();
  }

  /* ----- 渲染 ----- */

  function renderRender() {
    const r = S.cur.render || {};
    const wrap = $("renderProgWrap"), result = $("renderResult");
    if (r.status === "rendering") {
      wrap.classList.remove("hidden");
      result.classList.add("hidden");
      $("renderProg").style.width = `${Math.round((r.progress || 0) * 100)}%`;
      $("renderMsg").textContent = r.message || "渲染中…";
    } else if (r.status === "done" && r.url) {
      wrap.classList.add("hidden");
      result.classList.remove("hidden");
      const v = $("renderVideo");
      if (v.getAttribute("src") !== r.url) { v.src = r.url; v.load(); }
      $("renderInfo").textContent =
        `${(r.duration || 0).toFixed(1)} 秒 · ${r.size_mb || 0} MB` +
        (r.message ? ` · ${r.message}` : "");
    } else {
      wrap.classList.add("hidden");
      if (r.status === "failed") {
        result.classList.remove("hidden");
        $("renderInfo").textContent = `渲染失败：${r.error || ""}`;
        $("renderVideo").removeAttribute("src");
      } else {
        result.classList.add("hidden");
      }
    }
    $("btnRender").disabled = r.status === "rendering";
  }

  async function startRender() {
    if (S.beatsDirty) await saveBeats();
    try {
      await api(`/projects/${S.cur.id}/render`, {
        method: "POST",
        body: JSON.stringify({ burn_subtitles: $("renderSubs").checked }),
      });
      renderRender();
      pollLoop();
    } catch (e) { toast(e.message); }
  }

  /* ----- 旁白 ----- */

  function voiceInputs() {
    const sp = $("voiceSpeaker");
    return {
      speaker: sp ? sp.value : "",
      speed: Number($("voiceSpeed").value) || 1,
      emo_alpha: Number($("voiceEmoAlpha").value),
    };
  }

  async function voiceOne(index, btn) {
    if (!S.cur) return;
    const opts = voiceInputs();
    if (btn) { btn.disabled = true; btn.textContent = "念中…"; }
    try {
      await api(`/projects/${S.cur.id}/voice`, {
        method: "POST", body: JSON.stringify({ ...opts, indices: [index] }),
      });
      pollLoop();
    } catch (e) {
      toast(e.message);
      if (btn) { btn.disabled = false; btn.textContent = "念这句"; }
    }
  }

  async function voiceAll(onlyMissing) {
    if (!S.cur) return;
    if (S.beatsDirty) await saveBeats();
    try {
      await api(`/projects/${S.cur.id}/voice`, {
        method: "POST",
        body: JSON.stringify({ ...voiceInputs(), only_missing: !!onlyMissing }),
      });
      $("voiceProgWrap").classList.remove("hidden");
      $("voiceProg").style.width = "2%";
      $("voiceMsg").textContent = "准备中…";
      pollLoop();
    } catch (e) { toast(e.message); }
  }

  function renderVoice(p) {
    const v = p.voice || {};
    const running = v.status === "running";
    $("btnVoiceAll").disabled = running;
    $("btnVoiceMissing").disabled = running;
    $("btnVoiceClear").disabled = running;
    $("voiceProgWrap").classList.toggle("hidden", !running);
    if (running) {
      $("voiceProg").style.width = `${Math.round((v.progress || 0) * 100)}%`;
      $("voiceMsg").textContent = v.message || "生成中…";
    }
    const done = (p.beats || []).filter((b) => b.audio_status === "ready").length;
    if (v.status === "done") {
      $("voiceHint").textContent = v.message || `${done} 句旁白已就绪`;
    } else if (v.status === "failed") {
      $("voiceHint").textContent = `旁白失败：${v.error || ""}`;
    } else if (!running) {
      $("voiceHint").textContent = done
        ? `${done} 句旁白已就绪。改过文案记得重念这一句。`
        : "有了旁白，每一格的画面时长会跟着配音走，成片改用硬切保证音画同步。";
    }
    $("voiceBackend").textContent = v.backend === "stub"
      ? "占位音（未装模型）"
      : (v.backend ? `IndexTTS` : $("voiceBackend").textContent);
  }

  async function clearVoice() {
    if (!S.cur) return;
    if (!confirm("清空所有旁白音频？文案会保留。")) return;
    try {
      await api(`/projects/${S.cur.id}/voice`, { method: "DELETE" });
      const p = await api(`/projects/${S.cur.id}`);
      S.cur = p;
      renderBeats();
      renderVoice(p);
      toast("旁白已清空");
    } catch (e) { toast(e.message); }
  }

  /* ----- 轮询（只更新动态部分，不打断编辑） ----- */

  function refreshDynamic(p) {
    const prevRenderStatus = (S.cur.render || {}).status;
    const prevVoice = (S.cur.voice || {}).status;
    S.cur = p;

    // cast
    const c = p.cast || {};
    if (c.image_url && $("castImg").getAttribute("src") !== c.image_url) {
      $("castImg").src = c.image_url;
      $("castImg").classList.remove("hidden");
      $("castEmpty").classList.add("hidden");
    } else if (!c.image_url && c.status === "imaging") {
      $("castEmpty").textContent = "出图中…";
    }
    const st = { none: "还没出", imaging: "出图中…", ready: "已就绪 ✓", failed: `失败：${c.error || ""}` };
    $("castState").textContent = st[c.status] || "";
    $("btnCast").disabled = c.status === "imaging";

    // beats 状态与图
    (p.beats || []).forEach((b) => {
      const row = document.querySelector(`.sb-beat[data-index="${b.index}"]`);
      if (!row) return;
      const stateEl = row.querySelector(".sb-beat-state");
      const map = {
        empty: ["空", ""], imaging: ["出图中…", "imaging"], ready: ["已出图 ✓", "ready"],
        failed: [`失败：${(b.error || "").slice(0, 60)}`, "failed"],
      };
      const cur = map[b.status] || ["", ""];
      if (stateEl) { stateEl.textContent = cur[0]; stateEl.className = `sb-beat-state ${cur[1]}`; }
      const imgBox = document.querySelector(`.sb-beat-img[data-index="${b.index}"]`);
      if (imgBox && b.image_url && !imgBox.querySelector("img")) {
        imgBox.innerHTML = `<img src="${esc(b.image_url)}" alt="" data-full="${esc(b.image_url)}" />`;
        const im = imgBox.querySelector("img");
        im.onclick = () => lightbox(b.image_url);
      }
    });
    updateShootAllState();

    // 旁白
    renderVoice(p);
    const vstate = (p.voice || {}).status;
    const audioMissing = (p.beats || []).some((b) => b.audio_status === "ready"
      && !document.querySelector(`.sb-beat[data-index="${b.index}"] audio`));
    if (!S.beatsDirty && (audioMissing || (prevVoice === "running" && vstate !== "running"))) {
      renderBeats();      // 念完要把播放器挂上去 / 去掉「念中…」
    }
    if (prevVoice === "running" && vstate === "done") toast("旁白已生成 🎙");

    // render
    renderRender();
    if (prevRenderStatus !== "done" && (p.render || {}).status === "done") {
      refreshProjects();
      toast("成片渲染完成 🎬");
    }
  }

  function pollLoop() {
    clearInterval(S.timer);
    S.timer = setInterval(async () => {
      if (!S.cur || document.hidden) return;
      if ($("paneProj").classList.contains("hidden")) return;
      try {
        const p = await api(`/projects/${S.cur.id}`);
        refreshDynamic(p);
        if (!busyOf(p)) clearInterval(S.timer);
      } catch { /* 网络抖动不提示，下一轮再说 */ }
    }, POLL_MS);
    // 立即踢一脚
    if (S.cur) {
      api(`/projects/${S.cur.id}`).then((p) => { refreshDynamic(p); }).catch(() => {});
    }
  }

  /* ----- 大图预览 ----- */

  function lightbox(src) {
    if (!src) return;
    const box = document.createElement("div");
    box.className = "sb-lightbox";
    box.innerHTML = `<img src="${esc(src)}" alt="" />`;
    box.onclick = () => box.remove();
    document.body.appendChild(box);
  }

  /* ----- AI 重写 ----- */

  async function rewrite() {
    if (S.beatsDirty && !confirm("有未保存的分镜修改，重写会覆盖未出图的文案。继续？")) return;
    try {
      await api(`/projects/${S.cur.id}/beats/regenerate`, { method: "POST" });
      $("pjRewriteState").textContent = "本地模型写作中…（可能要几分钟）";
      $("pjSettings").open = true;
      clearInterval(S.timer);
      S.timer = setInterval(async () => {
        const p = await api(`/projects/${S.cur.id}`).catch(() => null);
        if (!p) return;
        if (p.beats_state === "generating") {
          $("pjRewriteState").textContent =
            `本地模型写作中… ${Math.round((p.llm_progress || 0) * 100)}%`;
          return;
        }
        clearInterval(S.timer);
        $("pjRewriteState").textContent = p.beats_note || "";
        S.cur = p;
        S.beatsDirty = false;
        renderProject();
        refreshProjects();
        pollLoop();
        toast(p.beats_state === "failed" ? `重写失败：${p.beats_note || ""}` : "分镜已更新");
      }, POLL_MS);
    } catch (e) { toast(e.message); }
  }

  /* ----- 新建 ----- */

  async function createProject() {
    const topic = $("npTopic").value.trim();
    if (!topic) { toast("先写一句选题"); return; }
    try {
      $("btnCreate").disabled = true;
      const p = await api("/projects", {
        method: "POST",
        body: JSON.stringify({
          topic, style: $("npStyle").value, preset: $("npPreset").value,
          aspect: $("npAspect").value, total_seconds: Number($("npSeconds").value),
          subject: $("npSubject").value.trim(), tone: $("npTone").value.trim(),
          extra: $("npExtra").value.trim(), use_llm: $("npUseLLM").checked,
        }),
      });
      S.cur = p;
      $("npTopic").value = "";
      await refreshProjects();
      renderProject();
      showPane("proj");
      toast(p.beats_source === "llm"
        ? "已用本地模型生成浅分镜" : "已生成规则分镜，可点「让本地模型重写分镜」");
      if (p.beats_note) toast(p.beats_note, 4200);
    } catch (e) { toast(e.message); }
    finally { $("btnCreate").disabled = false; }
  }

  /* ----- 项目头操作 ----- */

  async function saveSettings() {
    try {
      const body = {
        style: $("pjStyle").value, preset: $("pjPreset").value,
        total_seconds: Number($("pjSeconds").value),
        subject: $("pjSubject").value.trim(), lock: $("pjLock").value.trim(),
      };
      S.cur = await api(`/projects/${S.cur.id}`, {
        method: "PATCH", body: JSON.stringify(body),
      });
      renderProject();
      refreshProjects();
      toast("设定已保存（已按新时长重排每格秒数）");
    } catch (e) { toast(e.message); }
  }

  async function deleteProject() {
    if (!confirm("删除这个小片？出过的图和成片文件会保留在磁盘上。")) return;
    try {
      await api(`/projects/${S.cur.id}`, { method: "DELETE" });
      S.cur = null;
      clearInterval(S.timer);
      showPane("new");
      refreshProjects();
    } catch (e) { toast(e.message); }
  }

  /* ---------------- 启动 ---------------- */

  async function boot() {
    try {
      S.opt = await api("/options");
      fillOptionForms();
    } catch (e) { toast("加载选项失败：" + e.message); }
    await refreshProjects();
    showPane("new");

    // ?p=<项目id> 直达某个项目；刷新后也能回到正在做的事
    const want = new URLSearchParams(location.search).get("p");
    if (want && S.projects.some((x) => x.id === want)) {
      selectProject(want);
    }

    $("btnNew").onclick = () => { showPane("new"); };
    $("btnCreate").onclick = createProject;
    $("btnSaveSettings").onclick = saveSettings;
    $("btnRewrite").onclick = rewrite;
    $("btnDel").onclick = deleteProject;
    $("btnCast").onclick = shootCast;
    $("btnSaveBeats").onclick = saveBeats;
    $("btnShootAll").onclick = shootAll;
    $("btnRender").onclick = startRender;
    $("btnVoiceAll").onclick = () => voiceAll(false);
    $("btnVoiceMissing").onclick = () => voiceAll(true);
    $("btnVoiceClear").onclick = clearVoice;
    $("npStyle").onchange = () => {
      const st = (S.opt.styles || []).find((x) => x.id === $("npStyle").value);
      if (st) $("npStyleHint").textContent = `${st.ratio} · ${st.render}`;
    };
    $("npPreset").onchange = () => {
      const ps = (S.opt.presets || []).find((x) => x.id === $("npPreset").value);
      if (ps) $("npPresetHint").textContent = ps.desc || "";
    };
  }

  boot();
})();
