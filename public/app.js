const socket = io({ autoConnect: false });
const $ = (id) => document.getElementById(id);

let me = "";
let current = null;
const contacts = {};
let history = {};
const peers = {};
const rtcConfig = { iceServers: [{ urls: "stun:stun.l.google.com:19302" }] };
const MAX_HISTORY = 200;
let typingTimer;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function storageKey() {
  return "mychat-history-" + me;
}

function loadHistory() {
  try {
    history = JSON.parse(localStorage.getItem(storageKey())) || {};
  } catch {
    history = {};
  }
}

function saveHistory() {
  try {
    localStorage.setItem(storageKey(), JSON.stringify(history));
  } catch {
  }
}

async function api(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong");
  return data;
}

async function auth(url) {
  $("authError").textContent = "";
  try {
    const data = await api(url, {
      username: $("username").value.trim(),
      password: $("password").value,
    });
    start(data.username);
  } catch (err) {
    $("authError").textContent = err.message;
  }
}

$("authForm").addEventListener("submit", (e) => {
  e.preventDefault();
  auth("/api/login");
});
$("registerBtn").addEventListener("click", () => auth("/api/register"));
$("logoutBtn").addEventListener("click", async () => {
  await api("/api/logout");
  location.reload();
});

fetch("/api/me")
  .then((r) => r.json())
  .then((d) => {
    if (d.username) start(d.username);
  });

function start(name) {
  me = name;
  loadHistory();
  $("myName").textContent = name;
  $("myAvatar").textContent = name[0].toUpperCase();
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  socket.connect();
}

socket.on("kicked", () => {
  socket.disconnect();
  document.body.textContent = "MyChat was opened in another window. This window is now inactive.";
});

socket.on("contacts", (list) => {
  list.forEach((c) => addContact(c.name, c.online));
  fetchPending();
});
socket.on("contact-added", (c) => addContact(c.name, c.online));

socket.on("presence", ({ name, online }) => {
  if (!contacts[name]) return;
  contacts[name].online = online;
  closePeer(name);
  if (online) maybeConnect(name);
  renderContact(name);
});

$("addForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("addInput").value.trim().toLowerCase();
  if (!name) return;
  socket.emit("add_contact", name, (res) => {
    $("addError").textContent = res.ok ? "" : res.error;
    if (res.ok) $("addInput").value = "";
  });
});

function addContact(name, online) {
  if (!contacts[name]) {
    const li = el("li");
    li.addEventListener("click", () => openChat(name));
    contacts[name] = { online: false, unread: 0, li };
    history[name] = history[name] || [];
    $("contactList").appendChild(li);
  }
  contacts[name].online = online;
  renderContact(name);
  if (online) maybeConnect(name);
}

function statusText(name) {
  if (!contacts[name].online) return "offline";
  return isConnected(name) ? "online · P2P" : "online · connecting...";
}

function renderContact(name) {
  const c = contacts[name];
  c.li.className = name === current ? "active" : "";
  c.li.textContent = "";
  const info = el("div", "info");
  info.append(el("div", "title", name), el("div", "status", statusText(name)));
  c.li.append(el("div", "avatar", name[0].toUpperCase()), info);
  if (c.unread) c.li.appendChild(el("span", "badge", c.unread));
  if (name === current) renderHeader();
}

function openChat(name) {
  const previous = current;
  current = name;
  contacts[name].unread = 0;
  if (previous && contacts[previous]) renderContact(previous);
  renderContact(name);

  $("emptyChat").classList.add("hidden");
  $("chatView").classList.remove("hidden");
  document.body.classList.add("in-chat");
  $("messages").textContent = "";
  history[name].forEach(renderMessage);
  $("input").focus();
}

$("backBtn").addEventListener("click", () => document.body.classList.remove("in-chat"));

function renderHeader() {
  $("chatName").textContent = current;
  $("chatAvatar").textContent = current[0].toUpperCase();
  $("chatStatus").textContent = statusText(current);
}

function isConnected(name) {
  const p = peers[name];
  return !!(p && p.channel && p.channel.readyState === "open");
}

function closePeer(name) {
  if (peers[name]) {
    peers[name].pc.close();
    delete peers[name];
  }
}

function createPeer(name, iStart) {
  closePeer(name);
  const pc = new RTCPeerConnection(rtcConfig);
  const peer = { pc, channel: null, queue: Promise.resolve() };
  peers[name] = peer;

  pc.onicecandidate = (e) => {
    if (e.candidate) socket.emit("signal", { to: name, signal: { candidate: e.candidate } });
  };
  if (iStart) {
    setupChannel(name, peer, pc.createDataChannel("chat"));
  } else {
    pc.ondatachannel = (e) => setupChannel(name, peer, e.channel);
  }
  return peer;
}

async function maybeConnect(name) {
  if (peers[name] || me > name) return;
  const peer = createPeer(name, true);
  await peer.pc.setLocalDescription(await peer.pc.createOffer());
  socket.emit("signal", { to: name, signal: { description: peer.pc.localDescription } });
}

