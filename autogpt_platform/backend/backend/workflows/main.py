"""Entry point for the Render Workflows service.

Manual Dashboard setup (Render Blueprints cannot declare Workflows):
  - New > Workflow, link this repo
  - Root Directory:  autogpt_platform/backend
  - Build Command:   poetry install && poetry run pip install --no-deps
                     --require-hashes -r render_sdk.requirements.txt
                     (that file is the single source of truth for the pin; do
                     NOT restate the version here — see backend/Dockerfile,
                     which installs it the same way)
  - Start Command:   poetry run python -m backend.workflows.main
  - Env: DATABASE_URL/DIRECT_URL (+ ?schema=platform), REDIS_* (standalone),
    RENDER_API_KEY, EXECUTION_BACKEND=workflows, RENDER_WORKFLOW_SLUG,
    ENCRYPTION_KEY, UNSUBSCRIBE_SECRET_KEY, and JWT_VERIFY_KEY. ENCRYPTION_KEY
    and JWT_VERIFY_KEY MUST be copied verbatim from rest-server (both are
    generated/owned there); a mismatch breaks credential decryption / auth.

Registers the graph-execution task(s) and starts the SDK task server.
"""

from backend.workflows.tasks import app


def main() -> None:
    app.start()


if __name__ == "__main__":
    main()
