"""Entry point for the Render Workflows service.

Manual Dashboard setup (Render Blueprints cannot declare Workflows):
  - New > Workflow, link this repo
  - Root Directory:  autogpt_platform/backend
  - Build Command:   poetry install
  - Start Command:   poetry run python -m backend.workflows.main
  - Env: DATABASE_URL/DIRECT_URL (+ ?schema=platform), REDIS_* (standalone),
    RENDER_API_KEY, EXECUTION_BACKEND=workflows, RENDER_WORKFLOW_SLUG,
    ENCRYPTION_KEY, JWT_VERIFY_KEY, and the app secrets group — same as the
    executor service.

Registers the graph-execution task(s) and starts the SDK task server.
"""

from backend.workflows.tasks import app


def main() -> None:
    app.start()


if __name__ == "__main__":
    main()