socket.on("signal", ({ from, signal }) => {
  if (!contacts[from] || !signal) return;
  let peer = peers[from];
  if (signal.description && signal.description.type === "offer") {
    peer = createPeer(from, false);
  }
  if (!peer) return;
  peer.queue = peer.queue
    .then(() => handleSignal(peer, from, signal))
    .catch((err) => console.log("P2P error", err));
});

async function handleSignal(peer, name, signal) {
  if (signal.description) {
    await peer.pc.setRemoteDescription(signal.description);
    if (signal.description.type === "offer") {
      await peer.pc.setLocalDescription(await peer.pc.createAnswer());
      socket.emit("signal", { to: name, signal: { description: peer.pc.localDescription } });
    }
  } else if (signal.candidate) {
    await peer.pc.addIceCandidate(signal.candidate);
  }
}

function setupChannel(name, peer, channel) {
  peer.channel = channel;
  channel.onopen = () => renderContact(name);
  channel.onclose = () => renderContact(name);
  channel.onmessage = (e) => {
    let data;
    try {
      data = JSON.parse(e.data);
    } catch {
      return;
    }
    if (data.type === "typing") {
      showTyping(name);
    } else {
      receive(name, data, Date.now());
    }
  };
}

function sendP2P(name, data) {
  if (!isConnected(name)) return false;
  peers[name].channel.send(JSON.stringify(data));
  return true;
}

async function sendMessage(to, data) {
  if (sendP2P(to, data)) return "✓✓";
  const res = await socket.timeout(10000).emitWithAck("send_message", { to, message: data });
  if (!res.ok) throw new Error(res.error);
  return res.delivered ? "✓✓" : "✓";
}

function fetchPending() {
  socket.emit("fetch_pending", (list) => {
    list.forEach((m) => receive(m.from, m, m.time));
  });
}

socket.on("message", (m) => receive(m.from, m, m.time));

function receive(name, data, time) {
  if (!contacts[name]) addContact(name, false);
  if (data.type === "text" && typeof data.text === "string") {
    addMessage(name, { from: name, text: data.text.slice(0, 2000), time });
  } else if (data.type === "file" && /^[a-f0-9]{32}$/.test(data.id)) {
    const file = { id: data.id, name: String(data.name).slice(0, 100), image: !!data.image };
    addMessage(name, { from: name, file, time });
  }
}

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || !current) return;
  const to = current;
  $("input").value = "";
  try {
    const status = await sendMessage(to, { type: "text", text });
    addMessage(to, { from: me, text, time: Date.now(), status });
  } catch (err) {
    $("input").value = text;
    systemNote("❌ " + err.message);
  }
});

$("input").addEventListener("input", () => {
  if (current) sendP2P(current, { type: "typing" });
});

function showTyping(name) {
  if (name !== current) return;
  $("chatStatus").textContent = "typing...";
  clearTimeout(typingTimer);
  typingTimer = setTimeout(renderHeader, 1500);
}

$("fileInput").addEventListener("change", async () => {
  const file = $("fileInput").files[0];
  $("fileInput").value = "";
  if (!file || !current) return;
  const to = current;

  if (file.size > 10 * 1024 * 1024) return systemNote("File is too big (max 10 MB).");

  const note = systemNote("🔍 Checking " + file.name + " for viruses...");
  const data = new FormData();
  data.append("file", file);
  data.append("to", to);

  try {
    const res = await fetch("/upload", { method: "POST", body: data });
    const info = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(info.error || "Upload failed");
    note.remove();

    const msg = { type: "file", id: info.id, name: info.name, image: info.image };
    const status = await sendMessage(to, msg);
    addMessage(to, { from: me, file: msg, time: Date.now(), status });
  } catch (err) {
    note.textContent = "❌ " + err.message;
  }
});

function addMessage(name, msg) {
  history[name].push(msg);
  if (history[name].length > MAX_HISTORY) history[name].shift();
  saveHistory();
  if (name === current) {
    renderMessage(msg);
  } else {
    contacts[name].unread++;
    renderContact(name);
  }
}

function renderMessage(msg) {
  const div = el("div", msg.from === me ? "msg me" : "msg");

  if (msg.file) {
    const url = "/files/" + msg.file.id;
    if (msg.file.image) {
      const link = el("a");
      link.href = url;
      link.target = "_blank";
      const img = el("img", "chat-img");
      img.src = url;
      img.alt = msg.file.name;
      img.addEventListener("load", scrollDown);
      link.appendChild(img);
      div.appendChild(link);
    } else {
      const link = el("a", "file-link", "📄 " + msg.file.name);
      link.href = url;
      div.appendChild(link);
    }
  } else {
    div.appendChild(el("div", "", msg.text));
  }

  let time = new Date(msg.time).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  if (msg.status) time += " " + msg.status;
  div.appendChild(el("span", "time", time));
  $("messages").appendChild(div);
  scrollDown();
}

function systemNote(text) {
  const div = el("div", "system", text);
  $("messages").appendChild(div);
  scrollDown();
  return div;
}

function scrollDown() {
  $("messages").scrollTop = $("messages").scrollHeight;
}
