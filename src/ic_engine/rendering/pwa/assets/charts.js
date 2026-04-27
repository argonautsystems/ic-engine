/**
 * Chart rendering using Plotly.js
 * Shared across all deployment modes
 */

const Charts = {
    /**
     * Render asset allocation pie chart
     */
    renderAssetAllocation(holdings) {
        if (!holdings.summary || !holdings.summary.position_count) {
            return;
        }

        const labels = [];
        const values = [];

        // Count positions by asset type
        const counts = holdings.summary.position_count || {};
        for (const [assetType, count] of Object.entries(counts)) {
            labels.push(assetType.charAt(0).toUpperCase() + assetType.slice(1));
            values.push(count);
        }

        const data = [{
            labels,
            values,
            type: 'pie',
            marker: { colors: ['#f0a000', '#2ecc71', '#e74c3c', '#3498db', '#9b59b6', '#1abc9c'] }
        }];

        const layout = {
            title: 'Asset Allocation by Type',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#ecf0f1' },
            margin: { l: 40, r: 40, t: 60, b: 40 }
        };

        Plotly.newPlot('asset-allocation-chart', data, layout, { responsive: true });
    },

    /**
     * Render sector breakdown pie chart
     */
    renderSectorBreakdown(holdings) {
        const sectors = holdings.sector_breakdown || holdings.sector_weights;
        if (!sectors) {
            return;
        }

        const labels = Object.keys(sectors);
        const values = Object.values(sectors).map(s => {
            if (s && typeof s === 'object') return s.weight || s.value || 0;
            return s || 0;
        });

        const data = [{
            labels,
            values,
            type: 'pie',
            marker: { colors: ['#f0a000', '#2ecc71', '#e74c3c', '#3498db', '#9b59b6', '#1abc9c', '#f39c12', '#34495e'] }
        }];

        const layout = {
            title: 'Sector Breakdown',
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#ecf0f1' },
            margin: { l: 40, r: 40, t: 60, b: 40 }
        };

        Plotly.newPlot('sector-breakdown-chart', data, layout, { responsive: true });
    },

    /**
     * Render returns chart (bar)
     */
    renderReturnsChart(performance) {
        if (!performance) {
            return;
        }

        const periods = ['1D', '1W', '1M', '3M', 'YTD', '1Y', '3Y', '5Y'];
        const returns = [
            performance.return_1d || 0,
            performance.return_1w || 0,
            performance.return_1m || 0,
            performance.return_3m || 0,
            performance.return_ytd || 0,
            performance.return_1y || 0,
            performance.return_3y || 0,
            performance.return_5y || 0
        ];

        const colors = returns.map(r => r >= 0 ? '#2ecc71' : '#e74c3c');

        const data = [{
            x: periods,
            y: returns,
            type: 'bar',
            marker: { color: colors }
        }];

        const layout = {
            title: 'Portfolio Returns by Period (%)',
            xaxis: { title: 'Period' },
            yaxis: { title: 'Return (%)' },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { color: '#ecf0f1' },
            margin: { l: 60, r: 40, t: 60, b: 60 }
        };

        Plotly.newPlot('returns-chart', data, layout, { responsive: true });
    },

    /**
     * Render top equity positions table
     */
    renderTopEquities(holdings) {
        if (!holdings.top_equity || holdings.top_equity.length === 0) {
            return;
        }

        const container = document.querySelector('.performance-metrics');
        if (!container) return;

        let html = '<h3>Top Equity Positions</h3><table class="data-table"><thead><tr><th>Symbol</th><th>Value</th><th>Weight</th><th>Return</th></tr></thead><tbody>';

        for (const equity of holdings.top_equity.slice(0, 10)) {
            const value = equity.value ?? equity.market_value ?? equity.marketValue ?? 0;
            const totalValue = holdings.summary?.total_value ?? holdings.summary?.totalPortfolioValue ?? 0;
            const weightValue = equity.weight_pct ?? equity.pct ?? (totalValue ? value / totalValue * 100 : 0);
            const weight = Number(weightValue || 0).toFixed(2);
            const returnValue = Number(equity.gl_pct ?? equity.unrealized_gain_loss_pct ?? equity.unrealizedGainLossPct ?? equity.unrealizedPct ?? 0);
            const returnColor = (returnValue >= 0) ? 'success' : 'negative';

            html += `<tr>
                <td>${equity.symbol}</td>
                <td>$${(value || 0).toLocaleString('en-US', { maximumFractionDigits: 0 })}</td>
                <td>${weight}%</td>
                <td class="${returnColor}">${returnValue.toFixed(2)}%</td>
            </tr>`;
        }

        html += '</tbody></table>';
        container.innerHTML = html;
    }
};

// Placeholder for Plotly - will be loaded via CDN or bundled
if (typeof Plotly === 'undefined') {
    window.Plotly = {
        newPlot: function() {
            console.warn('Plotly not loaded. Include Plotly.js via CDN or bundle.');
        }
    };
}

// Export for use in app.js
window.Charts = Charts;
