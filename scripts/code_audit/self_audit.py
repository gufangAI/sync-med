# -*- coding: utf-8 -*-
# Code self-audit for guyaofang-web (static checks, zero AI calls, zero cost, zero third-party quota).
#
# Why this exists (2026-07-17): a manual ChatGPT review of guyaofang-web found real P0 bugs our
# existing "intel-radar" never could have caught -- intel-radar scans arXiv/HuggingFace/GitHub
# trending/PubMed for *external AI tech trends*, it has never looked at our own repo's code
# quality/security. This script is the missing self-audit: it targets the EXACT bug shapes found
# that day, not a vague "find bugs" pass:
#   1) D1 schema drift: SQL in functions/api/**/*.js references a column name that does not
#      actually exist in the table -- e.g. `WHERE id=?` on `users` when the real PK column is
#      `user_id`. This class of bug crashes the endpoint at runtime.
#   2) AI gateway bypass: anything outside functions/api/gateway/_providers.js calling
#      Cloudflare Workers AI (env.AI.run / @cf/*) is a hard redline (burned real money before,
#      2026-07-09, ~$8). Direct calls to paid LLM hosts bypassing the free-first fallback chain
#      are a softer, reminder-level flag (known existing case: _lib/provenance_judge.js calls
#      dashscope/qwen-plus directly).
#   3) Hardcoded secrets: API-key-shaped literals committed to source instead of read from env.
#   4) Git asset health: days since last real commit + working-tree untracked/modified file
#      counts -- a stale/uncommitted repo is a real data-loss risk (found: repo hadn't been
#      pushed in over a month, thousands of uncommitted files sitting only on one machine).
#
# Design notes / honest limitations (read before trusting blindly):
#   - Schema ground truth: migrations/*.sql alone is NOT reliable -- verified 2026-07-17 against
#     the real production D1 (guyaofang-db) that several columns actually in use in production
#     (books_assets_v2.collection/library/category_code/author/req_no/pan_dir_id/..., users.username)
#     were NEVER added via any checked-in migration file (added directly to prod, undocumented --
#     real schema drift, a separate finding in its own right). Trusting migrations/*.sql alone
#     would have produced a pile of false alarms on perfectly-working code. So: this script prefers
#     live D1 introspection (`SELECT name,sql FROM sqlite_master WHERE type='table'`, the same
#     approach scripts/fleet_watch.py already uses for D1<->123 reconciliation) via the Cloudflare
#     D1 HTTP API when CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN are available (they already are,
#     as repo secrets, for exactly this reason), falling back to migrations/*.sql-only when they
#     are not (e.g. standalone/offline runs) -- and always says in the report which source was used
#     so nobody mistakes a degraded-mode run for a full one.
#   - The SQL parser is a careful regex heuristic, NOT a real SQL parser. It deliberately skips
#     anything it can't confidently resolve (function calls in SELECT lists, multi-table queries
#     with an unqualified/ambiguous column, dynamically-built SQL strings) rather than guess and
#     risk a false alarm. It is tuned to catch the simple, common shape of the real bugs found
#     (`WHERE id=?`, `SELECT ... title ...`) -- it will under-report exotic queries, by design.
#   - "untracked/modified file counts" are inherently a WORKING-TREE (local machine) signal. A
#     fresh `actions/checkout` in CI is always clean (0/0) -- that's not the tool being broken,
#     it's the tool correctly reporting that a cloud runner cannot see a founder's local machine.
#     The one signal that DOES carry over to CI faithfully is "days since last commit" (that's a
#     property of the remote ref, not the local working tree), and it's the one that matters most
#     for the "did this get pushed anywhere" risk.
#
# Usage:
#   python self_audit.py --repo <path-to-checked-out-guyaofang-web> [--json out.json] [--no-issue]
#   python self_audit.py --repo <path> --wrangler-db guyaofang-db --no-issue   # local live-schema test
#
# Env:
#   GH_TOKEN / GITHUB_TOKEN                      - gh CLI auth for `gh issue create/edit/comment`
#   GITHUB_REPOSITORY                            - defaults to gufangAI/sync-med (where Issue posts)
#   CF_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN - optional; live-schema check via Cloudflare D1 API.
#                                                   All optional -- script degrades gracefully without
#                                                   any of them (migrations-only mode), zero AI calls
#                                                   and zero mandatory third-party dependency either way.

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPORT_REPO = os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med")
AUDITED_REPO_LABEL = os.environ.get("AUDITED_REPO_LABEL", "hosonzuo8848/guyaofang (guyaofang-web)")
TITLE_PREFIX = "\U0001F9FE code-self-audit"  # receipt emoji, distinct from fleet-health's ship emoji
ISSUE_LABEL = "code-self-audit"

GIT_STALE_WARN_DAYS = 14   # >14 days since last commit -> WARN
GIT_STALE_ALERT_DAYS = 30  # >30 days -> counts toward the overall red badge

