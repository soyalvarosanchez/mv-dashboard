const frame = document.getElementById('contentFrame');
const sidebar = document.getElementById('sidebar');
const eventNav = document.getElementById('eventNavItems');

async function loadNav() {
  try {
    const res = await fetch('dashboards.json');
    const data = await res.json();
    data.eventDashboards.forEach((d, i) => {
      const a = document.createElement('a');
      a.className = 'nav-item' + (i === 0 ? ' active' : '');
      a.dataset.view = d.path;
      a.innerHTML = `<span class="nav-icon">${d.icon}</span><span class="nav-label">${d.label}</span><span class="nav-desc">${d.description}</span>`;
      eventNav.appendChild(a);
      a.addEventListener('click', () => navigate(d.path));
    });

    const saved = sessionStorage.getItem('activeView');
    if (saved) navigate(saved);
  } catch (e) {
    console.error('Failed to load dashboards config', e);
  }
}

function navigate(path) {
  frame.src = path;
  sessionStorage.setItem('activeView', path);
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === path);
  });
  sidebar.classList.remove('open');
}

document.getElementById('hamburger').addEventListener('click', () => {
  sidebar.classList.toggle('open');
});

document.getElementById('sidebarClose').addEventListener('click', () => {
  sidebar.classList.remove('open');
});

loadNav();
