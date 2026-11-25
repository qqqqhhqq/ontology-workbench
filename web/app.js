/* Operational Ontology Interactive Console Frontend (Zero external dependencies) */

const TYPE_COLORS = {
  Customer: "#a78bfa",
  SalesOwner: "#38bdf8",
  SalesOrder: "#22c55e",
  Shipment: "#f59e0b",
  Product: "#f472b6",
  ProductCategory: "#14b8a6",
};

const TYPE_RADIUS = {
  Customer: 15,
  SalesOwner: 14,
  ProductCategory: 14,
  Product: 11,
  SalesOrder: 10,
  Shipment: 9,
};

let state = {
  overview: null,
  currentPreset: "core",
  currentAsset: "tbox",
  selectedNodeUid: null,
};

// ---------------- Utility ----------------

function fmtMoney(n) {
  return "¥" + Number(n || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function tierBadge(tier) {
  if (tier === "VIP") return `<span class="badge b-vip">VIP</span>`;
  if (tier === "KEY_ACCOUNT") return `<span class="badge b-key">KEY_ACCOUNT</span>`;
  return `<span class="badge b-std">${tier}</span>`;
}

function sevBadge(sev) {
  if (sev === "HIGH") return `<span class="badge b-high">HIGH</span>`;
  if (sev === "MEDIUM") return `<span class="badge b-med">MEDIUM</span>`;
  return `<span class="badge b-low">LOW</span>`;
}

async function apiGet(url) {
  const r = await fetch(url);
  return await r.json();
}

async function apiPost(url, payload = {}) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return await r.json();
}

// ---------------- Navigation ----------------

function switchTab(tabName) {
  document.querySelectorAll(".nav-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${tabName}`);
  });
  window.scrollTo({ top: 0, behavior: "instant" });
  if (tabName === "graph") {
    resizeCanvas();
    if (!graphEngine.nodes.length) {
      loadGraph(state.currentPreset);
    } else {
      graphEngine.draw();
    }
  }
}

document.querySelectorAll(".nav-tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// ---------------- Overview & Cockpit ----------------

async function refreshAll() {
  const ov = await apiGet("/api/overview");
  state.overview = ov;

  document.getElementById("metaSubtitle").textContent =
    `${ov.metadata.ontology_name} v${ov.metadata.version} · AS_OF ${ov.metadata.as_of_date} · 9 MCP Tools Ready`;

  const modePill = document.getElementById("modePill");
  modePill.textContent = ov.mode === "sandbox" ? "SANDBOX 安全沙箱" : "LIVE 生产模式";
  modePill.className = `pill ${ov.mode}`;
  document.getElementById("resetSandboxBtn").style.display = ov.mode === "sandbox" ? "inline-flex" : "none";

  renderKPIs(ov);
  renderCQ1(ov.cq1);
  renderCQ2(ov.cq2);
  renderCQ3(ov.cq3);
  renderLedger(ov.ledger);
  renderAgentLiveOntologyBox(ov);
  await loadDQTable();
  if (document.getElementById("tab-graph").classList.contains("active")) {
    await loadGraph(state.currentPreset);
  }
}

function renderKPIs(ov) {
  const cq1Total = ov.cq1.reduce((acc, r) => acc + r.total_amount, 0);
  const sevMap = {};
  (ov.cq4.by_severity || []).forEach((s) => (sevMap[s.severity] = s.issue_count));
  const totalSales = ov.cq3.reconciliation.valid_orders_total;
  const objs = ov.summary.object_counts;

  document.getElementById("kpiGrid").innerHTML = `
    <div class="kpi-card danger">
      <div class="kpi-label">CQ1 · VIP 近 30 天逾期未有效发货</div>
      <div class="kpi-value">${ov.cq1.length} 张</div>
      <div class="kpi-sub">涉险金额 ${fmtMoney(cq1Total)}（WMS 事实源裁决）</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">CQ2 · 统一真实世界客户实体</div>
      <div class="kpi-value">${objs.Customer} 家</div>
      <div class="kpi-sub">融合 12 CRM + 12 ERP · 共 ${objs.SalesOrder} 订单 / ${objs.Shipment} 发货单</div>
    </div>
    <div class="kpi-card success">
      <div class="kpi-label">CQ3 · 有效订单聚合总额</div>
      <div class="kpi-value">${fmtMoney(totalSales)}</div>
      <div class="kpi-sub">分类传递闭包 100% 金额守恒 · 异常商品 ${ov.cq3.anomalous_products.length} 个</div>
    </div>
    <div class="kpi-card warn">
      <div class="kpi-label">CQ4 · 数据质量审计异常</div>
      <div class="kpi-value">${ov.cq4.total_issues} 条</div>
      <div class="kpi-sub">HIGH ${sevMap.HIGH || 0} · MEDIUM ${sevMap.MEDIUM || 0} · LOW ${sevMap.LOW || 0} · 动作账本 ${ov.ledger.length} 条</div>
    </div>
  `;
}

function renderCQ1(rows) {
  document.getElementById("cq1CountBadge").textContent = `${rows.length} 张`;
  document.getElementById("cq1TableBody").innerHTML = rows
    .map((r) => {
      const alias = r.aliases && r.aliases.length ? `<span style="color:var(--text-muted)">(${r.aliases.join(",")})</span>` : "";
      return `
        <tr>
          <td class="mono"><a href="#" style="color:var(--accent)" onclick="inspectInGraph('SalesOrder','${r.order_no}');return false;">${r.order_no}</a></td>
          <td>${r.canonical_name} ${alias}</td>
          <td><span class="badge b-std">${r.resolution_method}</span></td>
          <td>${r.owner_name || "-"} <span style="color:var(--text-muted)">/ ${r.team || "-"}</span></td>
          <td class="mono">${r.order_date} → ${r.promised_date} <span class="badge b-high">逾期${r.overdue_days}天</span></td>
          <td><span class="badge b-med">${r.fulfillment_status}</span></td>
          <td class="num">${fmtMoney(r.total_amount)}</td>
          <td>
            <button class="btn sm" onclick="quickEscalate('${r.order_no}', true)">预演</button>
            <button class="btn sm primary" onclick="quickEscalate('${r.order_no}', false)">催办</button>
          </td>
        </tr>`;
    })
    .join("");
}

function renderCQ2(rows) {
  document.getElementById("cq2TableBody").innerHTML = rows
    .map((r) => {
      const alias = r.aliases && r.aliases.length ? `<div style="font-size:11px;color:var(--text-muted)">别名: ${r.aliases.join(" / ")}</div>` : "";
      const rate = r.on_time_rate_pct !== null ? `${r.on_time_rate_pct.toFixed(1)}%` : "N/A";
      return `
        <tr>
          <td>
            <a href="#" style="color:var(--text-primary);font-weight:600;text-decoration:none" onclick="inspectInGraph('Customer','${r.customer_id}');return false;">${r.canonical_name}</a>
            ${alias}
          </td>
          <td>${tierBadge(r.tier)} <span class="badge b-std">${r.source_presence}</span></td>
          <td>${r.owner_name || "-"}</td>
          <td class="num">${fmtMoney(r.valid_order_amount)}</td>
          <td class="num">${rate}</td>
        </tr>`;
    })
    .join("");
}

function renderCQ3(cq3) {
  const total = cq3.reconciliation.valid_orders_total || 1;
  document.getElementById("cq3ReconcileHint").textContent =
    `分类汇总 ${fmtMoney(cq3.reconciliation.category_sum)} == 有效订单总额 ${fmtMoney(cq3.reconciliation.valid_orders_total)}`;
  document.getElementById("cq3BalanceBadge").textContent = cq3.reconciliation.is_balanced ? "100% 金额守恒" : "存在差额";

  document.getElementById("cq3Bars").innerHTML = cq3.root_category_summary
    .map((c) => {
      const pctValid = ((c.valid_leaf_amount / total) * 100).toFixed(1);
      const pctNonLeaf = ((c.non_leaf_amount / total) * 100).toFixed(1);
      const pctAnom = ((c.anomalous_amount / total) * 100).toFixed(1);
      const pctTot = ((c.total_amount / total) * 100).toFixed(1);
      return `
        <div class="bar-row">
          <div class="bar-head">
            <span><strong>${c.root_category_code}</strong> · ${c.root_category_name}</span>
            <span class="mono">${fmtMoney(c.total_amount)} (${pctTot}%)</span>
          </div>
          <div class="bar-track">
            <div class="bar-seg-valid" style="width:${pctValid}%" title="严格合规叶子: ${fmtMoney(c.valid_leaf_amount)}"></div>
            <div class="bar-seg-nonleaf" style="width:${pctNonLeaf}%" title="非叶子上卷: ${fmtMoney(c.non_leaf_amount)}"></div>
            <div class="bar-seg-anom" style="width:${pctAnom}%" title="异常挂账: ${fmtMoney(c.anomalous_amount)}"></div>
          </div>
        </div>`;
    })
    .join("");

  document.getElementById("cq3AnomBody").innerHTML = cq3.anomalous_products.length
    ? cq3.anomalous_products
        .map(
          (p) => `
        <tr>
          <td><span class="mono" style="color:var(--pink)">${p.sku}</span> ${p.product_name}</td>
          <td>${sevBadge(p.category_status === "NON_LEAF" ? "MEDIUM" : "HIGH")} <span class="mono">${p.category_status}</span></td>
          <td class="num">${fmtMoney(p.valid_sales_amount)}</td>
          <td><button class="btn sm warn" onclick="jumpFixProduct('${p.sku}')">去修复</button></td>
        </tr>`
        )
        .join("")
    : `<tr><td colspan="4" style="color:var(--emerald)">全部商品分类已合规，无异常挂账！</td></tr>`;
}

document.getElementById("customerSearchInput").addEventListener("input", async (e) => {
  const q = e.target.value.trim();
  const res = await apiGet(`/api/customers?q=${encodeURIComponent(q)}`);
  if (res.ok) renderCQ2(res.data);
});

// ---------------- DQ Table & W3C / MCP Viewer ----------------

async function loadDQTable() {
  const sev = document.getElementById("dqSeverityFilter").value;
  const rule = document.getElementById("dqRuleFilter").value.trim().toUpperCase();
  const qs = new URLSearchParams();
  if (sev) qs.set("severity", sev);
  if (rule) qs.set("rule_code", rule);

  const res = await apiGet(`/api/dq?${qs.toString()}`);
  let rows = [];
  if (res.ok) {
    rows = Array.isArray(res.data) ? res.data : res.data.high_severity_details || [];
    if (!sev && !rule) {
      // 拉全量列表便于浏览
      const [h, m, l] = await Promise.all([
        apiGet("/api/dq?severity=HIGH"),
        apiGet("/api/dq?severity=MEDIUM"),
        apiGet("/api/dq?severity=LOW"),
      ]);
      rows = [...(h.data || []), ...(m.data || []), ...(l.data || [])];
    }
  }
  document.getElementById("dqTableBody").innerHTML = rows
    .map(
      (r) => `
      <tr>
        <td class="mono">${r.issue_id}</td>
        <td class="mono">${r.rule_code}</td>
        <td>${sevBadge(r.severity)}</td>
        <td class="mono">
          <a href="#" style="color:var(--accent)" onclick="inspectInGraph('${r.target_object_type}','${r.target_object_id}');return false;">
            ${r.target_object_type}:${r.target_object_id}
          </a>
        </td>
        <td>${r.source_system}</td>
        <td>${r.description}</td>
      </tr>`
    )
    .join("");
}

document.getElementById("dqSeverityFilter").addEventListener("change", loadDQTable);
document.getElementById("dqRuleFilter").addEventListener("input", loadDQTable);

async function loadW3CAsset(asset) {
  state.currentAsset = asset;
  document.querySelectorAll("#w3cTabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.asset === asset);
  });
  const data = await apiGet(`/api/w3c/preview?asset=${encodeURIComponent(asset)}`);
  document.getElementById("w3cAssetTitle").textContent = data.title || asset;
  document.getElementById("w3cCodeViewer").textContent = data.content || "";
}

