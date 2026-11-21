'use strict';
(() => {
  class SmaStreamPlayer {
    constructor(video, options) {
      this.video = video;
      this.options = options;
      this.serial = 0;
      this.pc = null;
      this.hls = null;
      this.session = null;
      this.timers = new Set();
      this.mode = null;
      this.destroyed = false;
    }

    state(label, mode = this.mode) { this.options.onState?.(label, mode); }
    later(callback, delay) {
      const timer = setTimeout(() => { this.timers.delete(timer); callback(); }, delay);
      this.timers.add(timer);
      return timer;
    }
    cancel(timer) { clearTimeout(timer); this.timers.delete(timer); }
    async request(url, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal});
        if (!response.ok) {
          const error = new Error('Reprodução indisponível'); error.status = response.status; throw error;
        }
        return response;
      } finally { clearTimeout(timer); }
    }
    release(session) {
      if (!session) return;
      fetch(session.url, {method: 'DELETE', credentials: 'same-origin', keepalive: true,
        headers: {'X-SMA-RTC-CSRF': session.csrf}}).catch(() => {});
    }
    reset() {
      this.serial += 1;
      this.timers.forEach(clearTimeout); this.timers.clear();
      if (this.pc) this.pc.close();
      this.pc = null;
      if (this.hls) this.hls.destroy();
      this.hls = null;
      this.release(this.session); this.session = null;
      this.video.pause(); this.video.srcObject = null; this.video.removeAttribute('src'); this.video.load();
    }
    denied() {
      this.reset(); this.state('Acesso indisponível'); this.options.onDenied?.();
    }
    async start(mode = 'auto') {
      if (this.destroyed) return;
      this.reset();
      const attempt = this.serial;
      if (mode === 'hls' || !this.options.webrtcUrl || !window.RTCPeerConnection) {
        this.startHls(attempt); return;
      }
      this.mode = 'rtc'; this.state('Conectando…');
      const prefix = this.options.webrtcUrl.replace(/\/whep$/, '');
      try {
        const permission = await (await this.request(prefix + '/access')).json();
        if (this.serial !== attempt) return;
        const connection = new RTCPeerConnection({iceServers: []});
        this.pc = connection;
        connection.addTransceiver('video', {direction: 'recvonly'});
        connection.addTransceiver('audio', {direction: 'recvonly'});
        const incoming = new MediaStream(); this.video.srcObject = incoming;
        connection.ontrack = event => {
          if (this.serial !== attempt) return;
          incoming.addTrack(event.track); this.video.play().catch(() => {});
        };
        let disconnectTimer = null;
        const fail = () => { if (this.serial === attempt) this.start('hls'); };
        connection.onconnectionstatechange = () => {
          if (this.serial !== attempt) return;
          if (connection.connectionState === 'connected') {
            if (disconnectTimer) this.cancel(disconnectTimer);
            disconnectTimer = null;
            this.state('Ao vivo');
          } else if (connection.connectionState === 'failed') fail();
          else if (connection.connectionState === 'disconnected' && !disconnectTimer) disconnectTimer = this.later(fail, 5000);
        };
        await connection.setLocalDescription(await connection.createOffer());
        if (connection.iceGatheringState !== 'complete') await new Promise(resolve => {
          const complete = () => { clearTimeout(timeout); connection.removeEventListener('icegatheringstatechange', changed); resolve(); };
          const changed = () => { if (connection.iceGatheringState === 'complete') complete(); };
          const timeout = setTimeout(complete, 5000);
          connection.addEventListener('icegatheringstatechange', changed);
        });
        if (this.serial !== attempt) return;
        const response = await this.request(this.options.webrtcUrl, {method: 'POST',
          headers: {'Content-Type': 'application/sdp', 'X-SMA-RTC-CSRF': permission.csrf}, body: connection.localDescription.sdp});
        const ticket = response.headers.get('X-SMA-RTC-Session');
        if (!ticket || !/^[A-Za-z0-9_-]{32}$/.test(ticket)) throw new Error('Sessão inválida');
        const session = {url: prefix + '/session/' + ticket, csrf: permission.csrf};
        if (this.serial !== attempt) { this.release(session); return; }
        this.session = session;
        await connection.setRemoteDescription({type: 'answer', sdp: await response.text()});
        if (this.serial !== attempt) return;
        this.later(() => { if (connection.connectionState !== 'connected' || this.video.readyState < 2) fail(); }, 12000);
        const heartbeat = async () => {
          if (this.serial !== attempt) return;
          try {
            await this.request(session.url, {method: 'POST', headers: {'X-SMA-RTC-CSRF': session.csrf}});
          } catch (error) {
            if (this.serial !== attempt) return;
            if ([401, 403].includes(error.status)) { this.denied(); return; }
            if (error.status === 404) { fail(); return; }
          }
          if (this.serial === attempt) this.later(heartbeat, 25000);
        };
        this.later(heartbeat, 25000);
      } catch (error) {
        if (this.serial !== attempt) return;
        if ([401, 403].includes(error.status)) this.denied();
        else this.start('hls');
      }
    }

    startHls(attempt) {
      if (this.serial !== attempt || this.destroyed) return;
      this.mode = 'hls'; this.state('Modo compatível · maior atraso');
      if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({liveSyncDuration: 4, liveMaxLatencyDuration: 7, maxBufferLength: 9});
        this.hls = hls;
        let retries = 0;
        hls.on(Hls.Events.MEDIA_ATTACHED, () => { if (this.serial === attempt) hls.loadSource(this.options.hlsUrl); });
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (this.serial === attempt) this.video.play().catch(() => {}); });
        hls.on(Hls.Events.ERROR, (_, data) => {
          if (this.serial !== attempt) return;
          if ([401, 403].includes(data?.response?.code)) { this.denied(); return; }
          if (!data.fatal) return;
          if (retries++ < 2 && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            this.state('Reconectando…'); this.later(() => { if (this.serial === attempt) hls.startLoad(); }, 1500);
          } else if (retries < 3 && data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
          else this.state('Reprodução interrompida');
        });
        hls.attachMedia(this.video);
      } else if (this.video.canPlayType('application/vnd.apple.mpegurl')) {
        this.video.src = this.options.hlsUrl; this.video.play().catch(() => {});
      } else this.state('Navegador sem reprodução compatível');
    }

    destroy() { this.destroyed = true; this.reset(); }
  }
  window.SmaStreamPlayer = SmaStreamPlayer;
})();
