"""Start the Shoppie Copilot AgentCore app on port 8080 for Runtime deployments."""

import os

import uvicorn

from agent.copilot_entrypoint import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))  # noqa: S104