document.querySelectorAll("#w3cTabs button").forEach((btn) => {
  btn.addEventListener("click", () => loadW3CAsset(btn.dataset.asset));
});

document.getElementById("copyCodeBtn").addEventListener("click", async () => {
  const txt = document.getElementById("w3cCodeViewer").textContent;
  try {
    await navigator.clipboard.writeText(txt);
    showToast("已复制到剪贴板");
  } catch {
    showToast("当前浏览器限制剪贴板访问，请手动选中复制");
  }
});

// ---------------- Actions & Playground ----------------

function renderLedger(entries) {
  const body = document.getElementById("ledgerTableBody");
  if (!entries || !entries.length) {
    body.innerHTML = `<tr><td colspan="5" style="color:var(--text-muted)">暂无已提交动作记录（试跑动作后将实时出现在这里）</td></tr>`;
    return;
  }
  body.innerHTML = [...entries]
    .reverse()
    .map(
      (e) => `
      <tr>
        <td class="mono">${e.action_id}</td>
        <td class="mono" style="color:var(--accent)">${e.action_name}</td>
        <td class="mono">${e.target_object_type}:${e.target_object_id}</td>
        <td>${e.actor}</td>
        <td><span class="badge b-ok">${e.status}</span></td>
      </tr>`
    )
    .join("");
}

async function submitAction(toolName, dryRun) {
  let payload = { dry_run: dryRun };
  if (toolName === "escalate_overdue_order") {
    payload = {
      ...payload,
      order_no: document.getElementById("act1OrderNo").value.trim(),
      priority: document.getElementById("act1Priority").value,
      reason: document.getElementById("act1Reason").value.trim(),
      actor: "console-operator",
    };
  } else if (toolName === "fix_product_category") {
    payload = {
      ...payload,
      sku: document.getElementById("act2Sku").value.trim(),
      target_leaf_category_code: document.getElementById("act2Cat").value.trim(),
      reviewer: "mdm-steward",
      reason: document.getElementById("act2Reason").value.trim(),
      rebuild: !dryRun && document.getElementById("act2Rebuild").checked,
    };
  } else if (toolName === "reconcile_customer_identity") {
    payload = {
      ...payload,
      crm_account_id: document.getElementById("act3Crm").value.trim(),
      erp_customer_no: document.getElementById("act3Erp").value.trim(),
      reviewer: "sales-steward",
      rationale: document.getElementById("act3Rationale").value.trim(),
      rebuild: !dryRun && document.getElementById("act3Rebuild").checked,
    };
  }

  const res = await apiPost(`/api/actions/${toolName}`, payload);
  const box = document.getElementById("actionResultBox");
  box.className = `result-box ${res.ok ? "ok" : "err"}`;
  box.textContent = JSON.stringify(res, null, 2);

  if (res.ok) {
    showToast(dryRun ? `Dry Run 预演通过：${toolName}` : `动作提交成功：${res.data.action_id}`);
    if (!dryRun) {
      await refreshAll();
      await loadW3CAsset(state.currentAsset);
    }
  } else {
    showToast(`前置条件守卫拦截：${res.error_type}`);
  }
}

window.submitAction = submitAction;

window.quickEscalate = async function (orderNo, dryRun) {
  document.getElementById("act1OrderNo").value = orderNo;
  document.getElementById("act1Priority").value = "P1";
  document.getElementById("act1Reason").value = "驾驶舱一键触发 VIP 逾期催办";
  switchTab("actions");
  await submitAction("escalate_overdue_order", dryRun);
};

window.jumpFixProduct = function (sku) {
  document.getElementById("act2Sku").value = sku;
  document.getElementById("act2Cat").value = sku === "P-1012" ? "0101" : "0102";
  document.getElementById("act2Reason").value = `修复异常商品 ${sku} 分类至标准叶子节点`;
  switchTab("actions");
  showToast(`已载入 ${sku} 到修复表单`);
};

