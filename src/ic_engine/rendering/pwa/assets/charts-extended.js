/**
 * InvestorClaw Extended Charts
 * Plotly.js chart renderers for dashboard tabs
 */

const ChartsExt = {
  /**
   * Render bond ladder (maturity bucketing)
   */
  renderBondLadder(data, containerId) {
    if (!data?.ladder) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const ladder = data.ladder.map(b => ({
      x: b.maturity,
      y: b.value,
      name: b.maturity
    }));

    const trace = {
      x: ladder.map(l => l.x),
      y: ladder.map(l => l.y),
      type: 'bar',
      marker: { color: '#3498db' }
    };

    const layout = {
      title: 'Bond Maturity Ladder',
      xaxis: { title: 'Maturity Year' },
      yaxis: { title: 'Value ($)' },
      hovermode: 'closest'
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  },

  /**
   * Render news sentiment timeline
   */
  renderSentimentTimeline(data, containerId) {
    if (!data?.timeline) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const timeline = data.timeline || [];
    const trace = {
      x: timeline.map(t => t.date),
      y: timeline.map(t => t.sentiment),
      type: 'scatter',
      mode: 'lines+markers',
      name: 'Sentiment',
      line: { color: '#2ecc71', width: 2 },
      marker: { size: 6 }
    };

    const layout = {
      title: 'News Sentiment Timeline',
      xaxis: { title: 'Date' },
      yaxis: { title: 'Sentiment Score' },
      hovermode: 'x unified'
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  },

  /**
   * Render efficient frontier (risk vs return)
   */
  renderEfficientFrontier(data, containerId) {
    if (!data?.efficient_frontier) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const ef = data.efficient_frontier;
    const trace = {
      x: ef.x,
      y: ef.y,
      mode: 'markers',
      type: 'scatter',
      marker: {
        size: 8,
        color: ef.y,
        colorscale: 'Viridis',
        showscale: true
      },
      text: ef.labels || [],
      hovertemplate: '<b>%{text}</b><br>Risk: %{x:.2f}<br>Return: %{y:.2f}<extra></extra>'
    };

    const layout = {
      title: 'Efficient Frontier',
      xaxis: { title: 'Risk (Volatility)' },
      yaxis: { title: 'Expected Return' },
      hovermode: 'closest'
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  },

  /**
   * Render attribution waterfall (factor contributions)
   */
  renderAttributionWaterfall(data, containerId) {
    if (!data?.factor_breakdown) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const factors = data.factor_breakdown || [];
    const trace = {
      type: 'waterfall',
      name: 'Attribution',
      x: factors.map(f => f.factor),
      y: factors.map(f => f.contribution),
      connector: { line: { color: '#95a5a6' } }
    };

    const layout = {
      title: 'Performance Attribution',
      xaxis: { title: 'Factor' },
      yaxis: { title: 'Contribution (%)' }
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  },

  /**
   * Render beta matrix heatmap
   */
  renderBetaMatrix(data, containerId) {
    if (!data?.beta_matrix) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const matrix = data.beta_matrix;
    const trace = {
      z: matrix.values,
      x: matrix.benchmarks,
      y: matrix.symbols,
      type: 'heatmap',
      colorscale: 'RdBu',
      zmid: 1
    };

    const layout = {
      title: 'Beta Matrix',
      xaxis: { title: 'Benchmark' },
      yaxis: { title: 'Holdings' }
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  },

  /**
   * Render scenario outcomes
   */
  renderScenarios(data, containerId) {
    if (!data?.scenarios) return;

    const container = document.getElementById(containerId);
    if (!container || typeof Plotly === 'undefined') return;

    const scenarios = data.scenarios || [];
    const trace = {
      x: scenarios.map(s => s.name),
      y: scenarios.map(s => s.portfolio_impact),
      type: 'bar',
      marker: {
        color: scenarios.map(s => s.portfolio_impact > 0 ? '#2ecc71' : '#e74c3c')
      }
    };

    const layout = {
      title: 'Scenario Outcomes',
      xaxis: { title: 'Scenario' },
      yaxis: { title: 'Portfolio Impact (%)' }
    };

    Plotly.newPlot(containerId, [trace], layout, { responsive: true });
  }
};

// Export for global access
if (typeof window !== 'undefined') {
  window.ChartsExt = ChartsExt;
}
