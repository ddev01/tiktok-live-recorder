(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.detail || data?.message || data?.error || `Request failed (${response.status})`);
    return data;
  }

  const listFrom = (data, key) => Array.isArray(data) ? data : (data?.[key] || []);
  const formatSize = (bytes) => {
    if (bytes == null || bytes === '') return '—';
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`;
  };
  const formatDate = (value) => {
    if (!value) return '—';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  };
  const formatDuration = (seconds) => {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '—';
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secs = Math.floor(value % 60);
    return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${secs}s` : `${secs}s`;
  };
  const statusClass = (status) => ({
    active: 'badge-success', recording: 'badge-success', completed: 'badge-neutral',
    stopped: 'badge-neutral', interrupted: 'badge-error', failed: 'badge-error', error: 'badge-error',
  })[String(status || '').toLowerCase()] || 'badge-neutral';
  const recordId = (record) => record.id ?? record.recording_id;
  const recordStatus = (record) => record.status || (record.is_active ? 'recording' : 'completed');
  const isActive = (record) => record.is_active === true || ['starting', 'active', 'recording', 'stopping'].includes(String(record.status).toLowerCase());

  function setupTheme() {
    const toggle = $('[data-theme-toggle]');
    if (!toggle) return;
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const stored = localStorage.getItem('vodder-theme');
    const apply = (theme) => {
      const dark = theme === 'dark';
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      toggle.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
      toggle.setAttribute('aria-pressed', String(dark));
      $$('[data-theme-icon]', toggle).forEach((icon) => {
        icon.hidden = icon.dataset.themeIcon !== (dark ? 'dark' : 'light');
      });
    };
    apply(stored || (media.matches ? 'dark' : 'light'));
    toggle.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      localStorage.setItem('vodder-theme', next);
      apply(next);
    });
    media.addEventListener?.('change', (event) => {
      if (!localStorage.getItem('vodder-theme')) apply(event.matches ? 'dark' : 'light');
    });
  }

  function setupSearch() {
    const form = $('[data-search-form]');
    if (!form) return;
    const isMac = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || '');
    const modKbd = isMac ? '⌘ K' : 'Ctrl+K';
    $$('[data-mod-key]').forEach((el) => {
      el.textContent = modKbd;
    });
    window.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        const field = $('[data-search-input]');
        if (!field) return;
        event.preventDefault();
        field.focus();
      }
      if (event.key === 'Escape') document.activeElement?.blur?.();
    });
    const input = $('[data-search-input]', form);
    const suggestions = $('[data-suggestions]');
    const feedback = $('[data-home-feedback]');
    const offlineActions = $('[data-offline-actions]');
    let timer;
    let request = 0;
    let currentUsername = '';

    const showFeedback = (message, error = false) => {
      feedback.textContent = message;
      feedback.hidden = !message;
      feedback.classList.toggle('alert-error', error);
      offlineActions.hidden = true;
    };
    const normalizedQuery = () => input.value.trim();
    const renderSuggestions = (recent = [], results = []) => {
      const parts = [];
      if (results.length) {
        parts.push('<p class="suggestion-heading">matches</p>');
        parts.push(results.map((item) => {
          const username = item.username || '';
          const name = item.display_name || username;
          const live = item.is_live ? '<span class="badge badge-success">live</span>' : '';
          const avatar = item.avatar_url
            ? `<img class="avatar" src="${escapeHTML(item.avatar_url)}" alt="" loading="lazy">`
            : `<span class="avatar avatar-fallback" aria-hidden="true">${escapeHTML((name[0] || '@').toUpperCase())}</span>`;
          return `<button class="suggestion-item" type="button" data-query="${escapeHTML(username)}">${avatar}<span class="suggestion-name"><strong>${escapeHTML(name)}</strong><small>@${escapeHTML(username)}</small></span>${live}<span class="suggestion-arrow" aria-hidden="true">↗</span></button>`;
        }).join(''));
      } else if (normalizedQuery()) {
        parts.push('<p class="suggestion-empty">no matches</p>');
      }
      if (!normalizedQuery() && recent.length) {
        parts.push('<p class="suggestion-heading">recent</p>');
        parts.push(recent.slice(0, 10).map((username) => `<button class="suggestion-item recent-item" type="button" data-query="${escapeHTML(username)}"><span class="recent-clock" aria-hidden="true">↺</span><span class="suggestion-name"><strong>@${escapeHTML(username)}</strong></span><span class="suggestion-arrow" aria-hidden="true">↗</span></button>`).join(''));
      }
      suggestions.innerHTML = parts.join('');
      suggestions.hidden = !parts.length;
      $$('[data-query]', suggestions).forEach((button) => button.addEventListener('click', () => {
        input.value = button.dataset.query;
        suggestions.hidden = true;
        startRecording(input.value);
      }));
    };

    async function search(query) {
      const id = ++request;
      const params = new URLSearchParams();
      params.set('q', query);
      try {
        const result = await api(`/api/search?${params}`);
        if (id !== request) return;
        renderSuggestions(result?.recent || [], result?.results || []);
      } catch {
        if (id === request) suggestions.hidden = true;
      }
    }

    async function startRecording(query, durationSeconds = 1800) {
      if (!query.trim()) { input.focus(); return; }
      currentUsername = query.trim();
      suggestions.hidden = true;
      showFeedback('Checking…');
      const submit = $('[data-search-submit]', form) || $('[type="submit"]', form);
      const submitLabel = submit ? $('[data-submit-label]', submit) : null;
      if (submit) submit.disabled = true;
      if (submitLabel) submitLabel.textContent = '…';
      try {
        const result = await api('/api/recordings', {
          method: 'POST', body: JSON.stringify({ query: currentUsername, duration_seconds: durationSeconds }),
        });
        if (result?.status === 'offline') {
          currentUsername = result.username || currentUsername;
          showFeedback(`@${currentUsername} offline`);
          offlineActions.hidden = false;
          return;
        }
        const id = recordId(result?.recording || result) ?? result?.recording_id;
        if (id !== undefined && id !== null) window.location.assign(`/recordings/${encodeURIComponent(id)}`);
        else showFeedback('Started (no id)', true);
      } catch (error) {
        showFeedback(error.message || 'Start failed', true);
      } finally {
        if (submit) submit.disabled = false;
        if (submitLabel) submitLabel.textContent = 'Start';
      }
    }

    input.addEventListener('focus', () => search(normalizedQuery()));
    input.addEventListener('input', () => {
      clearTimeout(timer);
      feedback.hidden = true;
      offlineActions.hidden = true;
      timer = setTimeout(() => search(normalizedQuery()), 180);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') suggestions.hidden = true;
    });
    form.addEventListener('submit', (event) => { event.preventDefault(); startRecording(normalizedQuery()); });
    $('[data-test-recording]')?.addEventListener('click', () => startRecording(normalizedQuery(), 30));
    document.addEventListener('click', (event) => {
      if (!form.contains(event.target)) suggestions.hidden = true;
    });
    $$('[data-watch-mode]').forEach((button) => button.addEventListener('click', async () => {
      const mode = button.dataset.watchMode;
      button.disabled = true;
      try {
        await api('/api/watchlist', { method: 'POST', body: JSON.stringify({ username: currentUsername.replace(/^@/, ''), mode }) });
        showFeedback(mode === 'once' ? 'Queued: next live' : 'Watchlist');
        offlineActions.hidden = true;
      } catch (error) {
        showFeedback(error.message || 'Watchlist failed', true);
      } finally { button.disabled = false; }
    }));
  }

  function setupDashboard() {
    const body = $('[data-recordings-body]');
    if (!body) return;
    const mobile = $('[data-recordings-mobile]');
    const showError = (element, message, columns = 1) => {
      element.innerHTML = `<tr><td colspan="${columns}" class="empty-state">${escapeHTML(message)}</td></tr>`;
    };
    const renderRecord = (record, compact = false) => {
      const id = recordId(record);
      const username = record.username || record.display_name || 'Recording';
      const status = recordStatus(record);
      const date = record.started_at || record.created_at || record.date;
      const size = record.file_size_bytes ?? record.size_bytes;
      const pinned = record.keep_forever ?? record.pinned ?? false;
      const playable = ['completed', 'stopped', 'interrupted'].includes(String(status).toLowerCase()) && record.file_name && !record.file_name.endsWith('_flv.mp4');
      const keepForever = `<label class="keep-toggle"><input type="checkbox" data-keep-record="${escapeHTML(id ?? '')}"${id == null ? ' disabled' : ''}${pinned ? ' checked' : ''} aria-label="Pin ${escapeHTML(username)}"><span>Pin</span></label>`;
      const actions = id == null ? '' : `<div class="row-actions"><a class="text-link" href="/recordings/${encodeURIComponent(id)}">Open</a>${playable ? `<a class="icon-link" href="/recordings/${encodeURIComponent(id)}/video" aria-label="Play ${escapeHTML(username)}">▶</a><a class="icon-link" href="/recordings/${encodeURIComponent(id)}/download" aria-label="Download ${escapeHTML(username)}">↓</a>` : ''}<button class="icon-link delete-record" type="button" data-delete-record="${escapeHTML(id)}" aria-label="Delete ${escapeHTML(username)}">×</button></div>`;
      if (compact) return `<article class="record-card card"><div class="record-card-top"><strong>@${escapeHTML(username)}</strong><span class="badge ${statusClass(status)}">${escapeHTML(status)}</span></div><div class="record-card-meta"><span>${escapeHTML(formatDate(date))}</span><span>${escapeHTML(formatSize(size))}</span></div><div class="record-card-bottom">${keepForever}${actions}</div></article>`;
      return `<tr><td><a class="creator-link" href="/recordings/${encodeURIComponent(id)}">@${escapeHTML(username)}</a></td><td><span class="badge ${statusClass(status)}">${escapeHTML(status)}</span></td><td>${escapeHTML(formatDate(date))}</td><td>${escapeHTML(formatSize(size))}</td><td><div class="table-actions">${keepForever}${actions}</div></td></tr>`;
    };
    const attachDelete = (root) => $$('[data-delete-record]', root).forEach((button) => button.addEventListener('click', async () => {
      if (!window.confirm('Delete?')) return;
      button.disabled = true;
      try { await api(`/api/recordings/${encodeURIComponent(button.dataset.deleteRecord)}`, { method: 'DELETE' }); loadRecordings(); }
      catch (error) { window.alert(error.message || 'Could not delete recording.'); button.disabled = false; }
    }));
    const attachKeepForever = (root) => $$('[data-keep-record]', root).forEach((checkbox) => checkbox.addEventListener('change', async () => {
      const keepForever = checkbox.checked;
      checkbox.disabled = true;
      try {
        await api(`/api/recordings/${encodeURIComponent(checkbox.dataset.keepRecord)}`, {
          method: 'PATCH', body: JSON.stringify({ keep_forever: keepForever }),
        });
        await loadRecordings();
      } catch (error) {
        checkbox.checked = !keepForever;
        window.alert(error.message || 'Could not update keep-forever setting.');
        checkbox.disabled = false;
      }
    }));
    async function loadRecordings() {
      try {
        const records = listFrom(await api('/api/recordings'), 'recordings');
        $('[data-recording-count]').textContent = records.length;
        if (!records.length) {
          showError(body, 'No recordings yet. Start one to see it here.', 5);
          mobile.innerHTML = '<div class="empty-card">—</div>';
        } else {
          body.innerHTML = records.map((record) => renderRecord(record)).join('');
          mobile.innerHTML = records.map((record) => renderRecord(record, true)).join('');
          attachDelete(body); attachDelete(mobile);
          attachKeepForever(body); attachKeepForever(mobile);
        }
      } catch (error) {
        showError(body, error.message || 'Load failed', 5);
        mobile.innerHTML = `<div class="empty-card">${escapeHTML(error.message || 'Load failed')}</div>`;
      }
    }
    async function loadWatchlist() {
      const container = $('[data-watchlist-list]');
      try {
        const entries = listFrom(await api('/api/watchlist'), 'watchlist');
        $('[data-watchlist-count]').textContent = entries.length;
        container.innerHTML = entries.length ? entries.map((item) => {
          const username = typeof item === 'string' ? item : item.username;
          const mode = typeof item === 'string' ? 'always' : (item.mode || 'always');
          const maxDuration = Number(item.max_duration_seconds || 14400);
          const durationOptions = [[900, '15m'], [1800, '30m'], [3600, '1h'], [14400, '4h'], [28800, '8h']];
          const maxControl = mode === 'always' ? `<label class="watch-max">max<select data-watch-max="${escapeHTML(username)}" data-previous="${maxDuration}" aria-label="Max duration @${escapeHTML(username)}">${durationOptions.map(([seconds, label]) => `<option value="${seconds}"${maxDuration === seconds ? ' selected' : ''}>${label}</option>`).join('')}</select></label>` : '';
          return `<article class="watch-card"><div class="watch-avatar" aria-hidden="true">${escapeHTML((username || '@')[0].toUpperCase())}</div><div class="watch-info"><strong>@${escapeHTML(username)}</strong><span>${mode === 'always' ? 'watch' : 'once'}</span></div>${maxControl}<span class="badge badge-neutral">${mode === 'always' ? 'watch' : 'once'}</span><button class="icon-link" type="button" data-remove-watch="${escapeHTML(username)}" aria-label="Remove @${escapeHTML(username)}">×</button></article>`;
        }).join('') : '<div class="empty-card">—</div>';
        $$('[data-watch-max]', container).forEach((select) => select.addEventListener('change', async () => {
          const previous = select.dataset.previous || '14400';
          select.disabled = true;
          try {
            await api('/api/watchlist', {
              method: 'POST',
              body: JSON.stringify({ username: select.dataset.watchMax, mode: 'always', max_duration_seconds: Number(select.value) }),
            });
            loadWatchlist();
          } catch (error) {
            select.value = previous;
            select.disabled = false;
            window.alert(error.message || 'Update failed');
          }
        }));
        $$('[data-remove-watch]', container).forEach((button) => button.addEventListener('click', async () => {
          button.disabled = true;
          try { await api(`/api/watchlist?username=${encodeURIComponent(button.dataset.removeWatch)}`, { method: 'DELETE' }); loadWatchlist(); }
          catch (error) { window.alert(error.message || 'Remove failed'); button.disabled = false; }
        }));
      } catch (error) { container.innerHTML = `<div class="empty-card">${escapeHTML(error.message || 'Load failed')}</div>`; }
    }
    async function loadSystem() {
      try {
        const system = await api('/api/system');
        const free = Number(system?.free_space_bytes ?? system?.free_bytes ?? system?.disk?.free_bytes);
        const lowStatus = /low|critical|full|warning/i.test(String(system?.status || system?.disk?.status || ''));
        if (lowStatus || (Number.isFinite(free) && free < 5 * 1024 ** 3)) {
          $('[data-storage-message]').textContent = Number.isFinite(free) ? `${formatSize(free)} free` : '';
          $('[data-storage-warning]').hidden = false;
        }
      } catch { /* System status is optional if the endpoint is unavailable. */ }
    }
    loadRecordings(); loadWatchlist(); loadSystem();
  }

  function setupRecording() {
    const root = $('[data-recording-detail]');
    if (!root) return;
    const id = root.dataset.recordingId || decodeURIComponent(location.pathname.split('/').filter(Boolean).pop() || '');
    const feedback = $('[data-detail-feedback]');
    const showFeedback = (message, error = false) => {
      feedback.textContent = message;
      feedback.hidden = !message;
      feedback.classList.toggle('alert-error', error);
    };
    let recording;
    let refreshTimer;
    const spaceWarning = $('[data-recording-space-warning]');
    async function load() {
      try {
        recording = await api(`/api/recordings/${encodeURIComponent(id)}`);
        recording = recording?.recording || recording;
        const username = recording.username || recording.display_name || 'Recording';
        const status = recordStatus(recording);
        $('[data-recording-title]').textContent = `@${username}`;
        $('[data-recording-subtitle]').textContent = recording.started_at ? `Started ${formatDate(recording.started_at)}` : formatDate(recording.created_at);
        const badge = $('[data-recording-status]');
        badge.textContent = status;
        badge.className = `badge status-badge ${statusClass(status)}`;
        $('[data-recording-time]').textContent = formatDuration(recording.elapsed_seconds ?? recording.duration_elapsed_seconds);
        $('[data-recording-size]').textContent = formatSize(recording.file_size_bytes ?? recording.size_bytes);
        $('[data-recording-estimate]').textContent = formatSize(recording.estimated_size_bytes ?? recording.estimated_total_bytes);
        spaceWarning.textContent = recording.low_space_warning || '';
        spaceWarning.hidden = !recording.low_space_warning;
        const active = isActive(recording);
        const playable = ['completed', 'stopped', 'interrupted'].includes(String(status).toLowerCase()) && recording.file_name && !recording.file_name.endsWith('_flv.mp4');
        $$('[data-duration]').forEach((button) => {
          const duration = button.dataset.duration === '' ? null : Number(button.dataset.duration);
          const selected = (recording.duration_seconds ?? null) === duration;
          button.classList.toggle('is-selected', selected);
          button.setAttribute('aria-pressed', String(selected));
          button.disabled = !active;
        });
        const play = $('[data-play-link]'); const download = $('[data-download-link]'); const stop = $('[data-stop-recording]');
        play.href = `/recordings/${encodeURIComponent(id)}/video`; download.href = `/recordings/${encodeURIComponent(id)}/download`;
        if (recording.error) showFeedback(recording.error, true);
        if (status === 'interrupted') {
          play.textContent = 'Play';
          download.textContent = 'Download';
          if (!recording.error) showFeedback('Interrupted (partial saved)', true);
        }
        play.hidden = !playable; download.hidden = !playable; stop.hidden = !active;
        if (active && !refreshTimer) refreshTimer = setInterval(load, 3000);
        else if (!active && refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
      } catch (error) {
        $('[data-recording-title]').textContent = 'Unavailable';
        $('[data-recording-subtitle]').textContent = '';
        showFeedback(error.message || 'Load failed', true);
      }
    }
    $$('[data-duration]').forEach((button) => button.addEventListener('click', async () => {
      const value = button.dataset.duration === '' ? null : Number(button.dataset.duration);
      button.disabled = true;
      try {
        await api(`/api/recordings/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ duration_seconds: value }) });
        showFeedback(value === null ? 'Until stream end' : formatDuration(value));
        await load();
      } catch (error) { showFeedback(error.message || 'Update failed', true); button.disabled = false; }
    }));
    $('[data-stop-recording]').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      if (!window.confirm('Stop?')) return;
      button.disabled = true;
      try { await api(`/api/recordings/${encodeURIComponent(id)}/stop`, { method: 'POST' }); showFeedback('Stopped'); await load(); }
      catch (error) { showFeedback(error.message || 'Stop failed', true); button.disabled = false; }
    });
    load();
  }

  setupTheme();
  setupSearch();
  setupDashboard();
  setupRecording();
})();