// Action 预设按钮
document.getElementById("presetBadEscalate").addEventListener("click", () => {
  document.getElementById("act1OrderNo").value = "SO-20261105-905";
  document.getElementById("act1Reason").value = "误信 ERP 状态 A，尝试催办（实际 WMS 已出库）";
  submitAction("escalate_overdue_order", false);
});
document.getElementById("presetBadCat").addEventListener("click", () => {
  document.getElementById("act2Sku").value = "P-1012";
  document.getElementById("act2Cat").value = "01";
  document.getElementById("act2Reason").value = "故意尝试挂到一级非叶子节点 01";
  submitAction("fix_product_category", false);
});
document.getElementById("presetBadMerge").addEventListener("click", () => {
  document.getElementById("act3Crm").value = "ACC-1011";
  document.getElementById("act3Erp").value = "C0011";
  document.getElementById("act3Rationale").value = "故意尝试合并信用代码不同的启明医疗与嘉禾物流";
  submitAction("reconcile_customer_identity", false);
});
document.getElementById("presetGoodEscalate").addEventListener("click", () => {
  document.getElementById("act1OrderNo").value = "SO-20261112-904";
  document.getElementById("act1Priority").value = "P1";
  document.getElementById("act1Reason").value = "VIP 客户星辰创意设计逾期 4 天，发起加急出库催办";
  submitAction("escalate_overdue_order", false);
});
document.getElementById("presetGoodCat").addEventListener("click", () => {
  document.getElementById("act2Sku").value = "P-1012";
  document.getElementById("act2Cat").value = "0101";
  document.getElementById("act2Reason").value = "核实标签打印机 BQ-50 归属二级叶子分类 0101(打印机)";
  document.getElementById("act2Rebuild").checked = true;
  submitAction("fix_product_category", false);
});
document.getElementById("presetGoodMerge").addEventListener("click", () => {
  document.getElementById("act3Crm").value = "ACC-1012";
  document.getElementById("act3Erp").value = "C0012";
  document.getElementById("act3Rationale").value = "沙箱演练：核实两单边个人客户同一手机号后确认合并";
  document.getElementById("act3Rebuild").checked = true;
  submitAction("reconcile_customer_identity", false);
});

document.getElementById("resetSandboxBtn").addEventListener("click", async () => {
  const res = await apiPost("/api/sandbox/reset");
  if (res.ok) {
    document.getElementById("actionResultBox").className = "result-box ok";
    document.getElementById("actionResultBox").textContent = JSON.stringify(res, null, 2);
    resetAgentUI();
    showToast(res.message);
    await refreshAll();
    await loadAgentPresets();
    await loadW3CAsset(state.currentAsset);
  }
});

// ---------------- Interactive Force-Directed Graph ----------------

const canvas = document.getElementById("graphCanvas");
const ctx2d = canvas.getContext("2d");

const graphEngine = {
  nodes: [],
  edges: [],
  nodeByUid: {},
  cam: { x: 0, y: 0, scale: 1 },
  draggingNode: null,
  panning: false,
  lastMouse: { x: 0, y: 0 },
  hoveredUid: null,
  searchTerm: "",
  rafId: null,
  ticks: 0,

  setData(data) {
    const w = canvas.clientWidth || 900;
    const h = canvas.clientHeight || 600;
    const typeAnchors = {
      SalesOwner: { x: w * 0.18, y: h * 0.25 },
      Customer: { x: w * 0.35, y: h * 0.48 },
      SalesOrder: { x: w * 0.56, y: h * 0.45 },
      Shipment: { x: w * 0.78, y: h * 0.45 },
      ProductCategory: { x: w * 0.25, y: h * 0.78 },
      Product: { x: w * 0.55, y: h * 0.78 },
    };
    this.nodes = data.nodes.map((n, idx) => {
      const anc = typeAnchors[n.type] || { x: w / 2, y: h / 2 };
      const angle = (idx * 2.39996) % (Math.PI * 2);
      const r = 45 + (idx % 7) * 18;
      return {
        ...n,
        x: anc.x + Math.cos(angle) * r,
        y: anc.y + Math.sin(angle) * r,
        vx: 0,
        vy: 0,
        radius: TYPE_RADIUS[n.type] || 11,
      };
    });
    this.nodeByUid = {};
    this.nodes.forEach((n) => (this.nodeByUid[n.uid] = n));
    this.edges = data.edges
      .map((e) => ({ ...e, s: this.nodeByUid[e.source], t: this.nodeByUid[e.target] }))
      .filter((e) => e.s && e.t);

    this.cam = { x: 0, y: 0, scale: 1 };
    this.ticks = 220;
    this.startLoop();
  },

  startLoop() {
    if (this.rafId) cancelAnimationFrame(this.rafId);
    const step = () => {
      if (this.ticks > 0) {
        this.simulateStep();
        this.ticks--;
        if (this.ticks === 120 || this.ticks === 1) this.fitToView();
      }
      this.draw();
      if (this.ticks > 0 || this.draggingNode) {
        this.rafId = requestAnimationFrame(step);
      } else {
        this.rafId = null;
      }
    };
    this.rafId = requestAnimationFrame(step);
  },

  simulateStep() {
    const nodes = this.nodes;
    const nLen = nodes.length;
    const repulsion = 2600;
    const springLen = 108;
    const springK = 0.032;
    const cx = (canvas.clientWidth || 900) / 2;
    const cy = (canvas.clientHeight || 600) / 2;

    for (let i = 0; i < nLen; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nLen; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy + 16;
        let f = repulsion / d2;
        let dist = Math.sqrt(d2);
        let fx = (dx / dist) * f;
        let fy = (dy / dist) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
      // 轻中心引力
      a.vx += (cx - a.x) * 0.0022;
      a.vy += (cy - a.y) * 0.0022;
    }

    for (const e of this.edges) {
      let dx = e.t.x - e.s.x;
      let dy = e.t.y - e.s.y;
      let dist = Math.sqrt(dx * dx + dy * dy) || 1;
      let diff = dist - springLen;
      let fx = (dx / dist) * diff * springK;
      let fy = (dy / dist) * diff * springK;
      e.s.vx += fx;
      e.s.vy += fy;
      e.t.vx -= fx;
      e.t.vy -= fy;
    }

    for (const n of nodes) {
      if (n === this.draggingNode) continue;
      n.vx *= 0.82;
      n.vy *= 0.82;
      n.x += Math.max(-18, Math.min(18, n.vx));
      n.y += Math.max(-18, Math.min(18, n.vy));
    }
  },

  fitToView() {
    if (!this.nodes.length) return;
    const w = canvas.clientWidth || 900;
    const h = canvas.clientHeight || 600;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const n of this.nodes) {
      minX = Math.min(minX, n.x - 40);
      maxX = Math.max(maxX, n.x + 40);
      minY = Math.min(minY, n.y - 40);
      maxY = Math.max(maxY, n.y + 40);
    }
    const spanX = Math.max(200, maxX - minX);
    const spanY = Math.max(200, maxY - minY);
    const scale = Math.min(1.25, Math.max(0.35, Math.min((w - 80) / spanX, (h - 90) / spanY)));
    this.cam.scale = scale;
    this.cam.x = w / 2 - ((minX + maxX) / 2) * scale;
    this.cam.y = h / 2 - ((minY + maxY) / 2) * scale;
    this.draw();
  },

  screenToWorld(sx, sy) {
    return {
      x: (sx - this.cam.x) / this.cam.scale,
      y: (sy - this.cam.y) / this.cam.scale,
    };
  },

  hitTest(sx, sy) {
    const pt = this.screenToWorld(sx, sy);
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i];
      const dx = pt.x - n.x;
      const dy = pt.y - n.y;
      if (dx * dx + dy * dy <= (n.radius + 6) * (n.radius + 6)) return n;
    }
    return null;
  },

  draw() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    ctx2d.clearRect(0, 0, w, h);
    ctx2d.save();
    ctx2d.translate(this.cam.x, this.cam.y);
    ctx2d.scale(this.cam.scale, this.cam.scale);

    const activeUid = state.selectedNodeUid || this.hoveredUid;
    const connected = new Set();
    if (activeUid) {
      connected.add(activeUid);
      this.edges.forEach((e) => {
        if (e.source === activeUid) connected.add(e.target);
        if (e.target === activeUid) connected.add(e.source);
      });
    }

    // 绘制语义边
    for (const e of this.edges) {
      const isHi = activeUid && (e.source === activeUid || e.target === activeUid);
      ctx2d.beginPath();
      ctx2d.moveTo(e.s.x, e.s.y);
      ctx2d.lineTo(e.t.x, e.t.y);
      ctx2d.strokeStyle = isHi ? "rgba(56, 189, 248, 0.85)" : "rgba(100, 116, 139, 0.32)";
      ctx2d.lineWidth = isHi ? 2 : 1;
      ctx2d.stroke();

      if (isHi || this.nodes.length <= 32) {
        const mx = (e.s.x + e.t.x) / 2;
        const my = (e.s.y + e.t.y) / 2;
        ctx2d.font = "10px ui-monospace, monospace";
        ctx2d.fillStyle = isHi ? "#bae6fd" : "rgba(148, 163, 184, 0.65)";
        ctx2d.textAlign = "center";
        ctx2d.fillText(e.link_type, mx, my - 4);
      }
    }

    // 绘制节点
    const q = this.searchTerm.toLowerCase();
    for (const n of this.nodes) {
      const matchesSearch = q && (n.id.toLowerCase().includes(q) || n.label.toLowerCase().includes(q) || n.subtitle.toLowerCase().includes(q));
      const dimmed = activeUid && !connected.has(n.uid) && !matchesSearch;
      const color = TYPE_COLORS[n.type] || "#94a3b8";

      ctx2d.globalAlpha = dimmed ? 0.22 : 1.0;

      // DQ 异常预警外环
      if (n.dq_count > 0) {
        ctx2d.beginPath();
        ctx2d.arc(n.x, n.y, n.radius + 5, 0, Math.PI * 2);
        ctx2d.strokeStyle = n.dq_severity === "HIGH" ? "#ef4444" : "#f59e0b";
        ctx2d.lineWidth = 2.2;
        ctx2d.stroke();
      }

      // 选中或搜索命中外环
      if (n.uid === state.selectedNodeUid || matchesSearch) {
        ctx2d.beginPath();
        ctx2d.arc(n.x, n.y, n.radius + 8, 0, Math.PI * 2);
        ctx2d.strokeStyle = "#38bdf8";
        ctx2d.lineWidth = 2;
        ctx2d.stroke();
      }

      ctx2d.beginPath();
      ctx2d.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
      ctx2d.fillStyle = color;
      ctx2d.fill();
      ctx2d.strokeStyle = "#0a0f1e";
      ctx2d.lineWidth = 1.5;
      ctx2d.stroke();

      // 标签
      ctx2d.font = `${n.uid === state.selectedNodeUid || matchesSearch ? "700 " : ""}11px -apple-system, sans-serif`;
      ctx2d.fillStyle = "#eef2ff";
      ctx2d.textAlign = "center";
      ctx2d.fillText(n.label, n.x, n.y + n.radius + 14);
    }

    ctx2d.globalAlpha = 1.0;
    ctx2d.restore();
  },
};

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
  graphEngine.draw();
}
window.addEventListener("resize", resizeCanvas);

