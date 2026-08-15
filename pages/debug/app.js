const bridge = window.AstrBotPluginPage;

const elements = {
  form: document.getElementById("parse-form"),
  text: document.getElementById("share-text"),
  charCount: document.getElementById("char-count"),
  runButton: document.getElementById("run-button"),
  cancelButton: document.getElementById("cancel-button"),
  clearButton: document.getElementById("clear-button"),
  refreshStatus: document.getElementById("refresh-status"),
  modeStatus: document.getElementById("mode-status"),
  modeStatusText: document.getElementById("mode-status-text"),
  modeNotice: document.getElementById("mode-notice"),
  platformList: document.getElementById("platform-list"),
  runSubtitle: document.getElementById("run-subtitle"),
  runMetrics: document.getElementById("run-metrics"),
  metricMessage: document.getElementById("metric-message"),
  metricTime: document.getElementById("metric-time"),
  emptyState: document.getElementById("empty-state"),
  chatBody: document.getElementById("qq-chat-body"),
  timeline: document.getElementById("message-timeline"),
  details: document.getElementById("parse-details"),
  toast: document.getElementById("toast"),
};

let debugEnabled = false;
let busy = false;
let activeSessionId = null;
let activeSubscriptionId = null;
let messageCount = 0;
let latestElapsedMs = 0;
let toastTimer = null;

function setText(element, value) {
  element.textContent = String(value ?? "");
  return element;
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "大小未知";
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}

function showToast(message) {
  clearTimeout(toastTimer);
  setText(elements.toast, message);
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 5200);
}

function updateButtons() {
  const hasText = elements.text.value.trim().length > 0;
  elements.text.disabled = !debugEnabled || busy;
  elements.runButton.disabled = !debugEnabled || busy || !hasText;
  elements.cancelButton.disabled = !busy;
  elements.refreshStatus.disabled = busy;
}

function setBusy(nextBusy) {
  busy = Boolean(nextBusy);
  updateButtons();
}

function updateMetrics() {
  elements.runMetrics.hidden = messageCount === 0 && latestElapsedMs === 0;
  setText(elements.metricMessage, `${messageCount} 条消息`);
  setText(elements.metricTime, `${latestElapsedMs} ms`);
}

function resetOutput() {
  messageCount = 0;
  latestElapsedMs = 0;
  elements.emptyState.hidden = false;
  elements.timeline.replaceChildren();
  elements.details.replaceChildren();
  elements.chatBody.scrollTop = 0;
  setText(elements.runSubtitle, "等待一次解析任务");
  updateMetrics();
}

