/* ============================================================
   主动聊天插件 - 管理面板 JavaScript
   通过 window.AstrBotPluginPage bridge 与后端通信
   ============================================================ */

const bridge = window.AstrBotPluginPage;

// ---- 全局状态 ----
let prompts = [];
let pluginStatus = {};

// ---- DOM 引用 ----
function $(id) {
    return document.getElementById(id);
}

// ---- 工具函数 ----
function showMsg(elementId, text, type) {
    const el = $(elementId);
    if (!el) return;
    el.textContent = text;
    el.className = "save-msg " + (type || "");
    // 错误消息不自动消失；成功消息 5 秒后消失
    if (type === "success") {
        setTimeout(() => {
            if (el.textContent === text) {
                el.textContent = "";
                el.className = "save-msg";
            }
        }, 5000);
    }
    // error 消息保持显示，直到下次操作覆盖
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ---- API 调用封装 ----
async function apiGet(endpoint, params = {}) {
    try {
        return await bridge.apiGet(endpoint, params);
    } catch (e) {
        console.error("API GET error:", endpoint, e);
        throw e;
    }
}

async function apiPost(endpoint, body = {}) {
    try {
        // 确保 body 是普通对象，bridge 内部会 JSON.stringify
        const result = await bridge.apiPost(endpoint, body);
        return result;
    } catch (e) {
        console.error("API POST error:", endpoint, e);
        throw e;
    }
}

// ---- 页面初始化 ----
async function init() {
    const context = await bridge.ready();
    console.log("[主动聊天] Bridge ready:", context);

    // 绑定事件
    $("btnAddPrompt").addEventListener("click", addPrompt);
    $("btnSaveAll").addEventListener("click", saveAllPrompts);

    // ★ 事件委托：在容器上绑一个监听器，靠冒泡捕获所有按钮点击
    $("promptList").addEventListener("click", (e) => {
        const btn = e.target.closest("button");
        if (!btn) return;
        const action = btn.dataset.action;
        const idx = parseInt(btn.dataset.index);
        if (isNaN(idx)) return;

        if (action === "delete") {
            e.preventDefault();
            deletePrompt(idx);
        } else if (action === "move-up") {
            e.preventDefault();
            movePrompt(idx, -1);
        } else if (action === "move-down") {
            e.preventDefault();
            movePrompt(idx, 1);
        }
    });

    // 初始加载
    await loadStatus();
    await loadPrompts();
}

// ---- 加载插件状态 ----
async function loadStatus() {
    try {
        const data = await apiGet("status");
        pluginStatus = data;
        $("statusPromptCount").textContent = data.prompt_count + " 条";
        $("statusEnabled").textContent = data.enabled ? "✅ 已启用" : "⛔ 已禁用";
        $("statusEnabled").style.color = data.enabled ? "var(--success)" : "var(--danger)";
        $("statusInterval").textContent = data.interval;
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

// ---- 加载提示词列表 ----
async function loadPrompts() {
    try {
        const data = await apiGet("prompts");
        prompts = data.prompts || [];
        renderPromptList();
        $("promptCountBadge").textContent = prompts.length;
        // 更新状态中的提示词数量
        $("statusPromptCount").textContent = prompts.length + " 条";
    } catch (e) {
        console.error("加载提示词失败:", e);
        $("promptList").innerHTML = '<p class="loading" style="color:var(--danger)">加载失败，请检查插件状态</p>';
    }
}

// ---- 渲染提示词列表 ----
function renderPromptList() {
    const container = $("promptList");
    if (prompts.length === 0) {
        container.innerHTML = '<p class="loading">暂无提示词，请在上方添加。建议添加 5-10 条多样化提示词。</p>';
        return;
    }

    container.innerHTML = prompts
        .map(
            (p, i) => `
        <div class="prompt-item" data-index="${i}">
            <div class="prompt-item-header">
                <span class="prompt-item-index">#${i + 1}</span>
                <span class="prompt-item-name">
                    <input
                        type="text"
                        class="prompt-name-input"
                        value="${escapeHtml(p.name || '')}"
                        placeholder="提示词名称"
                        data-index="${i}"
                        data-field="name"
                    />
                </span>
                <span class="prompt-item-actions">
                    <button class="btn btn-sm btn-move-up" data-index="${i}" data-action="move-up" title="上移">⬆</button>
                    <button class="btn btn-sm btn-move-down" data-index="${i}" data-action="move-down" title="下移">⬇</button>
                    <button class="btn btn-sm btn-danger btn-delete" data-index="${i}" data-action="delete" title="删除">🗑</button>
                </span>
            </div>
            <div class="prompt-item-content">
                <textarea
                    class="prompt-content-textarea"
                    placeholder="提示词内容..."
                    data-index="${i}"
                    data-field="content"
                    rows="3"
                >${escapeHtml(p.content || '')}</textarea>
            </div>
        </div>
    `
        )
        .join("");

    // 绑定输入事件
    container.querySelectorAll(".prompt-name-input, .prompt-content-textarea").forEach((input) => {
        input.addEventListener("input", () => {
            const idx = parseInt(input.dataset.index);
            const field = input.dataset.field;
            if (!isNaN(idx) && prompts[idx]) {
                prompts[idx][field] = input.value;
            }
        });
    });
}

// ---- 添加新提示词 ----
async function addPrompt() {
    const nameInput = $("newPromptName");
    const contentInput = $("newPromptContent");
    const name = nameInput.value.trim();
    const content = contentInput.value.trim();

    if (!name) {
        showMsg("addPromptMsg", "❌ 请输入提示词名称", "error");
        return;
    }
    if (!content) {
        showMsg("addPromptMsg", "❌ 请输入提示词内容", "error");
        return;
    }

    // 通过 API 添加到后端
    try {
        const data = await bridge.apiPost("prompts/add", { name, content });
        const result = (typeof data === 'string') ? JSON.parse(data) : data;
        prompts = result.prompts || [];
        renderPromptList();
        $("promptCountBadge").textContent = prompts.length;
        $("statusPromptCount").textContent = prompts.length + " 条";

        // 清空输入
        nameInput.value = "";
        contentInput.value = "";
        showMsg("addPromptMsg", `✅ 已添加「${name}」，共 ${prompts.length} 条提示词`, "success");
    } catch (e) {
        console.error("添加失败详情:", e);
        showMsg("addPromptMsg", "❌ 添加失败: " + (e.message || e), "error");
    }
}

// ---- 删除提示词 ----
async function deletePrompt(index) {
    const idx = parseInt(index);
    if (isNaN(idx) || idx < 0 || idx >= prompts.length) {
        showMsg("saveAllMsg", "❌ 无效的索引: " + index, "error");
        return;
    }
    const p = prompts[idx];
    if (!p) return;
    // 注意：AstrBot Plugin Page 运行在沙箱 iframe 中，confirm() 被禁用
    // 直接执行删除，后端 save 会持久化

    showMsg("saveAllMsg", "⏳ 正在删除并保存...", "");

    // 从本地数组删除
    prompts.splice(idx, 1);
    renderPromptList();
    $("promptCountBadge").textContent = prompts.length;
    $("statusPromptCount").textContent = prompts.length + " 条";

    // 持久化：把整个数组发给后端保存
    try {
        const data = await bridge.apiPost("prompts/save", { prompts: [...prompts] });
        if (data && data.prompts) {
            prompts = data.prompts;
            renderPromptList();
            $("promptCountBadge").textContent = prompts.length;
            $("statusPromptCount").textContent = prompts.length + " 条";
            showMsg("saveAllMsg", `✅ 已删除，共 ${prompts.length} 条提示词`, "success");
        } else {
            showMsg("saveAllMsg", `✅ 已删除（共 ${prompts.length} 条），但后端返回异常: ${JSON.stringify(data)}`, "error");
        }
    } catch (e) {
        showMsg("saveAllMsg", "❌ 保存失败: " + (e.message || String(e)), "error");
        // 保存失败但本地已删，重新加载
        try { await loadPrompts(); } catch (_) {}
    }
}

// ---- 移动提示词 ----
async function movePrompt(index, direction) {
    const idx = parseInt(index);
    const dir = parseInt(direction);
    const newIndex = idx + dir;
    if (newIndex < 0 || newIndex >= prompts.length) return;

    // 本地移动
    const item = prompts.splice(idx, 1)[0];
    prompts.splice(newIndex, 0, item);
    renderPromptList();

    // 持久化
    try {
        const data = await bridge.apiPost("prompts/save", { prompts: [...prompts] });
        if (data && data.prompts) {
            prompts = data.prompts;
            renderPromptList();
        }
    } catch (e) {
        console.error("移动保存失败:", e);
        try { await loadPrompts(); } catch (_) {}
    }
}

// ---- 保存所有提示词 ----
async function saveAllPrompts() {
    showMsg("saveAllMsg", "⏳ 正在保存...", "");
    try {
        const data = await bridge.apiPost("prompts/save", { prompts: [...prompts] });
        if (data && data.prompts) {
            prompts = data.prompts;
            renderPromptList();
            $("promptCountBadge").textContent = prompts.length;
            $("statusPromptCount").textContent = prompts.length + " 条";
            showMsg("saveAllMsg", `✅ 已保存 ${prompts.length} 条提示词`, "success");
        } else {
            showMsg("saveAllMsg", `⚠️ 保存后返回异常: ${JSON.stringify(data)}`, "error");
        }
    } catch (e) {
        showMsg("saveAllMsg", "❌ 保存失败: " + (e.message || String(e)), "error");
    }
}

// ---- 启动 ----
init();
