import { api } from "../../scripts/api.js";

// Both settings dialogs use the installed CLI's model/list menu, never a fixed model list.
export function createCodexModelPicker(value = "", onChange = () => {}) {
    const container = document.createElement("div");
    container.style.cssText = "flex:1;min-width:0;";
    const line = document.createElement("div");
    line.style.cssText = "display:flex;gap:8px;align-items:center;";
    const select = document.createElement("select");
    select.className = "jzl-input";
    select.setAttribute("aria-label", "Codex 模型");
    select.style.cssText = "flex:1;min-width:0;background:var(--comfy-input-bg,#1d1d1d);color:var(--fg-color,#ddd);border:1px solid var(--border-color,#444);border-radius:4px;padding:6px 8px;";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "重新识别";
    button.style.cssText = "flex:0 0 auto;padding:6px 10px;cursor:pointer;";
    const status = document.createElement("div");
    status.style.cssText = "font-size:12px;color:var(--descrip-text,#999);margin-top:5px;line-height:1.5;overflow-wrap:anywhere;";
    status.setAttribute("role", "status");
    let models = [], loading = false, attempted = false;

    function setOptions(selected) {
        select.replaceChildren();
        const option = (id, label) => {
            const item = document.createElement("option");
            item.value = id;
            item.textContent = label;
            select.append(item);
        };
        option("", "使用本机 Codex 默认模型");
        for (const model of models) option(model.id, `${model.name}（${model.id}）`);
        if (selected && !models.some((model) => model.id === selected)) {
            option(selected, `已保存，当前未识别：${selected}`);
        }
        select.value = selected;
    }

    async function load(refresh = false) {
        if (loading || (attempted && !refresh)) return;
        attempted = true;
        loading = true;
        button.disabled = true;
        status.textContent = "正在识别本机 Codex 模型…";
        try {
            const response = await api.fetchApi(`/jzl/codex/models${refresh ? "?refresh=1" : ""}`);
            const data = await response.json();
            if (!response.ok || !data.ok || !Array.isArray(data.models)) {
                throw new Error(data.error || "模型目录读取失败");
            }
            models = data.models.filter((model) => typeof model.id === "string" && typeof model.name === "string");
            const selected = select.value;
            setOptions(selected);
            const checked = new Date(data.checked_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
            status.textContent = `已识别 ${models.length} 个模型 · ${checked}（北京时间）。目录可选不代表每个模型均已完成实际调用验证。`;
            if (selected && !models.some((model) => model.id === selected)) {
                status.textContent += " 当前保存的模型不在目录中，已保留原值。";
            }
        } catch (error) {
            status.textContent = `${error.message || "无法识别模型"}。可保留本机默认或点击重新识别；不会自动换模型。`;
        } finally {
            loading = false;
            button.disabled = select.disabled;
        }
    }

    setOptions(String(value || ""));
    select.addEventListener("change", () => onChange(select.value));
    button.addEventListener("click", () => load(true));
    line.append(select, button);
    container.append(line, status);
    return { container, select, button, load };
}
