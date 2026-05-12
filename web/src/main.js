import './style.css'
import 'flowbite'
import axios from 'axios'
import Plotly from 'plotly.js-dist-min'

// State management
let currentState = {
    activePage: 'overview',
    data: {}
};

// Task State
let taskPollInterval = null;
let currentTaskId = null;

// UI Elements
const pageContent = document.getElementById('page-content');
const latestDateEl = document.getElementById('latest-date');
const triggerBtn = document.getElementById('trigger-btn');

// ============ Task System ============

function initTaskPanel() {
    // Create task panel container if not exists
    if (!document.getElementById('task-panel')) {
        const panel = document.createElement('div');
        panel.id = 'task-panel';
        panel.className = 'hidden fixed bottom-4 right-4 z-50 w-80 bg-white rounded-lg shadow-lg border border-gray-200 dark:bg-gray-800 dark:border-gray-700';
        panel.innerHTML = `
            <div class="flex items-center justify-between p-3 border-b border-gray-200 dark:border-gray-700">
                <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
                    <span id="task-panel-icon" class="inline-block w-2 h-2 rounded-full bg-gray-400 mr-2"></span>
                    任务队列
                </h3>
                <div class="flex gap-2">
                    <button id="task-panel-refresh" class="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400">刷新</button>
                    <button id="task-panel-close" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                    </button>
                </div>
            </div>
            <div id="task-panel-body" class="p-3 max-h-64 overflow-y-auto">
                <p class="text-sm text-gray-500 dark:text-gray-400">暂无任务</p>
            </div>
        `;
        document.body.appendChild(panel);

        document.getElementById('task-panel-close').addEventListener('click', () => {
            panel.classList.add('hidden');
        });
        document.getElementById('task-panel-refresh').addEventListener('click', () => {
            refreshTasks();
        });
    }
}

function toggleTaskPanel() {
    const panel = document.getElementById('task-panel');
    if (panel.classList.contains('hidden')) {
        panel.classList.remove('hidden');
        refreshTasks();
    } else {
        panel.classList.add('hidden');
    }
}

function getStatusColor(status) {
    switch (status) {
        case 'running': return 'bg-blue-500';
        case 'success': return 'bg-green-500';
        case 'failed': return 'bg-red-500';
        case 'cancelled': return 'bg-gray-500';
        default: return 'bg-yellow-500';
    }
}

function getStatusText(status) {
    const map = {
        'pending': '等待中',
        'running': '运行中',
        'success': '已完成',
        'failed': '失败',
        'cancelled': '已取消',
    };
    return map[status] || status;
}

