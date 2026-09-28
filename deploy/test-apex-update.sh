#!/usr/bin/env bash
# Integration test for `./apex update`.
#
# Builds a throwaway git remote + clone, puts precious local state in it
# (a SQLite database, a chroma blob, an ignored backend/.env, a hand-edited
# source file), then runs the real ./apex update with systemctl/sudo/npm
# stubbed so nothing touches the real machine. Verifies the databases survive
# the git override byte-for-byte.
#
# Run:  bash deploy/test-apex-update.sh
#
# Regression guard: the restore must not be a plain rsync sync. The backup is
# made with `cp -a`, so it keeps the live mtime, and git checks the committed
# database out in the same second. rsync's size+mtime quick check then skips
# the transfer and the restore silently does nothing. Both database files are
# page-aligned, so a 1-row and a 3-row database are byte-for-byte the same
# size — which is how that bug shipped unnoticed.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${TMPDIR:-/tmp}/apex-update-test"
rm -rf "$BASE" /tmp/apex-update.*; mkdir -p "$BASE"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_AUTHOR_DATE="2026-01-01T00:00:00Z" GIT_COMMITTER_DATE="2026-01-01T00:00:00Z"

PY="${APEX_TEST_PYTHON:-$REPO/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

# ── origin repo ──
git init -q --bare "$BASE/origin.git"
git init -q "$BASE/seed"
cd "$BASE/seed"
mkdir -p backend/data/chroma frontend
echo "print('v1')" > backend/app.py
echo "flask" > backend/requirements.txt
echo '{"name":"fe"}' > frontend/package.json
$PY - <<'PY'
import sqlite3
c = sqlite3.connect("backend/data/apex.db")
c.execute("create table chats(id integer primary key, body text)")
c.execute("insert into chats(body) values ('my precious history')")
c.commit(); c.close()
PY
echo "chroma-v1" > backend/data/chroma/blob.bin
git add -A && git commit -qm "v1"
git branch -M main
git remote add origin "$BASE/origin.git" && git push -q origin main

# ── working clone (the "deployment") ──
git clone -q "$BASE/origin.git" "$BASE/work"
cd "$BASE/work"
git branch -M v_2.0.27
mkdir -p frontend/node_modules && echo stub > frontend/node_modules/.keep

# local, precious state
$PY - <<'PY'
import sqlite3
c = sqlite3.connect("backend/data/apex.db")
c.execute("insert into chats(body) values ('chat #2 after v1')")
c.execute("insert into chats(body) values ('chat #3 local only')")
c.commit(); c.close()
PY
echo "SECRET_TOKEN=keepme" > backend/.env
printf 'backend/data/\nfrontend/node_modules/\nbackend/.env\n' > .gitignore
git add .gitignore && git commit -qm "ignore local state"
# a local code edit that the override should discard (but save as a patch)
echo "print('my hand edit')" > backend/app.py
echo "brand new local file" > backend/mine.txt
# a new commit lands on origin/main
cd "$BASE/seed"
echo "print('v2 - brand new file')" > backend/app.py
echo "new in v2" > backend/feature_v2.txt
echo "flask==3.0" > backend/requirements.txt
git add -A && git commit -qm "v2 work"
git push -q origin main

# ── stubs: exercise the real code path without touching the real system ──
mkdir -p "$BASE/bin"
cat > "$BASE/bin/systemctl" <<'EOS'
#!/usr/bin/env bash
[[ "$1" == "cat" ]] && exit 0
[[ "$1" == "is-active" ]] && exit 0
exit 0
EOS
cat > "$BASE/bin/sudo" <<'EOS'
#!/usr/bin/env bash
exec "$@"
EOS
cat > "$BASE/bin/npm" <<'EOS'
#!/usr/bin/env bash
echo "npm $*" >> "${TMPDIR:-/tmp}/apex-update-npm.log"
exit 0
EOS
chmod +x "$BASE/bin"/*
cp "$REPO/apex" "$BASE/work/apex"
chmod +x "$BASE/work/apex"

# ── run the update ──
cd "$BASE/work"
BRANCH_BEFORE=$(git rev-parse --short v_2.0.27)
DB_BEFORE=$($PY -c "import sqlite3;print(sqlite3.connect('backend/data/apex.db').execute('select count(*) from chats').fetchone()[0])")
export PATH="$BASE/bin:$PATH"
NO_COLOR=1 ./apex update 2>&1 | sed 's/^/    /'

# ── verify ──
echo
echo "── verification ──"
fail=0
chk() { if [[ "$2" == "$3" ]]; then echo "  PASS $1"; else echo "  FAIL $1 (got '$2', want '$3')"; fail=1; fi; }

chk "working tree at origin/main"   "$(git rev-parse --short HEAD)" "$(git rev-parse --short origin/main)"
chk "local branch untouched"        "$(git rev-parse --short v_2.0.27)" "$BRANCH_BEFORE"
chk "detached HEAD"                 "$(git rev-parse --abbrev-ref HEAD)" "HEAD"
chk "new v2 file present"           "$([[ -f backend/feature_v2.txt ]] && echo yes || echo no)" "yes"
chk "local code edit overridden"    "$(cat backend/app.py)" "print('v2 - brand new file')"
chk "chat rows preserved"           "$($PY -c "import sqlite3;print(sqlite3.connect('backend/data/apex.db').execute('select count(*) from chats').fetchone()[0])")" "$DB_BEFORE"
chk "newest chat preserved"         "$($PY -c "import sqlite3;print(sqlite3.connect('backend/data/apex.db').execute('select body from chats order by id desc limit 1').fetchone()[0])")" "chat #3 local only"
chk "db is valid sqlite"            "$($PY -c "import sqlite3;print(sqlite3.connect('backend/data/apex.db').execute('pragma integrity_check').fetchone()[0])")" "ok"
chk "chroma blob preserved"         "$(cat backend/data/chroma/blob.bin)" "chroma-v1"
chk "backend/.env preserved"        "$(cat backend/.env)" "SECRET_TOKEN=keepme"
chk "requirements.txt from git"     "$(cat backend/requirements.txt)" "flask==3.0"
chk "npm install ran"               "$(grep -c 'npm --prefix.* install' "${TMPDIR:-/tmp}/apex-update-npm.log" || echo 0)" "1"
chk "npm run build ran"             "$(grep -c 'run build' "${TMPDIR:-/tmp}/apex-update-npm.log" || echo 0)" "1"
PATCH=$(ls -dt /tmp/apex-update.*/local-changes.patch 2>/dev/null | head -1)
chk "edits saved as patch"          "$([[ -n "$PATCH" ]] && echo yes || echo no)" "yes"
chk "patch holds the hand edit"    "$([[ -n "$PATCH" ]] && grep -q 'my hand edit' "$PATCH" && echo yes || echo no)" "yes"
chk "patch excludes binary db"     "$([[ -n "$PATCH" ]] && grep -q 'apex.db' "$PATCH" && echo no || echo yes)" "yes"
chk "restored db differs from git" "$(git status --porcelain -- backend/data/apex.db | cut -c1-2)" " M"
[[ $fail -eq 0 ]] && echo && echo "ALL CHECKS PASSED" || { echo; echo "FAILURES PRESENT"; exit 1; }