canvas.addEventListener("mousedown", (e) => {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const hit = graphEngine.hitTest(sx, sy);
  graphEngine.lastMouse = { x: sx, y: sy };
  if (hit) {
    graphEngine.draggingNode = hit;
    selectNode(hit.type, hit.id);
    graphEngine.startLoop();
  } else {
    graphEngine.panning = true;
  }
});

canvas.addEventListener("mousemove", (e) => {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  if (graphEngine.draggingNode) {
    const pt = graphEngine.screenToWorld(sx, sy);
    graphEngine.draggingNode.x = pt.x;
    graphEngine.draggingNode.y = pt.y;
    graphEngine.draw();
  } else if (graphEngine.panning) {
    graphEngine.cam.x += sx - graphEngine.lastMouse.x;
    graphEngine.cam.y += sy - graphEngine.lastMouse.y;
    graphEngine.lastMouse = { x: sx, y: sy };
    graphEngine.draw();
  } else {
    const hit = graphEngine.hitTest(sx, sy);
    const uid = hit ? hit.uid : null;
    if (uid !== graphEngine.hoveredUid) {
      graphEngine.hoveredUid = uid;
      canvas.style.cursor = uid ? "pointer" : "grab";
      graphEngine.draw();
    }
  }
});

window.addEventListener("mouseup", () => {
  graphEngine.draggingNode = null;
  graphEngine.panning = false;
});

canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    const newScale = Math.min(2.6, Math.max(0.28, graphEngine.cam.scale * factor));
    const ratio = newScale / graphEngine.cam.scale;
    graphEngine.cam.x = sx - (sx - graphEngine.cam.x) * ratio;
    graphEngine.cam.y = sy - (sy - graphEngine.cam.y) * ratio;
    graphEngine.cam.scale = newScale;
    graphEngine.draw();
  },
  { passive: false }
);

document.getElementById("zoomInBtn").addEventListener("click", () => {
  graphEngine.cam.scale = Math.min(2.6, graphEngine.cam.scale * 1.2);
  graphEngine.draw();
});
document.getElementById("zoomOutBtn").addEventListener("click", () => {
  graphEngine.cam.scale = Math.max(0.28, graphEngine.cam.scale * 0.82);
  graphEngine.draw();
});
document.getElementById("fitGraphBtn").addEventListener("click", () => graphEngine.fitToView());

document.getElementById("graphSearchInput").addEventListener("input", (e) => {
  graphEngine.searchTerm = e.target.value.trim();
  graphEngine.draw();
});

async function loadGraph(preset) {
  state.currentPreset = preset;
  document.querySelectorAll("#graphPresetGroup button").forEach((b) => {
    b.classList.toggle("active", b.dataset.preset === preset);
  });
  const data = await apiGet(`/api/graph?preset=${encodeURIComponent(preset)}`);
  document.getElementById("graphStatsPill").textContent = `${data.node_count} 节点 · ${data.edge_count} 语义边`;
  resizeCanvas();
  graphEngine.setData(data);
}

document.querySelectorAll("#graphPresetGroup button").forEach((btn) => {
  btn.addEventListener("click", () => loadGraph(btn.dataset.preset));
});

