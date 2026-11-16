const m4 = {
  pending: null,
  requestId: null,
};

function newRequestId(){
  if(window.crypto?.randomUUID) return crypto.randomUUID();
  return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

function setFeedback(text){
  const el = document.querySelector("#feedback");
  if(el) el.textContent = text;
}

function show(payload){
  const out = document.querySelector("#result");
  if(out) out.textContent = JSON.stringify(payload, null, 2);
}

async function postJSON(url, body){
  const response = await fetch(url, {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if(!response.ok){
    const detail = data.detail || {};
    const error = new Error(detail.message || "请求失败");
    error.code = detail.code;
    error.detail = detail;
    throw error;
  }
  return data;
}

/* 导入：真实清单文件直接上传，成功后重载页面以显示新的采纳页与批次。 */
const importDialog = document.querySelector("#import-dialog");
document.querySelector("#import-manifest")?.addEventListener("click", () => importDialog?.showModal());
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));

document.querySelector("#import-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  setFeedback("导入中");
  try{
    const response = await fetch(form.action, {method:"POST", body:new FormData(form)});
    const data = await response.json();
    if(!response.ok) throw new Error(data.detail?.message || "导入失败");
    show(data);
    setFeedback(data.created === false ? "批次已存在，复用" : "导入完成");
    window.location.reload();
  }catch(error){
    setFeedback("导入失败：" + error.message);
  }finally{
    button.disabled = false;
  }
});

/* 采信：普通采信直接提交；页眉缺失/不符只能走复核采信，必须写理由。 */
const reviewDialog = document.querySelector("#review-dialog");
const reviewForm = document.querySelector("#review-form");
let reviewTarget = null;

document.querySelectorAll("#candidates [data-action]").forEach((button) => {
  button.addEventListener("click", async () => {
    const row = button.closest("tr");
    const action = button.dataset.action;
    const payload = {
      business_key: row.dataset.key,
      scan_id: row.dataset.scan,
      action,
      expected_revision: Number(row.querySelector("[data-revision]")?.dataset.revision ?? button.dataset.revision),
      request_id: newRequestId(),
    };
    if(action === "REVIEW_APPROVE"){
      reviewTarget = payload;
      document.querySelector("#review-target").textContent = `${row.dataset.key} · ${row.children[1].textContent}`;
      reviewForm.querySelector("input[name=reason_text]").value = "";
      reviewDialog.showModal();
      return;
    }
    await submitDecision(payload);
  });
});

reviewForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if(!reviewTarget) return;
  reviewTarget.reason_text = reviewForm.querySelector("input[name=reason_text]").value.trim();
  reviewDialog.close();
  await submitDecision(reviewTarget);
  reviewTarget = null;
});

async function submitDecision(payload){
  setFeedback("裁决中");
  try{
    const data = await postJSON("/api/rescans/decide", payload);
    show(data);
    setFeedback(`已发布 REV ${data.revision} · ${data.reason_code}`);
    window.location.reload();
  }catch(error){
    if(error.code === "PUBLISH_INTERRUPTED"){
      m4.pending = error.detail;
      show(error.detail);
      setFeedback("发布中断：决定已保存，页面仍为原采纳页。可重试发布。");
      offerResume(error.detail.decision_id, "重试发布");
      return;
    }
    if(error.code === "STALE_VERSION"){
      setFeedback(`版本已过期（当前 REV ${error.detail.current_revision}），请刷新后重新裁决。`);
      return;
    }
    if(error.code === "HEADER_MISMATCH" || error.code === "HEADER_MISSING"){
      setFeedback("页眉证据不足，只能复核采信并写明理由。");
      return;
    }
    setFeedback("裁决失败：" + error.message);
  }
}

/* 发布重试：同一个决定只重放发布，不新增决定。 */
function banner(){
  let element = document.querySelector("#pending-banner");
  if(element) return element;
  element = document.createElement("section");
  element.id = "pending-banner";
  element.className = "banner banner--warn";
  element.innerHTML = `<div><b>发布中断</b> <span id="pending-count">1</span> 条决定已保存但未发布，页面仍显示原采纳页。</div>`;
  const grid = document.querySelector(".grid--rescan");
  grid.parentNode.insertBefore(element, grid);
  return element;
}

