#!/bin/sh
# Render entrypoint for the AutoGPT backend image.
#
# Render's `dockerCommand` runs a SINGLE command with arguments (env vars ARE
# expanded), NOT a shell script. A multi-statement `export ...; exec ...` body
# is handed to the shell as one command token and exits 127. So the per-service
# startup logic lives here and is invoked as one command: `entrypoint.sh <role>`.
#
# Wrap the Render-provided connection strings with ?schema=platform (Render
# can't interpolate env in YAML) so Prisma resolves the `platform` schema.
# GoTrue owns the `auth` schema and must NOT use this image.
set -eu

export DATABASE_URL="${RENDER_DATABASE_URL}?schema=platform"
export DIRECT_URL="${RENDER_DIRECT_URL}?schema=platform"

# Absolute venv bin dir. Overridable only so the script is testable locally.
BIN="${RENDER_VENV_BIN:-/app/autogpt_platform/backend/.venv/bin}"

role="${1:-}"
case "$role" in
  rest)
    export PLATFORM_BASE_URL="${PLATFORM_BASE_URL:-$RENDER_EXTERNAL_URL}"
    export BACKEND_CORS_ALLOW_ORIGINS="[\"$FRONTEND_BASE_URL\"]"
    exec env AGENT_API_PORT="$PORT" "$BIN/rest"
    ;;
  ws)
    export BACKEND_CORS_ALLOW_ORIGINS="[\"$FRONTEND_BASE_URL\"]"
    exec env WEBSOCKET_SERVER_PORT="$PORT" "$BIN/ws"
    ;;
  scheduler)
    exec env EXECUTION_SCHEDULER_PORT="$PORT" "$BIN/scheduler"
    ;;
  db)
    exec env DATABASE_API_PORT="$PORT" "$BIN/db"
    ;;
  migrate)
    # GoTrue's stock image can't bootstrap its `auth` schema on Render: its first
    # migration runs `CREATE TABLE auth.users` and aborts with `schema "auth"
    # does not exist` if the schema is absent (in the stock Supabase stack the
    # Postgres init scripts create it; on Render nothing does). rest-server is the
    # sole DB-DDL owner (inv #3), so create it here on the RAW url (no
    # ?schema=platform wrapper) before Prisma runs, so it lands in the database
    # default rather than scoped under `platform`.
    #
    # GoTrue's OTHER migration gap: add_mfa_schema creates enum types with
    # UNqualified DDL (`create type factor_type ...`) that land in search_path[0],
    # while a later migration references `auth.factor_type` explicitly and fails
    # unless those enums landed in `auth`. GoTrue's pop/pgx driver sets no
    # search_path of its own (internal/storage/dial.go), so it inherits the
    # connecting role's default. Pin `auth` first on that role's default here
    # (GoTrue and this migrate step share the same DB role). Backend services are
    # unaffected: they connect with ?schema=platform, which sets a per-session
    # search_path that overrides this default.
    "$BIN/prisma" db execute --url "$RENDER_DIRECT_URL" --stdin <<'SQL'
CREATE SCHEMA IF NOT EXISTS auth;
ALTER ROLE CURRENT_USER SET search_path = auth, public, extensions;
SQL
    exec "$BIN/prisma" migrate deploy
    ;;
  *)
    echo "entrypoint.sh: unknown role '${role}' (expected: rest|ws|scheduler|db|migrate)" >&2
    exit 64
    ;;
esac
