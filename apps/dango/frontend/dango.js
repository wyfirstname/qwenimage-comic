/* ============================================================
   团子衣橱 —— 前端逻辑（无框架，直接操作 DOM）
   ------------------------------------------------------------
   三段式：捏团子（文生图素体）→ 做衣服（文生图单品）→ 试衣间（多参考图换装）

   与后端的约定：
   * 一切出图都是「提交任务 + 轮询」，不在请求里等。同一时刻只追一个任务，
     因为引擎本来就是串行的，同时开两个只会互相排队。
   * 出图结果先进「候选」，用户点一下才写进资料库——模型出的东西不能自动
     变成资产，否则衣柜两天就变成垃圾场。
   ============================================================ */
(function () {
  "use strict";

  const API_BASE = (typeof API !== "undefined" ? API : "/api/v1") + "/dango";
  const POLL_MS = 2500;

  const S = {
    opt: null,
    lib: { characters: [], outfits: [] },
    tab: "create",
    create: {
      style: "mochi", proportion: 2.5, hair: "short", hair_color: "栗棕色",
      eye: "big", eye_color: "琥珀色", skin: "白皙偏暖", expression: "smile",
      width: 768, height: 1152, steps: 8, count: 1,
    },
    design: { slot: "full", style: "mochi", width: 768, height: 768, steps: 8, count: 1 },
    charId: null,
    picked: new Set(),
    slotFilter: "",
    tweak: "hair_color",
  };

  const J = { id: null, timer: null, pane: "", onDone: null };

  const $ = (id) => document.getElementById(id);
  const all = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  /* ---------------- 请求 ---------------- */
  async function req(path, options = {}) {
    const res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
    if (!res.ok) {
      const d = data && data.detail ? data.detail : data;
      const msg = typeof d === "string" ? d : (d && d.message) || res.statusText;
      const err = new Error(msg || `请求失败 (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function say(msg) {
    if (typeof toast === "function") toast(msg);
    else console.log(msg);
  }

  /* ---------------- 进度 ---------------- */
  const PANES = {
    create: { wrap: "crProgressWrap", fill: "crProgressFill", text: "crProgressText", err: "crError" },
    design: { wrap: "dsProgressWrap", fill: "dsProgressFill", text: "dsProgressText", err: "dsError" },
    room: { wrap: "rmProgressWrap", fill: "rmProgressFill", text: "rmProgressText", err: "rmError" },
  };

  function progress(pane, percent, text) {
    const p = PANES[pane]; if (!p) return;
    $(p.fill).style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
    $(p.text).textContent = text || "就绪";
  }
  function showProgress(pane) { $(PANES[pane].wrap).classList.remove("hidden"); }
  function hideProgress(pane) { $(PANES[pane].wrap).classList.add("hidden"); }
  function fail(pane, msg) {
    const el = $(PANES[pane].err);
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function clearFail(pane) { $(PANES[pane].err).classList.add("hidden"); }

  /* ---------------- 任务轮询 ---------------- */
  function stopJob() {
    clearInterval(J.timer);
    J.timer = null;
    if (J.pane) hideProgress(J.pane);
  }

  function startJob(pane, resp, onDone) {
    stopJob();
    clearFail(pane);
    J.id = resp.job_id;
    J.pane = pane;
    J.onDone = onDone;
    showProgress(pane);
    progress(pane, 2, "已排队");
    const note = estimateNote(resp.estimate);
    if (note) $(PANES[pane].text).textContent = note;

    J.timer = setInterval(poll, POLL_MS);
    poll();
  }

  async function poll() {
    if (!J.id) return;
    let job;
    try {
      job = await req(`/jobs/${J.id}`);
    } catch (e) {
      stopJob();
      if (e.status === 404) fail(J.pane, "任务丢了（服务可能重启过），重新提交一次");
      else fail(J.pane, e.message);
      return;
    }
    const pct = job.progress || 0;
    progress(J.pane, pct, job.message || statusText(job.status));
    if (job.status === "succeeded") {
      const imgs = job.images || [];
      const done = J.onDone;
      stopJob();
      if (done) done(imgs, job);
    } else if (job.status === "failed" || job.status === "canceled") {
      stopJob();
      fail(J.pane, job.error || (job.status === "canceled" ? "已取消" : "生成失败"));
    }
  }

  function statusText(s) {
    return { queued: "排队中", running: "推理中", succeeded: "完成",
             failed: "失败", canceled: "已取消" }[s] || s;
  }

  function estimateNote(est) {
    if (!est || !est.estimated_seconds) return "";
    const sec = est.estimated_seconds;
    if (sec < 90) return `预计 ${Math.round(sec)} 秒`;
    return `预计 ${(sec / 60).toFixed(sec < 600 ? 1 : 0)} 分钟`;
  }

  /* ---------------- 图片放大 ---------------- */
  function zoom(url) {
    const box = document.createElement("div");
    box.className = "dg-lightbox";
    box.innerHTML = `<img src="${esc(url)}" alt="" />`;
    box.onclick = () => box.remove();
    document.body.appendChild(box);
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") all(".dg-lightbox").forEach((n) => n.remove());
  });

  /* ---------------- 通用：选项目录渲染 ---------------- */
  function chips(container, items, isActive, onPick) {
    const box = $(container);
    box.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "dg-chip" + (isActive(it) ? " active" : "");
      b.textContent = it.label;
      if (it.title) b.title = it.title;
      b.onclick = () => { onPick(it); refreshChips(container, items, isActive, onPick); };
      box.appendChild(b);
    });
  }

  function refreshChips(container, items, isActive, onPick) {
    all(".dg-chip", $(container)).forEach((el, i) => {
      el.classList.toggle("active", isActive(items[i]));
    });
  }

  function segmented(container, items, isActive, onPick) {
    const box = $(container);
    box.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "seg" + (isActive(it) ? " active" : "");
      b.textContent = it.label;
      b.dataset.key = String(it.key);
      b.onclick = () => {
        all(".seg", box).forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        onPick(it);
      };
      box.appendChild(b);
    });
  }

  function fillSelect(id, items, value) {
    const sel = $(id);
    sel.innerHTML = items.map((it) =>
      `<option value="${esc(it.value)}"${it.value === value ? " selected" : ""}>${esc(it.label)}</option>`
    ).join("");
  }

  /* ============================================================
     初始化
     ============================================================ */
  async function boot() {
    bindTabs();
    bindCreate();
    bindDesign();
    bindRoom();
    try {
      S.opt = await req("/options");
      S.create.style = S.opt.defaults.style;
      S.create.proportion = S.opt.defaults.proportion;
      S.design.style = S.opt.defaults.style;
      renderCreateOptions();
      renderDesignOptions();
    } catch (e) {
      fail("create", "选项加载失败：" + e.message);
    }
    await refreshLibrary();
    const initial = (location.hash || "").replace("#", "");
    if (["design", "room"].includes(initial)) switchTab(initial);
  }

  function bindTabs() {
    all(".dg-tab").forEach((b) => {
      b.onclick = () => switchTab(b.dataset.tab);
    });
    // 页签记住在 hash 里：刷新不跳回第一步，也方便直接收藏「试衣间」
    window.addEventListener("hashchange", () => {
      const tab = (location.hash || "").replace("#", "");
      if (tab && tab !== S.tab && ["create", "design", "room"].includes(tab)) switchTab(tab);
    });
  }

  function switchTab(tab) {
    S.tab = tab;
    all(".dg-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    all(".dg-pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${tab}`));
    if (location.hash.replace("#", "") !== tab) {
      try { history.replaceState(null, "", `#${tab}`); } catch (e) { /* 忽略 */ }
    }
    if (tab === "room") renderRoom();
  }

  async function refreshLibrary() {
    try {
      const lib = await req("/library");
      S.lib = lib;
      const n = lib.stats || { characters: 0, outfits: 0 };
      $("dgStats").textContent = `${n.characters} 个团子 · ${n.outfits} 件衣服`;
      if (!S.charId && lib.characters.length) S.charId = lib.characters[0].id;
      if (S.charId && !lib.characters.some((c) => c.id === S.charId)) {
        S.charId = lib.characters.length ? lib.characters[0].id : null;
      }
      renderRoom();
      renderWardrobeMini();
    } catch (e) {
      say("资料库加载失败：" + e.message);
    }
  }

  function currentChar() {
    return S.lib.characters.find((c) => c.id === S.charId) || null;
  }

  /* ============================================================
     ① 捏团子
     ============================================================ */
  function renderCreateOptions() {
    if (!S.opt) return;
    chips("crStyle", S.opt.styles, (it) => it.id === S.create.style,
      (it) => { S.create.style = it.id; });

    segmented("crProp", S.opt.proportions.map((p) => ({ ...p, key: p.value })),
      (it) => it.value === S.create.proportion,
      (it) => { S.create.proportion = it.value; $("crPropHint").textContent = it.desc; });

    const prop = S.opt.proportions.find((p) => p.value === S.create.proportion);
    $("crPropHint").textContent = prop ? prop.desc : "";

    fillSelect("crHair", S.opt.hairs.map((h) => ({ value: h.id, label: h.label })), S.create.hair);
    fillSelect("crHairColor", S.opt.hair_colors.map((c) => ({ value: c, label: c })), S.create.hair_color);
    fillSelect("crEye", S.opt.eyes.map((h) => ({ value: h.id, label: h.label })), S.create.eye);
    fillSelect("crEyeColor", S.opt.eye_colors.map((c) => ({ value: c, label: c })), S.create.eye_color);
    fillSelect("crSkin", S.opt.skins.map((c) => ({ value: c, label: c })), S.create.skin);
    fillSelect("crExpr", S.opt.expressions.map((h) => ({ value: h.id, label: h.label })), S.create.expression);

    segmented("crSize", S.opt.sizes.character.map((s) => ({
      label: s.label, key: `${s.w}x${s.h}`, w: s.w, h: s.h,
    })), (it) => it.w === S.create.width && it.h === S.create.height,
      (it) => { S.create.width = it.w; S.create.height = it.h; });
  }

  function bindCreate() {
    $("crHair").onchange = (e) => { S.create.hair = e.target.value; };
    $("crHairColor").onchange = (e) => { S.create.hair_color = e.target.value; };
    $("crEye").onchange = (e) => { S.create.eye = e.target.value; };
    $("crEyeColor").onchange = (e) => { S.create.eye_color = e.target.value; };
    $("crSkin").onchange = (e) => { S.create.skin = e.target.value; };
    $("crExpr").onchange = (e) => { S.create.expression = e.target.value; };

    $("crSteps").oninput = (e) => {
      S.create.steps = Number(e.target.value);
      $("crStepsVal").textContent = S.create.steps;
    };
    all("#crCount .seg").forEach((b) => {
      b.onclick = () => {
        all("#crCount .seg").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        S.create.count = Number(b.dataset.n);
      };
    });

    $("crGo").onclick = doCreate;
    $("crCancel").onclick = cancelJob;
    $("crTogglePrompt").onclick = () => $("crPromptBox").classList.toggle("hidden");
  }

  async function doCreate() {
    clearFail("create");
    $("crGo").disabled = true;
    try {
      const body = {
        params: {
          style: S.create.style,
          proportion: S.create.proportion,
          hair: S.create.hair,
          hair_color: S.create.hair_color,
          eye: S.create.eye,
          eye_color: S.create.eye_color,
          skin: S.create.skin,
          expression: S.create.expression,
        },
        steps: S.create.steps,
        count: S.create.count,
        width: S.create.width,
        height: S.create.height,
        seed: -1,
      };
      const resp = await req("/characters", { method: "POST", body: JSON.stringify(body) });
      $("crPromptText").value = resp.prompt;
      renderSections("crSections", resp.sections);
      startJob("create", resp, (images) => {
        renderCreateResults(images);
        if (images.length) {
          $("crEmpty").classList.add("hidden");
          $("crHero").classList.remove("hidden");
          $("crHeroImg").src = images[0].url;
        }
      });
    } catch (e) {
      fail("create", e.message);
    } finally {
      $("crGo").disabled = false;
    }
  }

  function renderSections(container, sections) {
    $(container).innerHTML = (sections || []).map((s) => `
      <div class="dg-section">
        <div class="dg-section-label">${esc(s.label)}</div>
        <div class="dg-section-text">${esc(s.text)}</div>
      </div>`).join("");
  }

  function renderCreateResults(images) {
    const box = $("crResults");
    box.innerHTML = "";
    images.forEach((img) => {
      const card = document.createElement("div");
      card.className = "dg-card";
      card.innerHTML = `
        <img src="${esc(img.url)}" alt="" />
        <div class="dg-card-foot">
          <span class="muted">seed ${esc(img.seed)}</span>
          <button class="btn btn-primary" data-act="keep">就用这张</button>
        </div>`;
      card.querySelector("img").onclick = () => zoom(img.url);
      card.querySelector('[data-act="keep"]').onclick = () => keepCharacter(img);
      box.appendChild(card);
    });
  }

  function keepCharacter(img) {
    openSave({
      title: "给这个团子起个名字",
      preview: img.url,
      defaultName: "",
      withSlot: false,
      onConfirm: async (name) => {
        const ch = await req("/characters/commit", {
          method: "POST",
          body: JSON.stringify({
            name, image_id: img.id, params: {
              style: S.create.style, proportion: S.create.proportion,
              hair: S.create.hair, hair_color: S.create.hair_color,
              eye: S.create.eye, eye_color: S.create.eye_color,
              skin: S.create.skin, expression: S.create.expression,
            },
          }),
        });
        S.charId = ch.id;
        await refreshLibrary();
        say(`「${ch.name}」已经住进衣橱了`);
        switchTab("room");
      },
    });
  }

  /* ============================================================
     ② 做衣服
     ============================================================ */
  function renderDesignOptions() {
    if (!S.opt) return;
    chips("dsSlot", S.opt.slots, (it) => it.id === S.design.slot,
      (it) => { S.design.slot = it.id; });
    chips("dsStyle", S.opt.styles, (it) => it.id === S.design.style,
      (it) => { S.design.style = it.id; });
    segmented("dsSize", S.opt.sizes.outfit.map((s) => ({
      label: s.label, key: `${s.w}x${s.h}`, w: s.w, h: s.h,
    })), (it) => it.w === S.design.width && it.h === S.design.height,
      (it) => { S.design.width = it.w; S.design.height = it.h; });
  }

  function bindDesign() {
    $("dsSteps").oninput = (e) => {
      S.design.steps = Number(e.target.value);
      $("dsStepsVal").textContent = S.design.steps;
    };
    all("#dsCount .seg").forEach((b) => {
      b.onclick = () => {
        all("#dsCount .seg").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        S.design.count = Number(b.dataset.n);
      };
    });
    $("dsGo").onclick = doDesign;
    $("dsCancel").onclick = cancelJob;
    $("dsTogglePrompt").onclick = () => $("dsPromptBox").classList.toggle("hidden");
  }

  async function doDesign() {
    clearFail("design");
    const desc = $("dsDesc").value.trim();
    if (!desc) { fail("design", "先描述一下这件衣服长什么样"); return; }
    $("dsGo").disabled = true;
    try {
      const resp = await req("/outfits/design", {
        method: "POST",
        body: JSON.stringify({
          description: desc,
          slot: S.design.slot,
          style: S.design.style,
          steps: S.design.steps,
          count: S.design.count,
          width: S.design.width,
          height: S.design.height,
          seed: -1,
        }),
      });
      $("dsPromptText").value = resp.prompt;
      renderSections("dsSections", resp.sections);
      startJob("design", resp, (images) => {
        renderDesignResults(images, desc);
        $("dsEmpty").classList.add("hidden");
      });
    } catch (e) {
      fail("design", e.message);
    } finally {
      $("dsGo").disabled = false;
    }
  }

  function renderDesignResults(images, desc) {
    const box = $("dsResults");
    box.innerHTML = "";
    images.forEach((img) => {
      const card = document.createElement("div");
      card.className = "dg-card is-square";
      card.innerHTML = `
        <img src="${esc(img.url)}" alt="" />
        <div class="dg-card-foot">
          <span class="muted">seed ${esc(img.seed)}</span>
          <button class="btn btn-primary" data-act="keep">存进衣柜</button>
        </div>`;
      card.querySelector("img").onclick = () => zoom(img.url);
      card.querySelector('[data-act="keep"]').onclick = () => keepOutfit(img, desc);
      box.appendChild(card);
    });
  }

  function keepOutfit(img, desc) {
    openSave({
      title: "给这件衣服起个名字",
      preview: img.url,
      defaultName: desc.slice(0, 12),
      withSlot: true,
      onConfirm: async (name, slot) => {
        await req("/outfits/commit", {
          method: "POST",
          body: JSON.stringify({
            name, slot: slot || S.design.slot, image_id: img.id,
            prompt: $("dsPromptText").value,
          }),
        });
        await refreshLibrary();
        say(`「${name}」挂进衣柜了`);
      },
    });
  }

  function renderWardrobeMini() {
    const box = $("dsWardrobe");
    if (!box) return;
    const outfits = S.lib.outfits || [];
    $("dsWardrobeCount").textContent = outfits.length ? `${outfits.length} 件` : "";
    box.innerHTML = outfits.slice(0, 24).map((o) =>
      `<img src="${esc(o.image && o.image.url)}" title="${esc(o.name)}" alt="" />`
    ).join("");
    all("img", box).forEach((im, i) => {
      im.onclick = () => zoom(outfits[i].image.url);
    });
  }

  /* ============================================================
     ③ 试衣间
     ============================================================ */
  function bindRoom() {
    $("rmNew").onclick = () => switchTab("create");
    $("rmWear").onclick = doWear;
    $("rmCancel").onclick = cancelJob;
    $("rmUndo").onclick = () => restore(0);
    $("rmReset").onclick = () => restore(-1);
    $("rmExport").onclick = exportPng;
    $("rmTweakGo").onclick = doTweak;
    $("rmTweak").onchange = (e) => {
      S.tweak = e.target.value;
      const spec = (S.opt.tweaks || []).find((t) => t.id === S.tweak);
      $("rmTweakValue").placeholder = spec ? spec.placeholder : "填目标值";
    };
  }

  function renderRoom() {
    renderCharList();
    renderHero();
    renderWardrobe();
    renderHistory();
    renderTweakOptions();
  }

  function renderTweakOptions() {
    if (!S.opt) return;
    const sel = $("rmTweak");
    if (sel.options.length) return;
    sel.innerHTML = S.opt.tweaks.map((t) =>
      `<option value="${esc(t.id)}">${esc(t.label)}</option>`).join("");
    const first = S.opt.tweaks[0];
    $("rmTweakValue").placeholder = first ? first.placeholder : "填目标值";
  }

  function renderCharList() {
    const box = $("rmCharList");
    const list = S.lib.characters || [];
    if (!list.length) {
      box.innerHTML = `<p class="muted">还没有团子。切到「① 捏团子」生成第一个。</p>`;
      return;
    }
    box.innerHTML = "";
    list.forEach((c) => {
      const el = document.createElement("div");
      el.className = "dg-char" + (c.id === S.charId ? " active" : "");
      el.innerHTML = `
        <img src="${esc(c.current && c.current.url)}" alt="" />
        <div class="dg-char-body">
          <div class="dg-char-name">${esc(c.name)}</div>
          <div class="dg-char-meta">${esc(paramSummary(c))}</div>
        </div>
        <button class="dg-char-del" title="删除">×</button>`;
      el.onclick = () => { S.charId = c.id; S.picked.clear(); renderRoom(); };
      el.querySelector(".dg-char-del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删掉「${c.name}」？图还在库里，只是不再挂在衣橱里。`)) return;
        await req(`/library/characters/${c.id}`, { method: "DELETE" });
        if (S.charId === c.id) S.charId = null;
        await refreshLibrary();
      };
      box.appendChild(el);
    });
  }

  function paramSummary(c) {
    const p = (c.params || {});
    const opt = S.opt || { proportions: [] };
    const prop = (opt.proportions || []).find((x) => x.value === p.proportion);
    const bits = [];
    if (prop) bits.push(prop.label);
    if (p.style && opt.styles) {
      const st = opt.styles.find((x) => x.id === p.style);
      if (st) bits.push(st.label);
    }
    return bits.join(" · ") || "—";
  }

  function renderHero() {
    const c = currentChar();
    if (!c) {
      $("rmEmpty").classList.remove("hidden");
      $("rmHero").classList.add("hidden");
      $("rmCharName").textContent = "试衣间";
      $("rmCharProp").textContent = "—";
      return;
    }
    $("rmEmpty").classList.add("hidden");
    $("rmHero").classList.remove("hidden");
    const look = c.current || c.base;
    $("rmHeroImg").src = look.url;
    $("rmHeroImg").onclick = () => zoom(look.url);
    $("rmCharName").textContent = c.name;
    $("rmCharProp").textContent = paramSummary(c);

    const tag = $("rmHeroTag");
    const worn = c.worn || {};
    const names = Object.keys(worn).map((slot) => {
      const o = (S.lib.outfits || []).find((x) => x.id === worn[slot]);
      return o ? o.name : "";
    }).filter(Boolean);
    if (names.length) {
      tag.textContent = "穿着：" + names.join(" / ");
      tag.classList.remove("hidden");
    } else {
      tag.classList.add("hidden");
    }
  }

  function renderWardrobe() {
    const box = $("rmWardrobe");
    const filter = $("rmSlotFilter");
    const slots = [{ id: "", label: "全部" }].concat(
      (S.opt ? S.opt.slots : []).map((s) => ({ id: s.id, label: s.label }))
    );
    filter.innerHTML = slots.map((s) =>
      `<button class="dg-chip${s.id === S.slotFilter ? " active" : ""}" data-slot="${esc(s.id)}">${esc(s.label)}</button>`
    ).join("");
    all(".dg-chip", filter).forEach((b) => {
      b.onclick = () => { S.slotFilter = b.dataset.slot; renderWardrobe(); };
    });

    const outfits = (S.lib.outfits || []).filter(
      (o) => !S.slotFilter || o.slot === S.slotFilter
    );
    $("rmPickCount").textContent = S.picked.size ? `已选 ${S.picked.size} 件` : "未选";

    if (!outfits.length) {
      box.innerHTML = `<p class="muted" style="grid-column:1/-1">这一格还空着，去「② 做衣服」做一件。</p>`;
      return;
    }
    box.innerHTML = "";
    outfits.forEach((o) => {
      const slotLabel = (S.opt.slots.find((s) => s.id === o.slot) || {}).label || o.slot;
      const el = document.createElement("div");
      el.className = "dg-fit" + (S.picked.has(o.id) ? " picked" : "");
      el.innerHTML = `
        <img src="${esc(o.image && o.image.url)}" alt="" title="${esc(o.name)}" />
        <span class="dg-fit-slot">${esc(slotLabel)}</span>
        <button class="dg-fit-del" title="删除">×</button>
        <div class="dg-fit-name">${esc(o.name)}</div>`;
      el.onclick = () => {
        if (S.picked.has(o.id)) S.picked.delete(o.id); else S.picked.add(o.id);
        renderWardrobe();
      };
      el.querySelector(".dg-fit-del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`把「${o.name}」从衣柜里删掉？`)) return;
        await req(`/library/outfits/${o.id}`, { method: "DELETE" });
        S.picked.delete(o.id);
        await refreshLibrary();
      };
      box.appendChild(el);
    });
  }

  function renderHistory() {
    const box = $("rmHistory");
    const c = currentChar();
    if (!c) { box.innerHTML = ""; return; }
    const hist = c.history || [];
    if (!hist.length) {
      box.innerHTML = `<p class="muted">还是素体，没有历史造型。</p>`;
      return;
    }
    box.innerHTML = "";
    hist.forEach((h, i) => {
      const el = document.createElement("div");
      el.className = "dg-hist";
      el.innerHTML = `
        <img src="${esc(h.url)}" alt="" />
        <div class="dg-hist-label">${esc(h.outfit_name || (i === 0 ? "上一版" : `第 ${i + 1} 版`))}</div>`;
      el.onclick = () => restore(i);
      box.appendChild(el);
    });
  }

  async function restore(index) {
    const c = currentChar();
    if (!c) return;
    clearFail("room");
    try {
      await req(`/characters/${c.id}/restore`, {
        method: "POST", body: JSON.stringify({ index }),
      });
      await refreshLibrary();
      say(index < 0 ? "已经脱回素体" : "已经回到那一版");
    } catch (e) {
      fail("room", e.message);
    }
  }

  async function doWear() {
    const c = currentChar();
    if (!c) { fail("room", "先捏一个团子"); return; }
    if (!S.picked.size) { fail("room", "先在右边衣柜里选一件衣服"); return; }
    clearFail("room");
    $("rmWear").disabled = true;
    try {
      const resp = await req("/wear", {
        method: "POST",
        body: JSON.stringify({
          character_id: c.id,
          outfit_ids: Array.from(S.picked).slice(0, 3),
          slot: "",
          steps: S.create.steps,
          count: 1,
          seed: -1,
        }),
      });
      await showWearSections(resp.sections);
      startJob("room", resp, async (images) => {
        renderWearResults(images, resp.outfit_ids, resp.slot);
        // 只出一张时直接穿上，省一次点击
        if (images.length === 1) await adoptImage(images[0], resp.outfit_ids, resp.slot);
      });
    } catch (e) {
      fail("room", e.message);
    } finally {
      $("rmWear").disabled = false;
    }
  }

  async function showWearSections(sections) {
    // 试衣间没有提示词面板，把段落塞进「看提示词」式的折叠区太吵，
    // 这里只把它挂在 stage 的 data 上供排查用。
    $("rmStage").dataset.lastPrompt = JSON.stringify(sections || []);
  }

  function renderWearResults(images, outfitIds, slot) {
    const box = $("rmResults");
    box.innerHTML = "";
    images.forEach((img) => {
      const card = document.createElement("div");
      card.className = "dg-card";
      card.innerHTML = `
        <img src="${esc(img.url)}" alt="" />
        <div class="dg-card-foot">
          <button class="btn btn-primary" data-act="adopt">就穿这件</button>
        </div>`;
      card.querySelector("img").onclick = () => zoom(img.url);
      card.querySelector('[data-act="adopt"]').onclick = () =>
        adoptImage(img, outfitIds, slot);
      box.appendChild(card);
    });
  }

  async function adoptImage(img, outfitIds, slot) {
    const c = currentChar();
    if (!c) return;
    const slotValue = (outfitIds && outfitIds.length === 1) ? (slot || "") : (slot || "full");
    const outfitId = (outfitIds && outfitIds.length === 1) ? outfitIds[0] : "";
    try {
      await req(`/characters/${c.id}/adopt`, {
        method: "POST",
        body: JSON.stringify({
          image_id: img.id,
          outfit_id: outfitId,
          slot: (outfitIds && outfitIds.length > 1) ? "full" : slotValue,
          note: "",
        }),
      });
      S.picked.clear();
      await refreshLibrary();
      say("换好啦");
    } catch (e) {
      fail("room", e.message);
    }
  }

  async function doTweak() {
    const c = currentChar();
    if (!c) { fail("room", "先捏一个团子"); return; }
    const value = $("rmTweakValue").value.trim();
    if (!value) { fail("room", "填一下要改成什么值"); return; }
    clearFail("room");
    $("rmTweakGo").disabled = true;
    try {
      const resp = await req("/tweak", {
        method: "POST",
        body: JSON.stringify({
          character_id: c.id,
          tweak: S.tweak,
          value,
          steps: S.create.steps,
          count: 1,
          seed: -1,
        }),
      });
      startJob("room", resp, async (images) => {
        renderWearResults(images, [], "");
        if (images.length === 1) await adoptImage(images[0], [], "");
      });
    } catch (e) {
      fail("room", e.message);
    } finally {
      $("rmTweakGo").disabled = false;
    }
  }

  async function cancelJob() {
    if (!J.id) return;
    try { await req(`/jobs/${J.id}/cancel`, { method: "POST" }); say("已请求中断"); }
    catch (e) { say(e.message); }
  }

  async function exportPng() {
    const c = currentChar();
    if (!c) return;
    const look = c.current || c.base;
    try {
      const res = await fetch(look.url);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${c.name || "dango"}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch (e) {
      fail("room", "导出失败：" + e.message);
    }
  }

  /* ============================================================
     存档弹窗
     ============================================================ */
  let saveCb = null;

  function openSave({ title, preview, defaultName, withSlot, onConfirm }) {
    $("saveTitle").textContent = title;
    $("savePreview").src = preview;
    $("saveName").value = defaultName || "";
    $("saveSlotField").classList.toggle("hidden", !withSlot);
    if (withSlot && S.opt) {
      $("saveSlot").innerHTML = S.opt.slots.map((s) =>
        `<option value="${esc(s.id)}"${s.id === S.design.slot ? " selected" : ""}>${esc(s.label)}</option>`
      ).join("");
    }
    saveCb = onConfirm;
    $("saveModal").classList.remove("hidden");
    setTimeout(() => $("saveName").focus(), 60);
  }

  function closeSave() {
    $("saveModal").classList.add("hidden");
    saveCb = null;
  }

  $("saveClose").onclick = closeSave;
  $("saveModal").onclick = (e) => { if (e.target.id === "saveModal") closeSave(); };
  $("saveConfirm").onclick = async () => {
    const name = $("saveName").value.trim();
    if (!name) { $("saveName").focus(); return; }
    const slot = $("saveSlot").value;
    const cb = saveCb;
    closeSave();
    if (!cb) return;
    try { await cb(name, slot); } catch (e) { say(e.message); }
  };
  $("saveName").onkeydown = (e) => { if (e.key === "Enter") $("saveConfirm").click(); };

  boot();
})();
