let dashboardData = null;
let sortColumn = 'totalRegistrations';
let sortDirection = -1; // -1 = desc

const CHART_COLORS = [
  '#6c5ce7', '#74b9ff', '#00cec9', '#fdcb6e', '#ff7675',
  '#a29bfe', '#55efc4', '#ffeaa7', '#fab1a0', '#81ecec',
  '#dfe6e9', '#636e72', '#b2bec3', '#fd79a8', '#e17055'
];

// --- Init (static mode: data is pre-baked in window.__DASHBOARD_DATA__) ---
function init() {
  if (window.__DASHBOARD_DATA__) {
    dashboardData = window.__DASHBOARD_DATA__;
    render();
  }
}

// --- Render ---
function render() {
  const d = dashboardData;
  const events = d.events;

  const fetchedAt = new Date(d.fetchedAt);
  document.getElementById('lastUpdated').textContent = `Last updated: ${fetchedAt.toLocaleString()}`;

  const totalRegs = events.reduce((s, e) => s + e.totalRegistrations, 0);
  const totalRevenue = events.reduce((s, e) => s + e.revenue, 0);
  document.getElementById('kpiEvents').textContent = events.length;
  document.getElementById('kpiRegistrations').textContent = totalRegs.toLocaleString();
  document.getElementById('kpiRevenue').textContent = '$' + Math.round(totalRevenue).toLocaleString();

  const years = events.map(e => parseInt(e.startDate?.substring(0, 4))).filter(y => y > 2000);
  const minYear = Math.min(...years);
  const maxYear = Math.max(...years);
  const span = maxYear - minYear + 1;
  document.getElementById('kpiTimespan').textContent = `${span} years of data (${minYear} \u2013 ${maxYear})`;

  renderRegsPerEvent(events);
  renderRevenuePerEvent(events);
  renderRegsByYear(events);
  renderEventsByYear(events);
  renderSpendersTable(d.topSpenders || []);
  renderEventsTable(events);
  setupTableSort();
  setupTableSearch();
}

// --- Charts ---

function renderRegsPerEvent(events) {
  const sorted = [...events].sort((a, b) => b.totalRegistrations - a.totalRegistrations);
  const height = Math.max(400, sorted.length * 30);
  document.getElementById('wrapRegsPerEvent').style.height = height + 'px';

  createChart('chartRegsPerEvent', 'bar', {
    labels: sorted.map(e => truncate(e.name, 35)),
    datasets: [{
      label: 'Registrations',
      data: sorted.map(e => e.totalRegistrations),
      backgroundColor: '#6c5ce7'
    }]
  }, { indexAxis: 'y', scales: { x: { beginAtZero: true } } });
}

function renderRevenuePerEvent(events) {
  const sorted = [...events].filter(e => e.revenue > 0).sort((a, b) => b.revenue - a.revenue);
  const height = Math.max(400, sorted.length * 30);
  document.getElementById('wrapRevenuePerEvent').style.height = height + 'px';

  createChart('chartRevenuePerEvent', 'bar', {
    labels: sorted.map(e => truncate(e.name, 35)),
    datasets: [{
      label: 'Revenue ($)',
      data: sorted.map(e => Math.round(e.revenue)),
      backgroundColor: '#00cec9'
    }]
  }, { indexAxis: 'y', scales: { x: { beginAtZero: true } } });
}

function renderRegsByYear(events) {
  const yearMap = {};
  events.forEach(e => {
    e.regDates.forEach(d => {
      const year = d.substring(0, 4);
      yearMap[year] = (yearMap[year] || 0) + 1;
    });
  });
  const years = Object.keys(yearMap).sort();
  createChart('chartRegsByYear', 'bar', {
    labels: years,
    datasets: [{
      label: 'Registrations',
      data: years.map(y => yearMap[y]),
      backgroundColor: '#6c5ce7'
    }]
  }, { scales: { y: { beginAtZero: true } } });
}

