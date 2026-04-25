/**
 * InvestorClaw grouped dashboard frontend
 *
 * Reads DASHBOARD_DATA global (injected by dashboard-full.html)
 * Handles grouped navigation, sub-view controls, rendering, and data presentation.
 */

const IC = {
  data: null,
  currentGroup: 'core',
  currentTab: 'holdings',
  renderedTabs: new Set(),

  TAB_GROUPS: {
    core: {
      label: 'Core',
      tabs: ['holdings', 'performance', 'cashflow', 'bonds', 'synthesis', 'news'],
    },
    analysis: {
      label: 'Analysis',
      tabs: ['whatchanged', 'scenarios', 'peer', 'analyst'],
    },
    recommendations: {
      label: 'Recommendations',
      tabs: ['optimize', 'rebalancetax', 'recommendations'],
    },
    utility: {
      label: 'Utility',
      tabs: ['reports', 'settings', 'about'],
    },
  },

  SUBVIEWS: {
    holdings: ['Summary', 'Detail', 'Sectors', 'Allocation'],
    performance: ['Summary', 'Ticker Performance', 'Risk Metrics', 'Comparison'],
    cashflow: ['Timeline', 'Monthly Breakdown', 'Tax Summary'],
    bonds: ['Summary', 'Ladder', 'Quality Spread'],
    synthesis: ['Insights', 'Risks', 'Concentration', 'Opportunities'],
    news: ['By Holding', 'Sentiment', 'Timeline'],
    whatchanged: ['7-Day', 'Month-To-Date', 'Year-To-Date', 'Factor Breakdown'],
    scenarios: ['Base Case', 'Bear', 'Bull', 'VaR Analysis'],
    peer: ['Factor Exposure', 'Benchmark Comparison', 'Style Drift'],
    analyst: ['By Holding', 'Upside/Downside', 'Rating Distribution'],
    optimize: ['Conservative', 'Balanced', 'Growth', 'Current', 'Efficient Frontier'],
    rebalancetax: ['Recommended Trades', 'Tax Impact', 'Cost Basis', 'Implementation'],
    recommendations: ['High Priority', 'Medium Priority', 'Monitoring'],
    reports: ['Quarterly', 'Year-To-Date', 'Custom Period'],
    settings: ['General', 'Data Sources', 'Risk Profile'],
    about: ['Overview', 'Documentation', 'Help'],
  },

  /**
   * Initialize dashboard - called on page load
   */
  init() {
    console.log('IC.init: Starting dashboard initialization');

    // Get injected data from HTML
    if (typeof DASHBOARD_DATA === 'undefined') {
      console.error('DASHBOARD_DATA not found - dashboard.html must inject it');
      this.showError('Dashboard data not loaded');
      return;
    }

    this.data = DASHBOARD_DATA;
    console.log('IC.init: Data loaded, holdings:', this.data.holdings?.summary?.position_count || 0);
    this.checkFreshness();

    // Setup grouped tab navigation
    this.setupTabs();

    const savedTab = this._normalizeTab(this._readStorage('ic.dashboard.tab') || 'holdings');
    const initialTab = this._tabExists(savedTab) ? savedTab : 'holdings';
    this.renderTab(initialTab);

    console.log('IC.init: Dashboard ready');
  },

  /**
   * Setup tab click handlers
   */
  setupTabs() {
    document.querySelectorAll('[data-group]').forEach(groupBtn => {
      groupBtn.addEventListener('click', (e) => {
        e.preventDefault();
        this.renderGroup(groupBtn.dataset.group);
      });
    });

    document.querySelectorAll('[data-tab]').forEach(tab => {
      tab.addEventListener('click', (e) => {
        e.preventDefault();
        this.renderTab(tab.dataset.tab);
      });
    });

    document.querySelectorAll('.primary-tabs, .secondary-tabs').forEach(tablist => {
      tablist.addEventListener('keydown', (event) => this._handleTabKeydown(event));
    });
  },

  renderGroup(groupName) {
    const group = this.TAB_GROUPS[groupName];
    if (!group) return;

    const storedTab = this._normalizeTab(this._readStorage(`ic.dashboard.group.${groupName}`) || '');
    const tabName = group.tabs.includes(storedTab) ? storedTab : group.tabs[0];
    this.renderTab(tabName);
  },

  /**
   * Switch to a specific tab and render its content
   */
  renderTab(tabName) {
    tabName = this._normalizeTab(tabName);
    console.log(`IC.renderTab: Switching to ${tabName}`);

    const groupName = this._groupForTab(tabName) || 'core';

    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(tab => {
      tab.classList.remove('active');
      tab.setAttribute('aria-hidden', 'true');
    });

    // Deactivate all buttons
    document.querySelectorAll('[data-group], [data-tab]').forEach(btn => {
      btn.classList.remove('active');
      btn.setAttribute('aria-selected', 'false');
    });

    document.querySelectorAll('[data-secondary-group]').forEach(group => {
      const active = group.dataset.secondaryGroup === groupName;
      group.classList.toggle('active', active);
      group.setAttribute('aria-hidden', active ? 'false' : 'true');
    });

    // Show selected tab
    const tabContent = document.getElementById(`${tabName}-tab`);
    if (tabContent) {
      tabContent.classList.add('active');
      tabContent.setAttribute('aria-hidden', 'false');
    }

    // Activate button
    const groupBtn = document.querySelector(`[data-group="${groupName}"]`);
    if (groupBtn) {
      groupBtn.classList.add('active');
      groupBtn.setAttribute('aria-selected', 'true');
    }

    const activeBtn = document.querySelector(`[data-tab="${tabName}"]`);
    if (activeBtn) {
      activeBtn.classList.add('active');
      activeBtn.setAttribute('aria-selected', 'true');
    }

    this.currentGroup = groupName;
    this.currentTab = tabName;
    this._writeStorage('ic.dashboard.group', groupName);
    this._writeStorage('ic.dashboard.tab', tabName);
    this._writeStorage(`ic.dashboard.group.${groupName}`, tabName);
    this._renderSubtabs(tabName);

    // Lazy-render tab bodies on first visit so initial load stays light.
    const renderFn = this['render' + this._toMethodName(tabName)];
    if (!this.renderedTabs.has(tabName) && renderFn && typeof renderFn === 'function') {
      renderFn.call(this);
      this.renderedTabs.add(tabName);
    }
  },

  // =========================================================================
  // TAB RENDERERS
  // =========================================================================

  renderHoldings() {
    const data = this.data.holdings;
    const summary = data?.summary || {};
    const holdings = data?.top_holdings || [];
    const sectors = data?.sector_weights || {};

    const summaryHtml = `
      <div class="section-header">
        <h2>Portfolio Holdings</h2>
        <div class="navbar-status">
          <div class="total-value">Total: $${this._fmt(summary.total_value)}</div>
          <div class="last-update">As of ${summary.as_of || 'N/A'}</div>
        </div>
      </div>
      <div class="holdings-summary">
        <div class="summary-card">
          <div class="card-label">Total Value</div>
          <div class="card-value">$${this._fmt(summary.total_value)}</div>
        </div>
        <div class="summary-card">
          <div class="card-label">Positions</div>
          <div class="card-value">${summary.position_count || 0}</div>
        </div>
      </div>

      <h3>Top Holdings</h3>
      <table class="holdings-table">
        <thead><tr><th>Symbol</th><th>Value</th><th>Weight</th></tr></thead>
        <tbody>
          ${holdings.map(h => `<tr><td>${h.symbol}</td><td>$${this._fmt(h.value)}</td><td>${this._pct(h.pct)}</td></tr>`).join('')}
        </tbody>
      </table>

      <h3>Sectors</h3>
      <table class="sector-table">
        <thead><tr><th>Sector</th><th>Weight</th></tr></thead>
        <tbody>
          ${Object.entries(sectors).map(([s, p]) => `<tr><td>${s}</td><td>${this._pct(p)}</td></tr>`).join('')}
        </tbody>
      </table>
    `;

    this._setContent('holdings-content', summaryHtml);
  },

  renderPerformance() {
    const data = this.data.performance;
    if (!data) return this.showNoData('performance');

    const returns = data.returns || {};
    const html = `
      <div class="section-header"><h2>Performance Metrics</h2></div>
      <div class="performance-metrics">
        <div class="metric-card">
          <div class="metric-label">1Y Return</div>
          <div class="metric-value ${returns['1y'] >= 0 ? 'positive' : 'negative'}">
            ${this._pct(returns['1y'] || 0)}
          </div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Sharpe Ratio</div>
          <div class="metric-value">${(data.sharpe || 0).toFixed(2)}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Volatility</div>
          <div class="metric-value">${this._pct(data.volatility || 0)}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Max Drawdown</div>
          <div class="metric-value negative">${this._pct(data.max_drawdown || 0)}</div>
        </div>
      </div>
    `;
    this._setContent('performance-content', html);
  },

  renderBonds() {
    const data = this.data.bonds;
    if (!data) return this.showNoData('bonds');

    const summary = data.summary || {};
    const html = `
      <div class="section-header"><h2>Bond Analysis</h2></div>
      <div class="bonds-summary">
        <div class="summary-card">
          <div class="card-label">Total Value</div>
          <div class="card-value">$${this._fmt(summary.total_value)}</div>
        </div>
        <div class="summary-card">
          <div class="card-label">Avg YTM</div>
          <div class="card-value">${this._pct(summary.avg_ytm || 0)}</div>
        </div>
        <div class="summary-card">
          <div class="card-label">Avg Duration</div>
          <div class="card-value">${(summary.avg_duration || 0).toFixed(1)}</div>
        </div>
      </div>
      <div id="bond-ladder" class="bond-ladder"></div>
    `;
    this._setContent('bonds-content', html);
    if (typeof window.ChartsExt?.renderBondLadder === 'function') {
      window.ChartsExt.renderBondLadder(data, 'bond-ladder');
    }
  },

  renderAnalyst() {
    const data = this.data.analyst;
    if (!data || !data.recommendations) return this.showNoData('analyst');

    const recs = Object.entries(data.recommendations || {});
    const html = `
      <div class="section-header"><h2>Analyst Consensus</h2></div>
      <table class="analyst-ratings-table">
        <thead><tr><th>Symbol</th><th>Consensus</th><th>Target</th><th>Current</th></tr></thead>
        <tbody>
          ${recs.map(([sym, rec]) => `
            <tr>
              <td>${sym}</td>
              <td>${rec.consensus || 'N/A'}</td>
              <td>$${this._fmt(rec.target_price_mean)}</td>
              <td>$${this._fmt(rec.current_price)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    this._setContent('analyst-content', html);
  },

  renderNews() {
    const data = this.data.news;
    if (!data) return this.showNoData('news');

    const headlines = data.top_positive || [];
    const posture = data.posture || 'neutral';
    const sentimentMap = { 'Positive': 'positive', 'Negative': 'negative', 'Neutral': 'neutral' };
    const html = `
      <div class="section-header">
        <h2>News & Sentiment</h2>
        <span class="sentiment-score ${sentimentMap[posture] || 'neutral'}">
          ${posture}
        </span>
      </div>
      <div class="sentiment-narrative">${data.narrative || 'No narrative available'}</div>
      <div id="news-sentiment-chart"></div>
      <h3>Top Stories</h3>
      <div class="news-timeline">
        ${headlines.map(h => `
          <div class="news-item positive">
            <div class="news-headline">${h.title}</div>
            <div class="news-meta">${h.symbol}</div>
          </div>
        `).join('')}
      </div>
    `;
    this._setContent('news-content', html);
    if (typeof window.ChartsExt?.renderSentimentTimeline === 'function') {
      window.ChartsExt.renderSentimentTimeline(data, 'news-sentiment-chart');
    }
  },

  renderCashflow() {
    const data = this.data.cashflow;
    if (!data) return this.showNoData('cashflow');

    const events = data.events || [];
    const summary = data.summary || {};
    const html = `
      <div class="section-header"><h2>Cashflow & Dividends</h2></div>
      <div class="cashflow-summary">
        <div class="summary-card">
          <div class="card-label">Annual Dividend</div>
          <div class="card-value">$${this._fmt(summary.annual_dividend || 0)}</div>
        </div>
      </div>
      <table class="cashflow-table">
        <thead><tr><th>Date</th><th>Symbol</th><th>Amount</th></tr></thead>
        <tbody>
          ${events.map(e => `<tr><td>${e.date}</td><td>${e.symbol}</td><td>$${this._fmt(e.amount)}</td></tr>`).join('')}
        </tbody>
      </table>
    `;
    this._setContent('cashflow-content', html);
  },

  renderOptimize() {
    const data = this.data.optimize;
    if (!data) return this.showNoData('optimize');

    const trades = data.trades || [];
    const html = `
      <div class="section-header"><h2>Portfolio Optimization</h2></div>
      <div id="efficient-frontier"></div>
      <h3>Suggested Trades</h3>
      <table class="rebalance-trades">
        <thead><tr><th>Symbol</th><th>Action</th><th>Shares</th><th>Value</th></tr></thead>
        <tbody>
          ${trades.map(t => `
            <tr class="trade-row ${t.action.toLowerCase()}">
              <td>${t.symbol}</td>
              <td>${t.action}</td>
              <td>${(t.shares || 0).toFixed(2)}</td>
              <td>$${this._fmt(t.value)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    this._setContent('optimize-content', html);
    if (typeof window.ChartsExt?.renderEfficientFrontier === 'function') {
      window.ChartsExt.renderEfficientFrontier(data, 'efficient-frontier');
    }
  },

  renderSynthesis() {
    const data = this.data.synthesis;
    if (!data) return this.showNoData('synthesis');

    const brief = data.brief || '';
    const insights = data.key_insights || [];
    const html = `
      <div class="section-header"><h2>Portfolio Analysis</h2></div>
      <div class="synthesis-brief">${brief}</div>
      ${insights.length > 0 ? `
        <h3>Key Insights</h3>
        <div class="key-insights">
          ${insights.map(i => `<div class="insight-item">• ${i}</div>`).join('')}
        </div>
      ` : ''}
    `;
    this._setContent('synthesis-content', html);
  },

  renderWhatchanged() {
    const data = this.data.what_changed;
    if (!data) return this.showNoData('whatchanged');

    const movers = data.top_movers || [];
    const html = `
      <div class="section-header"><h2>What Changed</h2></div>
      <div id="attribution-waterfall"></div>
      <h3>Top Movers</h3>
      <div class="top-movers">
        ${movers.map(m => `
          <div class="mover-card ${m.impact > 0 ? 'positive' : 'negative'}">
            <div class="mover-symbol">${m.symbol}</div>
            <div class="mover-impact">${this._pct(m.impact)}</div>
          </div>
        `).join('')}
      </div>
    `;
    this._setContent('whatchanged-content', html);
    if (typeof window.ChartsExt?.renderAttributionWaterfall === 'function') {
      window.ChartsExt.renderAttributionWaterfall(data, 'attribution-waterfall');
    }
  },

  renderRebalancetax() {
    const data = this.data.rebalance_tax;
    if (!data) return this.showNoData('rebalancetax');

    const trades = data.trades || [];
    const summary = data.summary || {};
    const html = `
      <div class="section-header"><h2>Tax-Aware Rebalancing</h2></div>
      <div class="tax-summary">
        <div class="summary-card">
          <div class="card-label">Est. Tax Impact</div>
          <div class="card-value">$${this._fmt(summary.estimated_tax)}</div>
        </div>
      </div>
      <table class="tax-trade-list">
        <thead><tr><th>Symbol</th><th>Action</th><th>Tax Impact</th></tr></thead>
        <tbody>
          ${trades.map(t => `
            <tr>
              <td>${t.symbol}</td>
              <td>${t.action}</td>
              <td class="${t.tax_impact > 0 ? 'negative' : 'positive'}">$${this._fmt(t.tax_impact)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    this._setContent('rebalancetax-content', html);
  },

  renderRebalanctax() {
    return this.renderRebalancetax();
  },

  renderRecommendations() {
    const data = this.data.synthesis || {};
    const candidates = data.recommendations || data.action_items || data.key_insights || [];
    const items = Array.isArray(candidates)
      ? candidates
      : Object.values(candidates).flat();

    const html = `
      <div class="section-header"><h2>Recommendations</h2></div>
      <div class="priority-recommendations">
        ${items.length > 0 ? items.map((item, idx) => `
          <div class="insight-item priority-${idx < 3 ? 'high' : 'medium'}">
            <span class="insight-bullet">${idx < 3 ? 'High' : 'Monitor'}</span>
            <span>${this._escape(String(item))}</span>
          </div>
        `).join('') : `
          <div class="no-data">
            <p>No actionable recommendations available. Run synthesis to populate this view.</p>
          </div>
        `}
      </div>
    `;
    this._setContent('recommendations-content', html);
  },

  renderScenarios() {
    const data = this.data.scenarios;
    if (!data) return this.showNoData('scenarios');

    const scenarios = data.scenarios || [];
    const html = `
      <div class="section-header"><h2>Scenario Analysis</h2></div>
      <div id="scenario-results"></div>
      <div class="scenario-list">
        ${scenarios.map(s => `
          <div class="scenario-item">
            <h4>${s.name}</h4>
            <div class="scenario-outcome ${s.portfolio_impact > 0 ? 'positive' : 'negative'}">
              ${this._pct(s.portfolio_impact)}
            </div>
          </div>
        `).join('')}
      </div>
    `;
    this._setContent('scenarios-content', html);
    if (typeof window.ChartsExt?.renderScenarios === 'function') {
      window.ChartsExt.renderScenarios(data, 'scenario-results');
    }
  },

  renderPeer() {
    const data = this.data.peer_analysis;
    if (!data) return this.showNoData('peer');

    const summary = data.summary || {};
    const html = `
      <div class="section-header"><h2>Peer & Factor Analysis</h2></div>
      <div id="beta-matrix"></div>
      <div class="peer-metrics">
        <div class="summary-card">
          <div class="card-label">Portfolio Beta</div>
          <div class="card-value">${(summary.portfolio_beta || 0).toFixed(2)}</div>
        </div>
      </div>
    `;
    this._setContent('peer-content', html);
    if (typeof window.ChartsExt?.renderBetaMatrix === 'function') {
      window.ChartsExt.renderBetaMatrix(data, 'beta-matrix');
    }
  },

  renderReports() {
    const html = `
      <div class="section-header"><h2>Reports & Export</h2></div>
      <div class="export-controls">
        <button class="export-btn" onclick="IC.exportCSV()">📊 Export CSV</button>
        <button class="export-btn" onclick="IC.exportJSON()">📋 Export JSON</button>
        <button class="export-btn" onclick="window.print()">🖨️ Print PDF</button>
      </div>
    `;
    this._setContent('reports-content', html);
  },

  renderSettings() {
    const settings = this.data.settings || {};
    const html = `
      <div class="section-header"><h2>Settings</h2></div>
      <div class="settings-section">
        <h3>Data Provider</h3>
        <p>${settings.provider || 'yfinance'}</p>
      </div>
      <div class="settings-section">
        <h3>Risk Profile</h3>
        <p>${settings.risk_profile || 'moderate'}</p>
      </div>
    `;
    this._setContent('settings-content', html);
  },

  renderAbout() {
    const metadata = this.data.metadata || {};
    const html = `
      <div class="section-header"><h2>About</h2></div>
      <div class="about-content">
        <p>InvestorClaw v${this.data.version || '2.0.0'}</p>
        <p>Multi-account portfolio analysis. Educational purposes only.</p>
        <p>Generated: ${new Date(this.data.timestamp).toLocaleString()}</p>
      </div>
    `;
    this._setContent('about-content', html);
  },

  // =========================================================================
  // HELPERS
  // =========================================================================

  _fmt(n) {
    return new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n || 0);
  },

  _pct(n) {
    return new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 2 }).format(n || 0);
  },

  _setContent(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  },

  _normalizeTab(tabName) {
    if (tabName === 'rebalanctax' || tabName === 'rebalance-tax') return 'rebalancetax';
    if (tabName === 'what-changed') return 'whatchanged';
    return tabName || 'holdings';
  },

  _toMethodName(tabName) {
    return tabName
      .split('-')
      .map(part => part.charAt(0).toUpperCase() + part.slice(1))
      .join('');
  },

  _groupForTab(tabName) {
    return Object.entries(this.TAB_GROUPS)
      .find(([, group]) => group.tabs.includes(tabName))?.[0];
  },

  _tabExists(tabName) {
    return Boolean(this._groupForTab(tabName) && document.getElementById(`${tabName}-tab`));
  },

  _renderSubtabs(tabName) {
    const labels = this.SUBVIEWS[tabName] || [];
    const section = document.getElementById(`${tabName}-tab`);
    if (!section || labels.length === 0 || section.querySelector('.subview-tabs')) return;

    const key = `ic.dashboard.subtab.${tabName}`;
    const saved = this._readStorage(key);
    const activeLabel = labels.includes(saved) ? saved : labels[0];
    const nav = document.createElement('div');
    nav.className = 'subview-tabs';
    nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', `${tabName} sub-views`);

    labels.forEach(label => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'subview-tab';
      button.dataset.subtab = label;
      button.textContent = label;
      button.setAttribute('role', 'tab');
      const active = label === activeLabel;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
      button.addEventListener('click', () => {
        nav.querySelectorAll('.subview-tab').forEach(tab => {
          tab.classList.remove('active');
          tab.setAttribute('aria-selected', 'false');
        });
        button.classList.add('active');
        button.setAttribute('aria-selected', 'true');
        this._writeStorage(key, label);
      });
      nav.appendChild(button);
    });

    const header = section.querySelector('.section-header');
    if (header) {
      header.insertAdjacentElement('afterend', nav);
    } else {
      section.prepend(nav);
    }
  },

  _handleTabKeydown(event) {
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
    const buttons = Array.from(event.currentTarget.querySelectorAll('button'));
    const enabled = buttons.filter(btn => !btn.disabled && btn.offsetParent !== null);
    if (enabled.length === 0) return;
    const currentIndex = Math.max(0, enabled.indexOf(document.activeElement));
    let nextIndex = currentIndex;
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % enabled.length;
    if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + enabled.length) % enabled.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = enabled.length - 1;
    event.preventDefault();
    enabled[nextIndex].focus();
    enabled[nextIndex].click();
  },

  _readStorage(key) {
    try {
      return window.localStorage?.getItem(key);
    } catch (_err) {
      return null;
    }
  },

  _writeStorage(key, value) {
    try {
      window.localStorage?.setItem(key, value);
    } catch (_err) {
      // Ignore private-mode and embedded-browser storage failures.
    }
  },

  _escape(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  },

  showNoData(tab) {
    this._setContent(`${tab}-content`, '<div class="no-data"><p>No data available</p></div>');
  },

  showError(msg) {
    const main = document.querySelector('main');
    if (main) main.innerHTML = `<div class="error-message"><p>${msg}</p></div>`;
  },

  exportCSV() {
    alert('CSV export coming soon');
  },

  exportJSON() {
    const json = JSON.stringify(this.data, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `portfolio_${new Date().toISOString().split('T')[0]}.json`;
    a.click();
    URL.revokeObjectURL(url);
  },

  refreshDashboard() {
    window.location.reload();
  },

  checkFreshness() {
    const status = document.getElementById('freshness-status');
    if (!status) return;

    const timestamp = this.data.generated_at
      || this.data.timestamp
      || this.data.summary?.generated_at
      || this.data.metadata?.generated_at;
    const generatedAt = timestamp ? new Date(timestamp) : null;
    if (!generatedAt || Number.isNaN(generatedAt.getTime())) {
      status.textContent = 'Static artifact';
      status.className = 'freshness-status unknown';
      return;
    }

    const ageMs = Date.now() - generatedAt.getTime();
    const ageMinutes = Math.max(0, Math.floor(ageMs / 60000));
    const stale = ageMs > 5 * 60 * 1000;
    status.textContent = stale ? `Stale ${ageMinutes}m` : `Fresh ${ageMinutes}m`;
    status.className = stale ? 'freshness-status stale' : 'freshness-status fresh';
  }
};

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => IC.init());