async function selectNode(objType, objId) {
  state.selectedNodeUid = `${objType}:${objId}`;
  graphEngine.draw();

  const detail = await apiGet(`/api/node?type=${encodeURIComponent(objType)}&id=${encodeURIComponent(objId)}`);
  const ins = document.getElementById("nodeInspector");
  if (detail.ok === false) {
    ins.innerHTML = `<h3>对象未找到</h3><p>${detail.message}</p>`;
    return;
  }

  const propsHtml = Object.entries(detail.properties)
    .map(([k, v]) => `<div class="prop-key">${k}</div><div class="prop-val">${v === null ? "<i>null</i>" : String(v)}</div>`)
    .join("");

  const outHtml = detail.neighborhood.outgoing
    .map(
      (e) =>
        `<span class="neighbor-chip" onclick="selectNode('${e.neighbor_type}','${e.neighbor_id}')">${e.link_type} → ${e.neighbor_type}:${e.neighbor_id}</span>`
    )
    .join("");
  const inHtml = detail.neighborhood.incoming
    .slice(0, 14)
    .map(
      (e) =>
        `<span class="neighbor-chip" onclick="selectNode('${e.neighbor_type}','${e.neighbor_id}')">← ${e.link_type} (${e.neighbor_type}:${e.neighbor_id})</span>`
    )
    .join("");

  const dqHtml = detail.neighborhood.data_quality_issues.length
    ? detail.neighborhood.data_quality_issues
        .map(
          (i) =>
            `<div style="padding:8px; margin-bottom:6px; border-radius:6px; background:rgba(239,68,68,0.1); border:1px solid rgba(239,68,68,0.35); font-size:12px;">
               <div>${sevBadge(i.severity)} <strong class="mono">${i.rule_code}</strong> ${i.rule_name}</div>
               <div style="margin-top:4px; color:var(--text-secondary)">${i.description}</div>
             </div>`
        )
        .join("")
    : `<div style="font-size:12px; color:var(--emerald)">无数据质量异常</div>`;

  let quickActionHtml = "";
  if (objType === "SalesOrder") {
    const exp = detail.cq1_explain;
    quickActionHtml = `
      <div style="margin:10px 0; padding:8px; border-radius:6px; background:var(--bg-card); font-size:12px;">
        <strong>CQ1 规则解释：</strong>${exp.reasons.join("；")}
      </div>
      <button class="btn sm primary" onclick="quickEscalate('${objId}', false)">在演练台测试催办此单</button>`;
  } else if (objType === "Product" && detail.properties.category_status !== "VALID") {
    quickActionHtml = `<button class="btn sm warn" onclick="jumpFixProduct('${objId}')">前往修复该商品分类</button>`;
  }

  ins.innerHTML = `
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
      <span class="badge b-key">${detail.object_type}</span>
      <span class="mono" style="color:var(--accent)">${detail.object_id}</span>
    </div>
    ${quickActionHtml}
    <h3 style="margin-top:12px;">标准化属性与血缘</h3>
    <div class="prop-list">${propsHtml}</div>
    <h3>挂载数据质量异常 (${detail.neighborhood.data_quality_issues.length})</h3>
    <div style="margin-bottom:12px;">${dqHtml}</div>
    <h3>出边语义关系 (${detail.neighborhood.outgoing.length})</h3>
    <div style="margin-bottom:10px;">${outHtml || '<span style="font-size:12px;color:var(--text-muted)">无</span>'}</div>
    <h3>入边语义关系 (${detail.neighborhood.incoming.length})</h3>
    <div>${inHtml || '<span style="font-size:12px;color:var(--text-muted)">无</span>'}</div>
  `;
}

window.selectNode = selectNode;

window.inspectInGraph = async function (objType, objId) {
  switchTab("graph");
  if (!graphEngine.nodeByUid[`${objType}:${objId}`]) {
    await loadGraph("core");
  }
  await selectNode(objType, objId);
};

// ---------------- AI Agent Loop Workbench ----------------

let agentState = {
  presets: [],
  sessions: [],
  activeSessionId: null,
};

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderAgentLiveOntologyBox(ov) {
  const box = document.getElementById("agentLiveOntologyBox");
  if (!box || !ov) return;
  const catMap = {};
  (ov.cq3.root_category_summary || []).forEach((c) => (catMap[c.root_category_code] = c.total_amount));
  box.innerHTML = `
    <div style="display:grid; grid-template-columns:1fr auto; gap:5px 10px;">
      <span>VIP 逾期未发（CQ1）</span><strong class="mono" style="color:var(--crimson)">${ov.cq1.length} 张</strong>
      <span>统一客户实体（CQ2）</span><strong class="mono">${ov.summary.object_counts.Customer} 家</strong>
      <span>01 办公设备营收</span><strong class="mono" style="color:var(--accent)">${fmtMoney(catMap["01"] || 0)}</strong>
      <span>未归类异常挂账</span><strong class="mono" style="color:var(--amber)">${fmtMoney(catMap["UNCLASSIFIED"] || 0)}</strong>
      <span>数据质量异常（CQ4）</span><strong class="mono" style="color:var(--amber)">${ov.cq4.total_issues} 条</strong>
      <span>已落账审计动作</span><strong class="mono" style="color:var(--emerald)">${ov.ledger.length} 条</strong>
    </div>
  `;
}

function resetAgentUI() {
  agentState.activeSessionId = null;
  document.getElementById("activeSessionTitle").textContent = "执行轨迹（ReAct + MCP Trace）";
  document.getElementById("activeSessionSub").textContent =
    "点击上方任意一键场景卡片，或输入自然语言任务启动完整的 Agent Loop";
  document.getElementById("activeSessionStatus").textContent = "IDLE";
  document.getElementById("activeSessionStatus").className = "badge b-std";
  document.getElementById("agentTraceContainer").innerHTML = `
    <div style="padding:26px 12px; text-align:center; color:var(--text-muted); font-size:13px;">
      沙箱已重置。请点击上方一键场景卡片启动新的 Agent Loop。
    </div>`;
  document.getElementById("agentApprovalContainer").innerHTML = "";
  renderAgentMetrics({ iterations: 0, mcp_calls: 0, guard_blocks: 0, actions_committed: 0 });
}

function renderAgentMetrics(m) {
  const inTok = m.prompt_tokens || 0;
  const outTok = m.completion_tokens || 0;
  const totTok = m.total_tokens || 0;
  const lat = m.llm_latency_ms || 0;
  document.getElementById("agentMiniMetrics").innerHTML = `
    <div class="mini-metric"><div class="mm-val">${m.iterations || 0}</div><div class="mm-lbl">ReAct 轮次</div></div>
    <div class="mini-metric"><div class="mm-val">${m.mcp_calls || 0}</div><div class="mm-lbl">MCP tools/call</div></div>
    <div class="mini-metric"><div class="mm-val" style="color:${m.guard_blocks ? "var(--crimson)" : "var(--accent)"}">${m.guard_blocks || 0}</div><div class="mm-lbl">前置守卫拦截</div></div>
    <div class="mini-metric"><div class="mm-val" style="color:${m.actions_committed ? "var(--emerald)" : "var(--accent)"}">${m.actions_committed || 0}</div><div class="mm-lbl">已提交受控动作</div></div>
    <div class="mini-metric" title="prompt=${inTok}, completion=${outTok}"><div class="mm-val">${totTok}</div><div class="mm-lbl">LLM Tokens (in:${inTok}/out:${outTok})</div></div>
    <div class="mini-metric"><div class="mm-val">${lat}ms</div><div class="mm-lbl">LLM 推理耗时</div></div>
  `;
}

function statusBadgeClass(st) {
  if (st === "COMPLETED") return "badge b-ok";
  if (st === "AWAITING_APPROVAL") return "badge b-med";
  if (st === "FAILED") return "badge b-high";
  return "badge b-key";
}