function formatDuration(started, finished) {
    if (!started) return '';
    const end = finished || Date.now() / 1000;
    const secs = Math.round(end - started);
    if (secs < 60) return `${secs}s`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

async function refreshTasks() {
    try {
        const res = await axios.get('/api/tasks');
        const tasks = res.data.tasks || [];
        const body = document.getElementById('task-panel-body');

        if (tasks.length === 0) {
            body.innerHTML = '<p class="text-sm text-gray-500 dark:text-gray-400">暂无任务</p>';
            return;
        }

        let html = '';
        tasks.forEach(t => {
            const statusColor = getStatusColor(t.status);
            const statusText = getStatusText(t.status);
            const duration = formatDuration(t.started_at, t.finished_at);
            const progressBar = t.status === 'running' && t.progress && t.progress.total_steps > 0
                ? `<div class="w-full bg-gray-200 rounded-full h-1.5 mt-1 dark:bg-gray-700"><div class="bg-blue-600 h-1.5 rounded-full transition-all" style="width:${t.progress.percent}%"></div></div><div class="text-xs text-gray-400 mt-0.5">${t.progress.label} ${t.progress.step}/${t.progress.total_steps}</div>`
                : '';
            const errorInfo = t.error ? `<div class="text-xs text-red-500 mt-1">${t.error}</div>` : '';

            html += `
                <div class="mb-3 p-2 bg-gray-50 rounded dark:bg-gray-700">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-2">
                            <span class="w-2 h-2 rounded-full ${statusColor}"></span>
                            <span class="text-sm font-medium text-gray-900 dark:text-white">${t.name}</span>
                        </div>
                        <span class="text-xs text-gray-500 dark:text-gray-400">${statusText}</span>
                    </div>
                    <div class="text-xs text-gray-500 mt-1">ID: ${t.id} ${duration ? '· ' + duration : ''}</div>
                    ${progressBar}
                    ${errorInfo}
                </div>
            `;
        });
        body.innerHTML = html;
    } catch (err) {
        console.error('Failed to refresh tasks:', err);
    }
}

function startTaskPolling(taskId) {
    currentTaskId = taskId;
    if (taskPollInterval) clearInterval(taskPollInterval);

    taskPollInterval = setInterval(async () => {
        try {
            const res = await axios.get(`/api/tasks/${taskId}`);
            const task = res.data;
            updateTaskStatusInUI(task);

            if (['success', 'failed', 'cancelled'].includes(task.status)) {
                clearInterval(taskPollInterval);
                taskPollInterval = null;
                currentTaskId = null;
                setTriggerButtonEnabled(true);

                if (task.status === 'success') {
                    // Refresh current page data
                    renderPage();
                }
            }
        } catch (err) {
            console.error('Task polling error:', err);
        }
    }, 2000);
}

function updateTaskStatusInUI(task) {
    const icon = document.getElementById('task-panel-icon');
    const btn = document.getElementById('trigger-btn');

    if (icon) {
        icon.className = `inline-block w-2 h-2 rounded-full mr-2 ${getStatusColor(task.status)}`;
    }

    if (task.status === 'running') {
        if (btn) {
            btn.disabled = true;
            const pct = task.progress && task.progress.total_steps > 0
                ? ` (${task.progress.step}/${task.progress.total_steps})`
                : '';
            btn.textContent = `分析中${pct}`;
        }
    }
}

function setTriggerButtonEnabled(enabled) {
    const btn = document.getElementById('trigger-btn');
    if (!btn) return;
    btn.disabled = !enabled;
    btn.textContent = enabled ? '立即分析' : '分析中...';
}

async function checkActiveTaskOnLoad() {
    try {
        const res = await axios.get('/api/tasks/active');
        if (res.data) {
            currentTaskId = res.data.id;
            setTriggerButtonEnabled(false);
            startTaskPolling(currentTaskId);
            updateTaskStatusInUI(res.data);
        }
    } catch (err) {
        // No active task
    }
}

// ============ Navigation ============

document.querySelectorAll('[data-page]').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        const page = e.currentTarget.getAttribute('data-page');
        switchPage(page);
    });
});

function switchPage(page) {
    currentState.activePage = page;

    document.querySelectorAll('[data-page]').forEach(link => {
        if (link.getAttribute('data-page') === page) {
            link.classList.add('bg-gray-100', 'dark:bg-gray-700');
        } else {
            link.classList.remove('bg-gray-100', 'dark:bg-gray-700');
        }
    });

    renderPage();
}

// ============ Trigger Button ============

triggerBtn.addEventListener('click', async () => {
    try {
        setTriggerButtonEnabled(false);
        const res = await axios.post('/api/trigger');
        const taskId = res.data.task_id;
        startTaskPolling(taskId);

        // Show task panel
        const panel = document.getElementById('task-panel');
        if (panel) panel.classList.remove('hidden');
        refreshTasks();
    } catch (err) {
        console.error(err);
        const msg = err.response?.data?.detail || '触发失败';
        alert(msg);
        setTriggerButtonEnabled(true);
    }
});