# ────────────────────────────────────────────────────────────────────────────
# 1) D1 schema consistency: migrations/*.sql  vs  functions/api/**/*.js SQL
# ────────────────────────────────────────────────────────────────────────────

_CREATE_TABLE_HEAD_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)[`\"\]]?\s*\(",
    re.IGNORECASE,
)
_ALTER_ADD_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+[`\"\[]?(\w+)[`\"\]]?\s+ADD\s+COLUMN\s+[`\"\[]?(\w+)[`\"\]]?",
    re.IGNORECASE,
)
_TABLE_LEVEL_KEYWORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def _split_top_level(s, sep=","):
    """Split on `sep` but only at paren-depth 0 (so `FOREIGN KEY(a,b) REFERENCES t(a,b)` stays intact)."""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _strip_sql_comments(text):
    return re.sub(r"--[^\n]*", "", text)


def _find_balanced_paren_body(text, open_paren_pos):
    """text[open_paren_pos] must be '('. Return the substring strictly between it and its
    matching ')', tracking depth (handles nested parens from FOREIGN KEY(...) REFERENCES t(...)).
    Returns None if unbalanced (truncated/malformed text)."""
    depth = 0
    for i in range(open_paren_pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_pos + 1:i]
    return None


def _columns_from_create_table_body(body):
    """Column names from a CREATE TABLE (...) body, skipping table-level constraint clauses."""
    cols = set()
    for seg in _split_top_level(body):
        seg = seg.strip()
        if not seg:
            continue
        fw = re.match(r"^[`\"\[]?(\w+)[`\"\]]?", seg)
        if not fw:
            continue
        if fw.group(1).upper() in _TABLE_LEVEL_KEYWORDS:
            continue
        cols.add(fw.group(1))
    return cols


def parse_create_table_statements(text):
    """Find every `CREATE TABLE ... (...)` in `text` (no trailing ';' required -- SQLite's
    sqlite_master.sql column stores CREATE TABLE DDL WITHOUT a trailing semicolon, and it inlines
    every later `ALTER TABLE ... ADD COLUMN` as extra ", col type" fragments appended just before
    the final ')' -- so this same balanced-paren scan handles both migration-file text (has ';')
    and live sqlite_master DDL text (no ';') identically, no separate code path needed.
    Returns {table_name: set(columns)}."""
    tables = {}
    text = _strip_sql_comments(text)
    for m in _CREATE_TABLE_HEAD_RE.finditer(text):
        tname = m.group(1)
        open_pos = m.end() - 1  # position of the '(' itself
        body = _find_balanced_paren_body(text, open_pos)
        if body is None:
            continue
        tables.setdefault(tname, set()).update(_columns_from_create_table_body(body))
    return tables


def parse_migrations_schema(repo_root):
    """Return ({table_name: set(column_names)}, n_migration_files) from migrations/*.sql
    (CREATE TABLE + any standalone ALTER TABLE ADD COLUMN not already inlined by the DB)."""
    tables = {}
    mig_dir = os.path.join(repo_root, "migrations")
    files = sorted(glob.glob(os.path.join(mig_dir, "*.sql")))
    for fp in files:
        try:
            text = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for tname, cols in parse_create_table_statements(text).items():
            tables.setdefault(tname, set()).update(cols)
        stripped = _strip_sql_comments(text)
        for m in _ALTER_ADD_COL_RE.finditer(stripped):
            tname, col = m.group(1), m.group(2)
            tables.setdefault(tname, set()).add(col)
    return tables, len(files)


# ── Live D1 schema (authoritative -- see module docstring for why this matters) ────────────

def _extract_json_array(stdout_text):
    """wrangler --json prints a warning line or two before the JSON array; find where it starts."""
    idx = stdout_text.find("[")
    if idx < 0:
        return None
    try:
        return json.loads(stdout_text[idx:])
    except Exception:
        return None


def fetch_live_schema_via_wrangler(db_name, cwd):
    """Local/dev convenience path: shells out to `npx wrangler d1 execute --remote --json`.
    Requires the caller to already have a working wrangler login/session (same as any dev running
    `wrangler d1 execute` by hand) -- this is NOT the path CI uses (CI has no wrangler session;
    it uses fetch_live_schema_via_http with repo secrets instead)."""
    import shutil
    npx = shutil.which("npx.cmd") or shutil.which("npx") or "npx"  # Windows needs the .cmd shim
    try:
        r = subprocess.run(
            [npx, "wrangler", "d1", "execute", db_name, "--remote", "--json",
             "--command", "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL;"],
            cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", timeout=90,
        )
        if r.returncode != 0:
            return None, f"wrangler exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}"
        data = _extract_json_array(r.stdout)
        if not data:
            return None, "could not parse wrangler --json output"
        rows = (data[0] or {}).get("results", [])
        tables = {}
        for row in rows:
            tables.update(parse_create_table_statements(row.get("sql") or ""))
        return tables, None
    except Exception as e:
        return None, str(e)