function offerResume(decisionId, label){
  if(!decisionId) return;
  const element = banner();
  if(element.querySelector(`[data-resume="${decisionId}"]`)) return;
  const button = document.createElement("button");
  button.className = "btn btn--small";
  button.dataset.resume = decisionId;
  button.textContent = label || "重试发布";
  element.appendChild(button);
  const count = element.querySelector("#pending-count");
  count.textContent = String(element.querySelectorAll("[data-resume]").length);
  bindResume(button);
}

function bindResume(button){
  button.addEventListener("click", async () => {
    button.disabled = true;
    setFeedback("重试发布");
    try{
      const data = await postJSON(`/api/rescans/decisions/${button.dataset.resume}/publish`, {});
      show(data);
      setFeedback(`发布恢复 · REV ${data.revision}`);
      window.location.reload();
    }catch(error){
      if(error.code === "PUBLISH_INTERRUPTED"){
        show(error.detail);
        setFeedback("仍中断，决定保留待重试。");
      }else{
        setFeedback("重试失败：" + error.message);
      }
      button.disabled = false;
    }
  });
}

document.querySelectorAll("[data-resume]").forEach(bindResume);

/* 归档对照：归档批次只读，展示原扫描与当时缺页、混期证据。 */
document.querySelectorAll("[data-batch]").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll("[data-batch]").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");
    const response = await fetch(`/api/rescans/batches/${button.dataset.batch}`);
    const data = await response.json();
    const body = document.querySelector("#archive tbody");
    const label = document.querySelector("#archive-label");
    label.textContent = `${data.batch.batch_id} · ${data.batch.created_at} · 原扫描 ${data.batch.accepted} · 隔离 ${data.batch.quarantined}`;
    body.innerHTML = data.rows.map((row) => `
      <tr class="${row.changed ? "is-changed" : ""}">
        <td class="num">${row.line_no}</td>
        <td>${row.business_key}</td>
        <td>${row.file_name}</td>
        <td class="num">${row.clarity}</td>
        <td>${row.header_issue}</td>
        <td>${row.archived_adopted ? "采纳" : ""}</td>
        <td>${row.current_adopted ? "当前采纳" : row.changed ? "已变更" : "未变"}</td>
      </tr>`).join("") || `<tr><td colspan="7">无扫描件</td></tr>`;
    const evidenceBody = document.querySelector("#archive-evidence tbody");
    evidenceBody.innerHTML = data.evidence.map((item) => `
      <tr>
        <td class="num">${item.line_no}</td>
        <td>${item.business_key || ""}</td>
        <td>${item.reason_code}</td>
        <td>${item.raw_value || ""}</td>
        <td>${item.detail || ""}</td>
      </tr>`).join("") || `<tr><td colspan="5">无证据</td></tr>`;
    const gapsBody = document.querySelector("#archive-gaps tbody");
    gapsBody.innerHTML = data.gaps.map((gap) => `
      <tr>
        <td>${gap.issue}</td>
        <td>${gap.edition}</td>
        <td class="num">${gap.expected_count}</td>
        <td>${gap.missing.join(", ") || "完整"}</td>
      </tr>`).join("") || `<tr><td colspan="4">无</td></tr>`;
    show(data);
  });
});

/* 撤回采信：对当前采纳页提出不采信，缺页随发布一起出现。 */
document.querySelectorAll("[data-retract]").forEach((button) => {
  button.addEventListener("click", async () => {
    await submitDecision({
      business_key: button.dataset.retract,
      scan_id: button.dataset.scan,
      action: "REJECT",
      expected_revision: Number(button.dataset.revision),
      reason_text: "撤回当前采纳页",
      request_id: newRequestId(),
    });
  });
});

/* 页眉不符或缺失的候选在页面上显式标注，避免被当成普通重扫。 */
document.querySelectorAll("#candidates tr[data-key]").forEach((row) => {
  if(row.querySelector("[data-action=REVIEW_APPROVE]")) row.classList.add("is-review");
});