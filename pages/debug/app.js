const bridge = window.AstrBotPluginPage;

const elements = {
  form: document.getElementById("parse-form"),
  text: document.getElementById("share-text"),
  charCount: document.getElementById("char-count"),
  runButton: document.getElementById("run-button"),
  cancelButton: document.getElementById("cancel-button"),
  clearButton: document.getElementById("clear-button"),
  modeStatus: document.getElementById("mode-status"),
  modeStatusText: document.getElementById("mode-status-text"),
  runSubtitle: document.getElementById("run-subtitle"),
  emptyState: document.getElementById("empty-state"),
  chatBody: document.getElementById("qq-chat-body"),
  timeline: document.getElementById("message-timeline"),
  toast: document.getElementById("toast"),
};

let exclusiveMode = false;
let busy = false;
let activeSessionId = null;
let activeSubscriptionId = null;
let messageCount = 0;
let latestElapsedMs = 0;
let issueCount = 0;
let progressRow = null;
let cancellationDisplayed = false;
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

function formatTime(date = new Date()) {
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function showToast(message, title = "操作失败") {
  clearTimeout(toastTimer);
  setText(elements.toast, `${title}：${message}`);
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 4200);
}

function scrollToLatest() {
  requestAnimationFrame(() => {
    elements.chatBody.scrollTo({
      top: elements.chatBody.scrollHeight,
      behavior: "smooth",
    });
  });
}

function updateControls() {
  const hasText = elements.text.value.trim().length > 0;
  elements.text.disabled = busy;
  elements.runButton.disabled = busy || !hasText;
  elements.cancelButton.disabled = !busy || !activeSessionId;
  elements.cancelButton.hidden = !busy;
  elements.clearButton.disabled = busy;
}

function setBusy(nextBusy) {
  busy = Boolean(nextBusy);
  updateControls();
}

function resetConversation() {
  messageCount = 0;
  latestElapsedMs = 0;
  issueCount = 0;
  progressRow = null;
  cancellationDisplayed = false;
  elements.timeline.replaceChildren();
  elements.emptyState.hidden = false;
  elements.chatBody.scrollTop = 0;
  setText(
    elements.runSubtitle,
    exclusiveMode ? "在线 · 独占调试模式" : "在线 · 普通消息仍可解析",
  );
}

function appendTimeDivider(date = new Date()) {
  const row = document.createElement("div");
  row.className = "time-divider";
  setText(row, formatTime(date));
  elements.timeline.append(row);
}

function setProgress(message, kind = "loading") {
  elements.emptyState.hidden = true;
  if (!progressRow || !progressRow.isConnected) {
    progressRow = document.createElement("div");
    elements.timeline.append(progressRow);
  }
  progressRow.className = `system-message is-${kind}`;
  setText(progressRow, message);
  scrollToLatest();
}

