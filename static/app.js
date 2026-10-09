'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const COMMANDS = [
    { id: 'beeper.enable', label: 'Enable', fullLabel: 'Enable audible alarms', description: 'Enable the UPS audible alarms. This does not play a test sound.', symbol: '♪', group: 'beeper' },
    { id: 'beeper.disable', label: 'Disable', fullLabel: 'Disable audible alarms', description: 'Disable the UPS audible alarms.', symbol: '♮', group: 'beeper' },
    { id: 'beeper.mute', label: 'Mute', fullLabel: 'Temporarily mute alarms', description: 'Temporarily mute the UPS audible alarms.', symbol: '♩', group: 'beeper' },
    { id: 'beeper.off', label: 'Beeper OFF', fullLabel: 'Beeper OFF (legacy)', description: 'Deprecated command to disable audible alarms. Prefer Disable or Mute.', group: 'legacy' },
    { id: 'beeper.on', label: 'Beeper ON', fullLabel: 'Beeper ON (legacy)', description: 'Deprecated command to enable audible alarms. Prefer Enable.', group: 'legacy' },
    { id: 'load.off', label: 'Power off now', fullLabel: 'Turn output power off now', description: 'Immediately cut power to the equipment connected to the UPS. This does not gracefully shut down operating systems.', danger: true, group: 'power' },
    { id: 'load.off.delay', label: 'Delayed power off', fullLabel: 'Turn output power off after a delay', description: 'Request output shutdown using the delay configured in ups.delay.shutdown. This does not gracefully shut down operating systems.', danger: true, group: 'power' },
    { id: 'shutdown.reboot', label: 'Cycle output power', fullLabel: 'Cycle output power', description: 'Interrupt and restore power to connected equipment. This is not an operating system restart or a restart of this web panel.', danger: true, group: 'power' },
    { id: 'shutdown.stop', label: 'Cancel shutdown', fullLabel: 'Cancel a pending shutdown', description: 'Ask the UPS to stop a shutdown sequence if it can still be cancelled. This does not restore output power that is already off.', group: 'power' },
  ];
  const PARAMETERS = [
    { id: 'battery.charge.low', label: 'Low-charge threshold', description: 'Battery percentage at which the UPS reports a low-battery condition.', unit: '%', min: 1, max: 99 },
    { id: 'battery.runtime.low', label: 'Low-runtime threshold', description: 'Remaining runtime at which the UPS reports a low-battery condition.', unit: 's', min: 30, max: 7200 },
    { id: 'ups.delay.shutdown', label: 'Shutdown delay', description: 'Delay before cutting output power with the delayed shutdown command.', unit: 's', min: 0, max: 3600 },
  ];
  const commandButtons = new Map();
  const parameterEditors = new Map();
  const CONFIG_FIELDS = ['display_name', 'expected_model', 'device_ip', 'nut_host', 'nut_port', 'ups', 'expected_serial', 'nut_username', 'nut_password', 'poll_interval'];
  const outletEditors = new Map();
  const state = { authenticated: false, csrf: '', online: false, pending: false, status: null, config: null, configDirty: false, configLoading: false, outletsDirty: false, pollInterval: 15, poll: null, statusPromise: null, confirmation: null, toastTimer: null };

  class APIError extends Error {
    constructor(message, status = 0, uncertain = false) { super(message); this.status = status; this.uncertain = uncertain; }
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    const isMutation = options.method === 'POST' && path !== '/api/config/test';
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (options.method === 'POST') headers['Content-Type'] = 'application/json';
    if (options.method === 'POST' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
    try {
      const response = await fetch(path, { ...options, headers, signal: controller.signal, credentials: 'same-origin', cache: 'no-store' });
      let data;
      try { data = await response.json(); }
      catch (_) { throw new APIError(isMutation ? 'Unreadable response: command outcome is uncertain. Check the UPS status before sending it again.' : 'The server returned an unreadable response.', response.status, isMutation); }
      if (!response.ok) throw new APIError(data.error || `Request failed (HTTP ${response.status}).`, response.status);
      return data;
    } catch (error) {
      if (error instanceof APIError) throw error;
      if (isMutation) throw new APIError('Connection lost or timed out: the operation outcome is uncertain. Nothing will be retried automatically. Check the audit log and UPS status before trying again.', 0, true);
      throw new APIError('Cannot reach the server. Check your connection and try again.');
    } finally { window.clearTimeout(timeout); }
  }

  function showToast(message, error = false) {
    window.clearTimeout(state.toastTimer);
    $('toast').textContent = message;
    $('toast').classList.toggle('error', error);
    $('toast').hidden = false;
    if (!error) state.toastTimer = window.setTimeout(() => { $('toast').hidden = true; }, 9000);
  }

  function displayNumber(value, decimals = 0) {
    const number = Number(value);
    return value !== undefined && value !== null && value !== '' && Number.isFinite(number) ? number.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals }) : '—';
  }

  function timeLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function supported(kind, id) {
    const values = state.status && state.status[kind];
    return Array.isArray(values) && values.includes(id);
  }

  function updateAvailability() {
    const ready = state.authenticated && state.online && !state.pending;
    for (const [id, button] of commandButtons) {
      const available = supported('supported_commands', id);
      button.disabled = !ready || !available;
      button.title = available ? id : `Command unavailable: ${id}`;
    }
    for (const [id, editor] of parameterEditors) {
      const available = supported('supported_variables', id);
      editor.input.disabled = !ready || !available;
      editor.button.disabled = !ready || !available;
      editor.button.title = available ? `Save ${id}` : 'This parameter cannot be changed';
    }
    $('refresh-button').disabled = state.pending || Boolean(state.statusPromise);
    $('logout-button').disabled = state.pending;
    const adminReady = state.authenticated && !state.pending;
    const requiresConnection = state.confirmation && ['action', 'setting', 'beeper-test'].includes(state.confirmation.type);
    $('confirm-submit').disabled = !(requiresConnection ? ready : adminReady) || (Boolean(state.confirmation && state.confirmation.danger) && $('confirmation-input').value !== 'POWER OFF');
    $('dialog-close').disabled = state.pending;
    $('dialog-cancel').disabled = state.pending;
    $('confirmation-input').disabled = state.pending;
    $('config-fields').disabled = !adminReady || state.configLoading || !state.config;
    $('password-fields').disabled = !adminReady;
    $('outlets-fields').disabled = !adminReady || outletEditors.size === 0;
    const capability = (state.status && state.status.beeper_test) || (state.config && state.config.beeper_test);
    $('beeper-test-button').disabled = !ready || !capability || capability.supported !== true;
  }

  function clearPolling() {
    if (state.poll !== null) window.clearInterval(state.poll);
    state.poll = null;
  }

  function setPollingInterval(value) {
    const seconds = Number(value);
    if (!Number.isInteger(seconds) || seconds < 5 || seconds > 300) return;
    $('polling-label').textContent = `Refresh every ${seconds} s`;
    if (state.pollInterval === seconds && state.poll !== null) return;
    state.pollInterval = seconds;
    clearPolling();
    if (state.authenticated) state.poll = window.setInterval(() => { if (!state.pending && !document.hidden) fetchStatus(); }, seconds * 1000);
  }

  function showLogin(message = '') {
    clearPolling();
    state.authenticated = false;
    state.csrf = '';
    state.online = false;
    state.status = null;
    state.config = null;
    state.configDirty = false;
    state.outletsDirty = false;
    $('config-form').reset();
    $('password-form').reset();
    $('outlet-rows').replaceChildren();
    outletEditors.clear();
    if ($('confirm-dialog').open) $('confirm-dialog').close();
    $('app-shell').hidden = true;
    $('login-screen').hidden = false;
    $('login-error').textContent = message;
    $('password').value = '';
    document.title = 'APCc · APC Control';
    $('username').focus();
    updateAvailability();
  }

  function showApp(session) {
    state.authenticated = true;
    state.csrf = session.csrf;
    $('current-user').textContent = session.username || 'Administrator';
    $('password-username').textContent = session.username || 'admin';
    $('login-screen').hidden = true;
    $('app-shell').hidden = false;
    $('password').value = '';
    setPollingInterval(state.pollInterval);
    fetchConfig();
    fetchStatus();
  }

  function renderIdentity(data) {
    const name = data.display_name || (state.config && state.config.display_name) || 'Your UPS';
    const model = data.expected_model || data.model || (state.config && state.config.expected_model) || '—';
    for (const id of ['page-device-name', 'breadcrumb-device-name', 'sidebar-device-name']) $(id).textContent = name;
    $('device-model').textContent = model;
    $('detail-model').textContent = model;
    $('detail-device-ip').textContent = data.device_ip || 'Not set';
    if (data.endpoint) $('detail-endpoint').textContent = data.endpoint;
    else if (data.nut_host) $('detail-endpoint').textContent = `${data.ups}@${data.nut_host.includes(':') ? '[' + data.nut_host + ']' : data.nut_host}:${data.nut_port}`;
    document.title = `${name} · APCc`;
    setPollingInterval(data.poll_interval);
    const capability = data.beeper_test;
    if (capability) $('beeper-test-reason').textContent = capability.supported ? (capability.reason || 'Run the UPS panel test supported by the driver.') : (capability.reason || 'This driver does not provide an audible test. Enable allows audible alarms but does not play a test sound.');
  }

  function feedback(id, message, error = false) {
    $(id).textContent = message;
    $(id).classList.toggle('feedback-error', error);
  }

  async function fetchConfig(forceForm = false) {
    if (!state.authenticated) return;
    state.configLoading = true;
    updateAvailability();
    try {
      const config = await api('/api/config');
      if (!state.authenticated) return;
      state.config = config;
      renderIdentity(config);
      if (forceForm || !state.configDirty) {
        for (const field of CONFIG_FIELDS) $('config-form').elements.namedItem(field).value = field === 'nut_password' ? '' : (config[field] ?? '');
        state.configDirty = false;
      }
      $('nut-password-help').textContent = config.has_nut_password ? 'A password is already saved. Leave this blank to keep it. Saved passwords are never displayed.' : 'No NUT password is saved. Enter the password for your NUT account.';
      $('password-username').textContent = config.username || $('current-user').textContent;
      renderOutlets(config.outlets || []);
      if ($('config-feedback').textContent === 'Loading configuration…') feedback('config-feedback', 'Configuration loaded. Edit the fields and test your connection.');
    } catch (error) {
      if (error.status === 401) showLogin('Your session has expired. Sign in again.');
      else feedback('config-feedback', error.message, true);
    } finally { state.configLoading = false; updateAvailability(); }
  }

  function markOffline(message) {
    state.online = false;
    $('connection-pill').classList.add('offline');
    $('connection-text').textContent = 'Disconnected';
    $('status-error').hidden = false;
    $('status-error').textContent = `${message} Controls are disabled. Any visible readings are from the last successful update.`;
    $('ups-mode').textContent = 'Readings are out of date';
    updateAvailability();
  }

  async function fetchStatus(force = false) {
    if (!state.authenticated || (state.pending && !force)) return;
    if (state.statusPromise) {
      if (!force) return state.statusPromise;
      await state.statusPromise;
      if (!state.authenticated) return;
    }
    state.statusPromise = (async () => {
      try {
        const data = await api('/api/status');
        if (!state.authenticated) return;
        if (!data.variables || !Array.isArray(data.supported_commands) || !Array.isArray(data.supported_variables)) throw new APIError('The status response is incomplete.');
        state.status = data;
        state.online = true;
        renderStatus(data);
      } catch (error) {
        if (error.status === 401) showLogin('Your session has expired. Sign in again.');
        else if (state.authenticated) markOffline(error.message);
      }
    })();
    updateAvailability();
    try { await state.statusPromise; }
    finally { state.statusPromise = null; updateAvailability(); }
  }

  function renderStatus(data) {
    const v = data.variables;
    renderIdentity(data);
    $('connection-pill').classList.remove('offline');
    $('connection-text').textContent = 'UPS connected';
    $('status-error').hidden = true;
    const statuses = String(v['ups.status'] || '').split(/\s+/);
    const onBattery = statuses.includes('OB');
    const lowBattery = statuses.includes('LB');
    const online = statuses.includes('OL');
    const powerLabel = lowBattery ? 'Low battery' : onBattery ? 'On battery' : online ? 'Utility power' : String(v['ups.status'] || 'Unknown');
    $('power-status').textContent = powerLabel;
    $('power-status').classList.toggle('warning-value', onBattery || lowBattery);
    $('power-detail').textContent = `UPS status: ${v['ups.status'] || '—'}`;
    $('power-dot').hidden = !online;
    $('ups-mode').textContent = lowBattery ? 'UPS reports low battery' : onBattery ? 'UPS running on battery' : online ? 'UPS running on utility power' : 'Check UPS status';
    $('battery-charge').textContent = displayNumber(v['battery.charge']);
    $('battery-progress').value = Math.min(100, Math.max(0, Number(v['battery.charge']) || 0));
    $('battery-detail').textContent = `Low-charge threshold: ${displayNumber(v['battery.charge.low'])}%`;
    $('battery-voltage').textContent = displayNumber(v['battery.voltage'], 1);
    $('battery-nominal').textContent = `Nominal voltage: ${displayNumber(v['battery.voltage.nominal'])} V`;
    const runtime = Number(v['battery.runtime']);
    if (Number.isFinite(runtime) && v['battery.runtime'] !== undefined) {
      $('battery-runtime').textContent = runtime >= 3600 ? displayNumber(runtime / 3600, runtime % 3600 === 0 ? 0 : 1) : displayNumber(runtime / 60);
      $('runtime-unit').textContent = runtime >= 3600 ? 'h' : 'min';
      $('runtime-detail').textContent = runtime >= 86400 ? 'Unverified device estimate; runtime is not guaranteed' : 'Estimate reported by the UPS';
    } else { $('battery-runtime').textContent = '—'; $('runtime-detail').textContent = 'Not available'; }
    const beeper = { enabled: 'Enabled', disabled: 'Disabled', muted: 'Muted' };
    $('beeper-status').textContent = beeper[v['ups.beeper.status']] || String(v['ups.beeper.status'] || '—');
    const serial = String(v['device.serial'] || v['ups.serial'] || '—').trim();
    $('device-serial').textContent = `S/N ${serial}`;
    $('detail-serial').textContent = serial;
    $('detail-firmware').textContent = v['ups.firmware'] || '—';
    $('detail-driver').textContent = v['driver.name'] || '—';
    $('detail-nut').textContent = v['driver.version'] || '—';
    $('detail-reported-model').textContent = v['device.model'] || v['ups.model'] || '—';
    $('last-updated').textContent = `Last updated: ${timeLabel(data.checked_at)}`;
    for (const parameter of PARAMETERS) {
      const editor = parameterEditors.get(parameter.id);
      const value = v[parameter.id];
      if (!editor.dirty && document.activeElement !== editor.input) editor.input.value = value === undefined ? '' : String(value);
      editor.current.textContent = `Reported value: ${value === undefined ? '—' : String(value)} ${parameter.unit}`;
    }
    renderAudit(data.audit || []);
    renderConsumption(data.consumption);
  }

  function renderAudit(entries) {
    const container = $('activity-list');
    container.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-activity'; empty.textContent = 'No activity recorded.';
      container.append(empty); return;
    }
    for (const entry of entries.slice(0, 30)) {
      const row = document.createElement('div'); row.className = 'activity-item';
      const indicator = document.createElement('span'); indicator.className = 'activity-indicator';
      const result = String(entry.result || '');
      const failed = /fail|error|reject|denied|incert|uncertain/i.test(result);
      indicator.classList.toggle('failed', failed); indicator.textContent = failed ? '!' : '↗'; indicator.setAttribute('aria-hidden', 'true');
      const content = document.createElement('div'); content.className = 'activity-content';
      const title = document.createElement('p'); title.className = 'activity-title';
      const command = COMMANDS.find((item) => item.id === entry.target);
      title.textContent = command ? command.fullLabel : `${entry.action || 'Operation'} · ${entry.target || 'Session'}`;
      const meta = document.createElement('p'); meta.className = 'activity-meta';
      meta.textContent = `${entry.actor || 'System'} · ${result || 'Recorded'}`;
      content.append(title, meta);
      const time = document.createElement('time'); time.className = 'activity-time'; time.textContent = timeLabel(entry.time);
      if (entry.time) { time.dateTime = String(entry.time); time.title = new Date(entry.time).toLocaleString('en-US'); }
      row.append(indicator, content, time); container.append(row);
    }
  }

  function renderConsumption(consumption) {
    if (!consumption) return;
    $('measured-total').textContent = displayNumber(consumption.measured_total_w, 1);
    $('measured-total-note').textContent = consumption.measured_total_w == null ? 'Not provided by the driver' : 'Total power reported by the UPS';
    $('apparent-total').textContent = displayNumber(consumption.apparent_power_va, 1);
    $('load-total').textContent = displayNumber(consumption.load_percent, 1);
    $('estimated-total').textContent = displayNumber(consumption.estimated_total_w, 1);
    const count = Number(consumption.estimated_count) || 0;
    const total = Number(consumption.outlet_count) || 0;
    $('estimated-total-note').textContent = count === 0 ? 'No saved estimates' : `${count} of ${total} outlets have saved estimates${count < total ? ' · Partial total' : ''}`;
    renderOutlets(consumption.outlets || []);
  }

  function renderOutlets(outlets) {
    if (state.outletsDirty || $('outlet-rows').contains(document.activeElement)) return;
    $('outlet-rows').replaceChildren();
    outletEditors.clear();
    let estimate = 0;
    let estimatedCount = 0;
    for (const [index, outlet] of outlets.entries()) {
      const row = document.createElement('div'); row.className = 'outlet-row';
      const identity = document.createElement('div'); identity.className = 'outlet-identity';
      const number = document.createElement('b'); number.textContent = `Outlet ${index + 1}`;
      const connector = document.createElement('span'); connector.textContent = outlet.connector || '—';
      identity.append(number, connector); row.append(identity);
      const fields = {};
      for (const [key, labelText] of [['label', 'Label'], ['device', 'Connected equipment'], ['estimated_w', 'Estimated power (W)']]) {
        const wrapper = document.createElement('div'); wrapper.className = 'config-field';
        const label = document.createElement('label'); label.textContent = labelText;
        const input = document.createElement('input'); input.id = `outlet-${key}-${index}`; label.htmlFor = input.id;
        input.setAttribute('aria-label', `${labelText}, outlet ${index + 1}`);
        if (key === 'estimated_w') {
          input.type = 'number'; input.min = '0'; input.max = '100000'; input.step = 'any'; input.placeholder = 'No estimate';
        } else { input.type = 'text'; input.maxLength = key === 'label' ? 80 : 100; input.required = key === 'label'; input.placeholder = key === 'device' ? 'For example, application server' : `Outlet ${index + 1}`; }
        input.value = outlet[key] == null ? '' : String(outlet[key]);
        input.addEventListener('input', () => { state.outletsDirty = true; feedback('outlets-feedback', 'Unsaved changes. The total above includes saved estimates only.'); });
        fields[key] = input; wrapper.append(label, input); row.append(wrapper);
      }
      outletEditors.set(outlet.id, { connector: outlet.connector, ...fields });
      $('outlet-rows').append(row);
      if (outlet.estimated_w != null && Number.isFinite(Number(outlet.estimated_w))) { estimate += Number(outlet.estimated_w); estimatedCount += 1; }
    }
    $('estimated-total').textContent = estimatedCount ? displayNumber(estimate, 1) : '—';
    $('estimated-total-note').textContent = estimatedCount ? `${estimatedCount} of ${outlets.length} outlets have saved estimates${estimatedCount < outlets.length ? ' · Partial total' : ''}` : 'No saved estimates';
    updateAvailability();
  }

  function configCandidate() {
    const result = {};
    for (const key of CONFIG_FIELDS) {
      const value = $('config-form').elements.namedItem(key).value;
      result[key] = ['nut_port', 'poll_interval'].includes(key) ? Number(value) : key === 'nut_password' ? value : value.trim();
    }
    return result;
  }

  function requireUUID() {
    if (!window.crypto || typeof window.crypto.randomUUID !== 'function') throw new APIError('Use HTTPS to save settings or send commands from this browser.');
    return crypto.randomUUID();
  }

  function openConfirmation(operation) {
    if (!state.authenticated || state.pending || (['action', 'setting', 'beeper-test'].includes(operation.type) && !state.online)) return;
    state.confirmation = operation;
    $('confirm-dialog').classList.toggle('danger-dialog', Boolean(operation.danger));
    $('confirm-title').textContent = operation.title;
    $('confirm-description').textContent = operation.description;
    $('confirmation-detail').textContent = operation.detail;
    $('danger-confirmation').hidden = !operation.danger;
    $('confirmation-input').value = '';
    $('confirm-error').textContent = '';
    $('confirm-submit').textContent = operation.danger ? 'Run command' : 'Confirm';
    $('confirm-submit').classList.toggle('danger', Boolean(operation.danger));
    $('confirm-submit').classList.toggle('primary', !operation.danger);
    $('dialog-symbol').textContent = operation.danger ? '⏻' : '↗';
    $('confirm-dialog').showModal();
    updateAvailability();
    if (operation.danger) $('confirmation-input').focus(); else $('dialog-cancel').focus();
  }

  function closeConfirmation() {
    if (state.pending) return;
    $('confirm-dialog').close();
    state.confirmation = null;
  }

  function createCommands() {
    const targets = { beeper: $('beeper-actions'), legacy: $('legacy-actions'), power: $('power-actions') };
    for (const command of COMMANDS) {
      const button = document.createElement('button');
      button.type = 'button'; button.disabled = true;
      button.className = `button ${command.danger ? 'danger' : 'subtle'}`;
      button.setAttribute('aria-label', `${command.fullLabel} (${command.id})`);
      if (command.symbol) { const symbol = document.createElement('span'); symbol.className = 'command-symbol'; symbol.textContent = command.symbol; symbol.setAttribute('aria-hidden', 'true'); button.append(symbol); }
      const label = document.createElement('span'); label.textContent = command.label; button.append(label);
      button.addEventListener('click', () => {
        if (!supported('supported_commands', command.id)) return;
        const delay = state.status && state.status.variables['ups.delay.shutdown'];
        const detail = `Command: ${command.id}${command.id === 'load.off.delay' ? ` · Reported delay: ${delay === undefined ? 'unavailable' : delay + ' s'}` : ''}`;
        openConfirmation({ type: 'action', command: command.id, danger: Boolean(command.danger), title: command.fullLabel, description: command.description, detail });
      });
      commandButtons.set(command.id, button); targets[command.group].append(button);
    }
  }

  function createParameters() {
    for (const parameter of PARAMETERS) {
      const form = document.createElement('form'); form.className = 'parameter-row';
      const info = document.createElement('div'); info.className = 'parameter-info';
      const heading = document.createElement('h3'); heading.textContent = parameter.label;
      const description = document.createElement('p'); description.textContent = parameter.description;
      info.append(heading, description);
      const inputGroup = document.createElement('div');
      const inputRow = document.createElement('div'); inputRow.className = 'parameter-editor';
      const input = document.createElement('input'); input.type = 'number'; input.min = String(parameter.min); input.max = String(parameter.max); input.step = '1'; input.required = true; input.disabled = true;
      input.setAttribute('aria-label', `${parameter.label}, ${parameter.min} to ${parameter.max} ${parameter.unit}`);
      input.title = `Allowed range: ${parameter.min} to ${parameter.max} ${parameter.unit}`;
      const unit = document.createElement('span'); unit.className = 'parameter-unit'; unit.textContent = parameter.unit;
      inputRow.append(input, unit);
      const current = document.createElement('div'); current.className = 'parameter-current'; current.textContent = 'Reported value: —';
      inputGroup.append(inputRow, current);
      const button = document.createElement('button'); button.type = 'submit'; button.className = 'button subtle'; button.textContent = 'Save'; button.disabled = true;
      button.setAttribute('aria-label', `Save ${parameter.label.toLowerCase()}`);
      const editor = { input, button, current, dirty: false };
      input.addEventListener('input', () => { editor.dirty = true; });
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!form.reportValidity() || !supported('supported_variables', parameter.id)) return;
        const value = input.value;
        if (!Number.isInteger(Number(value))) return;
        const previous = state.status.variables[parameter.id];
        openConfirmation({ type: 'setting', variable: parameter.id, value, title: `Change ${parameter.label.toLowerCase()}`, description: `${parameter.description} This value will be written to the UPS configuration.`, detail: `${parameter.id}: ${previous === undefined ? '—' : previous} → ${value} ${parameter.unit}`, danger: false });
      });
      form.append(info, inputGroup, button); $('parameter-fields').append(form);
      parameterEditors.set(parameter.id, editor);
    }
  }

  $('confirm-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const operation = state.confirmation;
    const requiresConnection = operation && ['action', 'setting', 'beeper-test'].includes(operation.type);
    if (!operation || !state.authenticated || state.pending || (requiresConnection && !state.online) || (operation.danger && $('confirmation-input').value !== 'POWER OFF')) return;
    let requestId;
    try { requestId = requireUUID(); } catch (error) { $('confirm-error').textContent = error.message; return; }
    state.pending = true;
    $('confirm-error').textContent = '';
    $('confirm-submit').textContent = 'Sending…';
    updateAvailability();
    let message;
    let failure = false;
    let expired = false;
    let passwordChanged = false;
    try {
      const payload = { ...(operation.payload || {}), request_id: requestId };
      if (operation.type === 'action') {
        payload.command = operation.command;
        if (operation.danger) payload.confirmation = $('confirmation-input').value;
      } else if (operation.type === 'setting') { payload.variable = operation.variable; payload.value = operation.value; }
      const paths = { action: '/api/action', setting: '/api/setting', config: '/api/config', password: '/api/password', 'beeper-test': '/api/beeper-test', outlets: '/api/outlets' };
      const result = await api(paths[operation.type], { method: 'POST', body: JSON.stringify(payload) });
      message = result.message || 'Request accepted. Check the updated UPS status.';
      if (operation.type === 'setting') parameterEditors.get(operation.variable).dirty = false;
      if (operation.type === 'config') {
        state.configDirty = false;
        await fetchConfig(true);
        feedback('config-feedback', message);
      }
      if (operation.type === 'outlets') {
        state.outletsDirty = false;
        await fetchConfig();
        feedback('outlets-feedback', message);
      }
      if (operation.type === 'password') passwordChanged = true;
    } catch (error) {
      failure = true; message = error.message;
      if (error.status === 401) expired = true;
      const target = { config: 'config-feedback', password: 'password-feedback', outlets: 'outlets-feedback' }[operation.type];
      if (target) feedback(target, message, true);
    } finally {
      state.online = false;
      $('confirm-dialog').close();
      state.confirmation = null;
      if (passwordChanged) showLogin('Password changed. Sign in with your new password.');
      else if (expired) showLogin('Your session has expired. Sign in again.');
      else await fetchStatus(true);
      state.pending = false;
      updateAvailability();
    }
    showToast(message, failure);
  });

  $('confirmation-input').addEventListener('input', updateAvailability);
  $('dialog-cancel').addEventListener('click', closeConfirmation);
  $('dialog-close').addEventListener('click', closeConfirmation);
  $('confirm-dialog').addEventListener('cancel', (event) => { if (state.pending) event.preventDefault(); else state.confirmation = null; });
  $('refresh-button').addEventListener('click', () => { fetchStatus(); fetchConfig(); });

  $('beeper-test-button').addEventListener('click', () => {
    const capability = state.status && state.status.beeper_test;
    if (!capability || !capability.supported) return;
    openConfirmation({ type: 'beeper-test', title: 'Test UPS indicators and beeper', description: capability.reason || 'Start the UPS panel test, which may test its indicator lights and audible alarm.', detail: `Available command: ${capability.command || 'UPS panel test'}` });
  });

  $('config-form').addEventListener('input', () => { state.configDirty = true; feedback('config-feedback', 'Unsaved changes. You can test the connection before saving.'); });
  $('config-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.pending || !$('config-form').reportValidity()) return;
    const payload = configCandidate();
    openConfirmation({ type: 'config', payload, title: 'Save configuration', description: 'Update the display name, inventory details, and NUT connection used by this panel. This does not change the UPS network address or physical model.', detail: `${payload.display_name} · NUT server ${payload.nut_host}:${payload.nut_port} · UPS ${payload.ups}` });
  });

  $('config-test-button').addEventListener('click', async () => {
    if (state.pending || !state.authenticated) return;
    const serialInput = $('config-serial');
    serialInput.required = false;
    const valid = $('config-form').reportValidity();
    serialInput.required = true;
    if (!valid) return;
    const payload = configCandidate();
    state.pending = true; updateAvailability();
    feedback('config-feedback', 'Testing the connection…');
    try {
      const result = await api('/api/config/test', { method: 'POST', body: JSON.stringify(payload) });
      const details = [result.model ? `Reported model: ${result.model}` : '', result.serial ? `Serial: ${result.serial}` : ''].filter(Boolean).join(' · ');
      feedback('config-feedback', `${result.message || 'Connection successful.'}${details ? ' ' + details + '.' : ''} No settings have been saved.`);
      if (!serialInput.value.trim() && result.serial) { serialInput.value = result.serial; state.configDirty = true; }
    } catch (error) {
      if (error.status === 401) showLogin('Your session has expired. Sign in again.');
      else feedback('config-feedback', error.message, true);
    } finally { state.pending = false; updateAvailability(); }
  });

  $('password-form').addEventListener('submit', (event) => {
    event.preventDefault();
    $('repeat-password').setCustomValidity($('new-password').value === $('repeat-password').value ? '' : 'The passwords do not match.');
    if (state.pending || !$('password-form').reportValidity()) return;
    const payload = { current_password: $('current-password').value, new_password: $('new-password').value };
    openConfirmation({ type: 'password', payload, title: 'Change sign-in password', description: 'Your new password will be required at the next sign-in. All current sessions will be closed, including this one.', detail: `Account: ${$('current-user').textContent}` });
  });
  for (const id of ['new-password', 'repeat-password']) $(id).addEventListener('input', () => $('repeat-password').setCustomValidity(''));

  $('outlets-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.pending || !$('outlets-form').reportValidity()) return;
    const outlets = Array.from(outletEditors, ([id, editor]) => ({ id, connector: editor.connector, label: editor.label.value.trim(), device: editor.device.value.trim(), estimated_w: editor.estimated_w.value === '' ? null : Number(editor.estimated_w.value) }));
    openConfirmation({ type: 'outlets', payload: { outlets }, title: 'Save equipment and estimates', description: 'Save outlet labels, connected equipment, and your power estimates. These values are panel annotations, not measurements from the UPS.', detail: `${outlets.filter((outlet) => outlet.estimated_w !== null).length} power estimates across ${outlets.length} outlets. Equipment power will not change.` });
  });

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('login-button').disabled) return;
    $('login-error').textContent = '';
    $('login-button').disabled = true;
    try {
      const result = await api('/api/login', { method: 'POST', body: JSON.stringify({ username: $('username').value, password: $('password').value }) });
      if (!result.csrf) throw new APIError('The session response is missing its security token.');
      showApp(result);
    } catch (error) { $('login-error').textContent = error.uncertain ? 'Sign-in could not be confirmed. Refresh the page to check your session.' : error.message; }
    finally { $('login-button').disabled = false; }
  });

  $('logout-button').addEventListener('click', async () => {
    if (state.pending) return;
    state.pending = true; updateAvailability();
    try { await api('/api/logout', { method: 'POST', body: '{}' }); showLogin(); }
    catch (error) { if (error.status === 401) showLogin(); else showToast('Sign-out could not be confirmed. Refresh the page to check your session.', true); }
    finally { state.pending = false; updateAvailability(); }
  });

  for (const link of document.querySelectorAll('.nav-link')) link.addEventListener('click', () => {
    for (const item of document.querySelectorAll('.nav-link')) item.classList.remove('active');
    link.classList.add('active');
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden && state.authenticated && !state.pending) fetchStatus(); });

  createCommands();
  createParameters();
  (async () => {
    $('login-button').disabled = true;
    try {
      const session = await api('/api/session');
      if (session.authenticated && session.csrf) showApp(session); else showLogin();
    } catch (error) { showLogin(error.message); }
    finally { $('login-button').disabled = false; }
  })();
})();