// Add task toggle button to header
function initTaskToggleButton() {
    const headerRight = triggerBtn.parentElement;
    const btn = document.createElement('button');
    btn.id = 'task-toggle-btn';
    btn.type = 'button';
    btn.className = 'mr-3 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300 relative';
    btn.innerHTML = `
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
        </svg>
        <span id="task-badge" class="hidden absolute -top-1 -right-1 w-2 h-2 bg-blue-500 rounded-full"></span>
    `;
    btn.addEventListener('click', toggleTaskPanel);
    headerRight.insertBefore(btn, triggerBtn);
}

// ============ Render Pages ============

async function renderPage() {
    pageContent.innerHTML = '<div class="flex justify-center items-center h-64"><div class="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-700"></div></div>';

    try {
        switch (currentState.activePage) {
            case 'overview':
                await renderOverview();
                break;
            case 'rankings':
                await renderRankings();
                break;
            case 'heatmap':
                await renderHeatmap();
                break;
            case 'portfolio':
                await renderPortfolio();
                break;
            case 'risk':
                await renderRisk();
                break;
        }
    } catch (err) {
        pageContent.innerHTML = `<div class="p-4 mb-4 text-sm text-red-800 rounded-lg bg-red-50 dark:bg-gray-800 dark:text-red-400" role="alert">加载失败: ${err.message}</div>`;
    }
}

async function renderOverview() {
    const res = await axios.get('/api/overview');
    const data = res.data;

    if (!data.has_data) {
        pageContent.innerHTML = '<div class="p-4 mb-4 text-sm text-blue-800 rounded-lg bg-blue-50 dark:bg-gray-800 dark:text-blue-400" role="alert">暂无数据，请先点击右上角“立即分析”。</div>';
        return;
    }

    latestDateEl.textContent = `最新数据日期: ${data.last_date}`;

    const dir = data.direction;
    const dirColor = dir.status === '进攻' ? 'text-green-500' : (dir.status === '防守' ? 'text-red-500' : 'text-yellow-500');
    const borderLeft = dir.status === '进攻' ? 'border-green-500' : (dir.status === '防守' ? 'border-red-500' : 'border-yellow-500');

    let html = `
        <h2 class="text-2xl font-bold mb-6 dark:text-white">今日概览</h2>

        <!-- Market Direction Card -->
        <div class="p-6 mb-6 bg-white border-l-4 ${borderLeft} rounded-lg shadow dark:bg-gray-800 dark:border-gray-700">
            <h5 class="mb-1 text-sm font-normal text-gray-500 dark:text-gray-400">今日定调</h5>
            <p class="text-2xl font-bold tracking-tight text-gray-900 dark:text-white">${dir.status} — ${dir.description}</p>
            <p class="mt-2 text-sm text-gray-600 dark:text-gray-400">${dir.summary}</p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">情绪评分</h5>
                <p class="text-2xl font-bold ${dirColor}">${dir.sentiment_score}/100</p>
                <p class="text-sm text-gray-500">${dir.sentiment_level}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">均线排列</h5>
                <p class="text-2xl font-bold text-gray-900 dark:text-white">${dir.ma_alignment}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">PE 分位</h5>
                <p class="text-2xl font-bold text-gray-900 dark:text-white">${data.temperature.pe_percentile ? data.temperature.pe_percentile.toFixed(1) + '%' : '暂无'}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">上涨占比</h5>
                <p class="text-2xl font-bold text-gray-900 dark:text-white">${(data.market_breadth.advances / data.market_breadth.total_etf * 100).toFixed(1)}%</p>
                <p class="text-sm text-gray-500">${data.market_breadth.advances}/${data.market_breadth.declines}</p>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div id="sentiment-gauge" class="h-64 bg-white rounded-lg shadow dark:bg-gray-800"></div>
            <div id="breadth-chart" class="h-64 bg-white rounded-lg shadow dark:bg-gray-800"></div>
        </div>
    `;

    pageContent.innerHTML = html;

    renderSentimentGauge(dir.sentiment_score);
    renderBreadthChart(data.market_breadth);
}

