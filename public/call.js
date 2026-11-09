let call = null;

$("voiceCallBtn").addEventListener("click", () => startCall(current, false));
$("videoCallBtn").addEventListener("click", () => startCall(current, true));
$("acceptBtn").addEventListener("click", acceptCall);
$("hangupBtn").addEventListener("click", () => endCall(true));
$("muteBtn").addEventListener("click", toggleMute);

function sendCall(name, data) {
  socket.emit("signal", { to: name, signal: { call: data } });
}

async function startCall(name, video) {
  if (!name || call) return;
  if (!contacts[name].online) return systemNote(name + " is offline.");
  if (!navigator.mediaDevices) return systemNote("Calls need HTTPS - use your Tailscale address.");

  call = { name, video, incoming: false, candidates: [] };
  showCallScreen("Calling...");
  try {
    await setupCallConnection();
    await call.pc.setLocalDescription(await call.pc.createOffer());
    sendCall(name, { type: "offer", description: call.pc.localDescription, video });
  } catch (err) {
    if (call) {
      systemNote("❌ " + err.message);
      endCall(true);
    }
  }
}

async function setupCallConnection() {
  const c = call;
  c.stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: c.video });
  if (call !== c) {
    c.stream.getTracks().forEach((t) => t.stop());
    throw new Error("Call ended");
  }
  $("localVideo").srcObject = c.video ? c.stream : null;

  c.pc = new RTCPeerConnection(rtcConfig);
  c.stream.getTracks().forEach((t) => c.pc.addTrack(t, c.stream));
  c.pc.ontrack = (e) => {
    $("remoteVideo").srcObject = e.streams[0];
  };
  c.pc.onicecandidate = (e) => {
    if (e.candidate) sendCall(c.name, { type: "candidate", candidate: e.candidate });
  };
  c.pc.onconnectionstatechange = () => {
    if (c.pc.connectionState === "connected") startTimer();
    if (c.pc.connectionState === "failed") {
      setCallStatus("Connection failed");
      setTimeout(() => endCall(true), 2000);
    }
  };
}

socket.on("signal", async ({ from, signal }) => {
  const data = signal && signal.call;
  if (!data || !contacts[from]) return;

  if (data.type === "offer") {
    if (call) return sendCall(from, { type: "busy" });
    call = { name: from, video: !!data.video, incoming: true, offer: data.description, candidates: [] };
    showCallScreen(call.video ? "Incoming video call" : "Incoming voice call");
    return;
  }

  if (!call || call.name !== from) return;
  if (data.type === "answer") {
    try {
      await call.pc.setRemoteDescription(data.description);
      flushCandidates();
    } catch (err) {
      endCall(true);
    }
  } else if (data.type === "candidate") {
    addCandidate(data.candidate);
  } else if (data.type === "busy") {
    setCallStatus(from + " is busy");
    setTimeout(() => endCall(false), 2000);
  } else if (data.type === "end") {
    endCall(false);
  }
});

socket.on("presence", ({ name, online }) => {
  if (!online && call && call.name === name) endCall(false);
});

function addCandidate(candidate) {
  if (call.pc && call.pc.remoteDescription) {
    call.pc.addIceCandidate(candidate).catch(() => {});
  } else {
    call.candidates.push(candidate);
  }
}

function flushCandidates() {
  while (call && call.candidates.length) {
    call.pc.addIceCandidate(call.candidates.shift()).catch(() => {});
  }
}

async function acceptCall() {
  if (!call || !call.incoming || call.accepted) return;
  call.accepted = true;
  if (!navigator.mediaDevices) {
    systemNote("Calls need HTTPS - use your Tailscale address.");
    return endCall(true);
  }
  $("acceptBtn").classList.add("hidden");
  setCallStatus("Connecting...");
  try {
    await setupCallConnection();
    await call.pc.setRemoteDescription(call.offer);
    flushCandidates();
    await call.pc.setLocalDescription(await call.pc.createAnswer());
    sendCall(call.name, { type: "answer", description: call.pc.localDescription });
  } catch (err) {
    if (call) {
      systemNote("❌ " + err.message);
      endCall(true);
    }
  }
}

function endCall(tellOther) {
  if (!call) return;
  if (tellOther) sendCall(call.name, { type: "end" });
  if (call.stream) call.stream.getTracks().forEach((t) => t.stop());
  if (call.pc) call.pc.close();
  clearInterval(call.timer);
  call = null;
  $("remoteVideo").srcObject = null;
  $("localVideo").srcObject = null;
  $("callScreen").classList.add("hidden");
}

function showCallScreen(status) {
  $("callName").textContent = call.name;
  $("callAvatar").textContent = call.name[0].toUpperCase();
  setCallStatus(status);
  $("callScreen").className = call.video ? "" : "voice";
  $("acceptBtn").classList.toggle("hidden", !call.incoming);
  $("muteBtn").classList.remove("off");
}

function setCallStatus(text) {
  $("callStatus").textContent = text;
}

function startTimer() {
  if (!call || call.timer) return;
  const start = Date.now();
  if (call.video) $("callScreen").classList.add("live");
  setCallStatus("00:00");
  call.timer = setInterval(() => {
    const s = Math.floor((Date.now() - start) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    setCallStatus(mm + ":" + ss);
  }, 1000);
}

function toggleMute() {
  if (!call || !call.stream) return;
  const track = call.stream.getAudioTracks()[0];
  track.enabled = !track.enabled;
  $("muteBtn").classList.toggle("off", !track.enabled);
}