function appendEvent(message, kind = "normal") {
  elements.emptyState.hidden = true;
  const row = document.createElement("div");
  row.className = "event-row";
  if (kind === "error") row.classList.add("is-error");
  if (kind === "success") row.classList.add("is-success");
  setText(row, message);
  elements.timeline.append(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function appendTimeDivider(date = new Date()) {
  const row = document.createElement("div");
  row.className = "qq-time-divider";
  setText(
    row,
    date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
  );
  elements.timeline.append(row);
}

function platformInitial(platform) {
  const value = String(platform || "PX").trim();
  return value.slice(0, 2).toUpperCase() || "PX";
}

function createDownloadButton(media, label = "下载") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "download-button";
  setText(button, label);
  button.disabled = !media?.token;
  button.addEventListener("click", async () => {
    if (!media?.token) return;
    try {
      await bridge.download(
        `debug/media/${media.token}`,
        {},
        media.name || "parser-x-media",
      );
    } catch (error) {
      showToast(error?.message || "媒体下载失败");
    }
  });
  return button;
}

function createMediaElement(type, dataUrl, label) {
  if (type === "image") {
    const image = document.createElement("img");
    image.alt = label || "解析图片";
    image.loading = "lazy";
    image.src = dataUrl;
    return image;
  }
  if (type === "video") {
    const video = document.createElement("video");
    video.controls = true;
    video.preload = "metadata";
    video.src = dataUrl;
    return video;
  }
  if (type === "audio") {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = dataUrl;
    return audio;
  }
  return null;
}

async function loadMediaPreview(card, type, media, placeholder) {
  let dataUrl = media?.data_url || media?.url || "";
  if (!dataUrl && media?.token && media?.previewable) {
    try {
      const payload = await bridge.apiGet(
        `debug/media/${media.token}/preview`,
      );
      dataUrl = payload?.data_url || "";
    } catch (error) {
      setText(placeholder, error?.message || "预览加载失败，可下载查看");
      return;
    }
  }
  if (!dataUrl) {
    setText(placeholder, media?.missing ? "媒体文件不存在" : "该媒体仅提供下载");
    return;
  }

  const mediaElement = createMediaElement(type, dataUrl, media?.name);
  if (!mediaElement) return;
  placeholder.replaceWith(mediaElement);
  card.classList.add("has-preview");
}

function createMediaCard(type, media = {}) {
  const card = document.createElement("div");
  card.className = `media-card is-${type}`;

  const placeholder = document.createElement("div");
  placeholder.className = "media-placeholder";
  const label = type === "image" ? "图片预览加载中" : `${type} 媒体`;
  setText(placeholder, label);
  card.append(placeholder);

  const footer = document.createElement("div");
  footer.className = "media-footer";
  const copy = document.createElement("span");
  setText(copy, `${media.name || type} · ${formatBytes(media.size)}`);
  footer.append(copy, createDownloadButton(media));
  card.append(footer);

  void loadMediaPreview(card, type, media, placeholder);
  return card;
}

function createFileCard(media = {}) {
  const card = document.createElement("div");
  card.className = "file-card";

  const icon = document.createElement("span");
  icon.className = "file-icon";
  setText(icon, "FILE");

  const body = document.createElement("span");
  body.className = "file-body";
  const copy = document.createElement("strong");
  copy.className = "file-copy";
  setText(copy, media.name || "文件");
  const meta = document.createElement("span");
  meta.className = "file-meta";
  setText(meta, formatBytes(media.size));
  body.append(copy, meta);

  card.append(icon, body, createDownloadButton(media, "接收"));
  return card;
}

function componentPreview(component) {
  const type = component?.type || "unknown";
  if (type === "text") {
    const text = String(component.text || "").replace(/\s+/g, " ").trim();
    return text || "[文字]";
  }
  if (type === "reply") return "[回复消息]";
  if (type === "image") return "[图片]";
  if (type === "video") return "[视频]";
  if (type === "audio") return "[语音]";
  if (type === "file") return `[文件] ${component.media?.name || ""}`.trim();
  if (type === "forward") return "[聊天记录]";
  return `[${component.label || type}]`;
}

function renderComponent(component) {
  const type = component?.type || "unknown";
  if (type === "text") {
    const bubble = document.createElement("div");
    bubble.className = "text-bubble";
    return setText(bubble, component.text || "");
  }
  if (type === "reply") {
    const reply = document.createElement("div");
    reply.className = "reply-strip";
    return setText(reply, `回复了一条消息 · ${component.id || "unknown"}`);
  }
  if (type === "image" || type === "video" || type === "audio") {
    return createMediaCard(type, component.media || {});
  }
  if (type === "file") {
    return createFileCard(component.media || {});
  }
  if (type === "forward") {
    return renderForward(component);
  }

  const unknown = document.createElement("div");
  unknown.className = "unknown-card";
  unknown.style.padding = "10px 12px";
  return setText(unknown, component.label || type);
}

function renderForward(component) {
  const details = document.createElement("details");
  details.className = "forward-card";

  const nodes = Array.isArray(component.nodes) ? component.nodes : [];
  const summary = document.createElement("summary");
  const summaryCopy = document.createElement("span");
  summaryCopy.className = "forward-summary-copy";
  const title = document.createElement("strong");
  setText(title, "聊天记录");
  const previewList = document.createElement("span");
  previewList.className = "forward-preview-list";
  if (nodes.length) {
    nodes.slice(0, 2).forEach((node) => {
      const line = document.createElement("span");
      line.className = "forward-preview-line";
      const preview = (node.content || []).map(componentPreview).find(Boolean) || "[空消息]";
      setText(line, `${node.name || "Parser X"}: ${preview}`);
      previewList.append(line);
    });
  } else {
    const line = document.createElement("span");
    line.className = "forward-preview-line";
    setText(line, "暂无消息");
    previewList.append(line);
  }
  const count = document.createElement("span");
  count.className = "forward-preview-count";
  setText(count, `查看 ${nodes.length} 条转发消息`);
  summaryCopy.append(title, previewList, count);
  const arrow = document.createElement("span");
  arrow.className = "forward-arrow";
  setText(arrow, "›");
  summary.append(summaryCopy, arrow);
  details.append(summary);

  const nodeList = document.createElement("div");
  nodeList.className = "forward-nodes";
  nodes.forEach((node) => {
    const row = document.createElement("article");
    row.className = "forward-node";
    const avatar = document.createElement("div");
    avatar.className = "node-avatar";
    setText(avatar, platformInitial(node.name));

    const copy = document.createElement("div");
    copy.className = "node-copy";
    const name = document.createElement("div");
    name.className = "node-name";
    setText(name, node.name || "Parser X 调试台");
    const components = document.createElement("div");
    components.className = "node-components";
    (node.content || []).forEach((item) => {
      components.append(renderComponent(item));
    });
    copy.append(name, components);
    row.append(avatar, copy);
    nodeList.append(row);
  });
  details.append(nodeList);
  return details;
}

function renderMessage(payload, platform) {
  elements.emptyState.hidden = true;
  messageCount += 1;
  latestElapsedMs = Math.max(
    latestElapsedMs,
    Number(payload?.elapsed_ms || 0),
  );
  updateMetrics();

  const entry = document.createElement("article");
  entry.className = "message-entry is-incoming";
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  setText(avatar, "PX");

  const main = document.createElement("div");
  main.className = "message-main";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const name = document.createElement("strong");
  setText(name, "Parser X");
  const platformBadge = document.createElement("span");
  platformBadge.className = "message-platform";
  setText(platformBadge, platform || "解析结果");
  const sequence = document.createElement("span");
  setText(sequence, `#${payload?.index || messageCount}`);
  const elapsed = document.createElement("span");
  setText(elapsed, `+${payload?.elapsed_ms || 0} ms`);
  meta.append(name, platformBadge, sequence, elapsed);

  const stack = document.createElement("div");
  stack.className = "component-stack";
  (payload?.components || []).forEach((component) => {
    stack.append(renderComponent(component));
  });
  main.append(meta, stack);
  entry.append(avatar, main);
  elements.timeline.append(entry);
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderUserMessage(text) {
  elements.emptyState.hidden = true;
  const entry = document.createElement("article");
  entry.className = "message-entry is-self";

  const avatar = document.createElement("div");
  avatar.className = "message-avatar user-avatar";
  setText(avatar, "我");

  const main = document.createElement("div");
  main.className = "message-main";

  const stack = document.createElement("div");
  stack.className = "component-stack";
  const bubble = document.createElement("div");
  bubble.className = "text-bubble";
  setText(bubble, text);
  stack.append(bubble);
  main.append(stack);
  entry.append(main, avatar);
  elements.timeline.append(entry);
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderParseDetail(event) {
  const details = document.createElement("details");
  details.className = "detail-card";
  const summary = document.createElement("summary");
  setText(summary, `${event.platform || "平台"} · 解析数据 · ${event.parse_ms || 0} ms`);
  const pre = document.createElement("pre");
  setText(pre, JSON.stringify(event.result || {}, null, 2));
  details.append(summary, pre);
  elements.details.append(details);
}

function finishRun(subtitle) {
  setBusy(false);
  activeSessionId = null;
  setText(elements.runSubtitle, subtitle);
}

function handleDebugEvent(event) {
  if (!event || typeof event !== "object") return;
  switch (event.event) {
    case "started":
      appendEvent(`开始处理 ${event.match_count || 0} 个匹配链接`);
      break;
    case "match":
      appendEvent(`${event.platform || "平台"}：已命中 ${event.url || "链接"}`);
      break;
    case "parsed":
      appendEvent(`${event.platform || "平台"}：解析完成（${event.parse_ms || 0} ms）`);
      renderParseDetail(event);
      break;
    case "message":
      renderMessage(event.message || {}, event.platform);
      break;
    case "delivered":
      latestElapsedMs = Math.max(latestElapsedMs, Number(event.elapsed_ms || 0));
      updateMetrics();
      appendEvent(`${event.platform || "平台"}：模拟投递完成`, "success");
      break;
    case "skipped":
      appendEvent(`${event.platform || "平台"}：${event.message || "已跳过"}`);
      break;
    case "error":
      appendEvent(
        `${event.platform ? `${event.platform}：` : ""}${event.message || "发生错误"}`,
        "error",
      );
      break;
    case "cancelled":
      appendEvent(event.message || "调试任务已取消", "error");
      finishRun("任务已取消");
      break;
    case "done":
      latestElapsedMs = Math.max(latestElapsedMs, Number(event.elapsed_ms || 0));
      updateMetrics();
      appendEvent(
        `完成 ${event.completed || 0} / ${event.match_count || 0} 个解析任务`,
        "success",
      );
      finishRun(`解析完成 · ${event.elapsed_ms || 0} ms`);
      break;
    case "session_end":
      activeSubscriptionId = null;
      if (busy) finishRun("调试会话已结束");
      break;
    default:
      break;
  }
}

async function refreshStatus() {
  elements.modeStatus.className = "mode-status is-loading";
  setText(elements.modeStatusText, "正在读取插件状态");
  try {
    const status = await bridge.apiGet("debug/status");
    debugEnabled = Boolean(status?.enabled);
    elements.modeStatus.className = debugEnabled
      ? "mode-status is-enabled"
      : "mode-status is-disabled";
    setText(
      elements.modeStatusText,
      debugEnabled ? "独占调试已开启" : "调试模式已关闭",
    );

    elements.modeNotice.className = debugEnabled
      ? "mode-notice is-enabled"
      : "mode-notice is-disabled";
    elements.modeNotice.replaceChildren();
    const title = document.createElement("strong");
    const description = document.createElement("span");
    if (debugEnabled) {
      setText(title, "当前仅接受本页面的解析请求");
      setText(description, "QQ 与其他消息适配器已暂停触发 Parser X。关闭调试开关后自动恢复。 ");
    } else {
      setText(title, "请先在插件配置中开启调试开关");
      setText(description, "开关默认关闭；未开启时本页不会执行任何解析任务。 ");
    }
    elements.modeNotice.append(title, description);

    elements.platformList.replaceChildren();
    const platforms = Array.isArray(status?.platforms) ? status.platforms : [];
    (platforms.length ? platforms : ["暂无平台"]).forEach((platform) => {
      const chip = document.createElement("span");
      chip.className = "platform-chip";
      setText(chip, platform);
      elements.platformList.append(chip);
    });
  } catch (error) {
    debugEnabled = false;
    elements.modeStatus.className = "mode-status is-disabled";
    setText(elements.modeStatusText, "调试接口不可用");
    showToast(error?.message || "读取调试状态失败");
  } finally {
    updateButtons();
  }
}

async function cancelRun() {
  const sessionId = activeSessionId;
  if (!sessionId) return;
  try {
    await bridge.apiPost("debug/cancel", { session_id: sessionId });
  } catch (error) {
    showToast(error?.message || "取消任务失败");
  }
  if (activeSubscriptionId) {
    try {
      await bridge.unsubscribeSSE(activeSubscriptionId);
    } catch {
      // The subscription may already have closed with the task.
    }
  }
  activeSubscriptionId = null;
  activeSessionId = null;
  finishRun("任务已取消");
}

async function startRun() {
  const text = elements.text.value.trim();
  if (!text || busy) return;
  await refreshStatus();
  if (!debugEnabled) {
    showToast("请先在 Parser X 插件配置中开启调试模式");
    return;
  }

  resetOutput();
  elements.emptyState.hidden = true;
  appendTimeDivider();
  renderUserMessage(text);
  setBusy(true);
  setText(elements.runSubtitle, "正在启动真实解析流程");

  try {
    const start = await bridge.apiPost("debug/start", { text });
    activeSessionId = start?.session_id || null;
    if (!activeSessionId) throw new Error("服务端未返回调试会话 ID");
    setText(elements.runSubtitle, `解析中 · ${start.match_count || 0} 个匹配链接`);

    activeSubscriptionId = await bridge.subscribeSSE(
      "debug/events",
      {
        onOpen() {
          appendEvent("实时消息通道已连接");
        },
        onMessage(event) {
          handleDebugEvent(event.parsed);
        },
        onError() {
          appendEvent("实时消息通道异常中断", "error");
          finishRun("连接已中断");
        },
      },
      { session_id: activeSessionId },
    );
  } catch (error) {
    appendEvent(error?.message || "启动调试任务失败", "error");
    finishRun("启动失败");
    showToast(error?.message || "启动调试任务失败");
  }
}

elements.text.addEventListener("input", () => {
  setText(elements.charCount, `${elements.text.value.length} / 20000`);
  updateButtons();
});

elements.text.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  void startRun();
});

elements.cancelButton.addEventListener("click", () => {
  void cancelRun();
});

elements.clearButton.addEventListener("click", () => {
  if (busy) void cancelRun();
  resetOutput();
});

elements.refreshStatus.addEventListener("click", () => {
  void refreshStatus();
});

window.addEventListener("focus", () => {
  if (!busy) void refreshStatus();
});

window.addEventListener("beforeunload", () => {
  if (activeSubscriptionId) {
    void bridge.unsubscribeSSE(activeSubscriptionId);
  }
});

await bridge.ready();
await refreshStatus();
resetOutput();
