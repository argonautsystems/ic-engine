#!/bin/bash
# Dashboard Launcher - generates dashboard and opens in system browser (local execution)

set -e

# Run the dashboard command
python3 investorclaw.py dashboard "$@"

# Find the most recently created dashboard (use ls for better macOS compatibility)
DASHBOARD_FILE=$(ls -t ~/portfolio_reports/*/dashboard.html 2>/dev/null | head -1)

if [ -z "$DASHBOARD_FILE" ]; then
    echo "Error: Dashboard file not found" >&2
    exit 1
fi

echo "📊 Dashboard generated: $DASHBOARD_FILE"

# Spawn browser (works on macOS/Linux with xdg-open)
if command -v open &> /dev/null; then
    # macOS
    echo "🌐 Opening in default browser..."
    open "$DASHBOARD_FILE"
elif command -v xdg-open &> /dev/null; then
    # Linux
    echo "🌐 Opening in default browser..."
    xdg-open "$DASHBOARD_FILE"
else
    echo "⚠️  Could not find 'open' or 'xdg-open'. Manually open: $DASHBOARD_FILE"
    exit 1
fi

echo "✅ Dashboard spawned successfully"