def fetch_live_schema_via_http(cf_account_id, db_id, token):
    """CI path: Cloudflare D1 HTTP API directly (stdlib urllib only, same auth shape
    scripts/fleet_watch.py already uses for D1<->123 reconciliation in this repo)."""
    import urllib.request
    url = f"https://api.cloudflare.com/client/v4/accounts/{cf_account_id}/d1/database/{db_id}/query"
    body = json.dumps({"sql": "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if not data.get("success"):
            return None, f"D1 API returned success=false: {str(data.get('errors'))[:200]}"
        rows = (data.get("result") or [{}])[0].get("results", [])
        tables = {}
        for row in rows:
            tables.update(parse_create_table_statements(row.get("sql") or ""))
        return tables, None
    except Exception as e:
        return None, str(e)


# SQL keywords/functions that can appear where a column name would, must never be flagged as
# a "missing column". Deliberately generous -- false negative here is cheap, false positive costs
# founder trust in the tool.
_SQL_STOPWORDS = {
    "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "BETWEEN", "EXISTS", "ASC", "DESC",
    "ORDER", "GROUP", "BY", "LIMIT", "OFFSET", "HAVING", "AS", "ON", "DISTINCT", "ALL",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "DATETIME", "DATE", "STRFTIME", "JSON_EXTRACT",
    "CAST", "COALESCE", "IFNULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END",
    "NOW", "ROUND", "ABS", "LENGTH", "SUBSTR", "REPLACE", "UPPER", "LOWER", "TRIM",
}

_FROM_JOIN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+[`\"\[]?(\w+)[`\"\]]?(?:\s+(?:AS\s+)?([a-zA-Z_]\w*))?",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(r"\bUPDATE\s+[`\"\[]?(\w+)[`\"\]]?", re.IGNORECASE)
_INSERT_RE = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+[`\"\[]?(\w+)[`\"\]]?\s*\((.*?)\)\s*(?:VALUES|SELECT)",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+[`\"\[]?(\w+)[`\"\]]?", re.IGNORECASE)
_PREPARE_CALL_RE = re.compile(
    r"\.prepare\s*\(\s*(`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")",
    re.DOTALL,
)
_COND_COL_RE = re.compile(
    r"(?:([a-zA-Z_]\w*)\.)?([a-zA-Z_]\w*)\s*(?:=|!=|<>|<=|>=|<|>)(?!=)|"
    r"(?:([a-zA-Z_]\w*)\.)?([a-zA-Z_]\w*)\s+(?:IN|LIKE|IS)\b",
    re.IGNORECASE,
)
_SET_ASSIGN_RE = re.compile(r"([a-zA-Z_]\w*)\s*=(?!=)")


def _extract_sql_text(raw_literal):
    """Strip the surrounding backtick/quote from a matched .prepare(...) argument."""
    if len(raw_literal) < 2:
        return None
    body = raw_literal[1:-1]
    # unescape the few sequences that show up in JS string literals we care about
    return body.replace("\\`", "`").replace("\\'", "'").replace('\\"', '"').replace("\\n", " ")


def _find_tables_and_aliases(sql):
    """Return (alias_or_name -> real_table_name, statement_type, primary_table_or_None)."""
    alias_map = {}
    stype = None
    primary = None

    m = _UPDATE_RE.search(sql)
    if m:
        stype = "UPDATE"
        primary = m.group(1)
        alias_map[primary] = primary

    m = _INSERT_RE.search(sql)
    if m:
        stype = "INSERT"
        primary = m.group(1)
        alias_map[primary] = primary

    m = _DELETE_RE.search(sql)
    if m:
        stype = "DELETE"
        primary = m.group(1)
        alias_map[primary] = primary

    from_matches = list(_FROM_JOIN_RE.finditer(sql))
    if from_matches:
        if stype is None:
            stype = "SELECT"
        for i, m in enumerate(from_matches):
            tname, alias = m.group(1), m.group(2)
            if alias and alias.upper() not in _SQL_STOPWORDS:
                alias_map[alias] = tname
            alias_map[tname] = tname
            if primary is None and i == 0 and stype == "SELECT":
                primary = tname

    return alias_map, stype, primary


def _columns_in_insert(sql):
    m = _INSERT_RE.search(sql)
    if not m:
        return None, []
    tname, collist = m.group(1), m.group(2)
    cols = [c.strip().strip("`\"[]") for c in _split_top_level(collist) if c.strip()]
    return tname, cols


def _columns_in_select_list(sql):
    """Column refs between SELECT and the first top-level FROM. Skip anything containing '(' --
    function calls / expressions are deliberately not fact-checked (precision over recall)."""
    m = re.search(r"\bSELECT\s+(?:DISTINCT\s+)?(.*?)\bFROM\b", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    seglist = _split_top_level(m.group(1))
    out = []
    for seg in seglist:
        seg = seg.strip()
        if not seg or "(" in seg or seg == "*":
            continue
        # drop "AS alias"
        seg = re.split(r"\bAS\b", seg, flags=re.IGNORECASE)[0].strip()
        if seg.endswith(".*"):
            continue
        mm = re.match(r"^([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)$", seg)
        if mm:
            out.append((mm.group(1), mm.group(2)))
            continue
        mm = re.match(r"^([a-zA-Z_]\w*)$", seg)
        if mm and mm.group(1).upper() not in _SQL_STOPWORDS:
            out.append((None, mm.group(1)))
    return out


def _columns_in_where_on(sql):
    # Only look at WHERE / ON / SET clauses, not the whole string, to reduce noise.
    out = []
    for clause_re in (r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
                      r"\bON\b(.*?)(?:\bWHERE\b|\bJOIN\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)"):
        for cm in re.finditer(clause_re, sql, re.IGNORECASE | re.DOTALL):
            clause = cm.group(1)
            for mm in _COND_COL_RE.finditer(clause):
                alias, col = (mm.group(1), mm.group(2)) if mm.group(2) else (mm.group(3), mm.group(4))
                if col and col.upper() not in _SQL_STOPWORDS:
                    out.append((alias, col))
    return out


def _columns_in_set_clause(sql):
    m = re.search(r"\bSET\b(.*?)(?:\bWHERE\b|$)", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    out = []
    for seg in _split_top_level(m.group(1)):
        mm = _SET_ASSIGN_RE.match(seg.strip())
        if mm and mm.group(1).upper() not in _SQL_STOPWORDS:
            out.append((None, mm.group(1)))
    return out


def check_schema_consistency(repo_root, schema):
    findings = []
    stats = {"prepare_calls_found": 0, "parsed_ok": 0, "skipped_complex": 0, "unknown_table_skipped": 0}
    js_files = glob.glob(os.path.join(repo_root, "functions", "api", "**", "*.js"), recursive=True)
    for fp in js_files:
        rel = os.path.relpath(fp, repo_root).replace("\\", "/")
        try:
            text = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _PREPARE_CALL_RE.finditer(text):
            stats["prepare_calls_found"] += 1
            sql = _extract_sql_text(m.group(1))
            if not sql or not re.search(r"\b(SELECT|UPDATE|INSERT|DELETE)\b", sql, re.IGNORECASE):
                stats["skipped_complex"] += 1
                continue
            alias_map, stype, primary = _find_tables_and_aliases(sql)
            if not stype or not alias_map:
                stats["skipped_complex"] += 1
                continue

            refs = []  # list of (alias_or_None, column)
            if stype == "INSERT":
                tname, cols = _columns_in_insert(sql)
                refs = [(tname, c) for c in cols]
            else:
                refs += _columns_in_select_list(sql)
                refs += _columns_in_where_on(sql)
                if stype == "UPDATE":
                    refs += _columns_in_set_clause(sql)

            multi_table = len([1 for v in set(alias_map.values())]) > 1
            resolved_any = False
            for alias, col in refs:
                if not col or col.upper() in _SQL_STOPWORDS:
                    continue
                table = alias_map.get(alias) if alias else (primary if not multi_table else None)
                if not table:
                    continue  # ambiguous bare column in a multi-table query -- skip, don't guess
                if table not in schema:
                    stats["unknown_table_skipped"] += 1
                    continue
                resolved_any = True
                if col not in schema[table]:
                    findings.append({
                        "severity": "CRITICAL",
                        "type": "schema_field_mismatch",
                        "file": rel,
                        "table": table,
                        "column": col,
                        "statement": stype,
                        "known_columns_sample": sorted(schema[table])[:8],
                        "snippet": " ".join(sql.split())[:220],
                    })
            if resolved_any:
                stats["parsed_ok"] += 1
            else:
                stats["skipped_complex"] += 1
    return findings, stats


# ────────────────────────────────────────────────────────────────────────────
# 2) AI gateway bypass (redline: CF Workers AI; reminder: direct paid-LLM calls)
# ────────────────────────────────────────────────────────────────────────────

_CFAI_RUN_RE = re.compile(r"\benv\.AI\.run\s*\(")
_CFAI_BINDING_RE = re.compile(r"^\s*AI\s*=", re.MULTILINE)  # wrangler.toml [ai] binding = "AI"
_CFAI_MODEL_LITERAL_RE = re.compile(r"@cf/[a-zA-Z0-9_\-./]+")

_KNOWN_LLM_HOSTS = [
    "dashscope.aliyuncs.com", "api.openai.com", "open.bigmodel.cn", "api.moonshot.cn",
    "api.deepseek.com", "api.siliconflow.cn", "api-inference.modelscope.cn",
    "integrate.api.nvidia.com", "token.sensenova.cn", "apihub.agnes-ai.com",
    "api.longcat.chat", "api.cerebras.ai", "generativelanguage.googleapis.com",
    "qianfan.baidubce.com", "tokenhub.tencentmaas.com", "ark.cn-beijing.volces.com",
    "maas-api.cn-huabei-1.xf-yun.com",
]

_GATEWAY_DIR = "functions/api/gateway/"


def _is_comment_line(line):
    s = line.strip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")


def check_ai_gateway_bypass(repo_root):
    findings = []
    js_files = glob.glob(os.path.join(repo_root, "functions", "**", "*.js"), recursive=True)
    for fp in js_files:
        rel = os.path.relpath(fp, repo_root).replace("\\", "/")
        try:
            lines = open(fp, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            comment = _is_comment_line(line)
            if _CFAI_RUN_RE.search(line) and not comment:
                findings.append({
                    "severity": "CRITICAL", "type": "cf_workers_ai_run_call",
                    "file": rel, "line": i, "snippet": line.strip()[:160],
                })
            m = _CFAI_MODEL_LITERAL_RE.search(line)
            if m and not comment:
                findings.append({
                    "severity": "CRITICAL", "type": "cf_workers_ai_model_literal",
                    "file": rel, "line": i, "snippet": line.strip()[:160],
                })
            if rel.startswith(_GATEWAY_DIR):
                continue  # the gateway itself is allowed to know about paid hosts (that's its job)
            for host in _KNOWN_LLM_HOSTS:
                if host in line and not comment:
                    findings.append({
                        "severity": "REMINDER", "type": "direct_llm_call_bypasses_gateway",
                        "file": rel, "line": i, "host": host, "snippet": line.strip()[:160],
                    })
    # also check wrangler.toml for an "AI" binding (would make env.AI available at all)
    wrangler_fp = os.path.join(repo_root, "wrangler.toml")
    if os.path.isfile(wrangler_fp):
        text = open(wrangler_fp, encoding="utf-8", errors="replace").read()
        if re.search(r"\[ai\]|binding\s*=\s*[\"']AI[\"']", text):
            findings.append({
                "severity": "CRITICAL", "type": "cf_workers_ai_binding_in_wrangler_toml",
                "file": "wrangler.toml", "line": 0,
                "snippet": "wrangler.toml declares an [ai] / AI binding -- this alone enables env.AI.run() to work",
            })
    return findings


# ────────────────────────────────────────────────────────────────────────────
# 3) Hardcoded secrets
# ────────────────────────────────────────────────────────────────────────────

_SECRET_LITERAL_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxoxb-[A-Za-z0-9-]{10,}\b"),
]
_SECRET_ASSIGN_RE = re.compile(
    r"\b(api[_-]?key|apikey|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*[:=]\s*"
    r"['\"]([A-Za-z0-9_\-./+]{12,})['\"]",
    re.IGNORECASE,
)
# `secret: 'NVIDIA_NIM_API_KEY'` is naming WHICH env var to read (gateway/_providers.js's own
# registry pattern: `apiKey: pickKey(env[def.secret])`), not a hardcoded value -- an ALL_CAPS_ID
# shaped value is almost always a reference/key-name, never an actual secret literal (real keys
# are high-entropy mixed-case/base64/hex, not clean env-var-style identifiers). Skip those.
_ENV_VAR_NAME_SHAPE_RE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$")


def check_hardcoded_secrets(repo_root):
    findings = []
    js_files = glob.glob(os.path.join(repo_root, "functions", "**", "*.js"), recursive=True)
    for fp in js_files:
        rel = os.path.relpath(fp, repo_root).replace("\\", "/")
        try:
            lines = open(fp, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if "env." in line or "process.env" in line or "secrets." in line or _is_comment_line(line):
                continue
            for pat in _SECRET_LITERAL_PATTERNS:
                if pat.search(line):
                    findings.append({
                        "severity": "CRITICAL", "type": "hardcoded_secret_literal",
                        "file": rel, "line": i, "snippet": line.strip()[:120],
                    })
            m = _SECRET_ASSIGN_RE.search(line)
            if m and not _ENV_VAR_NAME_SHAPE_RE.match(m.group(2)):
                findings.append({
                    "severity": "CRITICAL", "type": "hardcoded_secret_assignment",
                    "file": rel, "line": i, "field": m.group(1), "snippet": line.strip()[:120],
                })
    return findings


# ────────────────────────────────────────────────────────────────────────────
# 4) Git asset health
# ────────────────────────────────────────────────────────────────────────────

def _run_git(repo_root, args):
    r = subprocess.run(["git"] + args, cwd=repo_root, capture_output=True,
                        encoding="utf-8", errors="replace")
    return r.stdout.strip(), r.returncode


def check_git_asset_health(repo_root):
    out, rc = _run_git(repo_root, ["log", "-1", "--format=%ct|%H|%s"])
    if rc != 0 or not out:
        return {"ok": False, "error": "not a git repo or no commits", "severity": "CRITICAL"}
    ts_str, sha, subject = out.split("|", 2)
    days_since = (time.time() - int(ts_str)) / 86400.0

    untracked_out, _ = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    untracked_count = len([l for l in untracked_out.splitlines() if l.strip()])

    status_out, _ = _run_git(repo_root, ["status", "--porcelain"])
    modified_count = len([l for l in status_out.splitlines() if l.strip() and not l.startswith("??")])

    branch_out, _ = _run_git(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])

    severity = "OK"
    if days_since > GIT_STALE_ALERT_DAYS:
        severity = "ALERT"
    elif days_since > GIT_STALE_WARN_DAYS:
        severity = "WARN"

    return {
        "ok": True,
        "severity": severity,
        "days_since_last_commit": round(days_since, 1),
        "last_commit_sha": sha[:10],
        "last_commit_subject": subject[:80],
        "branch": branch_out,
        "untracked_files": untracked_count,
        "modified_tracked_files": modified_count,
    }


# ────────────────────────────────────────────────────────────────────────────
# Report assembly
# ────────────────────────────────────────────────────────────────────────────

def build_report(schema_findings, schema_stats, gateway_findings, secret_findings, git_health,
                 migrations_count, schema_source="migrations-only", live_error=None, drifted_cols=None):
    critical = [f for f in schema_findings + gateway_findings + secret_findings if f["severity"] == "CRITICAL"]
    reminders = [f for f in gateway_findings if f["severity"] == "REMINDER"]
    git_alert = git_health.get("ok") and git_health.get("severity") in ("ALERT",)
    alert = bool(critical) or git_alert or (git_health.get("ok") is False)

    lines = []
    lines.append(f"审计对象: `{AUDITED_REPO_LABEL}` | 迁移文件数: {migrations_count} | 时间: "
                  f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    lines.append("")
    lines.append("这是纯静态代码体检,不调用任何 AI 模型,零成本、零第三方配额依赖。"
                  "专门核对今天人工审计(ChatGPT)发现的那几类真实问题,而不是泛泛看 bug。")
    lines.append("")

    # ---- 1. schema consistency ----
    lines.append("## 1. 数据库字段对不对得上 (D1 schema 一致性)")
    lines.append("")
    if schema_source.startswith("live D1"):
        lines.append(f"字段真相来源:**{schema_source}**(线上数据库实时读取,不是只看 migrations 文件夹)。")
        if drifted_cols:
            lines.append(f"顺带发现:线上数据库里有 **{drifted_cols}** 个字段从来没写过 migration 文件"
                         f"(架构漂移 —— 不是本次任务重点,但说明 migrations/ 文件夹已经跟不上线上真实结构了,"
                         f"建议找时间补上,不然以后类似工具 / 新人看 migrations/ 都会被误导)。")
    else:
        note = f"(尝试连线上 D1 失败:{live_error[:150]})" if live_error else "(未配置线上 D1 访问)"
        lines.append(f"字段真相来源:**仅 migrations/*.sql 静态文件** {note}。"
                     f"注意:2026-07-17 已实测确认 migrations/ 文件夹本身跟线上数据库有偏差"
                     f"(线上有一些字段是直接加的、没写迁移文件),仅靠这个模式可能把「线上其实有这个字段」"
                     f"误判成红色问题 —— 下面列表里的字段名不匹配,建议对一下线上库再下结论。")
    lines.append("")
    lines.append(f"静态扫描 `env.DB.prepare(...)` 共 {schema_stats['prepare_calls_found']} 处 SQL,"
                 f"能可靠解析并核对字段的 {schema_stats['parsed_ok']} 处"
                 f"(复杂/动态拼接 SQL 跳过 {schema_stats['skipped_complex']} 处,不瞎猜、宁可漏报)。")
    lines.append("")
    if schema_findings:
        lines.append(f"**发现 {len(schema_findings)} 处字段名对不上 —— 这条 SQL 真跑起来一定会报错"
                     f"(有的接口没接住会直接 500,有的被 try/catch 接住只是那个功能默默失效/返回错误,"
                     f"具体哪种要打开文件看一眼,但不管哪种都代表这段代码没在正常工作):**")
        lines.append("")
        lines.append("| 文件 | 表 | SQL 用的字段 | 这张表真实字段(部分) | 语句类型 |")
        lines.append("|---|---|---|---|---|")
        for f in schema_findings[:40]:
            lines.append(f"| `{f['file']}` | `{f['table']}` | `{f['column']}` | "
                         f"{', '.join(f['known_columns_sample'])} | {f['statement']} |")
        if len(schema_findings) > 40:
            lines.append(f"| ... | 还有 {len(schema_findings) - 40} 处,见 artifact 里的完整 JSON | | | |")
    else:
        lines.append("**没发现字段名不匹配的问题。**")
    lines.append("")

    # ---- 2. AI gateway bypass ----
    lines.append("## 2. AI 网关红线 (Cloudflare Workers AI 绝不能碰 + 绕开免费网关的提醒)")
    lines.append("")
    cfai_hits = [f for f in gateway_findings if f["type"].startswith("cf_workers_ai")]
    if cfai_hits:
        lines.append(f"**\U0001F534 红线触发!发现 {len(cfai_hits)} 处疑似调用 Cloudflare Workers AI"
                     f"(按 Neuron 计费,2026-07-09 就因为这个烧过 $8):**")
        lines.append("")
        for f in cfai_hits:
            lines.append(f"- `{f['file']}:{f.get('line', '?')}` — `{f['snippet']}`")
    else:
        lines.append("没有发现调用 Cloudflare Workers AI 的痕迹(红线保持干净)。")
    lines.append("")
    if reminders:
        lines.append(f"提醒级别(不算硬性错误):以下 {len(reminders)} 处直接调了付费/第三方模型的 API,"
                     f"没有经过统一网关 `gateway/_providers.js` 的免费优先容错链,建议人工看一眼是不是刻意为之:")
        lines.append("")
        seen_files = {}
        for f in reminders:
            seen_files.setdefault(f["file"], []).append(f)
        for file, hits in seen_files.items():
            hosts = sorted(set(h["host"] for h in hits))
            lines.append(f"- `{file}` — 直连 {', '.join(hosts)}(共 {len(hits)} 处)")
    else:
        lines.append("没有发现绕开网关直连付费模型的情况。")
    lines.append("")

    # ---- 3. hardcoded secrets ----
    lines.append("## 3. 有没有把密钥/密码写死在代码里")
    lines.append("")
    if secret_findings:
        lines.append(f"**\U0001F534 发现 {len(secret_findings)} 处疑似硬编码密钥,而不是从 env 读:**")
        lines.append("")
        for f in secret_findings[:20]:
            lines.append(f"- `{f['file']}:{f['line']}` — `{f['snippet']}`")
    else:
        lines.append("没发现硬编码密钥,密钥都走 env 变量读取。")
    lines.append("")

    # ---- 4. git asset health ----
    lines.append("## 4. 代码资产健康度 (git 提交情况)")
    lines.append("")
    if not git_health.get("ok"):
        lines.append(f"**\U0001F534 拿不到 git 信息:{git_health.get('error')}**")
    else:
        icon = {"OK": "✅", "WARN": "⚠️", "ALERT": "\U0001F534"}[git_health["severity"]]
        lines.append(f"{icon} 距离上次真正提交(commit)已经 **{git_health['days_since_last_commit']:.0f} 天** "
                     f"(`{git_health['last_commit_sha']}` {git_health['last_commit_subject']},"
                     f"分支 `{git_health['branch']}`)")
        lines.append("")
        lines.append(f"- 未跟踪文件(从没 add 过的新文件):**{git_health['untracked_files']}** 个")
        lines.append(f"- 已修改但没提交的文件:**{git_health['modified_tracked_files']}** 个")
        lines.append("")
        lines.append("> 说明:这两个文件计数只在本机跑此脚本才准确。这个 workflow 在云端跑时,"
                     "checkout 下来的是刚拉的干净副本,未跟踪/未提交天然是 0 —— 不代表本机真的没有堆积,"
                     "云端唯一能忠实反映真相的是「距离上次提交多少天」(这是远端仓库自己的属性,不受本机影响)。")
    lines.append("")

    lines.append("---")
    lines.append(f"- 红色项(需要处理): {len(critical)}")
    lines.append(f"- 提醒项(建议看一眼): {len(reminders)}")
    conclusion = "\U0001F534 存在需要处理的红色问题" if alert else "✅ 本轮未发现红色问题"
    lines.append(f"- 结论: {conclusion}")
    lines.append(f"- ts: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    return "\n".join(lines), alert, critical, reminders


# ────────────────────────────────────────────────────────────────────────────
# GitHub Issue upsert (same pattern as scripts/intel_radar/fleet_health.py)
# ────────────────────────────────────────────────────────────────────────────

def gh(*args, inp=None):
    return subprocess.run(["gh"] + list(args), capture_output=True,
                          encoding="utf-8", errors="replace", input=inp)


def upsert_issue(body_md, alert, n_critical):
    q = gh("issue", "list", "-R", REPORT_REPO, "--search", TITLE_PREFIX, "--state", "open",
           "--json", "number,title", "--limit", "10")
    num = None
    try:
        for it in json.loads(q.stdout or "[]"):
            if TITLE_PREFIX in it.get("title", ""):
                num = it["number"]
                break
    except Exception:
        pass

    badge = f"\U0001F534 ALERT ({n_critical} critical)" if alert else "\U0001F7E2"
    title = f"{TITLE_PREFIX} {badge} {time.strftime('%m-%d %H:%M UTC', time.gmtime())}"

    if num:
        r = gh("issue", "edit", str(num), "-R", REPORT_REPO, "--title", title, "--body", body_md)
        print(f"issue #{num} updated: {(r.stdout or r.stderr)[:120]}")
    else:
        r = gh("issue", "create", "-R", REPORT_REPO, "--title", title, "--body", body_md,
               "--label", ISSUE_LABEL)
        print("issue created:", (r.stdout or r.stderr)[:160])
        try:
            num = int((r.stdout or "").strip().rsplit("/", 1)[-1])
        except Exception:
            num = None

    # Issue edits don't push a phone notification; a new comment does. Only comment on alert,
    # same rule fleet-health.yml uses (avoid spamming founder's phone on every green run).
    if alert and num:
        gh("issue", "comment", str(num), "-R", REPORT_REPO,
           "--body", f"\U0001F534 {n_critical} 个红色问题,详情见上方报告。"
                     f"{time.strftime('%m-%d %H:%M UTC', time.gmtime())}")
        print("alert comment posted (push notification)")
    return num


# ────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to checked-out guyaofang-web")
    ap.add_argument("--json", default=None, help="optional path to dump full findings as JSON")
    ap.add_argument("--no-issue", action="store_true", help="print report only, skip gh issue upsert")
    ap.add_argument("--wrangler-db", default=None,
                     help="local/dev only: D1 database name to introspect live via `npx wrangler "
                          "d1 execute --remote` (requires an already-logged-in wrangler session). "
                          "CI does not use this flag -- it uses CF_ACCOUNT_ID/D1_DATABASE_ID/"
                          "D1_API_TOKEN env vars instead.")
    args = ap.parse_args()

    repo_root = os.path.abspath(args.repo)
    if not os.path.isdir(repo_root):
        print(f"ERROR: repo path not found: {repo_root}", file=sys.stderr)
        sys.exit(2)

    migrations_schema, migrations_count = parse_migrations_schema(repo_root)

    live_schema, schema_source, live_error = None, "migrations-only", None
    cf_acc, d1_db, d1_tok = (os.environ.get("CF_ACCOUNT_ID"), os.environ.get("D1_DATABASE_ID"),
                             os.environ.get("D1_API_TOKEN"))
    if cf_acc and d1_db and d1_tok:
        live_schema, live_error = fetch_live_schema_via_http(cf_acc, d1_db, d1_tok)
        schema_source = "live D1 (Cloudflare API)" if live_schema else "migrations-only (D1 API call failed)"
    elif args.wrangler_db:
        live_schema, live_error = fetch_live_schema_via_wrangler(args.wrangler_db, repo_root)
        schema_source = "live D1 (wrangler CLI)" if live_schema else "migrations-only (wrangler call failed)"

    if live_schema:
        schema = {t: set(cols) for t, cols in migrations_schema.items()}
        for t, cols in live_schema.items():
            schema.setdefault(t, set()).update(cols)
        # schema drift = live has it, no migration file ever added it (informational, not a bug)
        drifted_cols = sum(len(live_schema.get(t, set()) - migrations_schema.get(t, set()))
                           for t in live_schema)
    else:
        schema = migrations_schema
        drifted_cols = None

    schema_findings, schema_stats = check_schema_consistency(repo_root, schema)
    gateway_findings = check_ai_gateway_bypass(repo_root)
    secret_findings = check_hardcoded_secrets(repo_root)
    git_health = check_git_asset_health(repo_root)

    body, alert, critical, reminders = build_report(
        schema_findings, schema_stats, gateway_findings, secret_findings, git_health,
        migrations_count, schema_source, live_error, drifted_cols,
    )
    print(body)
    print()
    print(f"SUMMARY schema_source={schema_source} tables_parsed={len(schema)} "
          f"critical={len(critical)} reminders={len(reminders)} alert={alert}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "schema_source": schema_source,
                "schema_findings": schema_findings,
                "schema_stats": schema_stats,
                "gateway_findings": gateway_findings,
                "secret_findings": secret_findings,
                "git_health": git_health,
                "tables_found": {k: sorted(v) for k, v in schema.items()},
            }, f, ensure_ascii=False, indent=2)
        print(f"full findings written to {args.json}")

    if not args.no_issue:
        upsert_issue(body, alert, len(critical))

    print(f"ALERT={alert}")


if __name__ == "__main__":
    main()