function renderEventsByYear(events) {
  const yearMap = {};
  events.forEach(e => {
    const year = e.startDate?.substring(0, 4);
    if (year) yearMap[year] = (yearMap[year] || 0) + 1;
  });
  const years = Object.keys(yearMap).sort();
  createChart('chartEventsByYear', 'bar', {
    labels: years,
    datasets: [{
      label: 'Events',
      data: years.map(y => yearMap[y]),
      backgroundColor: '#00cec9'
    }]
  }, { scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } });
}

function createChart(canvasId, type, data, extraOpts = {}) {
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const defaults = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { display: type === 'doughnut', labels: { color: '#8b8fa3', font: { size: 11 } } }
    },
    scales: type === 'doughnut' ? {} : {
      x: { ticks: { color: '#8b8fa3', font: { size: 11 } }, grid: { color: '#2e3145' } },
      y: { ticks: { color: '#8b8fa3', font: { size: 11 } }, grid: { color: '#2e3145' } }
    }
  };

  const options = deepMerge(defaults, extraOpts);
  return new Chart(ctx, { type, data, options });
}

// --- Tables ---

function renderSpendersTable(spenders) {
  const tbody = document.getElementById('spendersBody');
  tbody.innerHTML = spenders.map((s, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${esc(s.name)}</td>
      <td>${esc(s.email)}</td>
      <td class="text-right text-green">$${s.totalSpent.toLocaleString()}</td>
      <td class="text-right">${s.registrations}</td>
      <td>${s.events.length} event${s.events.length !== 1 ? 's' : ''}</td>
    </tr>
  `).join('');
}

function renderEventsTable(events) {
  const sorted = [...events].sort((a, b) => {
    const av = a[sortColumn], bv = b[sortColumn];
    if (typeof av === 'string') return sortDirection * av.localeCompare(bv);
    return sortDirection * ((av || 0) - (bv || 0));
  });

  const tbody = document.getElementById('eventsBody');
  tbody.innerHTML = sorted.map(e => {
    const topTicket = Object.entries(e.ticketTypes).sort((a, b) => b[1] - a[1])[0];
    return `
      <tr>
        <td>${esc(e.name)}</td>
        <td>${e.startDate}</td>
        <td>${e.venue?.city || '-'}, ${e.venue?.country || ''}</td>
        <td class="text-right">${e.totalRegistrations.toLocaleString()}</td>
        <td class="text-right text-green">$${Math.round(e.revenue).toLocaleString()}</td>
        <td class="text-right ${e.checkinRate > 50 ? 'text-green' : e.checkinRate > 20 ? 'text-orange' : 'text-red'}">${e.checkinRate}%</td>
        <td>${topTicket ? truncate(topTicket[0], 25) : '-'}</td>
      </tr>
    `;
  }).join('');
}

function setupTableSort() {
  document.querySelectorAll('#eventsTable th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      if (sortColumn === col) sortDirection *= -1;
      else { sortColumn = col; sortDirection = -1; }
      document.querySelectorAll('#eventsTable th').forEach(t => t.classList.remove('sort-asc', 'sort-desc'));
      th.classList.add(sortDirection === 1 ? 'sort-asc' : 'sort-desc');
      renderEventsTable(dashboardData.events);
    });
  });
}

function setupTableSearch() {
  document.getElementById('tableSearch').addEventListener('input', (e) => {
    const q = e.target.value.toLowerCase();
    const filtered = dashboardData.events.filter(ev =>
      ev.name.toLowerCase().includes(q) ||
      (ev.venue?.city || '').toLowerCase().includes(q) ||
      (ev.venue?.country || '').toLowerCase().includes(q)
    );
    renderEventsTable(filtered);
  });
}

// --- Helpers ---

function truncate(str, len) {
  return str.length > len ? str.substring(0, len) + '...' : str;
}

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function deepMerge(target, source) {
  const result = { ...target };
  for (const key in source) {
    if (source[key] && typeof source[key] === 'object' && !Array.isArray(source[key])) {
      result[key] = deepMerge(result[key] || {}, source[key]);
    } else {
      result[key] = source[key];
    }
  }
  return result;
}

init();
