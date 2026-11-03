'use strict';
const $ = id => document.getElementById(id);
const selected = new Map();
let allDates = [], offset = 0, galleryVersion = 0, lastDay = '', currentPrepared = [], previewIndex = 0, requestId = '', previewVersion = 0, printerOnline = false, statusTimer = null, lastLibraryCount = -1, preparing = false;
const labels = {pending:'待发送',sending:'发送中',submitted:'等待出片',completed:'已完成',failed:'未完成',unknown:'需要核对',acknowledged:'已核对',cancelled:'已取消'};
function esc(value) { return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 4500); }
function uuid() { if (crypto.randomUUID) return crypto.randomUUID(); return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c => (Number(c) ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> Number(c) / 4).toString(16)); }
async function api(path, body) {
  let response;
  try { response = await fetch('/api/' + path, {method:body === undefined ? 'GET':'POST', headers:body === undefined ? {}:{'Content-Type':'application/json','X-Photo-Request':'1'}, body:body === undefined ? undefined:JSON.stringify(body), credentials:'same-origin'}); }
  catch (error) { throw new Error('连接中断，请稍后重试。若刚刚提交打印，请先检查打印记录。'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== 'login') showLogin();
    throw new Error(typeof data.detail === 'string' ? data.detail : '操作未完成，请稍后重试');
  }
  return data;
}
function showLogin() { clearInterval(statusTimer); statusTimer = null; $('login').hidden = false; $('application').hidden = true; if ($('preview-dialog').open) $('preview-dialog').close(); if ($('settings-dialog').open) {$('settings-dialog').close(); $('password-form').reset();} selected.clear(); $('gallery').replaceChildren(); $('jobs-list').replaceChildren(); updateDock(); }
async function start() {
  const status = await api('status');
  $('password').value = ''; $('login').hidden = true; $('application').hidden = false;
  updateStatus(status); await loadDates(); await loadPhotos(true);
  clearInterval(statusTimer); statusTimer = setInterval(poll, 10000);
}
function updateStatus(data) {
  printerOnline = data.printer.online && data.printer.accepting;
  $('printer-name').textContent = data.printer.name || '照片打印机';
  $('printer-dot').classList.toggle('online', !!printerOnline); $('printer-message').textContent = data.printer.message;
  $('library-count').textContent = data.library.count.toLocaleString() + ' 张';
  let message = data.library.running ? '正在检查待打印文件夹…' : data.library.error || ('待打印 ' + data.library.count + ' 张 · 已归档 ' + data.library.archived + ' 张。上传照片后，可点击刷新列表。');
  $('library-message').textContent = message;
  $('refresh').disabled = data.library.running;
  if (lastLibraryCount >= 0 && lastLibraryCount !== data.library.count) { loadDates().then(() => loadPhotos(true)).catch(() => {}); }
  lastLibraryCount = data.library.count;
  updatePrintButton();
}
async function poll() { try { updateStatus(await api('status')); if (!$('jobs-view').hidden) await loadJobs(); } catch (error) { if (!$('application').hidden) $('printer-message').textContent = '服务暂时无法连接'; } }
async function loadDates() {
  allDates = await api('dates');
  const months = [...new Set(allDates.filter(d => /^\d{4}\/\d{2}\/\d{2}$/.test(d.day)).map(d => d.day.slice(0,7)))];
  const current = $('month-filter').value;
  $('month-filter').innerHTML = '<option value="">最近照片</option>' + months.map(m => `<option value="${m}">${m.replace('/', ' 年 ')} 月</option>`).join('');
  $('month-filter').value = months.includes(current) ? current : '';
  if (allDates.length) { const dates = allDates.map(d => d.day).filter(d => /^\d{4}\/\d{2}\/\d{2}$/.test(d)); if (dates.length) { $('day-filter').max = dates[0].replaceAll('/','-'); $('day-filter').min = dates[dates.length-1].replaceAll('/','-'); } }
}
async function loadPhotos(reset = false) {
  if (reset) { offset = 0; lastDay = ''; galleryVersion++; $('gallery').replaceChildren(); $('gallery-end').textContent = '正在打开照片…'; }
  const version = galleryVersion;
  $('load-more').disabled = true;
  const params = new URLSearchParams({offset:String(offset),limit:'48',day:$('day-filter').value.replaceAll('-','/'),month:$('month-filter').value});
  try {
    const data = await api('photos?' + params);
    if (version !== galleryVersion) return;
    for (const photo of data.photos) {
      if (photo.day !== lastDay) { const heading = document.createElement('div'); heading.className = 'date-divider'; heading.textContent = photo.day.replaceAll('/', '.'); $('gallery').append(heading); lastDay = photo.day; }
      const button = document.createElement('button'); button.type = 'button'; button.className = 'photo-card'; button.dataset.id = photo.id; button.setAttribute('aria-label', '选择照片 ' + photo.name); button.setAttribute('aria-pressed', String(selected.has(photo.id))); button.title = photo.name;
      button.innerHTML = `<div class="photo-wrap"><span class="image-failed" hidden>预览暂不可用</span><img src="/api/thumb/${photo.id}" alt="${esc(photo.name)}" loading="lazy" decoding="async"></div><span class="check" aria-hidden="true">✓</span><div class="photo-meta"><span>${esc(photo.day.replaceAll('/','.'))}</span><span class="photo-format">${esc(photo.format)}</span></div>`;
      if (selected.has(photo.id)) button.classList.add('selected');
      const img = button.querySelector('img'); img.addEventListener('error', () => { img.classList.add('failed'); button.querySelector('.image-failed').hidden = false; });
      button.addEventListener('click', () => {
        if (selected.has(photo.id)) selected.delete(photo.id);
        else { if (selected.size >= 10) return toast('每批最多选择 10 张照片'); selected.set(photo.id, {...photo, rotation:0,x:.5,y:.5}); }
        button.classList.toggle('selected', selected.has(photo.id)); button.setAttribute('aria-pressed', String(selected.has(photo.id))); updateDock();
      });
      $('gallery').append(button);
    }
    offset += data.photos.length;
    $('load-more').hidden = offset >= data.total;
    $('gallery-end').textContent = data.total === 0 ? '待打印文件夹中还没有照片。请先把照片上传到已配置的「待打印」文件夹，再点击「刷新列表」。' : offset >= data.total ? '打印机报告完成后，照片会自动移至「已完成」文件夹。' : `已展示 ${offset} / ${data.total.toLocaleString()} 张`;
  } catch (error) { $('gallery-end').textContent = error.message; }
  finally { $('load-more').disabled = false; }
}
function updateDock() { $('selection-dock').hidden = selected.size === 0 || !$('jobs-view').hidden; $('selected-count').textContent = selected.size; }
function clearSelection() { selected.clear(); document.querySelectorAll('.photo-card.selected').forEach(b => {b.classList.remove('selected'); b.setAttribute('aria-pressed','false');}); updateDock(); }
async function generatePreview() {
  const version = ++previewVersion; preparing = true; currentPrepared = []; $('preview-loading').hidden = false; $('preview-image').hidden = true; $('print-error').textContent = ''; updatePrintButton();
  try {
    const data = await api('prepare', {photos:[...selected.values()].map(p => ({id:p.id,rotation:p.rotation,x:p.x,y:p.y})), fit:document.querySelector('input[name=fit]:checked').value,paper:$('paper-size').value,layout:$('layout').value,border_mm:Number($('border').value),gap_mm:Number($('gap').value),background:$('background').value});
    if (version !== previewVersion || !$('preview-dialog').open) return;
    currentPrepared = data.prepared; requestId = uuid(); $('paper-description').textContent = data.paper + ' · 300 dpi'; previewIndex = Math.min(previewIndex, currentPrepared.length - 1); showPreview();
  } catch (error) { if (version === previewVersion) $('print-error').textContent = error.message; }
  finally { if (version === previewVersion) { preparing = false; $('preview-loading').hidden = true; updatePrintButton(); } }
}
function showPreview() {
  if (!currentPrepared.length) return;
  const p = currentPrepared[previewIndex]; $('preview-image').src = '/api/preview/' + p.id; $('preview-image').hidden = false;
  $('preview-position').textContent = `${previewIndex + 1} / ${currentPrepared.length}`;
  $('previous-preview').disabled = previewIndex === 0; $('next-preview').disabled = previewIndex === currentPrepared.length - 1;
  $('resolution-note').hidden = !p.low_resolution;
  const editing = $('edit-photo').value;
  $('edit-photo').innerHTML = p.photo_ids.map(id => `<option value="${id}">${esc(selected.get(id).name)}</option>`).join('');
  if (p.photo_ids.includes(editing)) $('edit-photo').value = editing;
  updateCropControls();
}
function updateCropControls() {
  const photo = selected.get($('edit-photo').value);
  if (!photo) return;
  $('crop-x').value = Math.round(photo.x * 100); $('crop-y').value = Math.round(photo.y * 100);
  $('crop-controls').hidden = document.querySelector('input[name=fit]:checked').value !== 'cover';
}
function updatePrintButton() {
  const capacities = {single:1,duo:2,grid4:4,grid9:9,square3:2};
  const pages = currentPrepared.length || Math.ceil(selected.size / capacities[$('layout').value]);
  const total = pages * Number($('copies').value); $('total-sheets').textContent = total;
  $('confirm-print').disabled = preparing || !currentPrepared.length || !printerOnline || total > 10;
  $('confirm-print').textContent = total > 10 ? '每批最多打印 10 张' : !printerOnline ? '等待打印机就绪' : `确认打印 · ${total} 张`;
  $('rotate-photo').disabled = preparing; document.querySelectorAll('input[name=fit],#paper-size,#layout,#border,#gap,#background,#edit-photo,#crop-x,#crop-y').forEach(i => i.disabled = preparing);
}
async function submitPrint() {
  $('confirm-print').disabled = true; $('confirm-print').textContent = '正在提交，请稍候…'; $('print-error').textContent = '';
  try {
    await api('print', {request_id:requestId,prepared_ids:currentPrepared.map(p => p.id),copies:Number($('copies').value),confirmed:true});
    $('preview-dialog').close(); clearSelection(); currentPrepared = []; showTab('jobs'); toast('打印任务已提交，请等待出片。'); await loadJobs();
  } catch (error) { $('print-error').textContent = error.message; updatePrintButton(); }
}
function showTab(tab) { const gallery = tab === 'gallery'; $('gallery-view').hidden = !gallery; $('jobs-view').hidden = gallery; $('gallery-tab').classList.toggle('active',gallery); $('jobs-tab').classList.toggle('active',!gallery); updateDock(); }
async function loadJobs() {
  const jobs = await api('jobs'); $('jobs-list').replaceChildren();
  const active = jobs.filter(j => ['pending','sending','submitted','unknown'].includes(j.status)).length; $('jobs-badge').textContent = active || '';
  if (!jobs.length) { $('jobs-list').innerHTML = '<div class="empty"><img src="/icon.svg" alt="">还没有打印记录。<br>先上传照片到「待打印」文件夹，再选片打印。</div>'; return; }
  for (const job of jobs) {
    const card = document.createElement('div'); card.className = 'job-card' + (job.status === 'unknown' ? ' problem' : '');
    card.innerHTML = `<span class="job-status">${esc(labels[job.status] || job.status)}</span><div class="job-text"><strong>${esc(job.message)}</strong><small>${esc(new Date(job.created * 1000).toLocaleString('zh-CN', {hour12:false}))}</small></div>`;
    if (['pending','unknown'].includes(job.status)) {
      const button = document.createElement('button'); button.className = 'secondary'; button.textContent = job.status === 'pending' ? '取消这张' : '已核对，结束本批';
      button.addEventListener('click', async () => {
        if (job.status === 'unknown' && !confirm('请先核对相纸是否已打印、打印机队列是否清空。结束本批不会重发照片，并会取消本批尚未发送的照片。')) return;
        button.disabled = true;
        try { await api(`jobs/${job.id}/resolve`, {action:job.status === 'pending'?'cancel':'acknowledge'}); await loadJobs(); } catch(error) {toast(error.message); button.disabled = false;}
      }); card.append(button);
    }
    $('jobs-list').append(card);
  }
}
$('login-form').addEventListener('submit', async event => { event.preventDefault(); $('login-button').disabled = true; $('login-error').textContent = ''; try { await api('login', {password:$('password').value}); await start(); } catch (error) { $('login-error').textContent = error.message; } finally { $('login-button').disabled = false; } });
$('logout').addEventListener('click', async () => {try {await api('logout',{});} finally {showLogin();}});
$('settings').addEventListener('click', () => {$('password-form').reset(); $('password-error').textContent=''; $('settings-dialog').showModal();});
$('close-settings').addEventListener('click', () => {$('settings-dialog').close(); $('password-form').reset();});
$('settings-dialog').addEventListener('cancel', () => $('password-form').reset());
$('password-form').addEventListener('submit', async event => {
  event.preventDefault(); $('password-error').textContent='';
  if ($('new-password').value !== $('confirm-password').value) {$('password-error').textContent='两次输入的新密码不一致'; return;}
  $('save-password').disabled=true;
  try {
    await api('password',{current_password:$('current-password').value,new_password:$('new-password').value,confirm_password:$('confirm-password').value});
    $('password-form').reset(); $('settings-dialog').close(); toast('密码已修改，其他设备需要重新登录。');
  } catch(error) {$('password-error').textContent=error.message;}
  finally {$('save-password').disabled=false;}
});
$('month-filter').addEventListener('change', () => {$('day-filter').value = ''; loadPhotos(true);});
$('day-filter').addEventListener('change', () => {if ($('day-filter').value) $('month-filter').value = $('day-filter').value.slice(0,7).replace('-','/'); loadPhotos(true);});
$('clear-filter').addEventListener('click', () => {$('day-filter').value = ''; $('month-filter').value = ''; loadPhotos(true);});
$('load-more').addEventListener('click', () => loadPhotos());
$('refresh').addEventListener('click', async () => {try {await api('refresh',{}); toast('正在检查待打印文件夹。打印完成的照片会自动归档。'); await poll(); setTimeout(async () => {await loadDates(); await loadPhotos(true);},3000);} catch(error) {toast(error.message);}});
$('clear-selection').addEventListener('click', clearSelection);
$('open-preview').addEventListener('click', () => {previewIndex = 0; $('preview-dialog').showModal(); generatePreview();});
$('close-preview').addEventListener('click', () => {$('preview-dialog').close(); previewVersion++; preparing = false;});
$('preview-dialog').addEventListener('cancel', () => {previewVersion++; preparing = false;});
document.querySelectorAll('input[name=fit]').forEach(input => input.addEventListener('change', generatePreview));
$('rotate-photo').addEventListener('click', () => {if (preparing || !currentPrepared.length) return; const p = selected.get($('edit-photo').value); p.rotation = (p.rotation + 90) % 360; generatePreview();});
$('edit-photo').addEventListener('change', updateCropControls);
for (const id of ['crop-x','crop-y']) $(id).addEventListener('change', () => {const p = selected.get($('edit-photo').value); p.x = Number($('crop-x').value)/100; p.y = Number($('crop-y').value)/100; generatePreview();});
for (const id of ['paper-size','layout','border','gap','background']) $(id).addEventListener('change', () => {
  if ($('paper-size').value !== '6inch' && $('layout').value === 'square3') $('layout').value = 'single';
  $('layout').querySelector('option[value=square3]').disabled = $('paper-size').value !== '6inch';
  const square = $('layout').value === 'square3';
  $('paper-note').textContent = square ? '将 2 张 3 × 3 英寸方形照片排在 1 张 6 寸相纸上，打印后按边缘标记裁切；未填满的位置留空。无需更换小相纸。' : '本服务当前使用 6 寸相纸。小尺寸照片通过排版后裁切获得，无需更换小相纸。';
  if (square) {$('border').value='0'; $('gap').value='0';}
  generatePreview();
});
$('previous-preview').addEventListener('click', () => {if (previewIndex > 0) {previewIndex--; showPreview();}});
$('next-preview').addEventListener('click', () => {if (previewIndex < currentPrepared.length-1) {previewIndex++; showPreview();}});
$('copies').addEventListener('change', () => {requestId = uuid(); updatePrintButton();});
$('confirm-print').addEventListener('click', submitPrint);
$('gallery-tab').addEventListener('click', () => showTab('gallery'));
$('jobs-tab').addEventListener('click', () => {showTab('jobs'); loadJobs().catch(error => toast(error.message));});
$('refresh-jobs').addEventListener('click', () => loadJobs().catch(error => toast(error.message)));
start().catch(() => showLogin());
