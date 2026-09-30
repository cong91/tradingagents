"""Control-panel web API for TradingAgents (docs/ui-api-contract.md).

The server wraps the existing engine without touching ``tradingagents/`` or
``cli/``. Run it from the repository root:

    .venv\\Scripts\\python.exe -m uvicorn server.main:app --port 8000
"""