function renderSentimentGauge(score) {
    const color = score >= 55 ? '#22c55e' : (score >= 40 ? '#eab308' : '#ef4444');
    const data = [{
        domain: { x: [0, 1], y: [0, 1] },
        value: score,
        title: { text: "市场情绪", font: { size: 16 } },
        type: "indicator",
        mode: "gauge+number",
        gauge: {
            axis: { range: [null, 100] },
            bar: { color: color },
            steps: [
                { range: [0, 40], color: "rgba(239, 68, 68, 0.1)" },
                { range: [40, 60], color: "rgba(234, 179, 8, 0.1)" },
                { range: [60, 100], color: "rgba(34, 197, 94, 0.1)" }
            ],
        }
    }];
    const layout = { width: 350, height: 250, margin: { t: 0, b: 0 }, paper_bgcolor: 'transparent' };
    Plotly.newPlot('sentiment-gauge', data, layout);
}

function renderBreadthChart(breadth) {
    const data = [{
        values: [breadth.advances, breadth.declines],
        labels: ['上涨', '下跌'],
        type: 'pie',
        marker: {
            colors: ['#22c55e', '#ef4444']
        },
        hole: .4
    }];
    const layout = { title: '涨跌分布', height: 250, margin: { t: 30, b: 0 }, paper_bgcolor: 'transparent' };
    Plotly.newPlot('breadth-chart', data, layout);
}

async function renderRankings() {
    const res = await axios.get('/api/market/etfs');
    const etfs = res.data;

    let html = `
        <h2 class="text-2xl font-bold mb-6 dark:text-white">ETF 排行榜 (今日涨跌前30)</h2>
        <div class="relative overflow-x-auto shadow-md sm:rounded-lg">
            <table class="w-full text-sm text-left text-gray-500 dark:text-gray-400">
                <thead class="text-xs text-gray-700 uppercase bg-gray-50 dark:bg-gray-700 dark:text-gray-400">
                    <tr>
                        <th class="px-6 py-3">代码</th>
                        <th class="px-6 py-3">名称</th>
                        <th class="px-6 py-3">最新价</th>
                        <th class="px-6 py-3">涨跌幅</th>
                        <th class="px-6 py-3">PE</th>
                    </tr>
                </thead>
                <tbody>
    `;

    const sortedEtfs = etfs.sort((a, b) => Math.abs(b.change_pct) - Math.abs(a.change_pct)).slice(0, 30);

    sortedEtfs.forEach(e => {
        const colorClass = e.change_pct >= 0 ? 'text-green-600' : 'text-red-600';
        html += `
            <tr class="bg-white border-b dark:bg-gray-800 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600">
                <td class="px-6 py-4 font-medium text-gray-900 whitespace-nowrap dark:text-white">${e.code}</td>
                <td class="px-6 py-4">${e.name}</td>
                <td class="px-6 py-4">¥${e.price.toFixed(3)}</td>
                <td class="px-6 py-4 ${colorClass}">${e.change_pct > 0 ? '+' : ''}${e.change_pct.toFixed(2)}%</td>
                <td class="px-6 py-4">${e.pe_ratio ? e.pe_ratio.toFixed(1) : '-'}</td>
            </tr>
        `;
    });

    html += `</tbody></table></div>`;
    pageContent.innerHTML = html;
}

