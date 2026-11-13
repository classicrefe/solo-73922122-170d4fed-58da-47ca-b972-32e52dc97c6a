(() => {
  const state = {
    revisionId: "",
    uploadText: "",
    preview: null,
    choices: {}, // "PARENT|CHILD" -> "current" | "upload"
    dirty: false, // choices changed since the shown preview: re-preview needed
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  function setStatus(text, kind = "") {
    const badge = $("badge-status");
    badge.textContent = text;
    badge.style.color = kind === "err" ? "#ff9a94" : kind === "ok" ? "#9be7b5" : "";
  }

  function showAlert(message, kind = "ok") {
    const box = $("alert");
    box.textContent = message;
    box.className = `alert ${kind}`;
    box.classList.remove("hidden");
  }

  function hideAlert() {
    $("alert").classList.add("hidden");
  }

  function showCycles(paths) {
    const box = $("cycle");
    if (!paths || !paths.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.innerHTML =
      `<h4>合并结果成环，整批未应用（当前库未改动）</h4><ul>` +
      paths.map((p) => `<li>${esc(p.join(" → "))}</li>`).join("") +
      "</ul>";
    box.classList.remove("hidden");
  }

  function showStale(text) {
    const box = $("stale");
    if (!text) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.innerHTML = `<h4>确认已过期</h4><p>${esc(text)}</p>`;
    box.classList.remove("hidden");
  }

  const valueCell = (value) =>
    value === null || value === undefined ? '<span class="hint">（无）</span>' : esc(value);

  const decisionLabel = (item) => {
    if (item.decision === "conflict") return "冲突";
    if (item.decision === "merged") return "双方相同→合并";
    if (item.decision === "upload") return "采用上传";
    if (item.decision === "current") return "采用当前";
    return esc(item.decision);
  };

  function keyOf(item) {
    return `${item.parent}|${item.child}`;
  }

  function safeHeader(value) {
    const text = String(value || "").trim();
    return /^[A-Za-z0-9._-]+$/.test(text) ? text : "";
  }

  function renderPreview(data) {
    state.preview = data;
    $("preview-meta").textContent =
      `基准 v${data.base_version} · 当前 v${data.current_version} · 预览 ${data.preview_id}`;
    const counts = data.counts || {};
    $("preview-summary").innerHTML =
      `<span class="chip">改动 <b>${counts.changed || 0}</b></span>` +
      `<span class="chip">新增 <b>${counts.added || 0}</b></span>` +
      `<span class="chip">删除 <b>${counts.deleted || 0}</b></span>` +
      `<span class="chip">双方一致 <b>${counts.merged || 0}</b></span>` +
      `<span class="chip">冲突 <b>${counts.conflict || 0}</b></span>` +
      `<span class="chip">最终关系 <b>${counts.final_edges || 0}</b></span>` +
      `<span class="chip">最终料号 <b>${counts.final_parts || 0}</b></span>`;

    const body = $("changes-body");
    body.innerHTML = "";
    const rows = (data.changes || []).concat(data.conflicts || []);
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="hint">上传稿与基准、当前库一致，没有需要应用的改动。</td></tr>`;
    }
    rows.forEach((item) => {
      const tr = document.createElement("tr");
      if (item.decision === "conflict") tr.className = "conflict-row";
      let result = valueCell(item.result);
      if (item.decision === "conflict") {
        const choice = state.choices[keyOf(item)];
        if (!data.resolved || !choice) {
          result = '<span class="hint">待选择</span>';
        } else {
          result = valueCell(choice === "upload" ? item.upload : item.current);
        }
      }
      tr.innerHTML =
        `<td class="mono">${esc(item.parent)}</td><td class="mono">${esc(item.child)}</td>` +
        `<td class="num">${valueCell(item.base)}</td>` +
        `<td class="num">${valueCell(item.upload)}</td>` +
        `<td class="num">${valueCell(item.current)}</td>` +
        `<td>${decisionLabel(item)}</td><td class="num">${result}</td>`;
      body.appendChild(tr);
    });

    renderConflicts(data.conflicts || []);
    updateButtons();
  }

  function renderConflicts(conflicts) {
    const wrap = $("conflicts-wrap");
    const body = $("conflicts-body");
    body.innerHTML = "";
    if (!conflicts.length) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    conflicts.forEach((item) => {
      const tr = document.createElement("tr");
      const key = keyOf(item);
      const choice = state.choices[key] || "";
      tr.innerHTML =
        `<td class="mono">${esc(item.parent)}</td><td class="mono">${esc(item.child)}</td>` +
        `<td class="num">${valueCell(item.base)}</td>` +
        `<td class="num">${valueCell(item.upload)}</td>` +
        `<td class="num">${valueCell(item.current)}</td>` +
        `<td>
          <button class="ghost ${choice === "upload" ? "selected" : ""}" data-choice="upload" data-key="${esc(key)}">用上传</button>
          <button class="ghost ${choice === "current" ? "selected" : ""}" data-choice="current" data-key="${esc(key)}">用当前</button>
        </td>`;
      body.appendChild(tr);
    });
    body.querySelectorAll("[data-choice]").forEach((button) => {
      button.addEventListener("click", () => {
        state.choices[button.dataset.key] = button.dataset.choice;
        state.dirty = true;
        renderConflicts(conflicts);
        renderResolvedColumn();
        updateButtons();
      });
    });
  }

  function renderResolvedColumn() {
    if (!state.preview) return;
    const rows = (state.preview.changes || []).concat(state.preview.conflicts || []);
    const body = $("changes-body");
    const cells = body.querySelectorAll("tr");
    rows.forEach((item, index) => {
      if (item.decision !== "conflict") return;
      const row = cells[index];
      if (!row) return;
      const last = row.lastElementChild;
      const choice = state.choices[keyOf(item)];
      if (!choice) {
        last.innerHTML = '<span class="hint">待选择</span>';
      } else if (state.dirty || !state.preview.resolved) {
        last.innerHTML = '<span class="hint">重新预览后生效</span>';
      } else {
        last.innerHTML = valueCell(choice === "upload" ? item.upload : item.current);
      }
    });
  }

  function unresolvedCount() {
    const conflicts = (state.preview && state.preview.conflicts) || [];
    return conflicts.filter((item) => !state.choices[keyOf(item)]).length;
  }

  function updateButtons() {
    const busy = state.busy;
    const conflicts = (state.preview && state.preview.conflicts) || [];
    const unresolved = unresolvedCount();
    $("btn-resolve").disabled = busy || !conflicts.length || unresolved > 0;
    const applicable = state.preview && state.preview.applicable && !state.dirty;
    $("btn-apply").disabled = busy || !applicable;
    if (!state.preview) {
      $("apply-note").textContent = "预览并解决冲突后可应用";
    } else if (unresolved) {
      $("apply-note").textContent = `还有 ${unresolved} 项冲突未选择，不能应用；选好后点「按选择重新预览」`;
    } else if (state.dirty) {
      $("apply-note").textContent = "选择已修改：请「按选择重新预览」后再应用";
    } else if (!state.preview.resolved) {
      $("apply-note").textContent = "请先按选择重新预览（冻结最终结构）";
    } else {
      $("apply-note").textContent = `可应用：基准 v${state.preview.base_version}，当前 v${state.preview.current_version}`;
    }
  }

  async function readJson(response) {
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { error: text };
    }
    if (!response.ok) {
      const error = new Error((data && data.error) || `HTTP ${response.status}`);
      error.payload = data || {};
      throw error;
    }
    return data;
  }

  async function downloadBase() {
    hideAlert();
    showStale("");
    showCycles(null);
    setStatus("生成修订单…");
    try {
      const response = await fetch("/api/offline/revisions", { method: "POST" });
      if (!response.ok) throw await readJson(response);
      const revisionId = safeHeader(response.headers.get("X-Bom-Revision"));
      const version = safeHeader(response.headers.get("X-Bom-Version"));
      const edges = safeHeader(response.headers.get("X-Bom-Edges"));
      const csv = await response.text();
      const downloadName = safeHeader(
        (response.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1]
      ) || "bom_structure.csv";
      state.revisionId = revisionId;
      $("f-revision").value = state.revisionId;
      $("base-info").innerHTML =
        `修订单 <b class="mono">${esc(state.revisionId)}</b> · 基于 v${esc(version)} · ${esc(edges)} 条关系`;
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = downloadName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      $("badge-version").textContent = `v${version}`;
      setStatus("已下载", "ok");
      showAlert(
        `基准已保存为修订单 ${state.revisionId}（v${version}，${edges} 条关系）。` +
          "离线修改后上传同一份完整 CSV；缺行即删除。",
        "ok"
      );
    } catch (error) {
      setStatus("下载失败", "err");
      showAlert(error.message, "err");
    }
  }

  function fileSelected() {
    const file = $("f-file").files && $("f-file").files[0];
    if (!file) {
      state.uploadText = "";
      updateButtons();
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.uploadText = String(reader.result || "");
      showAlert(`已选择 ${file.name}（${file.size} 字节），点「预览三方合并」。`, "ok");
    };
    reader.onerror = () => showAlert("读取文件失败", "err");
    reader.readAsText(file, "utf-8");
  }

  async function previewMerge() {
    hideAlert();
    showCycles(null);
    showStale("");
    const revisionId = ($("f-revision").value || state.revisionId || "").trim();
    if (!revisionId) {
      showAlert("请先下载完整 CSV 生成修订单，或填写修订单 ID。", "err");
      return;
    }
    if (!state.uploadText) {
      showAlert("请选择修订后的完整 CSV 文件。", "err");
      return;
    }
    state.revisionId = revisionId;
    const resolutions = Object.entries(state.choices).map(([key, choice]) => {
      const [parent, child] = key.split("|");
      return { parent, child, choice };
    });
    state.busy = true;
    updateButtons();
    setStatus("比较中…");
    try {
      const form = new FormData();
      form.append("revision_id", revisionId);
      form.append("file", new Blob([state.uploadText], { type: "text/csv" }), "bom.csv");
      if (resolutions.length) form.append("resolutions", JSON.stringify(resolutions));
      const response = await fetch("/api/offline/preview", { method: "POST", body: form });
      const data = await readJson(response);
      // New preview: drop choices that no longer correspond to a conflict so
      // a later apply can never piggy-back on stale selections.
      const keys = new Set((data.conflicts || []).map(keyOf));
      Object.keys(state.choices).forEach((key) => {
        if (!keys.has(key)) delete state.choices[key];
      });
      state.dirty = false;
      renderPreview(data);
      $("badge-version").textContent = `v${data.current_version}`;
      setStatus("已预览", "ok");
      if (data.conflicts.length) {
        showAlert(
          `发现 ${data.conflicts.length} 项冲突：请在下方逐项选择当前或上传，然后重新预览；未解决前不能应用。`,
          "err"
        );
      } else {
        showAlert(
          `无冲突：基准 v${data.base_version} / 当前 v${data.current_version}，` +
            `将应用 ${data.counts.changed + data.counts.added + data.counts.deleted} 项差异。可确认应用。`,
          "ok"
        );
      }
    } catch (error) {
      state.preview = null;
      $("changes-body").innerHTML = "";
      $("conflicts-wrap").classList.add("hidden");
      if (error.payload && error.payload.paths) {
        showCycles(error.payload.paths);
        showAlert("合并结果成环，整批不会写入；请修改上传稿后重新预览。", "err");
      } else if (error.payload && error.payload.code === "ValidationError") {
        showAlert(`上传稿无效：${error.message}`, "err");
      } else {
        showAlert(error.message, "err");
      }
      setStatus("预览失败", "err");
      updateButtons();
    } finally {
      state.busy = false;
      updateButtons();
    }
  }

  function resolutionPayload() {
    const keys = new Set(((state.preview && state.preview.conflicts) || []).map(keyOf));
    return Object.entries(state.choices)
      .filter(([key]) => keys.has(key))
      .map(([key, choice]) => {
        const [parent, child] = key.split("|");
        return { parent, child, choice };
      });
  }

  function reopenAsResolved() {
    // Re-preview with the choices so the server freezes a resolved structure
    // (the application only ever uses a confirmed, conflict-free preview).
    if (!resolutionPayload().length) return;
    previewMerge();
  }

  async function applyMerge() {
    if (!state.preview) return;
    hideAlert();
    showStale("");
    setStatus("应用中…");
    state.busy = true;
    updateButtons();
    try {
      const response = await fetch(`/api/offline/apply/${state.preview.preview_id}`, {
        method: "POST",
      });
      const data = await readJson(response);
      showAlert(
        `合并已应用为版本 v${data.version_seq}：结构 ${data.counts.final_edges} 条关系，` +
          `物化叶件用量 ${data.leaf_rows} 行。`,
        "ok"
      );
      setStatus("已应用", "ok");
      $("badge-version").textContent = `v${data.version_seq}`;
      state.preview = null;
      state.choices = {};
      state.dirty = false;
      $("changes-body").innerHTML = "";
      $("conflicts-wrap").classList.add("hidden");
      $("preview-meta").textContent = "合并已应用；如需再次修订请重新下载基准。";
      $("preview-summary").innerHTML = "";
    } catch (error) {
      const payload = error.payload || {};
      if (payload.code === "StalePreviewError") {
        showStale(
          payload.error ||
            `预览基于 v${payload.preview_version}，当前库已是 v${payload.current_version}。`
        );
        showAlert("旧确认已拒绝：请重新下载/上传并预览，新的修改不会被覆盖。", "err");
        state.preview = null;
        $("btn-apply").disabled = true;
      } else if (payload.code === "CyclicStructureError") {
        showCycles(payload.paths);
        showAlert("合并结果成环，整批未写入，旧版保留。", "err");
      } else if (payload.code === "UnresolvedConflictError") {
        showAlert(payload.error || "仍有冲突未解决，不能应用。", "err");
      } else {
        showAlert(error.message, "err");
      }
      setStatus("应用失败", "err");
    } finally {
      state.busy = false;
      updateButtons();
    }
  }

  async function checkVersion() {
    try {
      const data = await readJson(await fetch("/api/offline/revisions"));
      $("badge-version").textContent = `v${data.current_version}`;
      setStatus(`当前 v${data.current_version}`, "ok");
      if (state.preview && state.preview.current_version !== data.current_version) {
        showStale(
          `预览基于 v${state.preview.current_version}，当前库已是 v${data.current_version}；请重新预览。`
        );
      }
    } catch (error) {
      showAlert(error.message, "err");
    }
  }

  function bind() {
    $("btn-download-base").addEventListener("click", downloadBase);
    $("f-file").addEventListener("change", fileSelected);
    $("btn-preview").addEventListener("click", previewMerge);
    $("btn-resolve").addEventListener("click", reopenAsResolved);
    $("btn-apply").addEventListener("click", applyMerge);
    $("btn-refresh-version").addEventListener("click", checkVersion);
  }

  bind();
  checkVersion();
})();
