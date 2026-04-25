/**
 * Data Loader Abstraction - Pluggable data sources for three deployment modes
 *
 * Detects deployment mode and loads appropriate data:
 * - file:// → OpenClaw (embedded JSON)
 * - http://localhost → Claude Basic (server API)
 * - https:// → Enterprise (OAuth + API)
 */

async function detectDeploymentMode() {
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;

    if (protocol === 'file:') {
        return 'openclaw';
    } else if (protocol === 'http:' && (hostname === 'localhost' || hostname === '127.0.0.1')) {
        return 'claude-basic';
    } else {
        return 'enterprise';
    }
}

async function loadData() {
    const mode = await detectDeploymentMode();

    try {
        switch (mode) {
            case 'openclaw':
                return await loadOpenClawData();
            case 'claude-basic':
                return await loadClaudeBasicData();
            case 'enterprise':
                return await loadEnterpriseData();
            default:
                throw new Error(`Unknown deployment mode: ${mode}`);
        }
    } catch (error) {
        console.error('Failed to load data:', error);
        return {
            error: `Failed to load portfolio data: ${error.message}`,
            holdings: { summary: {}, top_equity: [], sector_breakdown: {} },
            performance: {},
            bonds: {},
            analyst: {},
            metadata: { as_of: new Date().toISOString(), version: '2.0.0' }
        };
    }
}

/**
 * OpenClaw mode: Read from embedded JSON in window.IC_DATA
 * Works completely offline with file:// URLs
 */
async function loadOpenClawData() {
    if (typeof window.IC_DATA === 'undefined') {
        throw new Error('OpenClaw data not embedded in HTML');
    }

    console.log('[OpenClaw] Loaded data from embedded JSON');
    return window.IC_DATA;
}

/**
 * Claude Basic mode: Fetch from /api/data endpoint
 * Server provides same JSON schema as OpenClaw
 */
async function loadClaudeBasicData() {
    const response = await fetch('/api/data', {
        method: 'GET',
        headers: {
            'Content-Type': 'application/json'
        }
    });

    if (!response.ok) {
        throw new Error(`Server error: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    console.log('[Claude Basic] Loaded data from /api/data');
    return data;
}

/**
 * Enterprise mode: Fetch from /api/data with OAuth token
 * Enforces RBAC via server-side authorization
 */
async function loadEnterpriseData() {
    const token = localStorage.getItem('auth_token');

    if (!token) {
        // Redirect to OAuth login
        window.location.href = '/login';
        throw new Error('Not authenticated');
    }

    const response = await fetch('/api/data', {
        method: 'GET',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
        }
    });

    if (response.status === 401) {
        // Token expired, redirect to login
        localStorage.removeItem('auth_token');
        window.location.href = '/login';
        throw new Error('Authentication required');
    }

    if (!response.ok) {
        throw new Error(`Server error: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    console.log('[Enterprise] Loaded data from /api/data with OAuth');
    return data;
}

/**
 * Validate data schema
 */
function validateData(data) {
    const requiredKeys = ['holdings', 'performance', 'bonds', 'analyst', 'metadata'];
    for (const key of requiredKeys) {
        if (!(key in data)) {
            console.warn(`Missing expected key in data: ${key}`);
        }
    }
    return data;
}

// Export for use in app.js
window.DataLoader = {
    loadData,
    detectDeploymentMode,
    validateData
};