function cleanIssueText(value, fallback) {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function normalizeIssue(source, defaults = {}) {
  const issue = source && typeof source === "object" ? source : {};
  const level = issue.level || defaults.level || "error";
  return {
    level,
    code: cleanIssueText(issue.code, defaults.code || "debug_error"),
    title: cleanIssueText(issue.title, defaults.title || "操作失败"),
    stage: cleanIssueText(issue.stage, defaults.stage || "调试"),
    platform: cleanIssueText(issue.platform, defaults.platform || ""),
    message: cleanIssueText(
      issue.message,
      defaults.message || "未提供具体原因。",
    ),
    action: cleanIssueText(
      issue.action,
      defaults.action || "请稍后重试；如果问题持续，请查看 AstrBot 日志。",
    ),
  };
}

function issueFromError(error, defaults = {}) {
  const data = error?.data;
  const parsed = data && typeof data === "object" ? data : {};
  const fallbackMessage = error?.message || defaults.message;
  const fallbackTitle = defaults.title || "操作失败";
  const fallbackAction = defaults.action || "请稍后重试；如果问题持续，请查看 AstrBot 日志。";
  if (
    !Object.keys(parsed).length &&
    typeof fallbackMessage === "string" &&
    fallbackMessage.includes("建议：")
  ) {
    const [reasonPart, actionPart] = fallbackMessage.split("建议：", 2);
    const [titlePart, messagePart] = reasonPart.split("：", 2);
    return normalizeIssue(
      {
        ...defaults,
        title: titlePart || fallbackTitle,
        message: messagePart || fallbackMessage,
        action: actionPart || fallbackAction,
      },
      defaults,
    );
  }
  return normalizeIssue(parsed, {
    ...defaults,
    message: fallbackMessage,
  });
}

function appendIssue(source, defaults = {}) {
  const issue = normalizeIssue(source, defaults);
  const card = document.createElement("article");
  card.className = `issue-card is-${issue.level}`;

  const header = document.createElement("div");
  header.className = "issue-header";
  const title = document.createElement("strong");
  setText(title, issue.title);
  const context = document.createElement("span");
  setText(
    context,
    [issue.platform, issue.stage].filter(Boolean).join(" · ") || "调试",
  );
  header.append(title, context);

  const reason = document.createElement("p");
  reason.className = "issue-reason";
  setText(reason, `原因：${issue.message}`);
  const action = document.createElement("p");
  action.className = "issue-action";
  setText(action, `建议：${issue.action}`);
  card.append(header, reason, action);

  if (progressRow?.isConnected) {
    elements.timeline.insertBefore(card, progressRow);
  } else {
    elements.timeline.append(card);
  }
  elements.emptyState.hidden = true;
  scrollToLatest();
  return issue;
}

function showIssueToast(source, defaults = {}) {
  const issue = normalizeIssue(source, defaults);
  showToast(`${issue.message} 建议：${issue.action}`, issue.title);
  return issue;
}

function showCancellationIssue(source = {}) {
  if (cancellationDisplayed) return;
  cancellationDisplayed = true;
  appendIssue(source, {
    level: "info",
    code: "session_cancelled",
    title: "解析已取消",
    stage: "任务",
    message: "调试任务已停止，不会继续生成消息。",
    action: "可以修改输入内容后重新发送。",
  });
}

function platformInitial(platform) {
  const value = String(platform || "PX").trim();
  return value.slice(0, 2).toUpperCase() || "PX";
}

function createDownloadButton(media, label = "保存") {
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
      showIssueToast(issueFromError(error), {
        code: "media_download_failed",
        title: "无法保存媒体",
        stage: "媒体",
        message: "媒体文件保存失败。",
        action: "请确认调试会话仍然有效，然后重新解析该链接。",
      });
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
      const payload = await bridge.apiGet(`debug/media/${media.token}/preview`);
      dataUrl = payload?.data_url || "";
    } catch (error) {
      const issue = issueFromError(error, {
        code: "media_preview_failed",
        title: "无法预览媒体",
        stage: "媒体",
        message: "媒体预览加载失败。",
        action: "可以尝试使用“保存”按钮，或重新解析该链接。",
      });
      placeholder.classList.add("is-error");
      setText(placeholder, `${issue.title}：${issue.message} 建议：${issue.action}`);
      return;
    }
  }
  if (!dataUrl) {
    setText(placeholder, media?.missing ? "媒体文件不存在" : "该媒体仅提供保存");
    return;
  }

  const mediaElement = createMediaElement(type, dataUrl, media?.name);
  if (!mediaElement) return;
  placeholder.replaceWith(mediaElement);
  card.classList.add("has-preview");
  mediaElement.addEventListener("load", scrollToLatest, { once: true });
}

function mediaTypeLabel(type) {
  return { image: "图片", video: "视频", audio: "音频" }[type] || "媒体";
}