async function renderHeatmap() {
    const res = await axios.get('/api/market/sectors');
    const sectors = res.data;

    let html = `
        <h2 class="text-2xl font-bold mb-6 dark:text-white">行业热力图</h2>
        <div id="sector-bar-chart" class="h-96 bg-white rounded-lg shadow mb-8 dark:bg-gray-800"></div>
        <div class="relative overflow-x-auto shadow-md sm:rounded-lg">
            <table class="w-full text-sm text-left text-gray-500 dark:text-gray-400">
                <thead class="text-xs text-gray-700 uppercase bg-gray-50 dark:bg-gray-700 dark:text-gray-400">
                    <tr>
                        <th class="px-6 py-3">行业</th>
                        <th class="px-6 py-3">今日涨跌</th>
                        <th class="px-6 py-3">1月动量</th>
                        <th class="px-6 py-3">3月动量</th>
                    </tr>
                </thead>
                <tbody>
    `;

    sectors.forEach(s => {
        const colorClass = s.change_pct >= 0 ? 'text-green-600' : 'text-red-600';
        html += `
            <tr class="bg-white border-b dark:bg-gray-800 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-600">
                <td class="px-6 py-4 font-medium text-gray-900 whitespace-nowrap dark:text-white">${s.name}</td>
                <td class="px-6 py-4 ${colorClass}">${s.change_pct > 0 ? '+' : ''}${s.change_pct.toFixed(2)}%</td>
                <td class="px-6 py-4">${s.momentum_1m.toFixed(2)}%</td>
                <td class="px-6 py-4">${s.momentum_3m.toFixed(2)}%</td>
            </tr>
        `;
    });

    html += `</tbody></table></div>`;
    pageContent.innerHTML = html;

    const topSectors = sectors.slice(0, 20);
    const trace = {
        x: topSectors.map(s => s.change_pct),
        y: topSectors.map(s => s.name),
        type: 'bar',
        orientation: 'h',
        marker: {
            color: topSectors.map(s => s.change_pct >= 0 ? '#22c55e' : '#ef4444')
        }
    };
    const layout = { title: '行业表现', margin: { l: 150 }, paper_bgcolor: 'transparent' };
    Plotly.newPlot('sector-bar-chart', [trace], layout);
}

async function renderPortfolio() {
    const res = await axios.get('/api/portfolio');
    const data = res.data;

    if (!data.configured) {
        pageContent.innerHTML = '<div class="p-4 mb-4 text-sm text-blue-800 rounded-lg bg-blue-50 dark:bg-gray-800 dark:text-blue-400" role="alert">请先配置 portfolio.yaml 文件。</div>';
        return;
    }

    const summary = data.summary;
    let html = `
        <h2 class="text-2xl font-bold mb-6 dark:text-white">持仓收益</h2>
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800 text-center">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">总市值</h5>
                <p class="text-2xl font-bold text-gray-900 dark:text-white">¥${summary.total_value.toLocaleString()}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800 text-center">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">总成本</h5>
                <p class="text-2xl font-bold text-gray-900 dark:text-white">¥${summary.total_cost.toLocaleString()}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800 text-center">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">总盈亏</h5>
                <p class="text-2xl font-bold ${summary.total_pnl >= 0 ? 'text-green-600' : 'text-red-600'}">¥${summary.total_pnl.toLocaleString()}</p>
            </div>
            <div class="p-4 bg-white rounded-lg shadow dark:bg-gray-800 text-center">
                <h5 class="text-sm font-normal text-gray-500 dark:text-gray-400">总收益率</h5>
                <p class="text-2xl font-bold ${summary.total_change_pct >= 0 ? 'text-green-600' : 'text-red-600'}">${summary.total_change_pct.toFixed(2)}%</p>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
    `;

    data.holdings.forEach(h => {
        const scoreColor = h.score >= 80 ? 'text-green-600' : (h.score >= 60 ? 'text-green-400' : (h.score >= 40 ? 'text-yellow-400' : 'text-red-500'));
        const pnlColor = h.pnl_pct >= 0 ? 'text-green-600' : 'text-red-600';

        html += `
            <div class="p-5 bg-white border border-gray-200 rounded-lg shadow dark:bg-gray-800 dark:border-gray-700">
                <div class="flex justify-between items-center mb-2">
                    <span class="text-lg font-bold text-gray-900 dark:text-white">${h.name}</span>
                    <span class="text-xl font-black ${scoreColor}">${h.score}分</span>
                </div>
                <div class="text-xs text-gray-500 mb-4">${h.code} · ${h.category}</div>
                <div class="flex justify-between items-center mb-4">
                    <div>
                        <div class="text-2xl font-bold ${pnlColor}">${h.pnl_pct.toFixed(2)}%</div>
                        <div class="text-xs text-gray-400">累计盈亏</div>
                    </div>
                    <div class="text-right">
                        <div class="text-lg font-semibold text-gray-700 dark:text-gray-300">¥${h.current_price.toFixed(3)}</div>
                        <div class="text-xs text-gray-400">今日 ${h.change_pct > 0 ? '+' : ''}${h.change_pct.toFixed(2)}%</div>
                    </div>
                </div>
                <div class="w-full bg-gray-200 rounded-full h-1.5 mb-4 dark:bg-gray-700">
                    <div class="bg-blue-600 h-1.5 rounded-full" style="width: ${h.score}%"></div>
                </div>
                <div class="text-right">
                    <span class="bg-blue-100 text-blue-800 text-xs font-medium px-2.5 py-0.5 rounded dark:bg-blue-900 dark:text-blue-300">${h.action}</span>
                </div>
            </div>
        `;
    });

    html += `</div>`;
    pageContent.innerHTML = html;
}

