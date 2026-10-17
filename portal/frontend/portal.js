/* 工作台门户逻辑：拉应用清单 → 渲染卡片墙；顺带显示引擎状态。 */
(function () {
  "use strict";

  const API = "/api/v1";
  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  async function json(path) {
    const res = await fetch(API + path);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function renderApps() {
    const grid = $("appGrid");
    try {
      const data = await json("/apps");
      const items = data.items || [];
      if (!items.length) { grid.innerHTML = '<p class="muted">没有发现任何应用。</p>'; return; }
      grid.innerHTML = items.map((a) => `
        <a class="app-card" href="${esc(a.entry)}">
          <div class="app-icon">${esc(a.icon || "▦")}</div>
          <div class="app-body">
            <div class="app-name">${esc(a.name)}${a.version ? ` <span class="app-tag">v${esc(a.version)}</span>` : ""}</div>
            <p class="app-desc">${esc(a.description || "")}</p>
            <div class="app-tags">${(a.capabilities || []).map((c) => `<span class="app-tag">${esc(c)}</span>`).join("")}</div>
            ${a.standalone_port ? `<div class="app-port">独立运行端口：${esc(a.standalone_port)}</div>` : ""}
          </div>
        </a>`).join("");
    } catch (e) {
      grid.innerHTML = `<p class="muted">应用清单加载失败：${esc(e.message)}</p>`;
    }
  }

  async function renderStatus() {
    try {
      const st = await json("/models/status");
      const badge = $("engineBadge");
      badge.className = "badge";
      if (st.engine_mode === "mock") { badge.classList.add("badge-mock"); badge.textContent = "占位模式"; }
      else if (st.loaded) { badge.classList.add("badge-local"); badge.textContent = "本地模型已加载"; }
      else if (st.last_error) { badge.classList.add("badge-error"); badge.textContent = "模型加载失败"; }
      else { badge.classList.add("badge-idle"); badge.textContent = "本地模型未加载"; }

      const parts = [];
      if (st.gpu_name) parts.push(st.gpu_name);
      if (st.vram_total_gb) parts.push(`显存 ${st.vram_free_gb}/${st.vram_total_gb} GB`);
      if (st.queue_size) parts.push(`队列 ${st.queue_size}`);
      $("vramInfo").textContent = parts.join(" · ") || "—";
    } catch (e) { /* 静默 */ }
  }

  async function main() {
    await Promise.all([renderApps(), renderStatus()]);
    $("portalFoot").innerHTML =
      "所有功能共用同一份模型（引擎层独占 GPU），彼此独立开发、可单独运行。"
      + "想单独跑某个功能时，可用 <code>run_app.bat &lt;id&gt;</code>。";
  }

  main();
})();