function createMediaCard(type, media = {}) {
  const card = document.createElement("figure");
  card.className = `media-card is-${type}`;

  const preview = document.createElement("div");
  preview.className = "media-preview";
  const placeholder = document.createElement("div");
  placeholder.className = "media-placeholder";
  setText(placeholder, `${mediaTypeLabel(type)}加载中…`);
  preview.append(placeholder);

  const footer = document.createElement("figcaption");
  footer.className = "media-footer";
  const copy = document.createElement("span");
  copy.className = "media-copy";
  const kind = document.createElement("strong");
  setText(kind, mediaTypeLabel(type));
  const name = document.createElement("span");
  name.className = "media-name";
  setText(name, media.name || `未命名${mediaTypeLabel(type)}`);
  const size = document.createElement("span");
  size.className = "media-size";
  setText(size, formatBytes(media.size));
  copy.append(kind, name, size);
  footer.append(copy, createDownloadButton(media));
  card.append(preview, footer);

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
  setText(copy, media.name || "未命名文件");
  const meta = document.createElement("span");
  meta.className = "file-meta";
  setText(meta, `文件 · ${formatBytes(media.size)}`);
  body.append(copy, meta);
  card.append(icon, body, createDownloadButton(media));
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
  if (type === "audio") return "[音频]";
  if (type === "file") return `[文件] ${component.media?.name || ""}`.trim();
  if (type === "forward") return "[聊天记录]";
  return `[${component.label || type}]`;
}

function renderComponent(component, compact = false) {
  const type = component?.type || "unknown";
  if (type === "text") {
    const bubble = document.createElement("div");
    bubble.className = compact ? "node-text" : "message-text";
    return setText(bubble, component.text || "");
  }
  if (type === "reply") {
    const reply = document.createElement("blockquote");
    reply.className = "reply-card";
    const label = document.createElement("strong");
    setText(label, "回复消息");
    const id = document.createElement("span");
    setText(id, component.id || "原消息");
    reply.append(label, id);
    return reply;
  }
  if (type === "image" || type === "video" || type === "audio") {
    return createMediaCard(type, component.media || {});
  }
  if (type === "file") return createFileCard(component.media || {});
  if (type === "forward") return renderForward(component);

  const unknown = document.createElement("div");
  unknown.className = "unknown-card";
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
  const previews = document.createElement("span");
  previews.className = "forward-preview-list";
  const previewNodes = nodes.slice(0, 2);
  (previewNodes.length ? previewNodes : [{ name: "Parser X", content: [] }]).forEach(
    (node) => {
      const line = document.createElement("span");
      const preview = (node.content || []).map(componentPreview).find(Boolean) || "暂无内容";
      setText(line, `${node.name || "Parser X"}：${preview}`);
      previews.append(line);
    },
  );
  const count = document.createElement("small");
  setText(count, `${nodes.length} 条消息`);
  summaryCopy.append(title, previews, count);
  const arrow = document.createElement("span");
  arrow.className = "forward-arrow";
  setText(arrow, "›");
  summary.append(summaryCopy, arrow);

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
    const name = document.createElement("strong");
    name.className = "node-name";
    setText(name, node.name || "Parser X");
    const content = document.createElement("div");
    content.className = "node-components";
    (node.content || []).forEach((item) => content.append(renderComponent(item, true)));
    copy.append(name, content);
    row.append(avatar, copy);
    nodeList.append(row);
  });

  details.append(summary, nodeList);
  return details;
}

function renderMessage(payload, platform) {
  elements.emptyState.hidden = true;
  messageCount += 1;
  latestElapsedMs = Math.max(latestElapsedMs, Number(payload?.elapsed_ms || 0));

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
  platformBadge.className = "platform-badge";
  setText(platformBadge, platform || "解析结果");
  const time = document.createElement("time");
  setText(time, formatTime());
  meta.append(name, platformBadge, time);

  const stack = document.createElement("div");
  stack.className = "component-stack";
  (payload?.components || []).forEach((component) => {
    stack.append(renderComponent(component));
  });
  if (!stack.childElementCount) {
    const empty = document.createElement("div");
    empty.className = "message-text";
    setText(empty, "这条消息没有可预览的内容");
    stack.append(empty);
  }
  main.append(meta, stack);
  entry.append(avatar, main);
  if (progressRow?.isConnected) {
    elements.timeline.insertBefore(entry, progressRow);
  } else {
    elements.timeline.append(entry);
  }
  scrollToLatest();
}