async function renderRisk() {
    const res = await axios.get('/api/risk');
    const data = res.data;

    let html = `
        <h2 class="text-2xl font-bold mb-6 dark:text-white">风险天梯</h2>
    `;

    if (data.alerts.length === 0) {
        html += `
            <div class="flex items-center p-4 mb-4 text-sm text-green-800 border border-green-300 rounded-lg bg-green-50 dark:bg-gray-800 dark:text-green-400 dark:border-green-800" role="alert">
                <svg class="flex-shrink-0 inline w-4 h-4 mr-3" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 20 20">
                    <path d="M10 .5a9.5 9.5 0 1 0 9.5 9.5A9.51 9.51 0 0 0 10 .5ZM9.5 4a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3ZM12 15H8a1 1 0 0 1 0-2h1v-3H8a1 1 0 0 1 0-2h2a1 1 0 0 1 1 1v4h1a1 1 0 0 1 0 2Z"/>
                </svg>
                <span class="sr-only">Info</span>
                <div><span class="font-medium">✅ 安全</span> 暂无新增风险信号，维持现状。</div>
            </div>
        `;
    } else {
        const severe = data.alerts.filter(a => a.level === '强');
        const medium = data.alerts.filter(a => a.level === '中');

        if (severe.length > 0) {
            html += `<h3 class="text-lg font-bold text-red-600 mb-4">Ὢ8 第一层 — 严重风险</h3>`;
            severe.forEach(a => {
                html += `
                    <div class="p-4 mb-4 text-red-800 border border-red-300 rounded-lg bg-red-50 dark:bg-gray-800 dark:text-red-400 dark:border-red-800">
                        <div class="flex items-center">
                            <span class="font-bold mr-2">[${a.type === 'volatility' ? '异常波动' : (a.type === 'drawdown' ? '最大回撤' : '相关性')}]</span>
                            <span>${a.name} (${a.code}): ${a.message}</span>
                        </div>
                    </div>
                `;
            });
        }

        if (medium.length > 0) {
            html += `<h3 class="text-lg font-bold text-orange-500 mt-8 mb-4">⚠️ 第二层 — 中度风险</h3>`;
            medium.forEach(a => {
                html += `
                    <div class="p-4 mb-4 text-orange-800 border border-orange-300 rounded-lg bg-orange-50 dark:bg-gray-800 dark:text-orange-400 dark:border-orange-800">
                        <div class="flex items-center">
                            <span class="font-bold mr-2">[${a.type === 'volatility' ? '异常波动' : (a.type === 'drawdown' ? '最大回撤' : '相关性')}]</span>
                            <span>${a.name} (${a.code}): ${a.message}</span>
                        </div>
                    </div>
                `;
            });
        }
    }

    pageContent.innerHTML = html;
}

// ============ Init ============

initTaskPanel();
initTaskToggleButton();
checkActiveTaskOnLoad();
switchPage('overview');
