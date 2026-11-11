const $ = (q) => document.querySelector(q);
const $$ = (q) => [...document.querySelectorAll(q)];
let gifts = [];
let giftCatalog = [];
let appConfig = null;
let toastTimer;
let CONNECT_COMMAND = '/connect ws://127.0.0.1:6464';
let WEBSOCKET_URL = 'ws://127.0.0.1:6464';

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}
function safeText(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function api(url, options = {}) {
  const headers = {'Content-Type':'application/json', ...(options.headers || {})};
  const token = localStorage.getItem('voxellive_access_token');
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(url, {...options, headers});
  const data = await response.json().catch(() => ({}));
  return {response, data};
}
function post(url, body) {
  return api(url, {method:'POST', body:JSON.stringify(body)});
}
function navigate(page) {
  $$('.page').forEach(el => el.classList.toggle('active', el.id === `page-${page}`));
  $$('[data-page]').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  const title = {home:'Overview', gifts:'Gift Rules', minecraft:'Minecraft Link'}[page] || 'Overview';
  $('#crumb-title').textContent = title;
  window.scrollTo({top:0,behavior:'smooth'});
}
$$('[data-page]').forEach(btn => btn.addEventListener('click', () => navigate(btn.dataset.page)));
$$('[data-goto]').forEach(btn => btn.addEventListener('click', () => navigate(btn.dataset.goto)));

function applyConfig(data) {
  if (!data || !data.active) return;
  appConfig = data;
  const select = $('#active-preset-select');
  select.innerHTML = (data.presets || []).map(p => `<option value="${safeText(p.id)}">${safeText(p.name)}</option>`).join('');
  select.value = data.active_preset_id;
  $('#preset-state-note').textContent = `Preset aktif: ${data.active.name}. Maksimal ${data.max_presets || 50} preset; pengaturan tiap preset tersimpan terpisah.`;
  $('#follow-enabled').checked = Boolean(data.active.follow?.enabled);
  $('#follow-command').value = data.active.follow?.command || '';
  $('#cooldown-ms').value = Number(data.active.cooldown_ms || 500);
  updateFollowNote();
}
async function loadConfig() {
  const {response, data} = await api('/api/config');
  if (!response.ok) throw new Error(data.message || 'Gagal memuat preset.');
  applyConfig(data);
}
async function refreshActiveConfig() {
  try { await loadConfig(); }
  catch (err) { toast(err.message); }
}

async function switchPreset(id) {
  if (!id || id === appConfig?.active_preset_id) return;
  const select = $('#active-preset-select');
  select.disabled = true;
  try {
    const {response, data} = await post('/api/presets', {action:'switch', id});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal mengganti preset.');
    applyConfig(data.config);
    await loadGifts();
    toast(`Preset "${appConfig.active.name}" aktif.`);
  } catch (err) {
    toast(err.message);
    await refreshActiveConfig();
  } finally { select.disabled = false; }
}
$('#active-preset-select').addEventListener('change', e => switchPreset(e.target.value));
$('#create-preset-btn').addEventListener('click', async () => {
  const name = prompt('Nama preset baru:', 'TNT Challenge');
  if (name === null) return;
  try {
    const {response, data} = await post('/api/presets', {action:'create', name});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal membuat preset.');
    applyConfig(data.config);
    await loadGifts();
    toast(`Preset "${appConfig.active.name}" dibuat dari salinan pengaturan saat ini.`);
  } catch (err) { toast(err.message); }
});
$('#rename-preset-btn').addEventListener('click', async () => {
  if (!appConfig?.active) return;
  const name = prompt('Nama baru untuk preset ini:', appConfig.active.name);
  if (name === null) return;
  try {
    const {response, data} = await post('/api/presets', {action:'rename', id:appConfig.active_preset_id, name});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal mengganti nama preset.');
    applyConfig(data.config);
    toast('Nama preset berhasil diperbarui.');
  } catch (err) { toast(err.message); }
});
$('#delete-preset-btn').addEventListener('click', async () => {
  if (!appConfig?.active) return;
  if (!confirm(`Hapus preset "${appConfig.active.name}"? Pengaturan preset ini akan dihapus.`)) return;
  try {
    const {response, data} = await post('/api/presets', {action:'delete', id:appConfig.active_preset_id});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal menghapus preset.');
    applyConfig(data.config);
    await loadGifts();
    toast('Preset dihapus. Preset lain sekarang aktif.');
  } catch (err) { toast(err.message); }
});


$('#share-preset-btn').addEventListener('click', async () => {
  if (!appConfig?.active) return;
  const button = $('#share-preset-btn');
  button.disabled = true;
  try {
    const {response, data} = await post('/api/presets/share', {});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal membuat kode preset.');
    const copied = await copyText(data.code);
    if (copied) toast(`Kode preset ${data.code} berhasil dibuat. Bagikan ke temanmu.`);
    else alert(`Kode preset kamu:\n\n${data.code}\n\nSalin kode ini dan kirim ke teman.`);
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
});
$('#import-preset-btn').addEventListener('click', async () => {
  const code = prompt('Masukkan kode preset dari teman:');
  if (code === null || !code.trim()) return;
  if (!confirm('Pastikan kode berasal dari orang yang kamu percaya. Preset dapat berisi Minecraft command yang berjalan otomatis saat gift/follow diterima. Import sekarang?')) return;
  const button = $('#import-preset-btn');
  button.disabled = true;
  try {
    const {response, data} = await post('/api/presets/import', {code:code.trim().toUpperCase()});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal mengimpor preset.');
    applyConfig(data.config);
    await loadGifts();
    toast(data.message || 'Preset berhasil diimpor.');
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
});

function renderSimulator() {
  const select = $('#simulate-gift');
  if (!select) return;
  const previous = select.value;
  const active = gifts.filter(g => g.enabled);
  select.innerHTML = active.length ? active.map(g => `<option value="${safeText(g.id)}">${safeText(g.emoji)} ${safeText(g.name)}</option>`).join('') : '<option value="">Tidak ada gift aktif</option>';
  if (active.some(g => g.id === previous)) select.value = previous;
  $('#simulate-btn').disabled = active.length === 0;
}
function renderQuickRules() {
  const active = gifts.filter(g => g.enabled).slice(0,2);
  $('#quick-rule-list').innerHTML = active.length ? active.map(g => `<div class="quick-chip"><span class="emoji">${safeText(g.emoji)}</span><div><b>${safeText(g.name)}</b><small>${safeText(g.command)}</small></div></div>`).join('') : '<div class="form-note">Belum ada gift aktif.</div>';
}
function renderGifts() {
  renderSimulator();
  $('#gift-count').textContent = `${String(gifts.filter(g=>g.enabled).length).padStart(2,'0')} active rules`;
  $('#rules-title').textContent = `${gifts.length} configured rule${gifts.length===1?'':'s'}`;
  const list = $('#gift-list');
  if (!gifts.length) {
    list.innerHTML = '<div class="panel"><p class="panel-copy">Belum ada mapping gift. Tekan Add gift untuk membuat yang pertama.</p></div>';
    renderQuickRules();
    return;
  }
  list.innerHTML = gifts.map(g => { const commands = String(g.command || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean); return `<article class="gift-row"><div class="gift-identity"><span class="gift-emoji">${safeText(g.emoji)}</span><div><b>${safeText(g.name)}</b><small>${g.enabled?'ACTIVE':'DISABLED'} · ${commands.length} command</small></div></div><div class="gift-command"><span>MINECRAFT COMMANDS</span><code>${commands.map(safeText).join('<br>')}</code></div><div class="gift-actions"><button class="test-btn" data-test="${safeText(g.id)}" ${g.enabled?'':'disabled'}>Test ↗</button><button class="edit-btn" data-edit="${safeText(g.id)}">Edit</button><button class="delete-btn" data-delete="${safeText(g.id)}">×</button></div></article>`; }).join('');
  $$('[data-test]').forEach(btn => btn.addEventListener('click', () => testGift(btn.dataset.test)));
  $$('[data-edit]').forEach(btn => btn.addEventListener('click', () => openGiftModal(btn.dataset.edit)));
  $$('[data-delete]').forEach(btn => btn.addEventListener('click', () => deleteGift(btn.dataset.delete)));
  renderQuickRules();
}
async function loadGifts() {
  const {response, data} = await api('/api/gifts');
  if (!response.ok) throw new Error(data.message || 'Tidak bisa memuat gift.');
  gifts = Array.isArray(data) ? data : [];
  renderGifts();
}
async function saveGifts() {
  const {response, data} = await post('/api/gifts', {gifts});
  if (!response.ok || !data.ok) throw new Error(data.message || 'Gagal menyimpan mapping.');
  gifts = data.gifts;
  renderGifts();
}
async function simulateGift(id) {
  const result = $('#simulation-result');
  const gift = gifts.find(g => g.id === id && g.enabled);
  if (!gift) { result.textContent = 'Pilih gift yang aktif terlebih dahulu.'; return; }
  result.textContent = `Simulasi ${gift.emoji} ${gift.name} sedang dikirim…`;
  try {
    const {response, data} = await post('/api/test', {gift_id:id});
    result.textContent = data.ok ? `Berhasil dikirim: ${gift.name} → ${Array.isArray(data.commands) ? data.commands.length : 1} command. Periksa Minecraft.` : `Belum terkirim: ${data.message || `HTTP ${response.status}`}`;
    toast(data.message || (data.ok ? 'Simulasi dikirim.' : 'Simulasi gagal.'));
  } catch (err) { result.textContent = `Error: ${err.message}`; toast(err.message); }
}
$('#simulate-btn').addEventListener('click', () => simulateGift($('#simulate-gift').value));
async function testGift(id) {
  const gift = gifts.find(g => g.id === id);
  if (!gift) return;
  toast(`Mengirim test ${gift.name}…`);
  try {
    const {data} = await post('/api/test', {gift_id:id});
    toast(data.message || (data.ok ? 'Command terkirim.' : 'Command gagal.'));
  } catch (err) { toast(err.message); }
}
async function deleteGift(id) {
  const gift = gifts.find(g => g.id === id);
  if (!gift || !confirm(`Hapus mapping ${gift.name}?`)) return;
  const previous = gifts;
  gifts = gifts.filter(g => g.id !== id);
  try { await saveGifts(); toast('Gift rule dihapus.'); }
  catch (err) { gifts = previous; renderGifts(); toast(err.message); }
}
function renderGiftCatalog(filter = '') {
  const select = $('#gift-catalog-select');
  if (!select) return;
  const query = String(filter || '').trim().toLocaleLowerCase('id-ID');
  const filtered = giftCatalog.filter(item => item.name.toLocaleLowerCase('id-ID').includes(query));
  const existingNames = new Set(gifts.map(g => g.name.toLocaleLowerCase('id-ID')));
  if (!giftCatalog.length) {
    select.innerHTML = '<option value="">Katalog belum tersedia</option>';
    return;
  }
  select.innerHTML = filtered.length ? filtered.map(item => {
    const label = `${item.name}${item.diamond_count ? ` · ${item.diamond_count} diamond` : ''}${existingNames.has(item.name.toLocaleLowerCase('id-ID')) ? ' · sudah dimapping' : ''}`;
    return `<option value="${safeText(item.id || item.name)}">${safeText(label)}</option>`;
  }).join('') : '<option value="">Gift tidak ditemukan</option>';
}
async function loadGiftCatalog({refresh = false} = {}) {
  const note = $('#gift-catalog-note');
  if (note) note.textContent = refresh ? 'Memuat ulang katalog gift…' : 'Memuat katalog gift…';
  try {
    const {response, data} = await api('/api/gift-catalog');
    if (!response.ok) throw new Error(data.message || 'Gagal membaca katalog.');
    giftCatalog = Array.isArray(data.gifts) ? data.gifts : [];
    renderGiftCatalog($('#gift-catalog-search')?.value || '');
    if (note) note.textContent = data.message || (giftCatalog.length ? `${giftCatalog.length} gift tersedia.` : 'Hubungkan TikTok LIVE untuk memuat katalog.');
    return giftCatalog;
  } catch (err) {
    if (note) note.textContent = err.message;
    return [];
  }
}
$('#gift-catalog-search').addEventListener('input', e => renderGiftCatalog(e.target.value));
$('#gift-catalog-select').addEventListener('change', e => {
  const selected = giftCatalog.find(item => String(item.id || item.name) === e.target.value);
  if (!selected) return;
  $('#gift-name').value = selected.name;
  $('#gift-catalog-id').value = selected.id || '';
  $('#gift-emoji').value = '🎁';
});
$('#refresh-catalog-btn').addEventListener('click', async () => {
  const button = $('#refresh-catalog-btn');
  button.disabled = true;
  try {
    const {response, data} = await post('/api/gift-catalog/refresh', {});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Tidak bisa refresh katalog.');
    $('#gift-catalog-note').textContent = 'Sedang memperbarui katalog dari TikTok…';
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      await loadGiftCatalog({refresh:true});
      const {data: statusData} = await api('/api/gift-catalog');
      if (statusData.status !== 'loading') break;
    }
    toast(giftCatalog.length ? `Gift Catalog diperbarui: ${giftCatalog.length} gift.` : 'Katalog belum tersedia. Pastikan TikTok LIVE sudah terhubung.');
  } catch (err) {
    $('#gift-catalog-note').textContent = err.message;
    toast(err.message);
  } finally { button.disabled = false; }
});

function openGiftModal(id = '') {
  const gift = gifts.find(g => g.id === id);
  $('#gift-id').value = gift?.id || '';
  $('#gift-name').value = gift?.name || '';
  $('#gift-catalog-id').value = gift?.gift_id || '';
  $('#gift-emoji').value = gift?.emoji || '🎁';
  $('#gift-command').value = gift?.command || '';
  $('#gift-catalog-search').value = '';
  $('#modal-title').textContent = gift ? 'Edit gift rule' : 'Add a gift rule';
  renderGiftCatalog();
  loadGiftCatalog();
  $('#gift-modal').classList.add('open');
  $('#gift-modal').setAttribute('aria-hidden','false');
  setTimeout(() => $('#gift-name').focus(), 50);
}
function closeGiftModal() { $('#gift-modal').classList.remove('open'); $('#gift-modal').setAttribute('aria-hidden','true'); }
$('#add-gift-btn').addEventListener('click', () => openGiftModal());
$('#close-modal').addEventListener('click', closeGiftModal);
$('#cancel-modal').addEventListener('click', closeGiftModal);
$('#gift-modal').addEventListener('click', e => { if (e.target === $('#gift-modal')) closeGiftModal(); });
$('#gift-form').addEventListener('submit', async e => {
  e.preventDefault();
  const id = $('#gift-id').value || `gift-${Date.now()}`;
  const commandLines = $('#gift-command').value.replace(/\r/g, '').split('\n').map(line => line.trim()).filter(Boolean);
  if (!commandLines.length || commandLines.length > 10) { toast('Isi 1–10 command, masing-masing satu baris.'); return; }
  const item = {id, gift_id:$('#gift-catalog-id').value.trim(), name:$('#gift-name').value.trim(), emoji:$('#gift-emoji').value.trim() || '🎁', command:commandLines.map(line => line.startsWith('/') ? line : `/${line}`).join('\n'), enabled:true};
  const index = gifts.findIndex(g => g.id === id);
  const previous = gifts;
  gifts = index >= 0 ? gifts.map(g => g.id === id ? item : g) : [...gifts, item];
  try { await saveGifts(); closeGiftModal(); toast('Gift rule tersimpan di preset aktif.'); }
  catch (err) { gifts = previous; renderGifts(); toast(err.message); }
});

function updateFollowNote() {
  const enabled = $('#follow-enabled').checked;
  $('#follow-note').textContent = enabled
    ? 'Follow Mapping aktif pada preset ini. Pastikan Minecraft tersambung sebelum LIVE.'
    : 'Follow Mapping nonaktif sampai kamu mengaktifkannya.';
}
$('#follow-enabled').addEventListener('change', updateFollowNote);
$('#follow-form').addEventListener('submit', async e => {
  e.preventDefault();
  const follow = {enabled:$('#follow-enabled').checked, command:$('#follow-command').value.trim()};
  const cooldown_ms = Number($('#cooldown-ms').value);
  $('#save-follow-btn').disabled = true;
  try {
    const {response, data} = await post('/api/config', {follow, cooldown_ms});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Pengaturan tidak tersimpan.');
    applyConfig(data.config);
    toast('Follow Mapping dan cooldown tersimpan di preset aktif.');
  } catch (err) { toast(err.message); $('#follow-note').textContent = err.message; }
  finally { $('#save-follow-btn').disabled = false; }
});
$('#test-follow-btn').addEventListener('click', async () => {
  toast('Mengirim test Follow command…');
  try {
    const {data} = await post('/api/test-follow', {});
    toast(data.message || (data.ok ? 'Follow test terkirim.' : 'Test gagal.'));
  } catch (err) { toast(err.message); }
});

$('#tiktok-form').addEventListener('submit', async e => {
  e.preventDefault();
  const username = $('#tiktok-username').value.trim().replace(/^@/,'');
  if (!username) { $('#tiktok-note').textContent = 'Masukkan username TikTok dulu.'; return; }
  $('#tiktok-connect-btn').disabled = true;
  $('#tiktok-note').textContent = `Menghubungkan ke @${username}…`;
  try {
    const {data} = await post('/api/tiktok/connect', {username});
    $('#tiktok-note').textContent = data.message || (data.ok ? 'Koneksi dimulai.' : 'Koneksi gagal.');
    toast(data.message || (data.ok ? 'Mencoba koneksi TikTok…' : 'Koneksi TikTok gagal.'));
    refreshStatus();
  } catch (err) { $('#tiktok-note').textContent = err.message; toast(err.message); }
  finally { $('#tiktok-connect-btn').disabled = false; }
});
$('#tiktok-disconnect-btn').addEventListener('click', async () => {
  $('#tiktok-note').textContent = 'Memutuskan koneksi…';
  try {
    const {data} = await post('/api/tiktok/disconnect', {});
    $('#tiktok-note').textContent = data.message || 'Koneksi dihentikan.';
    toast(data.message || 'Koneksi TikTok dihentikan.');
    refreshStatus();
  } catch (err) { $('#tiktok-note').textContent = err.message; toast(err.message); }
});
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Berhasil disalin.'); return true; }
  catch { toast(`Salin manual: ${text}`); return false; }
}
$('#copy-address').addEventListener('click', () => copyText(WEBSOCKET_URL));
$('#copy-connect').addEventListener('click', () => copyText(CONNECT_COMMAND));

function setConnection(connected, detail = '') {
  $('#mc-status').textContent = connected ? 'Connected' : 'Waiting for link';
  $('#mc-detail').textContent = connected ? 'WebSocket aktif · Port 6464' : (detail || 'WebSocket · Port 6464');
  $('#mc-dot').classList.toggle('connected', connected); $('#mc-dot').classList.toggle('muted', !connected);
  $('#mc-dot-large').classList.toggle('connected', connected); $('#mc-dot-large').classList.toggle('muted', !connected);
  $('#mc-badge').classList.toggle('connected', connected);
  $('#mc-badge').innerHTML = `<i></i> ${connected?'CONNECTED':'DISCONNECTED'}`;
  $('#mc-foot-text').textContent = connected ? 'Minecraft sudah terhubung ke VoxelLive.' : 'Menunggu Minecraft terhubung…';
}
function renderTikTokEvents(events) {
  const target = $('#tiktok-events-list');
  if (!target) return;
  if (!Array.isArray(events) || !events.length) {
    target.innerHTML = '<div class="empty-events">Belum ada gift atau follow masuk. Hubungkan akun yang sedang LIVE untuk mulai menerima event.</div>';
    return;
  }
  const labels = {sent:'SENT TO MC', unmapped:'NO MAPPING', minecraft_offline:'MC OFFLINE', failed:'FAILED', received:'PROCESSING'};
  target.innerHTML = events.slice(0, 8).map(event => {
    const time = new Date((event.time || 0) * 1000).toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    const sender = event.sender || event.username || 'Viewer';
    const isFollow = event.type === 'follow';
    const quantity = Number(event.quantity || 1);
    const icon = isFollow ? '♥' : (gifts.find(g => g.name.toLowerCase() === String(event.gift || '').toLowerCase()) || {}).emoji || '🎁';
    const label = isFollow ? 'FOLLOW' : `${quantity}× ${event.gift || 'Gift'}`;
    const status = event.status || 'received';
    return `<article class="event-item"><span class="event-gift-icon ${isFollow?'follow-event-icon':''}">${safeText(icon)}</span><div class="event-main"><b>${safeText(sender)} · ${safeText(label)}</b><small>${safeText(time)} · ${safeText(event.message || '')}</small></div><span class="event-status ${safeText(status)}">${safeText(labels[status] || status.toUpperCase())}</span></article>`;
  }).join('');
}
async function refreshStatus() {
  try {
    const {data} = await api('/api/status');
    setConnection(Boolean(data.minecraft_connected));
    if (data.websocket_url) {
      WEBSOCKET_URL = data.websocket_url;
      CONNECT_COMMAND = data.minecraft_connect_command || `/connect ${WEBSOCKET_URL}`;
      $('#server-address').textContent = WEBSOCKET_URL;
      const preview = document.querySelector('.command-preview code');
      if (preview) preview.textContent = CONNECT_COMMAND;
    }
    const status = data.tiktok_status || 'disconnected';
    const statusLabels = {connected:'Connected', connecting:'Connecting…', stopping:'Disconnecting…', disconnected:'Not connected', error:'Connection error'};
    $('#tiktok-status').textContent = statusLabels[status] || status;
    $('#tiktok-detail').textContent = data.tiktok_detail || (data.tiktok_username ? `@${data.tiktok_username}` : 'Akun belum dihubungkan');
    $('#tiktok-dot').classList.toggle('connected', status === 'connected');
    $('#tiktok-dot').classList.toggle('muted', status !== 'connected');
    $('#tiktok-chip').textContent = status === 'connected' ? 'LIVE CONNECTED' : status === 'connecting' ? 'CONNECTING' : status === 'error' ? 'CHECK CONNECTION' : 'LIVE LISTENER';
    if (data.tiktok_username && !$('#tiktok-username').value) $('#tiktok-username').value = data.tiktok_username;
    if (data.tiktok_last_error) $('#tiktok-note').textContent = `Error: ${data.tiktok_last_error}`;
    else if (data.tiktok_detail && status !== 'disconnected') $('#tiktok-note').textContent = data.tiktok_detail;
    $('#gift-count').textContent = `${String(data.gift_count ?? gifts.length).padStart(2,'0')} active rules`;
    renderTikTokEvents(data.tiktok_recent_gifts);
  } catch { setConnection(false, 'Server dashboard tidak terjangkau'); }
}
function updateClock() { $('#clock').textContent = new Date().toLocaleTimeString('id-ID',{hour12:false}); }

async function initialize() {
  try {
    await loadConfig();
    await loadGifts();
    await loadGiftCatalog();
  } catch (err) { toast(err.message); }
  await refreshStatus();
  updateClock();
  if (!window.voxelPollingStarted) {
    window.voxelPollingStarted = true;
    setInterval(refreshStatus, 2000);
    setInterval(updateClock, 1000);
  }
}

function showAuthMessage(message, isError = false) {
  const el = $('#auth-message');
  el.textContent = message || '';
  el.classList.toggle('show', Boolean(message));
  el.classList.toggle('error', Boolean(isError));
}
function showAccessForm() {
  $('#access-form').hidden = false;
  $('#auth-title').innerHTML = 'Unlock <em>access.</em>';
  $('#auth-description').textContent = 'Masukkan nama dan Redeem Code. Admin akan meninjau permintaanmu di VoxelCloud.';
}
function setAuthenticated(user) {
  document.body.classList.add('authenticated');
  $('#account-email').textContent = user?.name || 'VoxelLive account';
  $('#account-email').title = user?.name || '';
}
let accessPollTimer = null;
async function pollAccessStatus(token, showPending = true) {
  try {
    const {response, data} = await post('/api/access/status', {request_token:token});
    if (!response.ok || !data.ok) throw new Error(data.message || 'Tidak bisa memeriksa status permintaan.');
    if (data.status === 'approved' && data.token && data.user) {
      clearInterval(accessPollTimer); accessPollTimer = null;
      localStorage.removeItem('voxellive_pending_request_token');
      localStorage.setItem('voxellive_access_token', data.token);
      setAuthenticated(data.user);
      showAuthMessage('');
      await initialize();
      return 'approved';
    }
    if (data.status === 'rejected' || data.status === 'expired') {
      clearInterval(accessPollTimer); accessPollTimer = null;
      localStorage.removeItem('voxellive_pending_request_token');
      showAccessForm();
      showAuthMessage(data.message || 'Permintaan akses tidak disetujui.', true);
      return data.status;
    }
    if (showPending) showAuthMessage(`${data.message || 'Permintaan menunggu persetujuan admin.'}\nStatus diperiksa otomatis setiap 4 detik.`);
    return 'pending';
  } catch (err) {
    if (showPending) showAuthMessage(`${err.message}\nVoxelLive akan mencoba kembali otomatis.`, true);
    return 'error';
  }
}
function startAccessPolling(token) {
  if (accessPollTimer) clearInterval(accessPollTimer);
  pollAccessStatus(token);
  accessPollTimer = setInterval(() => pollAccessStatus(token), 4000);
}
$('#access-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#access-submit');
  button.disabled = true;
  showAuthMessage('Mengirim permintaan akses ke VoxelCloud…');
  try {
    const {response, data} = await post('/api/access/request', {
      name:$('#access-name').value.trim(), code:$('#access-code').value.trim().toUpperCase()
    });
    if (!response.ok || !data.ok || !data.request_token) throw new Error(data.message || 'Permintaan akses gagal.');
    localStorage.setItem('voxellive_pending_request_token', data.request_token);
    showAuthMessage(`${data.message || 'Permintaan dikirim.'}\nNama: ${$('#access-name').value.trim()}\nStatus akan diperiksa otomatis.`);
    $('#access-code').value = '';
    startAccessPolling(data.request_token);
  } catch (err) { showAuthMessage(err.message, true); }
  finally { button.disabled = false; }
});
$('#logout-button').addEventListener('click', async () => {
  clearInterval(accessPollTimer); accessPollTimer = null;
  try { await post('/api/auth/logout', {}); } catch (err) { console.warn('Logout API:', err); }
  localStorage.removeItem('voxellive_access_token');
  localStorage.removeItem('voxellive_pending_request_token');
  document.body.classList.remove('authenticated');
  showAccessForm();
  showAuthMessage('Kamu sudah keluar dari VoxelLive.');
});

async function bootstrapAuth() {
  showAccessForm();
  const token = localStorage.getItem('voxellive_access_token');
  if (token) {
    try {
      const {response, data} = await api('/api/auth/me');
      if (response.ok && data.ok && data.user) {
        setAuthenticated(data.user);
        await initialize();
        return;
      }
      localStorage.removeItem('voxellive_access_token');
    } catch (err) {
      showAuthMessage(`Tidak bisa memeriksa sesi: ${err.message}`, true);
      return;
    }
  }
  const pendingToken = localStorage.getItem('voxellive_pending_request_token');
  if (pendingToken) startAccessPolling(pendingToken);
}
bootstrapAuth();