function renderUserMessage(text) {
  elements.emptyState.hidden = true;
  const entry = document.createElement("article");
  entry.className = "message-entry is-self";
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  setText(bubble, text);
  const avatar = document.createElement("div");
  avatar.className = "message-avatar user-avatar";
  setText(avatar, "我");
  entry.append(bubble, avatar);
  elements.timeline.append(entry);
  scrollToLatest();
}

function finishRun(subtitle) {
  setBusy(false);
  activeSessionId = null;
  setText(elements.runSubtitle, subtitle);
  elements.text.focus();
}

function handleDebugEvent(event) {
  if (!event || typeof event !== "object") return;
  switch (event.event) {
    case "started":
      setProgress(`正在处理 ${event.match_count || 0} 个链接…`);
      break;
    case "match":
      setProgress(`正在解析 ${event.platform || "分享链接"}…`);
      break;
    case "parsed":
      setProgress(`${event.platform || "内容"}已解析，正在生成消息…`);
      break;
    case "message":
      renderMessage(event.message || {}, event.platform);
      break;
    case "delivered":
      latestElapsedMs = Math.max(latestElapsedMs, Number(event.elapsed_ms || 0));
      break;
    case "skipped":
      issueCount += 1;
      appendIssue(event, {
        level: "warning",
        code: "parser_skipped",
        title: "已跳过该链接",
        stage: "解析",
        message: "该分享没有继续处理。",
        action: "请检查链接内容和平台配置后重试。",
      });
      break;
    case "error":
      issueCount += 1;
      appendIssue(event, {
        code: "debug_error",
        title: "解析失败",
        stage: "解析",
        message: "调试任务未能完成。",
        action: "请检查插件配置和 AstrBot 日志后重试。",
      });
      break;
    case "cancelled":
      if (progressRow?.isConnected) progressRow.remove();
      progressRow = null;
      showCancellationIssue(event);
      finishRun("在线 · 上次任务已取消");
      break;
    case "done": {
      latestElapsedMs = Math.max(latestElapsedMs, Number(event.elapsed_ms || 0));
      const seconds = (latestElapsedMs / 1000).toFixed(latestElapsedMs >= 1000 ? 1 : 2);
      const totalIssues = Math.max(issueCount, Number(event.issue_count || 0));
      const completed = Number(event.completed || 0);
      if (completed === 0 && totalIssues > 0) {
        setProgress(`解析失败 · ${totalIssues} 个问题 · ${seconds} 秒`, "error");
        finishRun(`在线 · 上次解析失败`);
      } else if (totalIssues > 0) {
        setProgress(
          `解析完成但有问题 · ${messageCount} 条消息 · ${totalIssues} 个问题 · ${seconds} 秒`,
          "warning",
        );
        finishRun(`在线 · 上次解析有 ${totalIssues} 个问题`);
      } else {
        setProgress(`解析完成 · ${messageCount} 条消息 · ${seconds} 秒`, "success");
        finishRun(`在线 · 上次解析 ${seconds} 秒`);
      }
      break;
    }
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
  setText(elements.modeStatusText, "读取状态");
  try {
    const status = await bridge.apiGet("debug/status");
    exclusiveMode = Boolean(status?.exclusive);
    elements.modeStatus.className = exclusiveMode
      ? "mode-status is-enabled"
      : "mode-status is-disabled";
    setText(elements.modeStatusText, exclusiveMode ? "独占调试" : "普通模式");
    setText(
      elements.runSubtitle,
      exclusiveMode ? "在线 · 仅接收本页面请求" : "在线 · 调试台和普通消息均可用",
    );
    return true;
  } catch (error) {
    exclusiveMode = false;
    elements.modeStatus.className = "mode-status is-disabled";
    setText(elements.modeStatusText, "状态读取失败");
    const issue = issueFromError(error, {
      code: "status_unavailable",
      title: "无法读取调试状态",
      stage: "连接",
      message: "页面无法连接 Parser X 调试接口。",
      action: "请确认插件已启用并完成加载，然后刷新本页。",
    });
    setText(elements.runSubtitle, `${issue.title} · ${issue.message}`);
    showIssueToast(issue);
    return false;
  } finally {
    updateControls();
  }
}

async function cancelRun() {
  const sessionId = activeSessionId;
  if (!sessionId) return;
  try {
    await bridge.apiPost("debug/cancel", { session_id: sessionId });
  } catch (error) {
    showIssueToast(issueFromError(error), {
      code: "cancel_failed",
      title: "无法取消任务",
      stage: "会话",
      message: "取消请求未能完成。",
      action: "任务可能已经结束；可以等待当前会话关闭后重新测试。",
    });
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
  if (progressRow?.isConnected) progressRow.remove();
  progressRow = null;
  showCancellationIssue();
  finishRun("在线 · 上次任务已取消");
}

async function startRun() {
  const text = elements.text.value.trim();
  if (!text || busy) return;
  setBusy(true);
  const statusAvailable = await refreshStatus();
  if (!statusAvailable) {
    setBusy(false);
    return;
  }
  if (!elements.timeline.childElementCount) appendTimeDivider();
  progressRow = null;
  issueCount = 0;
  cancellationDisplayed = false;
  renderUserMessage(text);
  elements.text.value = "";
  setText(elements.charCount, "0 / 20000");
  setText(elements.runSubtitle, "正在启动解析");

  try {
    const start = await bridge.apiPost("debug/start", { text });
    activeSessionId = start?.session_id || null;
    if (!activeSessionId) throw new Error("服务端未返回调试会话 ID");
    updateControls();
    setProgress(`正在处理 ${start.match_count || 0} 个链接…`);
    setText(elements.runSubtitle, `解析中 · ${start.match_count || 0} 个链接`);

    activeSubscriptionId = await bridge.subscribeSSE(
      "debug/events",
      {
        onOpen() {},
        onMessage(event) {
          handleDebugEvent(event.parsed);
        },
        onError() {
          issueCount += 1;
          if (progressRow?.isConnected) progressRow.remove();
          progressRow = null;
          appendIssue({
            code: "event_stream_disconnected",
            title: "消息通道已中断",
            stage: "连接",
            message: "页面与调试会话的实时连接意外断开。",
            action: "请确认 AstrBot 仍在运行并刷新页面，然后重新发送。",
          });
          finishRun("在线 · 上次连接中断");
        },
      },
      { session_id: activeSessionId },
    );
  } catch (error) {
    const issue = issueFromError(error, {
      code: "start_failed",
      title: "无法启动解析",
      stage: "启动",
      message: "调试任务未能启动。",
      action: "请检查输入内容和插件配置后重试。",
    });
    if (progressRow?.isConnected) progressRow.remove();
    progressRow = null;
    appendIssue(issue);
    finishRun("在线 · 上次启动失败");
  }
}

elements.text.addEventListener("input", () => {
  setText(elements.charCount, `${elements.text.value.length} / 20000`);
  updateControls();
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
  resetConversation();
});

window.addEventListener("focus", () => {
  if (!busy) void refreshStatus();
});

window.addEventListener("beforeunload", () => {
  if (activeSubscriptionId) void bridge.unsubscribeSSE(activeSubscriptionId);
});

resetConversation();
await bridge.ready();
await refreshStatus();
