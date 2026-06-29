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
    if (type === "success" || type === "error") {
        setTimeout(() => {
            el.textContent = "";
            el.className = "save-msg";
        }, 4000);
    }
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
        return await bridge.apiPost(endpoint, body);
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
        $("statusSticker").textContent = data.sticker_available ? "✅ 已连接" : "⚠️ 未检测到";
        $("statusSticker").style.color = data.sticker_available ? "var(--success)" : "var(--warning)";
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
                    <button class="btn btn-sm" onclick="movePrompt(${i}, -1)" title="上移">⬆</button>
                    <button class="btn btn-sm" onclick="movePrompt(${i}, 1)" title="下移">⬇</button>
                    <button class="btn btn-sm btn-danger" onclick="deletePrompt(${i})" title="删除">🗑</button>
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

    // 绑定输入事件，实时更新内存中的数据
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
        const data = await apiPost("prompts/add", { name, content });
        prompts = data.prompts || [];
        renderPromptList();
        $("promptCountBadge").textContent = prompts.length;
        $("statusPromptCount").textContent = prompts.length + " 条";

        // 清空输入
        nameInput.value = "";
        contentInput.value = "";
        showMsg("addPromptMsg", `✅ 已添加「${escapeHtml(name)}」，共 ${prompts.length} 条提示词`, "success");
    } catch (e) {
        showMsg("addPromptMsg", "❌ 添加失败: " + e.message, "error");
    }
}

// ---- 删除提示词 ----
async function deletePrompt(index) {
    const p = prompts[index];
    if (!p) return;
    if (!confirm(`确定要删除提示词「${p.name}」吗？此操作不可恢复。`)) return;

    try {
        const data = await apiPost("prompts/delete", { index });
        prompts = data.prompts || [];
        renderPromptList();
        $("promptCountBadge").textContent = prompts.length;
        $("statusPromptCount").textContent = prompts.length + " 条";
        showMsg("saveAllMsg", `✅ 已删除，共 ${prompts.length} 条提示词`, "success");
    } catch (e) {
        showMsg("saveAllMsg", "❌ 删除失败: " + e.message, "error");
    }
}

// ---- 移动提示词 ----
async function movePrompt(index, direction) {
    const newIndex = index + direction;
    if (newIndex < 0 || newIndex >= prompts.length) return;

    try {
        const data = await apiPost("prompts/move", { index, direction });
        prompts = data.prompts || [];
        renderPromptList();
    } catch (e) {
        console.error("移动失败:", e);
    }
}

// ---- 保存所有提示词 ----
async function saveAllPrompts() {
    // 由于编辑是实时更新 prompts 数组的，这里直接提交整个列表
    try {
        const data = await apiPost("prompts/save", { prompts });
        prompts = data.prompts || [];
        renderPromptList();
        $("promptCountBadge").textContent = prompts.length;
        $("statusPromptCount").textContent = prompts.length + " 条";
        showMsg("saveAllMsg", `✅ 已保存 ${prompts.length} 条提示词`, "success");
    } catch (e) {
        showMsg("saveAllMsg", "❌ 保存失败: " + e.message, "error");
    }
}

// ---- 全局函数暴露给 HTML onclick ----
window.deletePrompt = deletePrompt;
window.movePrompt = movePrompt;

// ---- 启动 ----
init();
