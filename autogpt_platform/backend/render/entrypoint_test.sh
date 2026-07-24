#!/bin/sh
# Tests for render/entrypoint.sh. Stubs the venv bin (RENDER_VENV_BIN) with fake
# `prisma`/service binaries that record how they were invoked, then asserts the
# migrate role creates GoTrue's `auth` schema before running `prisma migrate deploy`.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/entrypoint.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }

# Build a throwaway fake venv bin dir. Each fake binary appends its argv to
# $CALLS so the test can inspect the exact invocation order.
setup() {
  TMP="$(mktemp -d)"
  BIN="$TMP/bin"
  mkdir -p "$BIN"
  CALLS="$TMP/calls.log"
  : > "$CALLS"

  cat > "$BIN/prisma" <<EOF
#!/bin/sh
# Record argv, then drain stdin so pipelines don't SIGPIPE.
echo "prisma \$*" >> "$CALLS"
cat > "$TMP/stdin.\$\$" 2>/dev/null || true
cat "$TMP/stdin.\$\$" >> "$CALLS" 2>/dev/null || true
EOF
  chmod +x "$BIN/prisma"

  export RENDER_VENV_BIN="$BIN"
  export RENDER_DATABASE_URL="postgres://u:p@host:5432/db"
  export RENDER_DIRECT_URL="postgres://u:p@host:5432/db"
}

teardown() { rm -rf "$TMP"; }

# --- migrate role creates the auth schema before deploying migrations ---
setup
sh "$ENTRYPOINT" migrate

grep -q "CREATE SCHEMA IF NOT EXISTS auth" "$CALLS" \
  || fail "migrate role did not create the auth schema"

grep -q "prisma migrate deploy" "$CALLS" \
  || fail "migrate role did not run prisma migrate deploy"

# Schema creation must happen BEFORE migrate deploy.
schema_line=$(grep -n "CREATE SCHEMA IF NOT EXISTS auth" "$CALLS" | head -1 | cut -d: -f1)
deploy_line=$(grep -n "prisma migrate deploy" "$CALLS" | head -1 | cut -d: -f1)
[ "$schema_line" -lt "$deploy_line" ] \
  || fail "auth schema must be created before migrate deploy (schema=$schema_line deploy=$deploy_line)"

# The schema must be created on the RAW url (no ?schema=platform wrapper) so it
# lands in the database default, not scoped under the platform schema.
grep -q -- "--url $RENDER_DIRECT_URL " "$CALLS" \
  || grep -q -- "--url $RENDER_DIRECT_URL$" "$CALLS" \
  || fail "auth schema not created against the raw DIRECT_URL"

# The migrate role must pin the connecting role's default search_path with `auth`
# first. GoTrue connects with the same DB role and its pop/pgx driver sets no
# search_path of its own, so it inherits this default; `auth` first makes its
# unqualified `create type` DDL land in `auth` (see entrypoint.sh migrate role).
grep -q "ALTER ROLE CURRENT_USER SET search_path = auth" "$CALLS" \
  || fail "migrate role did not set the role default search_path with auth first"

# The role search_path must also be set on the RAW url (same statement stream as
# CREATE SCHEMA), not the ?schema=platform-wrapped one.
searchpath_line=$(grep -n "ALTER ROLE CURRENT_USER SET search_path = auth" "$CALLS" | head -1 | cut -d: -f1)
[ "$searchpath_line" -gt "$schema_line" ] \
  || fail "search_path should be set alongside/after CREATE SCHEMA on the raw url"
teardown

echo "PASS: entrypoint migrate role creates auth schema + pins auth-first search_path on the raw url before migrate deploy"