function kindTag(kind) {
  const map = {
    thought: ["THOUGHT", "b-vip"],
    tool_call: ["MCP CALL", "b-key"],
    observation: ["OBSERVE", "b-ok"],
    guard_blocked: ["GUARD BLOCKED", "b-high"],
    approval_required: ["HITL GATE", "b-med"],
    approval_decision: ["APPROVED/DECIDED", "b-ok"],
    final_answer: ["FINAL", "b-key"],
    error: ["ERROR", "b-high"],
  };
  const [label, cls] = map[kind] || [kind.toUpperCase(), "b-std"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderAgentSession(sess) {
  if (!sess) return;
  agentState.activeSessionId = sess.session_id;
  document.getElementById("activeSessionTitle").textContent = `${sess.session_id} · ${sess.task}`;
  const mi = sess.model_info || {};
  const modelTag = mi.model ? `${mi.provider}/${mi.model}` : sess.engine_mode;
  document.getElementById("activeSessionSub").textContent =
    `模型=${modelTag} · HITL人工审批=${sess.require_approval ? "开启" : "关闭"} · 创建于 ${sess.created_at}`;
  const stEl = document.getElementById("activeSessionStatus");
  stEl.textContent = sess.status;
  stEl.className = statusBadgeClass(sess.status);

  renderAgentMetrics(sess.metrics);

  const container = document.getElementById("agentTraceContainer");
  container.innerHTML = sess.steps
    .map((step) => {
      let detailsHtml = "";
      if (step.tool_args || step.tool_result) {
        const jsonPayload = step.tool_result
          ? JSON.stringify(step.tool_result, null, 2)
          : JSON.stringify(step.tool_args, null, 2);
        const label = step.tool_result ? "展开查看 MCP 返回载荷 (JSON)" : "展开查看 MCP 调用参数 (JSON)";
        detailsHtml = `
          <details class="trace-details">
            <summary>${label}</summary>
            <pre>${escHtml(jsonPayload)}</pre>
          </details>`;
      }
      const dur = step.duration_ms ? `<span class="mono" style="color:var(--text-muted)">${step.duration_ms}ms</span>` : "";
      return `
        <div class="trace-card kind-${escHtml(step.kind)}">
          <div class="trace-head">
            <div class="trace-title">
              <span class="mono" style="color:var(--text-muted)">#${step.step_no}</span>
              ${kindTag(step.kind)}
              <span>${escHtml(step.title)}</span>
            </div>
            ${dur}
          </div>
          <div class="trace-body">${escHtml(step.content)}</div>
          ${detailsHtml}
        </div>`;
    })
    .join("");

  // HITL 审批闸门卡片
  const apEl = document.getElementById("agentApprovalContainer");
  if (sess.status === "AWAITING_APPROVAL" && sess.pending_approval) {
    const ap = sess.pending_approval;
    apEl.innerHTML = `
      <div class="approval-gate">
        <h3>⏸️ Human-in-the-Loop（HITL）审批闸门 · 等待人工放行</h3>
        <div class="ap-summary">${escHtml(ap.summary)}</div>
        <details class="trace-details" open>
          <summary>Dry Run 预演通过快照（${escHtml(ap.tool_name)}）</summary>
          <pre>${escHtml(JSON.stringify(ap.dry_run_preview, null, 2))}</pre>
        </details>
        <div class="ap-actions">
          <input type="text" id="hitlReviewerNote" placeholder="可选审批意见，如：已核实同意执行" style="flex:1; min-width:200px;" />
          <button class="btn success" id="hitlApproveBtn">✅ 批准并继续执行 Loop</button>
          <button class="btn danger" id="hitlRejectBtn">❌ 驳回本次写动作</button>
        </div>
      </div>`;
    document.getElementById("hitlApproveBtn").addEventListener("click", () => resolveApproval(sess.session_id, true));
    document.getElementById("hitlRejectBtn").addEventListener("click", () => resolveApproval(sess.session_id, false));
  } else {
    apEl.innerHTML = "";
  }
}

function renderSessionHistory(sessions) {
  agentState.sessions = sessions || [];
  const list = document.getElementById("agentSessionHistory");
  if (!agentState.sessions.length) {
    list.innerHTML = `<div style="font-size:12px; color:var(--text-muted);">暂无历史 Session</div>`;
    return;
  }
  list.innerHTML = agentState.sessions
    .map(
      (s) => `
      <div class="session-item ${s.session_id === agentState.activeSessionId ? "active" : ""}" onclick="selectAgentSession('${s.session_id}')">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
          <strong class="mono">${s.session_id}</strong>
          <span class="${statusBadgeClass(s.status)}">${s.status}</span>
        </div>
        <div style="color:var(--text-secondary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escHtml(s.task)}</div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:3px;">
          MCP调用 ${s.metrics.mcp_calls} · 守卫拦截 ${s.metrics.guard_blocks} · 已提交 ${s.metrics.actions_committed}
        </div>
      </div>`
    )
    .join("");
}

window.selectAgentSession = function (sid) {
  const found = agentState.sessions.find((s) => s.session_id === sid);
  if (found) {
    renderAgentSession(found);
    renderSessionHistory(agentState.sessions);
  }
};

function applyLlmConfigToUI(cfg) {
  if (!cfg) return;
  document.getElementById("llmProvider").value = cfg.provider || "openai_compatible";
  document.getElementById("llmBaseUrl").value = cfg.base_url || "";
  document.getElementById("llmModel").value = cfg.model || "";
  document.getElementById("llmMaxTurns").value = String(cfg.max_turns ?? 8);
  document.getElementById("llmTemperature").value = String(cfg.temperature ?? 0.1);

  const keyInput = document.getElementById("llmApiKey");
  keyInput.value = "";
  keyInput.placeholder = cfg.has_api_key
    ? `已配置 (${cfg.api_key_masked})，留空则继续使用`
    : "预留待填：输入你的真实 API Key（本地 Ollama 可留空）";

  const badge = document.getElementById("llmReadyBadge");
  if (cfg.is_ready) {
    badge.className = "badge b-ok";
    badge.textContent = cfg.is_local_endpoint
      ? `✅ 本地端点就绪 (${cfg.model})`
      : `✅ 真实模型就绪 (${cfg.api_key_masked})`;
  } else {
    badge.className = "badge b-med";
    badge.textContent = "⚠️ API Key 预留待填";
  }

  document.getElementById("llmSourceHint").textContent =
    `来源：${cfg.config_source || "default"} · 私有文件：${cfg.local_config_path || "config/llm.local.yaml"}`;
  document.getElementById("clearKeyBtn").style.display = cfg.has_api_key ? "inline-flex" : "none";

  // 渲染一键厂商预设按钮
  const presets = cfg.provider_presets || {};
  const bar = document.getElementById("providerPresetsBar");
  bar.innerHTML =
    `<span style="font-size:11px; color:var(--text-muted); align-self:center;">快捷厂商预设：</span>` +
    Object.entries(presets)
      .map(
        ([key, spec]) =>
          `<button class="btn sm" type="button" data-provider-preset="${key}">${escHtml(spec.label || key)}</button>`
      )
      .join("");
  bar.querySelectorAll("button[data-provider-preset]").forEach((b) => {
    b.addEventListener("click", () => {
      const spec = presets[b.dataset.providerPreset];
      if (!spec) return;
      document.getElementById("llmProvider").value = spec.provider;
      document.getElementById("llmBaseUrl").value = spec.base_url;
      document.getElementById("llmModel").value = spec.model;
      showToast(`已填入 ${spec.label} 官方端点与默认模型，请填写 API Key 后保存或测试`);
    });
  });
}

function readLlmFormPayload() {
  return {
    provider: document.getElementById("llmProvider").value,
    base_url: document.getElementById("llmBaseUrl").value.trim(),
    model: document.getElementById("llmModel").value.trim(),
    api_key: document.getElementById("llmApiKey").value.trim(),
    max_turns: Number(document.getElementById("llmMaxTurns").value || 8),
    temperature: Number(document.getElementById("llmTemperature").value || 0.1),
  };
}

async function loadAgentPresets() {
  const data = await apiGet("/api/agent/presets");
  agentState.presets = data.presets || [];
  if (data.llm_config) {
    applyLlmConfigToUI(data.llm_config);
  }
  const grid = document.getElementById("agentPresetsGrid");
  grid.innerHTML = agentState.presets
    .map((p) => {
      const badgeCls = p.kind === "guard" ? "b-high" : p.kind === "good" ? "b-ok" : "b-key";
      return `
        <button class="agent-preset-card" data-preset-id="${p.id}">
          <div><span class="badge ${badgeCls}">${escHtml(p.badge)}</span></div>
          <div class="p-title">${escHtml(p.title)}</div>
          <div class="p-prompt">${escHtml(p.prompt)}</div>
        </button>`;
    })
    .join("");

  grid.querySelectorAll(".agent-preset-card").forEach((card) => {
    card.addEventListener("click", () => {
      const p = agentState.presets.find((x) => x.id === card.dataset.presetId);
      if (!p) return;
      document.getElementById("agentTaskInput").value = p.prompt;
      runAgentLoop(p.prompt);
    });
  });

  renderSessionHistory(data.sessions || []);
}

async function runAgentLoop(customTask) {
  const input = document.getElementById("agentTaskInput");
  const task = (customTask ?? input.value).trim();
  if (!task) {
    showToast("请先输入任务或点击上方场景卡片");
    return;
  }
  const btn = document.getElementById("runAgentBtn");
  btn.disabled = true;
  btn.textContent = "Agent 推理执行中...";
  try {
    const engineMode = document.getElementById("agentEngineMode").value;
    const requireApproval = document.getElementById("agentRequireApproval").checked;
    const llmConfig = engineMode === "real_llm" ? readLlmFormPayload() : {};

    const res = await apiPost("/api/agent/run", {
      task,
      engine_mode: engineMode,
      require_approval: requireApproval,
      llm_config: llmConfig,
    });
    if (!res.ok) {
      showToast(`Agent 启动失败：${res.message}`);
      return;
    }
    const sess = res.session;
    renderAgentSession(sess);
    const hist = await apiGet("/api/agent/sessions");
    renderSessionHistory(hist.sessions || []);
    await refreshAll();
    if (sess.status === "AWAITING_APPROVAL") {
      showToast(`Agent 已预演通过并暂停，等待人工审批 (${sess.pending_approval.tool_name})`);
    } else if (sess.status === "COMPLETED") {
      showToast(`Agent Loop 完成 (${sess.session_id} · MCP 调用 ${sess.metrics.mcp_calls} 次)`);
    } else if (sess.status === "FAILED") {
      showToast("真实模型未就绪或调用失败，请查看下方详情");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "启动 Agent Loop";
  }
}

async function resolveApproval(sessionId, approved) {
  const noteEl = document.getElementById("hitlReviewerNote");
  const reviewerNote = noteEl ? noteEl.value.trim() : "";
  const res = await apiPost("/api/agent/approve", {
    session_id: sessionId,
    approved,
    reviewer_note: reviewerNote,
  });
  if (!res.ok) {
    showToast(`审批处理失败：${res.message}`);
    return;
  }
  renderAgentSession(res.session);
  const hist = await apiGet("/api/agent/sessions");
  renderSessionHistory(hist.sessions || []);
  await refreshAll();
  await loadW3CAsset(state.currentAsset);
  showToast(approved ? "已批准执行并完成闭环更新！" : "已驳回该写操作，本体保持只读无变更");
}

document.getElementById("agentEngineMode").addEventListener("change", (e) => {
  document.getElementById("llmConfigCard").classList.toggle("collapsed", e.target.value !== "real_llm");
});

document.getElementById("toggleKeyVisBtn").addEventListener("click", () => {
  const el = document.getElementById("llmApiKey");
  el.type = el.type === "password" ? "text" : "password";
});

document.getElementById("saveLlmBtn").addEventListener("click", async () => {
  const payload = readLlmFormPayload();
  const res = await apiPost("/api/agent/config", payload);
  if (res.ok) {
    applyLlmConfigToUI(res.config);
    const st = document.getElementById("llmPingStatus");
    st.className = "ping-status ok";
    st.textContent = `已保存到 ${res.config.local_config_path}（协议=${res.config.provider}，模型=${res.config.model}）`;
    showToast("模型配置已保存到本地 config/llm.local.yaml");
  } else {
    showToast(`保存失败：${res.message}`);
  }
});

document.getElementById("clearKeyBtn").addEventListener("click", async () => {
  const res = await apiPost("/api/agent/config", { clear_api_key: true });
  if (res.ok) {
    applyLlmConfigToUI(res.config);
    showToast("已清除本地保存的 API Key");
  }
});

document.getElementById("pingLlmBtn").addEventListener("click", async () => {
  const btn = document.getElementById("pingLlmBtn");
  const st = document.getElementById("llmPingStatus");
  btn.disabled = true;
  st.className = "ping-status";
  st.textContent = "正在向真实模型端点发送连通性探活请求...";
  try {
    const res = await apiPost("/api/agent/ping", readLlmFormPayload());
    if (res.ok) {
      st.className = "ping-status ok";
      st.textContent = `✅ 连通成功！${res.provider}/${res.model} · 延迟 ${res.latency_ms}ms · 回复="${res.reply}" · Tokens=${res.usage?.total_tokens ?? 0}`;
      showToast(`模型连通性测试通过 (${res.latency_ms}ms)`);
    } else {
      st.className = "ping-status err";
      st.textContent = `❌ 连通失败：${res.message}`;
      showToast("模型连通性测试失败，请检查端点或 Key");
    }
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("runAgentBtn").addEventListener("click", () => runAgentLoop());
document.getElementById("agentTaskInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runAgentLoop();
});

// ---------------- Ontology vs Text-to-SQL Arena ----------------

let arenaState = {
  benchmarks: [],
  cachedById: {},
  activeBenchmarkId: null,
  cachedReport: null,
};

function renderArenaSummaryKpis(report) {
  const el = document.getElementById("arenaSummaryKpis");
  if (!el) return;
  if (!report) {
    el.innerHTML = `
      <div class="kpi-card warn"><div class="kpi-label">Text-to-SQL 总分</div><div class="kpi-value">待实测</div><div class="kpi-sub">点击下方题目启动真实同题 PK</div></div>
      <div class="kpi-card success"><div class="kpi-label">Operational Ontology 总分</div><div class="kpi-value">待实测</div><div class="kpi-sub">同一模型通过 9 个 MCP 工具</div></div>
      <div class="kpi-card"><div class="kpi-label">Token 成本对比</div><div class="kpi-value">-</div><div class="kpi-sub">实时统计 prompt + completion</div></div>
      <div class="kpi-card"><div class="kpi-label">端到端耗时对比</div><div class="kpi-value">-</div><div class="kpi-sub">实时统计 LLM 推理总延迟</div></div>`;
    return;
  }
  const tTok = report.text_to_sql_total_tokens || 1;
  const oTok = report.ontology_total_tokens || 0;
  const tokSaved = (((tTok - oTok) / tTok) * 100).toFixed(1);
  const tMs = report.text_to_sql_total_latency_ms || 1;
  const oMs = report.ontology_total_latency_ms || 1;
  const speedup = (tMs / oMs).toFixed(1);

  document.getElementById("arenaModelBadge").textContent = `实测模型：${report.model || "Qwen3.8"}`;
  el.innerHTML = `
    <div class="kpi-card danger">
      <div class="kpi-label">Text-to-SQL（10张裸表）实测总分</div>
      <div class="kpi-value">${escHtml(report.text_to_sql_score)}</div>
      <div class="kpi-sub">共耗 ${tTok.toLocaleString()} Tokens · ${(tMs / 1000).toFixed(1)}s</div>
    </div>
    <div class="kpi-card success">
      <div class="kpi-label">Operational Ontology（MCP）实测总分</div>
      <div class="kpi-value">${escHtml(report.ontology_score)}</div>
      <div class="kpi-sub">仅耗 ${oTok.toLocaleString()} Tokens · ${(oMs / 1000).toFixed(1)}s</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Ontology 节省 Token 成本</div>
      <div class="kpi-value">-${tokSaved}%</div>
      <div class="kpi-sub">省去反复探查 10 张脏表与码表的盲试开销</div>
    </div>
    <div class="kpi-card warn">
      <div class="kpi-label">Ontology 推理提速倍数</div>
      <div class="kpi-value">${speedup}x</div>
      <div class="kpi-sub">大幅降低企业 AI 网关并发请求与 429 限流压力</div>
    </div>
  `;
}

function renderArenaEvalChecklist(elId, ev) {
  const el = document.getElementById(elId);
  if (!ev || !ev.has_ground_truth) {
    el.style.display = "none";
    return;
  }
  el.style.display = "flex";
  el.innerHTML = ev.details
    .map(
      (d) => `
      <div class="check-item ${d.passed ? "pass" : "fail"}">
        <span>${d.passed ? "✅" : "❌"} ${escHtml(d.label)}</span>
        <span class="mono" style="opacity:0.85;">${escHtml(d.note)}</span>
      </div>`
    )
    .join("");
}

function renderArenaResult(res) {
  if (!res) return;
  arenaState.activeBenchmarkId = res.benchmark_id || null;
  document.querySelectorAll(".arena-bench-card").forEach((c) => {
    c.classList.toggle("active", c.dataset.benchId === arenaState.activeBenchmarkId);
  });

  const t2s = res.text_to_sql;
  const onto = res.ontology;
  const tEv = t2s.evaluation;
  const oEv = onto.evaluation;

  // Ground truth 横幅
  const gtBanner = document.getElementById("arenaGroundTruthBanner");
  if (oEv && oEv.has_ground_truth && oEv.ground_truth_summary) {
    gtBanner.style.display = "block";
    gtBanner.innerHTML = `<strong style="color:var(--accent);">🎯 本题 Ground Truth 验收标准：</strong><span style="font-size:13px; color:var(--text-secondary);">${escHtml(oEv.ground_truth_summary)}</span>`;
  } else {
    gtBanner.style.display = "none";
  }

  // 左侧：Text-to-SQL
  const tBadge = document.getElementById("t2sScoreBadge");
  if (tEv && tEv.has_ground_truth) {
    tBadge.textContent = `得分 ${tEv.passed}/${tEv.total} (${tEv.score_pct}%)`;
    tBadge.className = `badge ${tEv.score_pct === 100 ? "b-ok" : tEv.score_pct >= 50 ? "b-med" : "b-high"}`;
  } else {
    tBadge.textContent = t2s.status || "DONE";
    tBadge.className = "badge b-med";
  }
  document.getElementById("t2sMetricsBar").innerHTML = `
    <span class="pill">轮次: ${t2s.metrics.iterations}</span>
    <span class="pill">SQL数: ${(t2s.sql_queries || []).length}</span>
    <span class="pill">Tokens: ${(t2s.metrics.total_tokens || 0).toLocaleString()}</span>
    <span class="pill">耗时: ${t2s.metrics.llm_latency_ms || 0}ms</span>
  `;
  renderArenaEvalChecklist("t2sCheckList", tEv);
  document.getElementById("t2sAnswerBox").textContent = t2s.final_answer || "(无最终文本回答)";
  document.getElementById("t2sStepsList").innerHTML = (t2s.sql_queries || [])
    .map(
      (q, i) => `
      <details class="trace-details" style="background:var(--bg-card); border:1px solid var(--border); border-radius:7px; padding:8px 10px;">
        <summary>SQL #${i + 1} (Round ${q.round} · ${q.ok ? `返回 ${q.row_count} 行` : "报错"}) — ${escHtml(q.sql.replace(/\s+/g, " ").slice(0, 72))}...</summary>
        <pre>${escHtml(q.sql)}</pre>
        <pre>${escHtml(JSON.stringify(q.ok ? q.sample_rows : { error: q.error }, null, 2))}</pre>
      </details>`
    )
    .join("");

  // 右侧：Operational Ontology
  const oBadge = document.getElementById("ontoScoreBadge");
  if (oEv && oEv.has_ground_truth) {
    oBadge.textContent = `得分 ${oEv.passed}/${oEv.total} (${oEv.score_pct}%)`;
    oBadge.className = `badge ${oEv.score_pct === 100 ? "b-ok" : oEv.score_pct >= 50 ? "b-med" : "b-high"}`;
  } else {
    oBadge.textContent = onto.status || "DONE";
    oBadge.className = "badge b-ok";
  }
  document.getElementById("ontoMetricsBar").innerHTML = `
    <span class="pill">轮次: ${onto.metrics.iterations}</span>
    <span class="pill">MCP调用: ${onto.metrics.mcp_calls}</span>
    <span class="pill">守卫拦截: ${onto.metrics.guard_blocks}</span>
    <span class="pill">Tokens: ${(onto.metrics.total_tokens || 0).toLocaleString()}</span>
    <span class="pill">耗时: ${onto.metrics.llm_latency_ms || 0}ms</span>
  `;
  renderArenaEvalChecklist("ontoCheckList", oEv);
  document.getElementById("ontoAnswerBox").textContent = onto.final_answer || "(无最终文本回答)";
  document.getElementById("ontoStepsList").innerHTML = (onto.steps || [])
    .filter((s) => s.kind !== "final_answer")
    .map(
      (s) => `
      <details class="trace-details" style="background:var(--bg-card); border:1px solid var(--border); border-radius:7px; padding:8px 10px;">
        <summary>#${s.step_no} [${s.kind.toUpperCase()}] ${escHtml(s.title)}</summary>
        <pre>${escHtml(s.content)}</pre>
        ${s.tool_result ? `<pre>${escHtml(JSON.stringify(s.tool_result, null, 2))}</pre>` : ""}
      </details>`
    )
    .join("");
}

async function runLiveArena(question, benchmarkId = null) {
  const q = (question ?? document.getElementById("arenaQuestionInput").value).trim();
  if (!q) {
    showToast("请先输入业务问题或点击上方题目卡片");
    return;
  }
  const btn = document.getElementById("runArenaBtn");
  btn.disabled = true;
  btn.textContent = "⚡ 真实模型双路 PK 运行中（约 10-30s）...";
  showToast("正在使用真实模型同时运行 Text-to-SQL 与 Operational Ontology...");
  try {
    const res = await apiPost("/api/arena/run", {
      question: q,
      benchmark_id: benchmarkId,
      llm_config: readLlmFormPayload(),
    });
    if (!res.ok) {
      showToast(`Arena 运行失败：${res.message}`);
      return;
    }
    renderArenaResult(res);
    showToast("同题 PK 完成！左右两栏已更新真实对比结果");
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ 启动同题 PK";
  }
}

async function loadArenaData() {
  const data = await apiGet("/api/arena/benchmarks");
  if (!data.ok) return;
  arenaState.benchmarks = data.benchmarks || [];
  arenaState.cachedReport = data.cached_report || null;
  arenaState.cachedById = {};
  if (arenaState.cachedReport && arenaState.cachedReport.benchmarks) {
    arenaState.cachedReport.benchmarks.forEach((b) => (arenaState.cachedById[b.benchmark_id] = b));
  }
  document.getElementById("arenaRawDdlBox").textContent = data.raw_ddl || "";
  renderArenaSummaryKpis(arenaState.cachedReport);

  const cardsEl = document.getElementById("arenaBenchCards");
  cardsEl.innerHTML = arenaState.benchmarks
    .map((b) => {
      const c = arenaState.cachedById[b.id];
      const scoreLine = c
        ? `<div class="mono" style="font-size:11px; display:flex; justify-content:space-between;">
             <span style="color:#fca5a5;">SQL: ${c.text_to_sql.evaluation.passed}/${c.text_to_sql.evaluation.total}</span>
             <span style="color:#86efac;">Ontology: ${c.ontology.evaluation.passed}/${c.ontology.evaluation.total}</span>
           </div>`
        : "";
      return `
        <div class="arena-bench-card" data-bench-id="${b.id}">
          <div><span class="badge b-key">${escHtml(b.tag)}</span></div>
          <div style="font-size:12px; font-weight:700; line-height:1.35;">${escHtml(b.title)}</div>
          ${scoreLine}
          <div style="display:flex; gap:6px; margin-top:auto; padding-top:4px;">
            ${c ? `<button class="btn sm" type="button" data-view-bench="${b.id}">查看实测</button>` : ""}
            <button class="btn sm primary" type="button" data-run-bench="${b.id}">⚡ 现场 PK</button>
          </div>
        </div>`;
    })
    .join("");

  cardsEl.querySelectorAll("button[data-view-bench]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const b = arenaState.benchmarks.find((x) => x.id === btn.dataset.viewBench);
      const c = arenaState.cachedById[btn.dataset.viewBench];
      if (b) document.getElementById("arenaQuestionInput").value = b.question;
      if (c) renderArenaResult(c);
    });
  });

  cardsEl.querySelectorAll("button[data-run-bench]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const b = arenaState.benchmarks.find((x) => x.id === btn.dataset.runBench);
      if (!b) return;
      document.getElementById("arenaQuestionInput").value = b.question;
      runLiveArena(b.question, b.id);
    });
  });

  // 默认自动展示第 1 题（CQ1）的真实 Qwen3.8 对比结果
  if (arenaState.cachedById["bench_cq1"]) {
    document.getElementById("arenaQuestionInput").value = arenaState.benchmarks[0].question;
    renderArenaResult(arenaState.cachedById["bench_cq1"]);
  }
}

document.getElementById("runArenaBtn").addEventListener("click", () => runLiveArena());
document.getElementById("arenaQuestionInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runLiveArena();
});

// ---------------- Init ----------------
(async function init() {
  await refreshAll();
  await loadAgentPresets();
  await loadArenaData();
  await loadW3CAsset("tbox");
})();
