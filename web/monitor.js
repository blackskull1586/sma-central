'use strict';
(() => {
  const container = document.getElementById('streams');
  if (!container) return;
  const checks = document.getElementById('dynamicCheckboxes');
  const selectAll = document.getElementById('selectAll');
  const lock = document.getElementById('lockLayout');
  const empty = document.getElementById('empty-streams');
  const status = document.getElementById('connection-status');
  const prefix = `sma:${container.dataset.storageScope}:`;
  const records = new Map();
  let refreshing = false;
  function read(key) { try { return localStorage.getItem(prefix + key); } catch { return null; } }
  function write(key, value) { try { localStorage.setItem(prefix + key, value); } catch {} }
  const savedSelectAll = read('selectAll');
  selectAll.checked = savedSelectAll === null ? container.dataset.defaultSelectAll === 'true' : savedSelectAll === 'true';
  lock.checked = read('lockLayout') === 'true';
  const sortable = window.Sortable ? new Sortable(container, { animation: 150, disabled: lock.checked, ghostClass: 'sortable-ghost' }) : null;
  function stop(record) {
    if (record.player) record.player.destroy();
    record.player = null;
    if (record.video) { record.video.pause(); record.video.removeAttribute('src'); record.video.load(); record.video.remove(); }
    record.video = null;
    record.placeholder.hidden = false;
    record.playback.textContent = '';
  }
  function start(record) {
    if (record.video) return;
    const video = document.createElement('video');
    video.controls = true; video.autoplay = true; video.muted = true; video.playsInline = true;
    record.video = video;
    record.card.appendChild(video);
    video.addEventListener('playing', () => { record.placeholder.hidden = true; });
    record.player = new SmaStreamPlayer(video, {
      hlsUrl: record.stream.url, webrtcUrl: record.stream.webrtc_url,
      onState: (label, mode) => {
        record.playback.textContent = label;
        record.modeButton.textContent = mode === 'rtc' ? 'Modo compatível' : 'Menor atraso';
        record.modeButton.hidden = !record.stream.webrtc_url;
      },
      onDenied: () => { record.placeholder.hidden = false; refresh(); }
    });
    record.player.start();
  }
  function show(record) {
    record.card.hidden = !record.checkbox.checked;
    if (record.checkbox.checked) start(record); else stop(record);
  }
  function updateEmpty() {
    const visible = [...records.values()].filter(record => record.checkbox.checked).length;
    empty.hidden = visible > 0;
    empty.textContent = records.size ? 'Selecione um canal para assistir.' : 'Nenhuma transmissão no momento.';
    if (records.size) {
      selectAll.checked = visible === records.size;
      selectAll.indeterminate = visible > 0 && visible < records.size;
      write('selectAll', selectAll.checked ? 'true' : 'false');
    } else selectAll.indeterminate = false;
  }
  function remove(id) {
    const record = records.get(id);
    if (!record) return;
    stop(record); record.card.remove(); record.label.remove(); records.delete(id);
  }
  function clear() { [...records.keys()].forEach(remove); updateEmpty(); }
  function streamLabel(stream) {
    return container.dataset.showEnvironment === 'true' && stream.environment?.name
      ? `${stream.environment.name} · ${stream.label}` : stream.label;
  }
  function create(stream) {
    const card = document.createElement('article');
    card.className = 'stream'; card.classList.toggle('locked', lock.checked);
    const title = document.createElement('h3'); title.textContent = streamLabel(stream);
    const placeholder = document.createElement('img');
    placeholder.className = 'placeholder'; placeholder.src = '/assets/placeholder.jpg'; placeholder.alt = 'Aguardando vídeo';
    const playback = document.createElement('span');
    const modeButton = document.createElement('button'); modeButton.type = 'button'; modeButton.className = 'secondary';
    const playbackControls = document.createElement('div'); playbackControls.className = 'stream-playback';
    playbackControls.append(playback, modeButton);
    card.append(title, placeholder, playbackControls); container.appendChild(card);
    const label = document.createElement('label');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    const saved = read(`stream:${stream.id}`);
    checkbox.checked = saved === null ? selectAll.checked : saved === 'true';
    const caption = document.createTextNode(streamLabel(stream));
    label.append(checkbox, caption); checks.appendChild(label);
    const record = { stream, card, title, placeholder, label, checkbox, caption, playback, modeButton, video: null, player: null };
    modeButton.addEventListener('click', () => record.player?.start(record.player.mode === 'rtc' ? 'hls' : 'auto'));
    records.set(stream.id, record);
    checkbox.addEventListener('change', () => { write(`stream:${stream.id}`, String(checkbox.checked)); show(record); updateEmpty(); });
    show(record);
  }
  lock.addEventListener('change', () => {
    sortable?.option('disabled', lock.checked); write('lockLayout', String(lock.checked));
    records.forEach(record => record.card.classList.toggle('locked', lock.checked));
  });
  selectAll.addEventListener('change', () => {
    write('selectAll', String(selectAll.checked));
    records.forEach(record => { record.checkbox.checked = selectAll.checked; write(`stream:${record.stream.id}`, String(selectAll.checked)); show(record); });
    updateEmpty();
  });
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(container.dataset.catalogUrl || '/streams.json', { cache: 'no-store', credentials: 'same-origin', signal: controller.signal });
      if (response.status === 401) { clear(); location.assign('/login/'); return; }
      if (response.status === 403) { clear(); status.textContent = 'Acesso indisponível'; empty.textContent = 'Atualize sua senha ou procure o administrador do ambiente.'; return; }
      if (!response.ok) throw new Error('Catálogo indisponível');
      const data = await response.json();
      if (!Array.isArray(data)) throw new Error('Catálogo inválido');
      const ids = new Set();
      data.forEach(stream => {
        if (!/^[0-9a-f]{32}$/.test(stream.id) || stream.url !== `/hls/${stream.id}.m3u8`) return;
        if (stream.webrtc_url && stream.webrtc_url !== `/rtc/${stream.id}/whep`) return;
        ids.add(stream.id);
        if (!records.has(stream.id)) create(stream);
        else { const record = records.get(stream.id); record.stream = stream; record.title.textContent = streamLabel(stream); record.caption.nodeValue = streamLabel(stream); }
      });
      [...records.keys()].forEach(id => { if (!ids.has(id)) remove(id); });
      status.textContent = `${ids.size} ${ids.size === 1 ? 'canal transmitindo' : 'canais transmitindo'}`;
      updateEmpty();
    } catch {
      status.textContent = 'Reconectando…';
    } finally {
      clearTimeout(timeout); refreshing = false;
    }
  }
  refresh();
  const timer = setInterval(refresh, 5000);
  window.addEventListener('beforeunload', () => { clearInterval(timer); clear(); });
})();
